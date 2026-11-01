"""Write the SALT / Bazin fit-table rows of runs that finished before the tables existed (#846).

New runs write their rows when they succeed; this fills the tables from the ``result`` JSON
of earlier succeeded runs. Dry run by default (counts only)::

  python manage.py backfill_fit_tables            # what would be written
  python manage.py backfill_fit_tables --apply    # write it

Runs that already have rows are skipped, so it can be run again at any time. Failed runs
and runs of other services write nothing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from YSE_App.models import ExternalServiceRun
from YSE_App.services import fit_tables


class Command(BaseCommand):
    help = "Fill the BazinFit / SaltFit tables from earlier succeeded fit runs (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the rows (default: only count them).")

    def handle(self, *args, **options):
        apply = options["apply"]
        runs = (ExternalServiceRun.objects
                .filter(status=ExternalServiceRun.STATUS_SUCCEEDED, transient__isnull=False,
                        service__analysis__runner_kind="inprocess",
                        service__analysis__runner_path__in=fit_tables.BAZIN_RUNNERS + fit_tables.SALT_RUNNERS)
                .select_related("service", "service__analysis").order_by("pk"))
        counts = {"runs": 0, "already": 0, "rows": 0, "runs_written": 0, "nothing": 0, "errors": 0}
        for run in runs.iterator():
            counts["runs"] += 1
            if fit_tables.has_rows(run):
                counts["already"] += 1
                continue
            if not apply:
                counts["runs_written"] += 1
                continue
            try:
                written = fit_tables.record_run(run)
            except Exception as exc:  # noqa: BLE001 - one bad result must not stop the backfill
                counts["errors"] += 1
                self.stderr.write("run %s: %s: %s" % (run.pk, type(exc).__name__, exc))
                continue
            if written:
                counts["runs_written"] += 1
                counts["rows"] += written
            else:
                counts["nothing"] += 1
        if apply:
            self.stdout.write(
                "%(runs)d succeeded fit run(s): %(runs_written)d written (%(rows)d row(s)), %(already)d already had "
                "rows, %(nothing)d gave no row, %(errors)d error(s)" % counts)
        else:
            self.stdout.write(
                "dry run: %(runs)d succeeded fit run(s), %(runs_written)d without rows would be read, "
                "%(already)d already have rows. Run again with --apply to write them." % counts)
