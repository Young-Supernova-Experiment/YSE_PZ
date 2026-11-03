"""
Import Open Supernova Catalog (OSC) entries; see docs/osc-import.md for the rules.

Dry run by default: parses each entry and says, from reads only, which transient it would go to,
which of its points and spectra duplicate data already in YSE_PZ and what would happen to them,
and what the host rule says. ``--apply`` writes, one transaction per entry. Re-running replaces
what an earlier import of the same entry wrote.

``--apply`` needs ``--database NAME`` naming the configured database (#904). With ``--apply`` every entry's download, parse and ingest outcome is recorded
(``CatalogImportAttempt``); ``--retry-failed`` runs the failed ones again (downloads are retried
with backoff on their own). ``--revert NAME`` undoes an entry's import.

Examples::

  python3 manage.py import_osc --fetch SN2011fe SN1987A
  python3 manage.py import_osc --fetch SN2011fe SN1987A --apply --user <USERNAME> --database <DB_NAME>
  python3 manage.py import_osc <OSC_DIR>/sne-* --apply --user <USERNAME> --database <DB_NAME> --quiet
  python3 manage.py import_osc --retry-failed --apply --user <USERNAME> --database <DB_NAME>
  python3 manage.py import_osc --revert SN2011fe --apply --database <DB_NAME>

``--offline`` parses only and touches no database (also: ``python -m YSE_App.catalog_import.osc``).
New transients wait for the TNS check (``manage.py osc_tns_check --tns-file``; ``--no-tns`` marks
them skipped instead).
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from YSE_App.catalog_import import guard, matching, osc, osc_fetch


class Command(BaseCommand):
    help = 'Import Open Supernova Catalog entries (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='*', help='Entry files (.json, .json.gz) or directories of them.')
        parser.add_argument('--fetch', nargs='+', default=[], metavar='NAME',
                            help='Download these entries from GitHub first (e.g. SN2011fe or 2011fe).')
        parser.add_argument('--fetch-dir', default=os.path.join(os.path.expanduser('~'), '.cache', 'yse_osc'),
                            help='Where downloaded entries and the GitHub index are kept.')
        parser.add_argument('--refresh-index', action='store_true', help='Rebuild the cached GitHub index.')
        parser.add_argument('--boneyard', action='store_true',
                            help='Also look in sne-boneyard (entries the OSC set aside) when fetching.')
        parser.add_argument('--retry-failed', action='store_true',
                            help='Also run the entries whose last download, parse or ingest failed.')
        parser.add_argument('--revert', nargs='+', default=[], metavar='NAME',
                            help='Undo the import of these entries (flags restored; transients are kept).')
        parser.add_argument('--apply', action='store_true', help='Write to the database (default: dry run).')
        parser.add_argument('--offline', action='store_true', help='Dry run without any database access.')
        parser.add_argument('--user', default='', help='Username recorded as creator of the rows (needed with --apply).')
        parser.add_argument('--status', default='Ignore',
                            help='Status of transients the import creates (default: Ignore, as the TNS import uses '
                                 'for old objects).')
        parser.add_argument('--match-radius', type=float, default=2.0,
                            help='Arcsec within which an existing transient counts as the same object '
                                 'when no name matches (0: names only).')
        parser.add_argument('--phot-tolerance', type=float, default=matching.PHOT_TIME_TOLERANCE,
                            help='Days within which a point matches an existing one (default %(default)s).')
        parser.add_argument('--spec-tolerance', type=float, default=matching.SPEC_TIME_TOLERANCE,
                            help='Days within which a spectrum matches an existing one (default %(default)s).')
        parser.add_argument('--no-spectra', action='store_true', help='Skip spectra.')
        parser.add_argument('--no-tns', action='store_true',
                            help='Do not mark new transients for the TNS check or queue it.')
        parser.add_argument('--limit', type=int, default=None, help='Stop after this many entries.')
        parser.add_argument('--report', default='', help='Write a JSON line per entry to this file.')
        parser.add_argument('--quiet', action='store_true', help='One line per entry instead of the full summary.')
        guard.add_argument(parser)

    # ---- main -----------------------------------------------------------------------------

    def handle(self, *args, **opts):
        self.report = None
        self.counts = Counter()
        self.failures = Counter()
        if opts['apply'] and opts['offline']:
            raise CommandError('--apply and --offline cannot be used together.')
        if not opts['offline']:
            guard.check(opts, opts['apply'], self.stdout)
        if opts['revert']:
            return self._revert(opts)
        self.apply = opts['apply']
        sources = [(path, None) for path in opts['paths']]
        if opts['fetch']:
            sources.extend(self._fetch(opts['fetch'], opts))
        if opts['retry_failed']:
            if opts['offline']:
                raise CommandError('--retry-failed reads the database; it cannot be used with --offline.')
            sources.extend(self._failed_sources(opts))
        if not sources:
            raise CommandError('Give entry files or directories, --fetch NAME or --retry-failed.')

        writer = None
        if self.apply:
            from YSE_App.catalog_import.osc_writer import Writer

            user = get_user_model().objects.filter(username=opts['user']).first() if opts['user'] else None
            if user is None:
                raise CommandError('--apply needs --user <existing username>.')
            writer = Writer(user, new_status=opts['status'], match_radius=opts['match_radius'],
                            with_spectra=not opts['no_spectra'], tns_check=not opts['no_tns'], phot_tolerance=opts['phot_tolerance'],
                            spec_tolerance=opts['spec_tolerance'])

        report = self.report = open(opts['report'], 'a') if opts['report'] else None
        started = time.time()
        try:
            for path, key in self._files(sources):
                if opts['limit'] is not None and self.counts['entries'] >= opts['limit']:
                    break
                key = key or osc_fetch.entry_key(path)
                origin = osc_fetch.origin_of(path)
                try:
                    entries = osc.load_entries(path)
                except (OSError, ValueError) as exc:
                    self._failed(key, origin, 'parse', 'unreadable: %s' % exc)
                    continue
                for name, entry in entries:
                    self.counts['entries'] += 1
                    try:
                        plan = osc.parse_entry(name, entry, origin=origin)
                    except Exception as exc:  # noqa: BLE001 - a malformed entry must not stop the run
                        self._failed(key, origin, 'parse', repr(exc))
                        continue
                    line = self._one(plan, writer, opts, key, origin)
                    if report:
                        report.write(json.dumps(line, default=str) + '\n')
        finally:
            if report:
                report.close()
        if writer is not None and self.counts['created'] and not opts['no_tns']:
            self.stdout.write('%d new transients wait for the TNS check: manage.py osc_tns_check --tns-file '
                              '<TNS public objects file> (see docs/osc-import.md).' % self.counts['created'])
        self.stdout.write('%s: %d entries, %d written (%d new transients), %d failed, %.0f s' % (
            'Imported' if writer else 'Dry run', self.counts['entries'], self.counts['written'], self.counts['created'],
            sum(self.failures.values()), time.time() - started))
        if self.failures:
            self.stdout.write('  failures by stage: %s%s' % (
                ', '.join('%s %d' % kv for kv in sorted(self.failures.items())),
                '; rerun with --retry-failed' if self.apply else ''))

    # ---- one entry ----------------------------------------------------------------------------

    def _one(self, plan, writer, opts, key, origin) -> dict:
        line = {'entry': plan['entry_name'], 'counts': osc.plan_counts(plan), 'warnings': plan['warnings']}
        if not opts['quiet']:
            self.stdout.write(osc.describe_plan(plan))
        if writer is None:
            if not opts['offline']:
                from YSE_App.catalog_import.osc_writer import preview_entry

                preview = preview_entry(plan, opts['match_radius'])
                line['preview'] = {k: v for k, v in preview.items() if k != 'transient'}
                if opts['quiet']:
                    self.stdout.write('%s: would %s' % (plan['entry_name'], preview['action']))
                else:
                    self.stdout.write('  would %s' % preview['action'])
                    self.stdout.write('    photometry: %s   spectra: %s' % (
                        _decisions(preview['photometry']), _decisions(preview['spectra'])))
                    if preview['host']:
                        self.stdout.write('    host: %s' % _host(preview['host']))
                    for catalog_name, yse_name in sorted(preview['instruments'].items()):
                        self.stdout.write('    instrument %s -> %s' % (catalog_name, yse_name))
            if not opts['quiet']:
                self.stdout.write('')
            return line
        from YSE_App.catalog_import.osc_writer import EntryNotImported

        try:
            result = writer.apply_entry(plan)
        except EntryNotImported as exc:
            line['error'] = str(exc)
            self._failed(key, origin, 'ingest', str(exc), entry=plan['entry_name'])
            return line
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop a 55,000-entry run
            line['error'] = repr(exc)
            self._failed(key, origin, 'ingest', repr(exc), entry=plan['entry_name'])
            return line
        self.counts['written'] += 1
        self.counts['created'] += int(result['created'])
        self._attempt(key, origin, 'ingest', None)
        line['result'] = result
        self.stdout.write('  %s %s: %d quantities; photometry %s; spectra %s; host %s%s' % (
            'created' if result['created'] else 'added data to', result['transient'], result['quantities'],
            _decisions(result['photometry']), _decisions(result['spectra']), result['host'],
            '' if result['created'] else ' (matched by %s)' % result['matched_by']))
        return line

    # ---- failures and retries -----------------------------------------------------------------

    def _attempt(self, key, origin, stage, error):
        """Record an outcome (with --apply only: a dry run writes nothing)."""
        if not self.apply:
            return
        from django.db.models import F

        from YSE_App.models import CatalogImportAttempt

        status = CatalogImportAttempt.FAILED if error else CatalogImportAttempt.OK
        row, created = CatalogImportAttempt.objects.get_or_create(
            catalog=osc.CATALOG, entry_key=key[:128],
            defaults={'origin': (origin or '')[:512] or None, 'stage': stage, 'status': status, 'attempts': 1,
                      'error': error})
        if not created:
            CatalogImportAttempt.objects.filter(pk=row.pk).update(
                origin=(origin or row.origin or '')[:512] or None, stage=stage, status=status,
                attempts=F('attempts') + 1, error=error)

    def _failed(self, key, origin, stage, error, entry=None):
        self.failures[stage] += 1
        self.stderr.write('%s: %s failed: %s' % (entry or key, stage, error))
        self._attempt(key, origin, stage, error)
        if getattr(self, 'report', None) and stage != 'ingest':  # ingest failures are in the entry's line
            self.report.write(json.dumps({'entry': entry or key, 'origin': origin, 'stage': stage,
                                          'error': error}) + '\n')

    def _failed_sources(self, opts):
        from YSE_App.models import CatalogImportAttempt

        out = []
        rows = list(CatalogImportAttempt.objects.filter(catalog=osc.CATALOG, status=CatalogImportAttempt.FAILED))
        self.stdout.write('%d failed entries to retry' % len(rows))
        names = []
        for row in rows:
            origin = row.origin or ''
            repo, _, path = origin.partition('/')
            if origin and os.path.exists(origin):
                out.append((origin, row.entry_key))
            elif repo in osc_fetch.DATA_REPOSITORIES + (osc_fetch.BONEYARD_REPOSITORY,) and path:
                try:
                    out.append((osc_fetch.download(repo, path, opts['fetch_dir']), row.entry_key))
                except (osc_fetch.FetchError, OSError) as exc:
                    self._failed(row.entry_key, origin, 'download', str(exc))
            else:
                names.append(row.entry_key)
        if names:
            out.extend(self._fetch(names, opts))
        return out

    def _fetch(self, names, opts):
        repos = list(osc_fetch.DATA_REPOSITORIES) + ([osc_fetch.BONEYARD_REPOSITORY] if opts['boneyard'] else [])
        cache = os.path.join(opts['fetch_dir'], 'index%s.json' % ('-boneyard' if opts['boneyard'] else ''))
        index = osc_fetch.load_index(cache, repos, token=os.environ.get('GITHUB_TOKEN'),
                                     refresh=opts['refresh_index'])
        if not index:
            raise CommandError('Could not list the OSC repositories on GitHub.')
        out = []
        for name in names:
            paths, missing, failed = osc_fetch.fetch_entries([name], opts['fetch_dir'], index)
            for path in paths:
                out.append((path, name))
            for missed in missing:
                self.stderr.write('%s: not found in the OSC repositories' % missed)
            for failed_name, error in failed.items():
                self.failures['download'] += 1
                self.stderr.write('%s: download failed: %s' % (failed_name, error))
                hit = osc_fetch.lookup(index, failed_name)
                self._attempt(failed_name, '%s/%s' % hit if hit else None, 'download', error)
        return out

    @staticmethod
    def _files(sources):
        for path, key in sources:
            if os.path.isdir(path):
                for name in osc_fetch.entry_files([path]):
                    yield name, None
            else:
                yield path, key

    # ---- revert -------------------------------------------------------------------------------

    def _revert(self, opts):
        from YSE_App.catalog_import.osc_writer import delete_entry_data
        from YSE_App.models import CatalogEntry

        for name in opts['revert']:
            entry = CatalogEntry.objects.filter(catalog=osc.CATALOG, entry_name=name).select_related('transient').first()
            if entry is None:
                self.stderr.write('%s: no imported entry of that name' % name)
                continue
            what = '%d photometry sets, %d spectra, %d superseded YSE observations restored' % (
                entry.photometry_sets.count(), entry.spectra.count(),
                entry.matches.filter(decision='superseded_existing').count())
            if opts['apply']:
                with transaction.atomic():
                    delete_entry_data(entry)
            self.stdout.write('%s %s (transient %s): %s%s' % (
                'Reverted' if opts['apply'] else 'Would revert', name, entry.transient.name, what,
                '; the transient was created by the import and is kept' if entry.created_transient else ''))


def _decisions(counts) -> str:
    if not counts:
        return 'none'
    labels = {matching.NEW: 'new', matching.KEPT_EXISTING: 'duplicate, YSE kept',
              matching.SUPERSEDED_EXISTING: 'duplicate, refereed OSC supersedes YSE',
              matching.REVIEW: 'possible duplicate, both kept for review'}
    return ', '.join('%d %s' % (n, labels.get(d, d)) for d, n in sorted(counts.items()))


def _host(check) -> str:
    text = check['status']
    if check.get('distance_mpc') is not None:
        text += ' (D = %.1f Mpc from %s)' % (check['distance_mpc'], check['distance_basis'])
    if check.get('yse_host') or check.get('osc_host'):
        text += '; OSC %s, YSE %s' % (check.get('osc_host') or '-', check.get('yse_host') or '-')
    if check.get('separation_arcsec') is not None:
        text += ', %.1f arcsec apart' % check['separation_arcsec']
    return text
