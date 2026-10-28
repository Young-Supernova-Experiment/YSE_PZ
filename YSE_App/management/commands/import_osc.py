"""
Import Open Supernova Catalog (OSC) entries.

Dry run by default: parses each entry, says which transient it would go to (reads only) and what
it would write. ``--apply`` writes, one transaction per entry; an entry that fails is reported
and the rest go on. Re-running replaces what an earlier import of the same entry wrote.

Examples::

  # a few objects, fetched from GitHub
  python3 manage.py import_osc --fetch SN2011fe SN1987A
  python3 manage.py import_osc --fetch SN2011fe SN1987A --apply --user <USERNAME>

  # everything, from local clones of the OSC data repositories (see catalog_import/osc_fetch.py)
  python3 manage.py import_osc /data/osc/sne-* --apply --user <USERNAME>

``--offline`` parses only and touches no database (also: ``python -m YSE_App.catalog_import.osc``).
"""

from __future__ import annotations

import json
import os
import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from YSE_App.catalog_import import osc, osc_fetch


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
        parser.add_argument('--apply', action='store_true', help='Write to the database (default: dry run).')
        parser.add_argument('--offline', action='store_true', help='Dry run without any database access.')
        parser.add_argument('--user', default='', help='Username recorded as creator of the rows (needed with --apply).')
        parser.add_argument('--status', default='Ignore',
                            help='Status of transients the import creates (default: Ignore, as TNS import uses '
                                 'for old objects).')
        parser.add_argument('--match-radius', type=float, default=2.0,
                            help='Arcsec within which an existing transient counts as the same object '
                                 'when no name matches (0: names only).')
        parser.add_argument('--no-spectra', action='store_true', help='Skip spectra.')
        parser.add_argument('--no-models', action='store_true', help='Skip model fits and model photometry.')
        parser.add_argument('--limit', type=int, default=None, help='Stop after this many entries.')
        parser.add_argument('--report', default='', help='Write a JSON line per entry to this file.')
        parser.add_argument('--quiet', action='store_true', help='One line per entry instead of the full summary.')

    def handle(self, *args, **opts):
        if opts['apply'] and opts['offline']:
            raise CommandError('--apply and --offline cannot be used together.')
        paths = list(opts['paths'])
        if opts['fetch']:
            paths.extend(self._fetch(opts))
        if not paths:
            raise CommandError('Give entry files or directories, or --fetch NAME.')

        writer = None
        if opts['apply']:
            from YSE_App.catalog_import.osc_writer import Writer

            user = get_user_model().objects.filter(username=opts['user']).first() if opts['user'] else None
            if user is None:
                raise CommandError('--apply needs --user <existing username>.')
            writer = Writer(user, new_status=opts['status'], match_radius=opts['match_radius'],
                            with_spectra=not opts['no_spectra'], with_models=not opts['no_models'])

        report = open(opts['report'], 'a') if opts['report'] else None
        counts = {'entries': 0, 'written': 0, 'created': 0, 'failed': 0}
        started = time.time()
        try:
            for path in osc_fetch.entry_files(paths):
                if opts['limit'] is not None and counts['entries'] >= opts['limit']:
                    break
                try:
                    entries = osc.load_entries(path)
                except (OSError, ValueError) as exc:
                    counts['failed'] += 1
                    self.stderr.write('%s: unreadable: %s' % (path, exc))
                    continue
                for name, entry in entries:
                    counts['entries'] += 1
                    plan = osc.parse_entry(name, entry, origin=osc_fetch.origin_of(path))
                    line = self._one(plan, writer, opts, counts)
                    if report:
                        report.write(json.dumps(line, default=str) + '\n')
        finally:
            if report:
                report.close()
        self.stdout.write('%s: %d entries, %d written (%d new transients), %d failed, %.0f s' % (
            'Imported' if writer else 'Dry run', counts['entries'], counts['written'], counts['created'],
            counts['failed'], time.time() - started))

    def _one(self, plan, writer, opts, counts) -> dict:
        line = {'entry': plan['entry_name'], 'counts': osc.plan_counts(plan), 'warnings': plan['warnings']}
        if not opts['quiet']:
            self.stdout.write(osc.describe_plan(plan))
        if writer is None:
            if not opts['offline']:
                from YSE_App.catalog_import.osc_writer import preview_entry

                preview = preview_entry(plan, opts['match_radius'])
                line['action'] = preview['action']
                self.stdout.write('  would %s' % preview['action'] if not opts['quiet'] else
                                  '%s: would %s' % (plan['entry_name'], preview['action']))
                if not opts['quiet']:
                    for catalog_name, yse_name in sorted(preview['instruments'].items()):
                        self.stdout.write('    instrument %s -> %s' % (catalog_name, yse_name))
            if not opts['quiet']:
                self.stdout.write('')
            return line
        from YSE_App.catalog_import.osc_writer import EntryNotImported

        try:
            result = writer.apply_entry(plan)
        except EntryNotImported as exc:
            counts['failed'] += 1
            line['error'] = str(exc)
            self.stderr.write('%s: not imported: %s' % (plan['entry_name'], exc))
            return line
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop a 50,000-entry run
            counts['failed'] += 1
            line['error'] = repr(exc)
            self.stderr.write('%s: failed: %r' % (plan['entry_name'], exc))
            return line
        counts['written'] += 1
        counts['created'] += int(result['created'])
        line['result'] = result
        self.stdout.write('  %s %s: %d quantities, %d photometry points, %d spectra, %d model fits%s' % (
            'created' if result['created'] else 'updated', result['transient'], result['quantities'],
            result['photometry_points'], result['spectra'], result['model_fits'],
            '' if result['created'] else ' (matched by %s)' % result['matched_by']))
        return line

    def _fetch(self, opts):
        repos = list(osc_fetch.DATA_REPOSITORIES) + ([osc_fetch.BONEYARD_REPOSITORY] if opts['boneyard'] else [])
        cache = os.path.join(opts['fetch_dir'], 'index%s.json' % ('-boneyard' if opts['boneyard'] else ''))
        index = osc_fetch.load_index(cache, repos, token=os.environ.get('GITHUB_TOKEN'),
                                     refresh=opts['refresh_index'])
        if not index:
            raise CommandError('Could not list the OSC repositories on GitHub.')
        paths, missing = osc_fetch.fetch_entries(opts['fetch'], opts['fetch_dir'], index)
        for name in missing:
            self.stderr.write('%s: not found in the OSC repositories' % name)
        return paths
