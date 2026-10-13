"""Delete decided or withdrawn team membership requests older than N days (#448).

Pending requests and group memberships are never touched.
"""

from django.core.management.base import BaseCommand

from YSE_App.services.membership import purge_days, purge_requests


class Command(BaseCommand):
    help = "Delete membership requests decided more than --days days ago (default GROUP_REQUEST_PURGE_DAYS)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = options["days"] if options["days"] is not None else purge_days()
        count = purge_requests(days, dry_run=options["dry_run"])
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write("group membership requests: %s %d (decided more than %d days ago)" % (verb, count, days))
