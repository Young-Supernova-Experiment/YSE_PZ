"""
Backfill or repair the per-scope photometry statistics (``TransientPhotStatScope``, #607).

A transient with photometry restricted to collaboration groups gets one scope
row for its public photometry and one per restricting group; a viewer who may
not see all of the transient's photometry is shown the best scope they may
see (``services.photstat_visibility``). New uploads and group changes keep the
rows current; this command writes them for the existing data. Until it has
run, such a viewer sees no derived values (last detection, peak, ...) for a
transient with restricted photometry, never the all-data values.

Dry run by default (counts only, nothing written)::

  python manage.py backfill_photstat_scopes                     # what would change
  python manage.py backfill_photstat_scopes --apply             # write the rows
  python manage.py backfill_photstat_scopes --apply --transient 2025aarm
  python manage.py backfill_photstat_scopes --apply --batch-size 200 --limit 5000

Only transients with restricted photometry (or stale scope rows) are visited,
in pk order, in batches (``--batch-size``, default 500) with three reads per
batch. Unchanged rows are not written, so the command is safe to re-run.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from YSE_App.models import Transient
from YSE_App.services import photstat


class Command(BaseCommand):
    help = "Write TransientPhotStatScope rows (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the rows (default: dry run).")
        parser.add_argument(
            "--transient", action="append", default=[], metavar="NAME",
            help="Only this transient (repeatable).",
        )
        parser.add_argument(
            "--batch-size", type=int, default=photstat.DEFAULT_BATCH_SIZE,
            help="Transients per batch (default %(default)s).",
        )
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many transients (0: all).")
        parser.add_argument("--quiet", action="store_true", help="Print the totals only.")

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1")
        apply = options["apply"]
        if options["transient"]:
            qs = Transient.objects.filter(name__in=options["transient"])
            missing = set(options["transient"]) - set(qs.values_list("name", flat=True))
            if missing:
                raise CommandError("Unknown transient(s): %s" % ", ".join(sorted(missing)))
            ids = sorted(qs.values_list("pk", flat=True))
        else:
            ids = photstat.transients_needing_scopes(limit=options["limit"] or None)
        total = len(ids)
        totals = photstat.ScopeResult()
        started = time.monotonic()
        for start in range(0, total, batch_size):
            batch = ids[start:start + batch_size]
            result = photstat.sync_scopes(batch, dry_run=not apply)
            for name in ("transients", "created", "updated", "unchanged", "deleted"):
                setattr(totals, name, getattr(totals, name) + getattr(result, name))
            if not options["quiet"]:
                self.stdout.write(
                    "%d/%d transients (%.0fs)" % (min(start + batch_size, total), total, time.monotonic() - started)
                )
        verb = "" if apply else "would be "
        self.stdout.write(self.style.SUCCESS(
            "%s: %d transients; scope rows %screated %d, updated %d, deleted %d; unchanged %d"
            % ("Applied" if apply else "Dry run", totals.transients, verb, totals.created, totals.updated,
               totals.deleted, totals.unchanged)
        ))
