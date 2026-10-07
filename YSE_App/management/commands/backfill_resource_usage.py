"""Estimate (and optionally record) resource usage from historical successful follow-ups (#304).

    python manage.py backfill_resource_usage           # preview per ToO / queued resource
    python manage.py backfill_resource_usage --apply   # charge them (one trigger + hours each, idempotent)
"""

from django.core.management.base import BaseCommand

from YSE_App.models.telescope_resource_models import QueuedResource, ToOResource
from YSE_App.services.allocations import estimate_resource_usage, record_followup_usage


class Command(BaseCommand):
    help = "Charge used_too_triggers / used_too_hours / used_hours for Successful follow-ups never accounted for."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Record the usage instead of only printing it.")
        parser.add_argument("--hours", type=float, default=None,
                            help="Hours to charge per follow-up when it carries none (default: its facility requests, else 0).")

    def handle(self, *args, **options):
        total = 0
        for model in (ToOResource, QueuedResource):
            for resource in model.objects.select_related("telescope"):
                estimate = estimate_resource_usage(resource)
                if not estimate["followups"]:
                    continue
                self.stdout.write("%s: %d successful follow-up(s) not charged, %.2f h" % (
                    resource, estimate["followups"], estimate["hours"]))
                if options["apply"]:
                    for followup in estimate["queryset"]:
                        if record_followup_usage(followup, hours=options["hours"]):
                            total += 1
        self.stdout.write("charged %d follow-up(s)" % total if options["apply"] else "preview only (use --apply)")
