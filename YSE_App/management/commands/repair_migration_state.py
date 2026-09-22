"""Reconcile ``django_migrations`` for YSE_App 0005-0008 with the live schema.

On Ziggy the experimental database recorded 0005-0007 as applied while the
columns they add were never created, so the 0008 data migration crashed on
``hostfollowup.is_public``. This command introspects the real tables, compares
them with what each migration should have produced, and either unrecords fake
applications / drops half-applied 0008 artifacts (``--apply``) or hands off to a
human when the state is ambiguous. Default is a dry run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.migrations.recorder import MigrationRecorder

APP = "YSE_App"


@dataclass(frozen=True)
class Artifact:
    """A table or column a migration creates (``present=True``) or removes."""

    table: str
    column: str = ""  # empty -> the artifact is the table itself
    present: bool = True  # expected state once the migration has run

    def __str__(self):
        name = f"{self.table}.{self.column}" if self.column else f"table {self.table}"
        return name if self.present else f"{name} (removed by this migration)"


@dataclass(frozen=True)
class Expected:
    artifacts: tuple
    cleanup_ok: bool = False  # may we DROP half-created artifacts of an unrecorded run?


def _cols(table, *names, present=True):
    return tuple(Artifact(table, n, present) for n in names)


# Derived from YSE_App/migrations/0005..0008. Keep in sync if those change.
EXPECTED: Dict[str, Expected] = {
    "0005_log_visibility": Expected(
        _cols("YSE_App_log", "is_public") + (Artifact("YSE_App_log_groups"),)
    ),
    "0006_followup_visibility": Expected(
        _cols("YSE_App_hostfollowup", "is_public", "requested_by_id")
        + _cols("YSE_App_transientfollowup", "is_public", "requested_by_id")
        + (Artifact("YSE_App_hostfollowup_groups"), Artifact("YSE_App_transientfollowup_groups"))
    ),
    "0007_resource_creator_only": Expected(
        _cols("YSE_App_classicalresource", "creator_only")
        + _cols("YSE_App_queuedresource", "creator_only")
        + _cols("YSE_App_tooresource", "creator_only")
    ),
    "0008_followup_requests": Expected(
        _cols("YSE_App_hostfollowup", "priority")
        + _cols("YSE_App_transientfollowup", "priority")
        + (Artifact("YSE_App_transientfollowuprequest"),)
        + _cols("YSE_App_hostfollowup", "phot_priority", "spec_priority", present=False)
        + _cols("YSE_App_transientfollowup", "phot_priority", "spec_priority", present=False),
        # Only DDL that adds things ran before the RunPython crash; nothing to lose.
        cleanup_ok=True,
    ),
}

CONSISTENT, UNRECORD, DROP, HANDOFF = "consistent", "unrecord", "drop", "handoff"


@dataclass
class Plan:
    name: str
    status: str
    detail: List[str] = field(default_factory=list)
    drop_sql: List[str] = field(default_factory=list)


def plan_migration(name, recorded, exists, quote=lambda n: f"`{n}`"):
    """Decide what to do for one migration.

    ``exists`` maps each Artifact of ``EXPECTED[name]`` to whether it is in the
    live schema. Returns a Plan; never touches the database.
    """
    spec = EXPECTED[name]
    adds = [a for a in spec.artifacts if a.present]
    removes = [a for a in spec.artifacts if not a.present]
    ran = [a for a in spec.artifacts if exists[a] == a.present]  # looks post-migration
    plan = Plan(name, CONSISTENT)
    plan.detail = [f"{'present' if exists[a] else 'absent '}  {a}" for a in spec.artifacts]

    if len(ran) == len(spec.artifacts):
        if not recorded:
            plan.status, plan.detail = HANDOFF, plan.detail + [
                "schema is fully post-migration but django_migrations has no row; "
                f"if certain, run: manage.py migrate {APP} {name} --fake"]
        return plan
    if not ran:  # pristine pre-migration schema
        if recorded:
            plan.status = UNRECORD
        return plan
    # Mixed state from here on.
    if recorded or not spec.cleanup_ok or any(not exists[a] for a in removes):
        plan.status = HANDOFF
        plan.detail.append(f"inspect with: manage.py sqlmigrate {APP} {name}")
        return plan
    plan.status = DROP
    for a in adds:
        if exists[a]:
            plan.drop_sql.append(
                f"ALTER TABLE {quote(a.table)} DROP COLUMN {quote(a.column)}" if a.column
                else f"DROP TABLE {quote(a.table)}")
    return plan


def snapshot_schema(connection):
    """Return {Artifact: exists?} for every artifact in EXPECTED."""
    exists = {}
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        columns = {}
        for spec in EXPECTED.values():
            for a in spec.artifacts:
                if a.table in tables and a.table not in columns:
                    desc = connection.introspection.get_table_description(cursor, a.table)
                    columns[a.table] = {c.name for c in desc}
                exists[a] = a.table in tables and (not a.column or a.column in columns[a.table])
    return exists


class Command(BaseCommand):
    help = __doc__.split("\n\n")[0]

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--apply", action="store_true",
                            help="Execute the plan (default: dry run).")
        parser.add_argument("--yes", action="store_true",
                            help="With --apply: do not prompt for confirmation.")

    def handle(self, *args, **options):
        connection = connections[options["database"]]
        recorder = MigrationRecorder(connection)
        applied = {n for app, n in recorder.applied_migrations() if app == APP}
        exists = snapshot_schema(connection)
        plans = [plan_migration(n, n in applied, exists, connection.ops.quote_name)
                 for n in EXPECTED]

        out = self.stdout.write
        out(f"Database alias: {options['database']} ({connection.vendor})")
        for p in plans:
            out(f"\n{p.name}: recorded={p.name in applied} -> {p.status.upper()}")
            for line in p.detail:
                out(f"    {line}")
            for sql in p.drop_sql:
                out(f"    SQL: {sql};")

        handoff = [p for p in plans if p.status == HANDOFF]
        todo = [p for p in plans if p.status in (UNRECORD, DROP)]
        out("")
        if handoff:
            raise CommandError(
                "Ambiguous state for: " + ", ".join(p.name for p in handoff)
                + ". Nothing was changed; finish those by hand (see details above).")
        if not todo:
            out(self.style.SUCCESS("Schema and django_migrations agree: nothing to do."))
            return
        for p in todo:
            action = "unrecord from django_migrations" if p.status == UNRECORD else "drop partial artifacts"
            out(f"PLAN  {p.name}: {action}")
        if not options["apply"]:
            out("\nDry run only. Re-run with --apply --yes to execute the plan.")
            return
        if not options["yes"] and input("Type 'yes' to execute the plan: ").strip() != "yes":
            raise CommandError("Aborted.")

        with transaction.atomic(using=connection.alias):
            for p in todo:
                if p.status == UNRECORD:
                    recorder.record_unapplied(APP, p.name)
                    out(f"unrecorded {APP}.{p.name}")
        with connection.cursor() as cursor:
            for p in todo:
                for sql in p.drop_sql:
                    cursor.execute(sql)  # MySQL DDL autocommits; not transactional
                    out(f"executed {sql}")
        out(self.style.SUCCESS(
            "\nDone. Now run:\n    manage.py migrate --noinput --skip-checks\n"
            "(or re-run the failed Deploy Stack workflow, which does the same)."))
