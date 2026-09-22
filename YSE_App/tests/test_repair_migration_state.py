"""Tests for ``manage.py repair_migration_state`` (Ziggy migration-state repair)."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase, SimpleTestCase

from YSE_App.management.commands.repair_migration_state import (
    CONSISTENT,
    DROP,
    EXPECTED,
    HANDOFF,
    UNRECORD,
    plan_migration,
)


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
