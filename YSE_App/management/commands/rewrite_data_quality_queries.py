"""
Rewrite saved Explorer queries that still use the dropped ``data_quality_id`` column.

Migration 0002 turned ``data_quality`` on the photometry and spectrum tables
into a ManyToMany, so a saved query (``explorer_query`` row, including the ones
pinned to personal dashboards) that says ``ISNULL(pd2.data_quality_id) = True``
fails with "Unknown column" on a migrated database. This command replaces each
such reference with the join-table form that returns the same rows (see
``YSE_App/queries/data_quality_sql.py``). It is a command, not a data
migration, so it runs only when someone runs it. Dry run is the default::

  python manage.py rewrite_data_quality_queries                 # show diffs
  python manage.py rewrite_data_quality_queries --apply         # write, with backup
  python manage.py rewrite_data_quality_queries --apply --id 62 --id 68
  python manage.py rewrite_data_quality_queries --revert <backup.json> --apply

``--apply`` refuses while the photometry table still has the
``data_quality_id`` column (the queries work there as they are). A query whose
references cannot all be resolved is reported as REVIEW and left alone.
Backups and ``--revert`` work as in ``rewrite_dashboard_queries``.
"""

from __future__ import annotations

from django.core.management.base import CommandError
from django.db import connection

from YSE_App.management.commands.rewrite_dashboard_queries import Command as _RewriteCommand
from YSE_App.management.commands.rewrite_dashboard_queries import _diff
from YSE_App.queries.data_quality_sql import rewrite_data_quality_sql


def photdata_has_data_quality_column(conn=None) -> bool:
    """True on the pre-0002 schema, where the saved queries still work."""
    from YSE_App.models import TransientPhotData

    conn = conn or connection
    with conn.cursor() as cursor:
        columns = conn.introspection.get_table_description(cursor, TransientPhotData._meta.db_table)
    return any(col.name == "data_quality_id" for col in columns)


class Command(_RewriteCommand):
    help = "Rewrite saved Explorer queries that use the dropped data_quality_id column (dry run unless --apply)."
    command_name = "rewrite_data_quality_queries"
    backup_prefix = "saved_queries_data_quality"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--id", type=int, action="append", default=[], dest="ids",
                            help="Only this saved-query id (repeatable).")

    def handle(self, *args, **options):
        if not options["revert"] and photdata_has_data_quality_column():
            msg = ("YSE_App_transientphotdata still has the data_quality_id column (migration 0002 not "
                   "applied here), so the saved queries work as they are.")
            if options["apply"]:
                raise CommandError(msg + " Not rewriting.")
            self.stdout.write(self.style.WARNING(msg + " Showing the plan only."))
        return super().handle(*args, **options)

    def _plan_rewrite(self, options):
        from explorer.models import Query

        rows = Query.objects.filter(sql__icontains="data_quality_id").order_by("id")
        if options["ids"]:
            rows = rows.filter(id__in=options["ids"])
        changes, review = [], 0
        for q in rows:
            if not self._wanted(options, q.title):
                continue
            result = rewrite_data_quality_sql(q.sql)
            if result.state == "clean":
                continue
            if result.state == "review":
                review += 1
                self.stdout.write(self.style.WARNING(
                    f"REVIEW     {q.id} {q.title}: data_quality_id used in a form this command does not "
                    f"rewrite; left alone. Context:"))
                for snippet in result.leftover:
                    self.stdout.write(f"    ... {snippet} ...")
                continue
            self.stdout.write(f"REWRITE    {q.id} {q.title}")
            self.stdout.write(_diff(q.sql, result.sql, f"{q.id}"))
            changes.append((q, result.sql))
        if review:
            self.stdout.write(f"{review} saved quer{'y' if review == 1 else 'ies'} need a hand edit (REVIEW above).")
        return changes
