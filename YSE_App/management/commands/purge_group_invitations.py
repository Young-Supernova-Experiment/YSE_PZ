"""Delete group invitations that expired, were revoked or were used up long ago (#445).

Open invitations and group memberships are never touched.
"""

from django.core.management.base import BaseCommand

from YSE_App.services.groups import invite_purge_days, purge_invitations


class Command(BaseCommand):
    help = "Delete invitation links that ended more than --days days ago (default GROUP_INVITE_PURGE_DAYS)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = options["days"] if options["days"] is not None else invite_purge_days()
        count = purge_invitations(days, dry_run=options["dry_run"])
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write("group invitations: %s %d (ended more than %d days ago)" % (verb, count, days))
