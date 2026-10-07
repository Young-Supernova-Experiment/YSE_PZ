"""Tests for ``manage.py repair_migration_state`` (Ziggy migration-state repair)."""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, SimpleTestCase

from YSE_App.management.commands.repair_migration_state import (
    APP,
    CONSISTENT,
    DROP,
    EXPECTED,
    HANDOFF,
    RESET,
    UNRECORD,
    plan_migration,
)

GROUP_TABLES = {
    "0005_log_visibility": ["YSE_App_log_groups"],
    "0006_followup_visibility": ["YSE_App_hostfollowup_groups", "YSE_App_transientfollowup_groups"],
}


def _exists(name, **overrides):
    """{Artifact: present?} in the pre-migration state, with per-artifact overrides."""
    state = {a: not a.present for a in EXPECTED[name].artifacts}
    for a in EXPECTED[name].artifacts:
        key = f"{a.table}.{a.column}" if a.column else a.table
        if key in overrides:
            state[a] = overrides[key]
    return state


def _post(name):
    return {a: a.present for a in EXPECTED[name].artifacts}


def _only_group_tables(name):
    """Pre-migration columns, but the M2M join tables exist (Ziggy after a --fake)."""
    return _exists(name, **{t: True for t in GROUP_TABLES[name]})


class PlanMigrationTests(SimpleTestCase):
    def test_recorded_and_all_artifacts_present_is_consistent(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                self.assertEqual(plan_migration(name, True, _post(name)).status, CONSISTENT)

    def test_unrecorded_and_pristine_schema_is_consistent(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                self.assertEqual(plan_migration(name, False, _exists(name)).status, CONSISTENT)

    def test_recorded_but_nothing_applied_is_unrecorded(self):
        for name in ("0005_log_visibility", "0006_followup_visibility", "0007_resource_creator_only"):
            with self.subTest(name=name):
                plan = plan_migration(name, True, _exists(name))
                self.assertEqual(plan.status, UNRECORD)
                self.assertEqual(plan.drop_sql, [])

    def test_recorded_partial_is_handoff(self):
        state = _exists("0006_followup_visibility", **{"YSE_App_hostfollowup.is_public": True})
        plan = plan_migration("0006_followup_visibility", True, state)
        self.assertEqual(plan.status, HANDOFF)
        self.assertTrue(any("sqlmigrate" in line for line in plan.detail))

    def test_0008_unrecorded_partial_ddl_plans_drops(self):
        state = _exists(
            "0008_followup_requests",
            **{
                "YSE_App_hostfollowup.priority": True,
                "YSE_App_transientfollowup.priority": True,
                "YSE_App_transientfollowuprequest": True,
            },
        )
        plan = plan_migration("0008_followup_requests", False, state)
        self.assertEqual(plan.status, DROP)
        self.assertEqual(
            plan.drop_sql,
            [
                "ALTER TABLE `YSE_App_hostfollowup` DROP COLUMN `priority`",
                "ALTER TABLE `YSE_App_transientfollowup` DROP COLUMN `priority`",
                "DROP TABLE `YSE_App_transientfollowuprequest`",
            ],
        )

    def test_0008_drops_only_what_exists(self):
        state = _exists("0008_followup_requests", **{"YSE_App_hostfollowup.priority": True})
        plan = plan_migration("0008_followup_requests", False, state)
        self.assertEqual(plan.status, DROP)
        self.assertEqual(plan.drop_sql, ["ALTER TABLE `YSE_App_hostfollowup` DROP COLUMN `priority`"])

    def test_0008_unrecorded_with_legacy_columns_gone_is_handoff(self):
        state = _exists(
            "0008_followup_requests",
            **{"YSE_App_hostfollowup.priority": True, "YSE_App_hostfollowup.phot_priority": False},
        )
        self.assertEqual(plan_migration("0008_followup_requests", False, state).status, HANDOFF)

    def test_0008_recorded_but_partial_is_handoff(self):
        state = _post("0008_followup_requests")
        state = {a: (False if a.table == "YSE_App_transientfollowuprequest" else v) for a, v in state.items()}
        self.assertEqual(plan_migration("0008_followup_requests", True, state).status, HANDOFF)

    def test_unrecorded_but_fully_applied_is_handoff_with_fake_hint(self):
        plan = plan_migration("0007_resource_creator_only", False, _post("0007_resource_creator_only"))
        self.assertEqual(plan.status, HANDOFF)
        self.assertTrue(any("--fake" in line for line in plan.detail))

    def test_0005_to_0007_never_plan_drops(self):
        state = _exists("0005_log_visibility", **{"YSE_App_log.is_public": True})
        self.assertEqual(plan_migration("0005_log_visibility", False, state).status, HANDOFF)

    def test_recorded_with_only_empty_group_tables_is_reset(self):
        for name, tables in GROUP_TABLES.items():
            with self.subTest(name=name):
                plan = plan_migration(name, True, _only_group_tables(name), row_count=lambda t: 0)
                self.assertEqual(plan.status, RESET)
                self.assertEqual(plan.drop_sql, [f"DROP TABLE `{t}`" for t in tables])
                for t in tables:
                    self.assertIn(f"table {t} has 0 rows", plan.detail)

    def test_recorded_with_populated_group_table_is_handoff(self):
        counts = {"YSE_App_hostfollowup_groups": 0, "YSE_App_transientfollowup_groups": 3}
        plan = plan_migration("0006_followup_visibility", True,
                              _only_group_tables("0006_followup_visibility"), row_count=counts.get)
        self.assertEqual(plan.status, HANDOFF)
        self.assertEqual(plan.drop_sql, [])
        self.assertIn("table YSE_App_transientfollowup_groups has 3 rows", plan.detail)
        self.assertTrue(any("sqlmigrate" in line for line in plan.detail))

    def test_recorded_with_group_tables_and_no_row_count_is_handoff(self):
        state = _only_group_tables("0005_log_visibility")
        self.assertEqual(plan_migration("0005_log_visibility", True, state).status, HANDOFF)

    def test_recorded_with_group_tables_and_a_column_is_handoff(self):
        state = _only_group_tables("0006_followup_visibility")
        state = _exists("0006_followup_visibility",
                        **{a.table: True for a in state if state[a] and not a.column},
                        **{"YSE_App_hostfollowup.is_public": True})
        plan = plan_migration("0006_followup_visibility", True, state, row_count=lambda t: 0)
        self.assertEqual(plan.status, HANDOFF)
        self.assertEqual(plan.drop_sql, [])


class RepairMigrationStateApplyTests(SimpleTestCase):
    """Ziggy's state: 0005/0006 recorded, only their empty group tables exist, 0007 recorded but pristine."""

    MODULE = "YSE_App.management.commands.repair_migration_state"

    def _run(self, *args, rows=0):
        exists = {}
        for name in EXPECTED:
            exists.update(_only_group_tables(name) if name in GROUP_TABLES else _exists(name))
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = (rows,)
        connection = mock.MagicMock(vendor="mysql", alias="default")
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.ops.quote_name = lambda n: f"`{n}`"
        recorder = mock.MagicMock()
        recorder.applied_migrations.return_value = {
            (APP, n) for n in ("0005_log_visibility", "0006_followup_visibility", "0007_resource_creator_only")}
        calls = mock.MagicMock()
        calls.attach_mock(cursor.execute, "execute")
        calls.attach_mock(recorder.record_unapplied, "record_unapplied")
        out = StringIO()
        with mock.patch(f"{self.MODULE}.connections", {"default": connection}), \
                mock.patch(f"{self.MODULE}.MigrationRecorder", return_value=recorder), \
                mock.patch(f"{self.MODULE}.snapshot_schema", return_value=exists), \
                mock.patch(f"{self.MODULE}.transaction"):
            call_command("repair_migration_state", *args, stdout=out)
        return out.getvalue(), [c for c in calls.mock_calls if not str(c[1][0]).startswith("SELECT COUNT")]

    def test_dry_run_plans_reset_without_touching_anything(self):
        output, calls = self._run()
        self.assertIn("0005_log_visibility: recorded=True -> RESET", output)
        self.assertIn("0006_followup_visibility: recorded=True -> RESET", output)
        self.assertIn("0007_resource_creator_only: recorded=True -> UNRECORD", output)
        self.assertIn("table YSE_App_log_groups has 0 rows", output)
        self.assertIn("PLAN  0005_log_visibility: drop empty group tables and unrecord", output)
        self.assertIn("will run: DROP TABLE `YSE_App_log_groups`;", output)
        self.assertNotIn("SQL:", output)
        self.assertIn("Dry run only", output)
        self.assertEqual(calls, [])

    def test_apply_drops_then_unrecords_in_order(self):
        output, calls = self._run("--apply", "--yes")
        self.assertEqual(calls, [
            mock.call.execute("DROP TABLE `YSE_App_log_groups`"),
            mock.call.record_unapplied(APP, "0005_log_visibility"),
            mock.call.execute("DROP TABLE `YSE_App_hostfollowup_groups`"),
            mock.call.execute("DROP TABLE `YSE_App_transientfollowup_groups`"),
            mock.call.record_unapplied(APP, "0006_followup_visibility"),
            mock.call.record_unapplied(APP, "0007_resource_creator_only"),
        ])
        self.assertIn("executed DROP TABLE `YSE_App_log_groups`", output)
        self.assertIn(f"unrecorded {APP}.0005_log_visibility", output)
        self.assertIn("Done.", output)

    def test_populated_group_table_is_handoff_and_apply_changes_nothing(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as ctx:
            self._run("--apply", "--yes", rows=2)
        self.assertIn("0005_log_visibility", str(ctx.exception))
        self.assertIn("0006_followup_visibility", str(ctx.exception))


class RepairMigrationStateCommandTests(TestCase):
    """The fully migrated test database must be reported as consistent."""

    def _run(self, *args):
        out = StringIO()
        call_command("repair_migration_state", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_nothing_to_do(self):
        output = self._run()
        for name in EXPECTED:
            self.assertIn(f"{name}: recorded=True -> CONSISTENT", output)
        self.assertIn("nothing to do", output)
        self.assertNotIn("PLAN", output)

    def test_apply_yes_is_a_noop_on_consistent_database(self):
        output = self._run("--apply", "--yes")
        self.assertIn("nothing to do", output)
        self.assertNotIn("unrecorded", output)
        self.assertNotIn("executed", output)
