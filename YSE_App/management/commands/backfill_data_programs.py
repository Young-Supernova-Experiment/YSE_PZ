"""Link existing photometry and spectra to the program (allocation) they were taken under (#648). Dry run unless ``--apply``.

Only datasets with no program link are considered, and only an unambiguous
link is written: the follow-up's resource or facility-request allocation, the
classical night of a matching AstroScheduler observation, or (spectra only) the
one allocation on the same instrument whose dates cover the spectrum. Prints
counts only (no names). Writing applies each program's standing access grants
to the newly linked data. See ``YSE_App.services.program_links.backfill_plan``.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from YSE_App.services.program_links import apply_backfill, backfill_plan

SOURCES = ("followup", "observed", "date")


class Command(BaseCommand):
    help = "Link photometry and spectra to their program (allocation) where unambiguous (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the links (default: dry run, nothing written).")
        parser.add_argument("--include-public", action="store_true",
                            help="Also link datasets with no collaboration groups (default: restricted data only).")
        parser.add_argument("--source", action="append", choices=SOURCES,
                            help="Sources in order of preference (default: followup, observed, date).")
        parser.add_argument("--limit", type=int, default=None, help="Look at most this many datasets of each kind.")

    def handle(self, *args, **options):
        sources = tuple(options.get("source") or SOURCES)
        plan = backfill_plan(include_public=options["include_public"], sources=sources, limit=options["limit"])
        counts = plan["counts"]
        self.stdout.write(
            "datasets to link: %d (followup %d, observed %d, date %d); ambiguous %d; unmatched %d"
            % (len(plan["link"]), counts["linked_followup"], counts["linked_observed"], counts["linked_date"],
               counts["ambiguous"], counts["unmatched"])
        )
        if not options["apply"]:
            self.stdout.write("dry run: nothing written (pass --apply to write)")
            return
        with transaction.atomic():
            n = apply_backfill(plan)
        self.stdout.write("written: %d datasets" % n)
