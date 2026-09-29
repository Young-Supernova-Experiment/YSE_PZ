"""
Backfill or repair the per-transient photometry statistics (``TransientPhotStat``, #268).

Every transient gets one stat row computed from its photometry; a row whose
values did not change is left alone, so the command is safe to re-run::

  python manage.py rebuild_photstats                      # every transient
  python manage.py rebuild_photstats --missing-only       # only transients without a row
  python manage.py rebuild_photstats --transient 2025aarm --transient 2026bzn
  python manage.py rebuild_photstats --batch-size 200

Transients are processed in batches (``--batch-size``, default 500) with one
photometry query and one stat query per batch; progress is printed per
batch and the totals (processed / created / updated / unchanged) at the end.
Interrupting and re-running picks up where the values differ, and
``--missing-only`` resumes a first backfill without touching finished rows.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from YSE_App.models import Transient, TransientPhotStat
from YSE_App.services import photstat


class Command(BaseCommand):
    help = "Recompute TransientPhotStat rows (all transients, --missing-only, or --transient NAME)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--transient", action="append", default=[], metavar="NAME",
            help="Only this transient (repeatable).",
        )
        parser.add_argument(
            "--missing-only", action="store_true",
            help="Only transients that have no stat row yet.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=photstat.DEFAULT_BATCH_SIZE,
            help="Transients per batch (default %(default)s).",
        )
        parser.add_argument(
            "--quiet", action="store_true", help="Print the totals only.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1")
        qs = Transient.objects.order_by("pk")
        if options["transient"]:
            qs = qs.filter(name__in=options["transient"])
            missing = set(options["transient"]) - set(qs.values_list("name", flat=True))
            if missing:
                raise CommandError("Unknown transient(s): %s" % ", ".join(sorted(missing)))
        if options["missing_only"]:
            qs = qs.exclude(pk__in=TransientPhotStat.objects.values("transient_id"))
        ids = list(qs.values_list("pk", flat=True))
        total = len(ids)
        totals = photstat.RecomputeResult()
        started = time.monotonic()
        for start in range(0, total, batch_size):
            batch = ids[start:start + batch_size]
            result = photstat.recompute_many(batch, batch_size=batch_size)
            totals.processed += result.processed
            totals.created += result.created
            totals.updated += result.updated
            totals.unchanged += result.unchanged
            if not options["quiet"]:
                self.stdout.write(
                    "batch %d-%d of %d: created %d, updated %d, unchanged %d (%.1f s)"
                    % (start + 1, start + len(batch), total, result.created, result.updated,
                       result.unchanged, time.monotonic() - started)
                )
        self.stdout.write(self.style.SUCCESS(
            "rebuild_photstats: processed %d transient(s); created %d, updated %d, unchanged %d in %.1f s"
            % (totals.processed, totals.created, totals.updated, totals.unchanged,
               time.monotonic() - started)
        ))
