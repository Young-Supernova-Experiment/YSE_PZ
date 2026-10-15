"""Backfill PIs on existing photometry and spectra (#446). Dry run unless ``--apply``.

Fills only datasets that have no PIs yet, from an allocation that covers the
spectrum (instrument and dates) or from the PIs linked to the dataset's
observation group; anything ambiguous or unmatched stays blank. Prints counts
only (no names). See ``YSE_App.services.data_pis.backfill_plan``.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from YSE_App.services.data_pis import apply_backfill, backfill_plan


class Command(BaseCommand):
    help = "Backfill PIs on photometry and spectra from allocations / observation groups (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the PIs (default: dry run, nothing written).")
        parser.add_argument("--include-public", action="store_true",
                            help="Also fill datasets with no collaboration groups (default: restricted data only).")
        parser.add_argument("--max-pis", type=int, default=1,
                            help="Skip a dataset when a source yields more PIs than this (default 1).")
        parser.add_argument("--source", action="append", choices=("allocation", "obs_group"),
                            help="Sources in order of preference (default: allocation, then obs_group).")

    def handle(self, *args, **options):
        sources = tuple(options.get("source") or ("allocation", "obs_group"))
        plan = backfill_plan(include_public=options["include_public"], max_pis=max(1, options["max_pis"]),
                             sources=sources)
        counts = plan["counts"]
        self.stdout.write(
            "datasets to fill: %d (allocation %d, obs_group %d); ambiguous %d; unmatched %d"
            % (len(plan["assign"]), counts["assigned_allocation"], counts["assigned_obs_group"],
               counts["ambiguous"], counts["unmatched"])
        )
        if not options["apply"]:
            self.stdout.write("dry run: nothing written (pass --apply to write)")
            return
        with transaction.atomic():
            n = apply_backfill(plan)
        self.stdout.write("written: %d datasets" % n)
