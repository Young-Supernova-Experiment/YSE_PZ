"""Delete finished ExternalServiceRun rows (and artifact files) older than N days.

    python manage.py expire_service_runs --days 90 [--dry-run]
"""

from django.core.management.base import BaseCommand

from YSE_App.services.external_services import expire_runs


class Command(BaseCommand):
    help = "Delete finished external-service runs older than --days (default 90)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        n = expire_runs(options["days"], dry_run=options["dry_run"])
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write("%s %d finished run(s) older than %d day(s)." % (verb, n, options["days"]))
