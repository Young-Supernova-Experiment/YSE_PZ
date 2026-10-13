"""Fetch researcher publication metrics from SciX by ORCID (#629).

    python manage.py refresh_researcher_metrics [--force] [--limit N] [--user USERNAME ...]

Off unless RESEARCHER_METRICS_ENABLED ([research] in settings.ini, or env
YSE_RESEARCHER_METRICS=1); ``--even-if-disabled`` runs it once anyway. The
SciX token is the EncryptedCredential with service ``scix`` (payload
``{"token": "..."}``). Rate-limited: REQUEST_INTERVAL_SECONDS between users,
MAX_PER_RUN users, and profiles fetched within MIN_AGE_DAYS are skipped unless
``--force``.
"""

from django.core.management.base import BaseCommand

from YSE_App.services import researcher_metrics


class Command(BaseCommand):
    help = "Refresh SciX publication metrics for researcher profiles with an ORCID iD."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Ignore MIN_AGE_DAYS.")
        parser.add_argument("--limit", type=int, default=None, help="Maximum users to fetch this run.")
        parser.add_argument("--user", action="append", dest="usernames", default=None,
                            help="Only this username (repeatable).")
        parser.add_argument("--even-if-disabled", action="store_true",
                            help="Run although RESEARCHER_METRICS_ENABLED is off.")

    def handle(self, *args, **options):
        if not researcher_metrics.enabled() and not options["even_if_disabled"]:
            self.stdout.write("researcher metrics: disabled (RESEARCHER_METRICS_ENABLED is off)")
            return
        summary = researcher_metrics.refresh_all(
            force=options["force"], limit=options["limit"], usernames=options["usernames"])
        for line in summary.errors:
            self.stderr.write(line)
        self.stdout.write("researcher metrics: %s" % summary)
