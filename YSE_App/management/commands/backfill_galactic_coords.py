"""
Fill the stored galactic coordinates (``Transient.gal_l`` / ``gal_b``, #286).

``Transient.save`` keeps the two columns in step with ``ra`` / ``dec`` and
migration ``0027_transient_galactic_coords`` filled every existing row, so
this command is only needed after rows were written around ``save()``
(``bulk_create``, raw SQL, ``QuerySet.update(ra=..., dec=...)``)::

  python manage.py backfill_galactic_coords          # rows with gal_b IS NULL
  python manage.py backfill_galactic_coords --all    # recompute every row

Both run as one UPDATE with the database evaluating the closed-form rotation
(``common/galactic.py``), a few seconds for 1e5 rows.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from YSE_App.common.galactic import backfill_galactic_coords
from YSE_App.models import Transient


class Command(BaseCommand):
    help = "Fill Transient.gal_l / gal_b from ra / dec (rows with NULL gal_b, or --all)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", dest="all_rows", help="Recompute every row.")

    def handle(self, *args, **options):
        started = time.perf_counter()
        n = backfill_galactic_coords(Transient, all_rows=options["all_rows"])
        remaining = Transient.objects.filter(gal_b__isnull=True).count()
        self.stdout.write(
            "updated %d transient(s) in %.1f s; %d still without gal_b (ra/dec unusable)"
            % (n, time.perf_counter() - started, remaining)
        )
