"""Delete old django-auditlog rows in batches (#486).

    python manage.py prune_auditlog --older-than-days 365 --dry-run
    python manage.py prune_auditlog --older-than-days 365
    python manage.py prune_auditlog --older-than-days 90 --model YSE_App.transient

--older-than-days is required: nothing is deleted unless the operator chooses N.
Ingest writes an auditlog row for every Transient save, so this table grows fast.
Rows are deleted in batches (default 5000) to keep locks and the binlog small.
"""

from __future__ import annotations

import time
from datetime import timedelta

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete auditlog LogEntry rows older than N days (required), optionally for one model."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-days", type=int, required=True,
                            help="Delete entries older than this many days (must be >= 1).")
        parser.add_argument("--model", default=None,
                            help="Only this model, as app_label.ModelName (e.g. YSE_App.Transient).")
        parser.add_argument("--batch-size", type=int, default=5000, help="Rows deleted per batch.")
        parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to pause between batches.")
        parser.add_argument("--dry-run", action="store_true", help="Count the rows without deleting.")

    def handle(self, *args, **options):
        from auditlog.models import LogEntry

        days = options["older_than_days"]
        if days < 1:
            raise CommandError("--older-than-days must be at least 1")
        batch = max(1, options["batch_size"])
        cutoff = timezone.now() - timedelta(days=days)
        qs = LogEntry.objects.filter(timestamp__lt=cutoff)
        if options["model"]:
            try:
                model = apps.get_model(options["model"])
            except (LookupError, ValueError):
                raise CommandError("Unknown model %r (use app_label.ModelName)" % options["model"])
            qs = qs.filter(content_type=ContentType.objects.get_for_model(model))

        if options["dry_run"]:
            self.stdout.write("would delete %d auditlog entries older than %s" % (qs.count(), cutoff.isoformat()))
            return

        total = 0
        while True:
            pks = list(qs.order_by("pk").values_list("pk", flat=True)[:batch])
            if not pks:
                break
            deleted, _ = LogEntry.objects.filter(pk__in=pks).delete()
            total += deleted
            if options["sleep"]:
                time.sleep(options["sleep"])
        self.stdout.write("deleted %d auditlog entries older than %s" % (total, cutoff.isoformat()))
