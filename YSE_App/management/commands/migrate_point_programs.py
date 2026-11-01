"""Copy each photometry header's program link onto its points (#714). Dry run unless ``--apply``.

Access is per point: a point's own ``allocation`` decides which program's
standing grants cover it, and the header's link is only the default. Until
this runs, points without a link already follow their header's, so the copy
changes no access; it makes every point carry its program explicitly, so a
later change of the header's default does not move earlier points. Points that
already have a program are never changed. Prints counts only.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from YSE_App.services.program_links import apply_point_links, point_link_plan


class Command(BaseCommand):
    help = "Copy photometry header program links onto their points (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the links (default: dry run).")

    def handle(self, *args, **options):
        plan = point_link_plan()
        points = sum(n for _a, n in plan.values())
        self.stdout.write("headers: %d; points to link: %d" % (len(plan), points))
        if not options["apply"]:
            self.stdout.write("dry run: nothing written (pass --apply to write)")
            return
        with transaction.atomic():
            n = apply_point_links(plan)
        self.stdout.write("written: %d points" % n)
