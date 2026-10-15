"""Delete old notification rows and finished job rows (issue #320 retention).

    python manage.py prune_notifications            # settings defaults
    python manage.py prune_notifications --dry-run  # only count
    python manage.py prune_notifications --read-days 30 --unread-days 180 --job-days 7

The ``PruneNotifications`` django_cron class runs the same ``prune()`` daily
from ``manage.py runcrons``; ``enqueue("notifications.prune")`` runs it on the
job queue. See docs/background-jobs.md.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from YSE_App.services.notify import prune


class Command(BaseCommand):
    help = "Delete read/unread notifications and finished jobs older than the retention settings."

    def add_arguments(self, parser):
        parser.add_argument("--read-days", type=int, default=None, help="Read notifications older than N days (0 keeps all).")
        parser.add_argument("--unread-days", type=int, default=None, help="Unread notifications older than N days (0 keeps all).")
        parser.add_argument("--job-days", type=int, default=None, help="Finished jobs older than N days (0 keeps all).")
        parser.add_argument("--dry-run", action="store_true", help="Count the rows without deleting.")

    def handle(self, *args, **options):
        result = prune(read_days=options["read_days"], unread_days=options["unread_days"],
                       job_days=options["job_days"], dry_run=options["dry_run"])
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write("%s %d read notifications, %d unread notifications, %d finished jobs" % (
            verb, result["notifications_read"], result["notifications_unread"], result["jobs"]))
