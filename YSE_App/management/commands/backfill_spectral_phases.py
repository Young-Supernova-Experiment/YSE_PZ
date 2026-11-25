"""Write the computed spectral phases (#848) of every transient that has spectra.

New fits and new spectra write their rows as they are stored; this fills the table for spectra
stored before it existed, and refreshes rows after redshift or discovery-date changes (those do
not trigger a recompute). Dry run by default (counts only)::

  python manage.py backfill_spectral_phases            # what would be written
  python manage.py backfill_spectral_phases --apply    # write it

Each transient's rows are replaced in one transaction, so it can be run again at any time.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from YSE_App.models import SpectrumPhase, TransientSpectrum
from YSE_App.services import spectral_phase


class Command(BaseCommand):
    help = "Compute the phase of every transient spectrum under each reference (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the rows (default: only count them).")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many transients (0: all).")

    def handle(self, *args, **options):
        apply = options["apply"]
        limit = options["limit"] or 0
        transient_ids = (TransientSpectrum.objects.order_by("transient_id")
                         .values_list("transient_id", flat=True).distinct())
        counts = {"transients": 0, "spectra": 0, "existing": 0, "rows": 0, "errors": 0}
        for transient_id in transient_ids.iterator():
            if limit and counts["transients"] >= limit:
                break
            counts["transients"] += 1
            if not apply:
                counts["spectra"] += TransientSpectrum.objects.filter(transient_id=transient_id).count()
                counts["existing"] += SpectrumPhase.objects.filter(transient_id=transient_id).count()
                continue
            try:
                counts["rows"] += spectral_phase.recompute_transient(transient_id)
            except Exception as exc:  # noqa: BLE001 - one bad transient must not stop the backfill
                counts["errors"] += 1
                self.stderr.write("transient %s: %s: %s" % (transient_id, type(exc).__name__, exc))
        if apply:
            self.stdout.write("%(transients)d transient(s) with spectra: %(rows)d phase row(s) written, "
                              "%(errors)d error(s)" % counts)
        else:
            self.stdout.write("dry run: %(transients)d transient(s) with %(spectra)d spectrum(s); %(existing)d "
                              "phase row(s) stored now. Run again with --apply to write them." % counts)
