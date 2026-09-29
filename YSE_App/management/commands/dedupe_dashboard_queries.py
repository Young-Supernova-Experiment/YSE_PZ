"""
Remove duplicate saved queries from users' personal dashboards.

``UserQuery`` has no uniqueness constraint, so before the add view became
idempotent (issue #203) the same explorer/Python query could be attached to a
user many times.  This keeps the oldest row of each (user, query) group and
deletes the rest::

  docker exec ysepz_web_container python3 manage.py dedupe_dashboard_queries --dry-run
  docker exec ysepz_web_container python3 manage.py dedupe_dashboard_queries
  docker exec ysepz_web_container python3 manage.py dedupe_dashboard_queries --user rfoley

Nothing else references ``UserQuery`` rows, so deleting the extra copies only
removes the duplicated boxes from the dashboard.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from YSE_App.models import UserQuery
from YSE_App.services.dashboard_queries import dedupe_user_queries

User = get_user_model()


def _label(row):
    if row.query_id:
        return f"SQL {row.query_id} ({row.query.title})"
    return f"python {row.python_query!r}"


class Command(BaseCommand):
    help = "Delete duplicate UserQuery rows, keeping the oldest copy per user and query."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted; do not update the database.",
        )
        parser.add_argument(
            "--user",
            help="Only de-duplicate this username's dashboard.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        rows = UserQuery.objects.select_related("user", "query")
        if options["user"]:
            if not User.objects.filter(username=options["user"]).exists():
                raise CommandError(f"No user named {options['user']!r}")
            rows = rows.filter(user__username=options["user"])

        groups, doomed = dedupe_user_queries(rows, dry_run=dry_run)

        for group in groups:
            keep, extra = group[0], group[1:]
            self.stdout.write(
                f"{keep.user.username}: {_label(keep)} x{len(group)} -> keep id {keep.id}, "
                f"{'would delete' if dry_run else 'deleted'} "
                + ", ".join(str(row.id) for row in extra)
            )

        self.stdout.write(
            f"Duplicate groups: {len(groups)}; extra rows: {len(doomed)} of {rows.count()}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes made."))
        elif doomed:
            self.stdout.write(self.style.SUCCESS(f"Deleted {len(doomed)} duplicate row(s)."))
        else:
            self.stdout.write(self.style.SUCCESS("No duplicates found."))
