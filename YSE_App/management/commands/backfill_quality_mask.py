"""Check, and with ``--apply`` fix, ``quality_mask`` against the DataQuality M2M (#81).

Migration ``0036_quality_mask`` fills the mask for every flagged row and the
``m2m_changed`` receivers keep it current, so on a healthy database this
reports nothing to change. It is needed after writes that bypass the signals
(``develop`` code on the shared ``YSE_test`` database, raw SQL, a restore)::

  python manage.py backfill_quality_mask                 # dry run: report only
  python manage.py backfill_quality_mask --apply         # write the fixes
  python manage.py backfill_quality_mask --model TransientSpectrum --batch-size 2000

For each row it recomputes the DataQuality-managed bits
(``quality_mask.merge_data_quality``: the bit each name maps to, OTHER for
unmapped names; only "Bad" sets REJECTED) and, on spectra, WAVELENGTH_MEDIUM_UNKNOWN from
``wavelength_medium``. Other bits are kept. Rows are read in primary-key
batches (``--batch-size``, default 5000); each batch's fixes are one UPDATE per
distinct mask value. Output lists the DataQuality names found and the bit each
maps to; it never prints row contents.
"""

from __future__ import annotations

import time

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from YSE_App.common import quality_mask as qm
from YSE_App.common import quality_mask_sync

_FLAG_CLASSES = {
    'TransientPhotData': qm.PhotFlag,
    'HostPhotData': qm.PhotFlag,
    'TransientSpectrum': qm.SpecFlag,
    'HostSpectrum': qm.HostSpecFlag,
}


class Command(BaseCommand):
    help = "Recompute quality_mask from the DataQuality M2M (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the fixes (default: report only).')
        parser.add_argument('--model', action='append', choices=list(quality_mask_sync.MASKED_MODELS),
                            help='Only this model (repeatable). Default: all four.')
        parser.add_argument('--batch-size', type=int, default=5000)
        parser.add_argument('--show', type=int, default=10,
                            help='Print up to this many example changes (primary key and flags) per model.')

    def handle(self, *args, **options):
        if options['batch_size'] < 1:
            raise CommandError('--batch-size must be positive')
        apply = options['apply']
        names = options['model'] or list(quality_mask_sync.MASKED_MODELS)
        total_changed = 0
        for name in names:
            model = apps.get_model('YSE_App', name)
            flag_cls = _FLAG_CLASSES[name]
            shown = []

            def on_change(pk, old, new, row_names, shown=shown, flag_cls=flag_cls):
                if len(shown) < options['show']:
                    shown.append('  pk=%s: %s -> %s' % (
                        pk, '|'.join(qm.flags_from_mask(old, flag_cls)) or '0',
                        '|'.join(qm.flags_from_mask(new, flag_cls)) or '0'))

            started = time.perf_counter()
            stats = quality_mask_sync.scan(model, apply=apply, batch_size=options['batch_size'],
                                           on_change=on_change)
            total_changed += stats['changed']
            self.stdout.write('%s: %d row(s) scanned, %d with DataQuality, %d %s (%.1f s)' % (
                name, stats['rows'], stats['flagged'], stats['changed'],
                'updated' if apply else 'to update', time.perf_counter() - started))
            for dq_name, count in sorted(stats['by_name'].items(), key=lambda kv: str(kv[0])):
                bit = qm.data_quality_bit(dq_name, name)
                flags = qm.flags_from_mask(bit, flag_cls)
                self.stdout.write('  DataQuality %r on %d row(s) -> %s' % (dq_name, count, '|'.join(flags)))
            for line in shown:
                self.stdout.write(line)
        if apply:
            self.stdout.write(self.style.SUCCESS('done: %d row(s) updated' % total_changed))
        else:
            self.stdout.write('dry run: %d row(s) would change; re-run with --apply to write them' % total_changed)
