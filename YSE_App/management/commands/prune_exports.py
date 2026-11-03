"""Delete background export files older than ``EXPORT_RETENTION_DAYS`` (#855).

    python manage.py prune_exports            # delete
    python manage.py prune_exports --dry-run  # only count

Every ``exports.build`` job runs the same ``prune_expired()`` before it builds its file, so this
command is only needed when no exports run for a while. See docs/background-exports.md.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from YSE_App.services.background_exports import prune_expired, retention_days


class Command(BaseCommand):
    help = "Delete background export files older than EXPORT_RETENTION_DAYS."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count the exports without deleting.")

    def handle(self, *args, **options):
        n = prune_expired(dry_run=options["dry_run"])
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write("%s %d export(s) older than %d days" % (verb, n, retention_days()))
