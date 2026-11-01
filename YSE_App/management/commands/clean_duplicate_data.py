"""
Find duplicated photometry points and spectra within each transient and flag all but one copy (#890).

Dry run by default: lists, from reads only, each group of copies, the copy that would be kept and
why. ``--apply`` flags the others REJECTED, one transaction per transient, and records each decision
(``DuplicateDataDecision``); ``--revert`` undoes them. The rules are in docs/duplicate-cleanup.md and
``YSE_App/catalog_import/duplicates.py``.

Transients are taken in id order. ``--max N`` stops after N of them and prints the ``--after ID`` that
continues the run; a re-run finds nothing new in transients already done (flagged rows are skipped).

``--apply`` needs ``--database NAME`` naming the configured database (#904).

Examples::

  python3 manage.py clean_duplicate_data --max 500
  python3 manage.py clean_duplicate_data --transient 2099abc
  python3 manage.py clean_duplicate_data --max 500 --after <ID> --apply --database <DB_NAME>
  python3 manage.py clean_duplicate_data --revert --transient 2099abc --apply --database <DB_NAME>
"""

from __future__ import annotations

import time
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from YSE_App.catalog_import import duplicates, guard, matching


class Command(BaseCommand):
    help = 'Flag duplicated photometry and spectra within each transient (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write to the database (default: dry run).')
        parser.add_argument('--revert', action='store_true',
                            help='Undo earlier decisions (all, or those of --transient) instead of finding duplicates.')
        parser.add_argument('--transient', nargs='+', default=[], metavar='NAME', help='Only these transients.')
        parser.add_argument('--max', type=int, default=None, metavar='N', help='Stop after N transients.')
        parser.add_argument('--after', type=int, default=0, metavar='ID',
                            help='Start after this transient id (to continue a run stopped by --max).')
        parser.add_argument('--phot-tolerance', type=float, default=matching.DUPLICATE_PHOT_TIME_TOLERANCE,
                            help='Days within which two points are copies (default %(default)s).')
        parser.add_argument('--spec-tolerance', type=float, default=matching.DUPLICATE_SPEC_TIME_TOLERANCE,
                            help='Days within which two spectra are copies (default %(default)s).')
        parser.add_argument('--no-photometry', action='store_true', help='Skip photometry.')
        parser.add_argument('--no-spectra', action='store_true', help='Skip spectra.')
        parser.add_argument('--quiet', action='store_true', help='One line per transient instead of one per copy.')
        guard.add_argument(parser)

    def handle(self, *args, **opts):
        from YSE_App.models import Transient

        guard.check(opts, opts['apply'], self.stdout)
        transients = Transient.objects.order_by('id')
        if opts['transient']:
            transients = transients.filter(name__in=opts['transient'])
            missing = set(opts['transient']) - set(transients.values_list('name', flat=True))
            if missing:
                raise CommandError('No transient named %s.' % ', '.join(sorted(missing)))
        if opts['revert']:
            return self._revert(transients, opts)
        if opts['after']:
            transients = transients.filter(id__gt=opts['after'])

        unknown_id = duplicates.unknown_instrument_id()
        counts = Counter()
        started = time.time()
        last_id = None
        ids = transients.values_list('id', 'name')
        if opts['max'] is not None:
            ids = ids[:opts['max']]
        for transient_id, name in ids.iterator():
            last_id = transient_id
            counts['transients'] += 1
            flags = []
            if not opts['no_photometry']:
                flags += duplicates.photometry_flags(transient_id, unknown_id, tolerance=opts['phot_tolerance'])
            if not opts['no_spectra']:
                flags += duplicates.spectrum_flags(transient_id, unknown_id, tolerance=opts['spec_tolerance'])
            if not flags:
                continue
            counts['with_duplicates'] += 1
            for f in flags:
                counts[f.kind] += 1
                counts['access_differs'] += f.access_differs
            flagged = duplicates.apply(transient_id, flags) if opts['apply'] else len(flags)
            counts['flagged'] += flagged
            self._report(name, flags, flagged, opts)

        self.stdout.write('%s: %d transients, %d with duplicates; %d photometry points and %d spectra %s, %.0f s' % (
            'Applied' if opts['apply'] else 'Dry run', counts['transients'], counts['with_duplicates'],
            counts[duplicates.PHOTOMETRY], counts[duplicates.SPECTRUM],
            'flagged REJECTED' if opts['apply'] else 'would be flagged', time.time() - started))
        if counts['access_differs']:
            self.stdout.write('  %d flagged copies have other groups or another allocation than the copy kept '
                              '(marked "access differs" above).' % counts['access_differs'])
        if opts['max'] is not None and counts['transients'] == opts['max'] and last_id is not None:
            self.stdout.write('  continue with --after %d' % last_id)

    def _report(self, name, flags, flagged, opts):
        self.stdout.write('%s: %d copies %s' % (name, len(flags), 'flagged' if opts['apply'] else 'to flag')
                          + ('' if flagged == len(flags) else ' (%d changed since read, skipped)' % (len(flags) - flagged)))
        if opts['quiet']:
            return
        for f in flags:
            o = f.copy.obs
            what = ('point %s (%s, MJD %.5f)' % (o.key, o.band, o.mjd) if f.kind == duplicates.PHOTOMETRY
                    else 'spectrum %s (MJD %.4f)' % (o.key, o.mjd))
            self.stdout.write('  %s: keep %s, %s%s%s' % (
                what, f.kept.obs.key, f.reason, ', weak match' if f.weak else '',
                ', access differs' if f.access_differs else ''))

    def _revert(self, transients, opts):
        from YSE_App.models import DuplicateDataDecision

        decisions = DuplicateDataDecision.objects.filter(reverted_date__isnull=True)
        if opts['transient']:
            decisions = decisions.filter(transient__in=transients)
        if not opts['apply']:
            self.stdout.write('Dry run: %d decisions would be reverted.' % decisions.count())
            return
        self.stdout.write('Reverted %d decisions.' % duplicates.revert(decisions))
