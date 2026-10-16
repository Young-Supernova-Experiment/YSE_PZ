"""Execution feedback: ObservedBlock/OutcomeReason (#666), recording planner actions (#667) and
tonight's outcomes in the planner (#668). Synthetic data only."""

import json
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import TestCase, override_settings
from django.urls import reverse

from YSE_App.models import NightPlan, ObservedBlock, OutcomeReason
from YSE_App.services import observations
from YSE_App.tests.test_night_plans import CONFIG_START, _PlansFixture
from YSE_App.tests.test_night_planner import BAZIN

MIGRATION_CODES = {"weather", "instrument", "telescope", "ran_out_of_time", "deprioritized", "low_sn",
                   "wrong_object", "not_visible", "too_faint", "classified_elsewhere", "superseded", "other"}


class ObservedBlockMigrationTests(TestCase):
    def test_migrations_match_the_models(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(
            graph=loader.graph)
        mine = [
            op for migration in changes.get("YSE_App", []) for op in migration.operations
            if (getattr(op, "model_name", None) or getattr(op, "name", "")).lower() in ("observedblock", "outcomereason")
        ]
        self.assertEqual(mine, [], [op.describe() for op in mine])

    def test_service_seed_matches_the_draft(self):
        self.assertEqual({code for code, *_ in observations.REASONS}, MIGRATION_CODES)


@override_settings(ASTROSCHEDULER_ENABLED=True)
class _ObservationsFixture(_PlansFixture):
    def _event(self, action, followup=None, **extra):
        followup = followup or self.open_fu
        body = {"action": action, "target_name": followup.transient.name, "external_id": str(followup.id),
                "time_utc": "2026-10-16T05:31:00+00:00", "planned_start": "2026-10-16T05:00:00+00:00",
                "planned_end": "2026-10-16T05:30:00+00:00", "reason": None, "note": ""}
        body.update(extra)
        return body

    def _post(self, user, body):
        return self._client(user).post(reverse("api-night-observations", kwargs={"night_id": self.night.pk}),
                                       data=json.dumps(body), content_type="application/json")

    def _status(self, followup=None):
        followup = followup or self.open_fu
        followup.refresh_from_db()
        return followup.status.name


class RecordEventTests(_ObservationsFixture):
    def test_start_then_complete(self):
        started = self._post(self.staff, self._event("start", time_utc="2026-10-16T05:02:00+00:00"))
        self.assertEqual(started.status_code, 201, started.content[:300])
        self.assertIsNone(started.json()["observed_block"])
        self.assertEqual(self._status(), "InProcess")

        done = self._post(self.staff, self._event("complete", started_utc="2026-10-16T05:02:00+00:00", note="good"))
        self.assertEqual(done.status_code, 201)
        block = ObservedBlock.objects.get(pk=done.json()["observed_block"])
        self.assertEqual((block.outcome, block.reason, block.source), ("success", None, "astroscheduler"))
        self.assertEqual((block.followup, block.transient, block.night), (self.open_fu, self.open_sn, self.night))
        self.assertEqual(block.start_ut.isoformat(), "2026-10-16T05:02:00+00:00")
        self.assertEqual(block.end_ut.isoformat(), "2026-10-16T05:31:00+00:00")
        self.assertEqual((block.comments, block.recorded_by), ("good", self.staff))
        self.assertEqual(block.settings["planned_start"], "2026-10-16T05:00:00+00:00")
        # Data taken: the request stays InProcess until someone judges it (#670).
        self.assertEqual(self._status(), "InProcess")

    def test_fail_and_punt_need_a_reason(self):
        for action in ("fail", "punt"):
            response = self._post(self.staff, self._event(action))
            self.assertEqual(response.status_code, 400, action)
            response = self._post(self.staff, self._event(action, reason="no_such_code"))
            self.assertEqual(response.status_code, 400, action)
        self.assertFalse(ObservedBlock.objects.exists())

        # Failed and Punted are outcomes of the observation; the request status is unchanged (#670).
        punted = self._post(self.staff, self._event("punt", reason="deprioritized"))
        self.assertEqual(punted.status_code, 201)
        self.assertEqual(ObservedBlock.objects.get(pk=punted.json()["observed_block"]).outcome, "punted")
        self.assertEqual(self._status(), "Requested")
        failed = self._post(self.staff, self._event("fail", reason="weather", note="clouds"))
        block = ObservedBlock.objects.get(pk=failed.json()["observed_block"])
        self.assertEqual((block.outcome, block.reason.code), ("failed", "weather"))
        self.assertEqual(self._status(), "Requested")

    def test_failed_observation_leaves_an_in_process_request_in_process(self):
        self._post(self.staff, self._event("start"))
        self._post(self.staff, self._event("fail", reason="low_sn"))
        self.assertEqual(self._status(), "InProcess")

    def test_planner_never_decides_the_request(self):
        for action, reason in (("start", None), ("complete", None), ("fail", "weather"), ("punt", "other")):
            self._post(self.staff, self._event(action, reason=reason))
            self.assertNotIn(self._status(), ("Successful", "Failed"), action)

    def test_reason_rule_on_the_model(self):
        block = ObservedBlock(night=self.night, kind="science", target_name="x", outcome="failed",
                              recorded_by=self.staff, source="admin", created_by=self.staff, modified_by=self.staff)
        with self.assertRaises(ValidationError):
            block.full_clean()

    def test_refusals(self):
        self.assertEqual(self._post(self.staff, self._event("dance")).status_code, 400)
        self.assertEqual(self._post(self.staff, self._event("complete", external_id="not-a-followup")).status_code, 400)
        # A follow-up the user cannot see.
        self.assertEqual(self._post(self.outsider, self._event("complete", followup=self.private_fu)).status_code, 403)
        # A user not authorised for the night's resource.
        self.resource.groups.add(Group.objects.create(name="planner-other-group"))
        self.assertEqual(self._post(self.outsider, self._event("complete")).status_code, 403)
        self.assertEqual(self._status(), "Requested")
        self.assertFalse(ObservedBlock.objects.exists())

    def test_links_the_plan_and_marks_it_executing(self):
        plan = NightPlan.objects.get(pk=self._save(self.staff).json()["id"])
        self._client(self.staff).post(reverse("api-night-plan-approve", kwargs={"plan_id": plan.id}))
        response = self._post(self.staff, self._event("complete"))
        block = ObservedBlock.objects.get(pk=response.json()["observed_block"])
        self.assertEqual(block.planned_block, plan.blocks.get(target_name="plannerbright"))
        self.assertEqual(block.mag, 17.2)
        plan.refresh_from_db()
        self.assertEqual(plan.state, "executing")

    def test_open_plan_is_preferred(self):
        v1 = NightPlan.objects.get(pk=self._save(self.staff).json()["id"])
        v2 = NightPlan.objects.get(pk=self._save(self.staff).json()["id"])
        response = self._post(self.staff, self._event("complete", plan=v2.id))
        block = ObservedBlock.objects.get(pk=response.json()["observed_block"])
        self.assertEqual(block.planned_block.plan, v2)
        self.assertNotEqual(block.planned_block.plan, v1)

    def test_flag_off(self):
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            self.assertEqual(self._post(self.staff, self._event("complete")).status_code, 404)

    def test_reasons_are_seeded_on_first_use(self):
        OutcomeReason.objects.all().delete()
        self._post(self.staff, self._event("punt", reason="too_faint"))
        self.assertEqual(set(OutcomeReason.objects.values_list("code", flat=True)), MIGRATION_CODES)


class TonightTests(_ObservationsFixture):
    def _frame_targets(self):
        url = reverse("night_planner_frame", kwargs={"night_id": self.night.pk})
        with mock.patch(BAZIN, return_value={}):
            html = self._client(self.staff).get(url).content.decode()
        config = json.loads(html.split(CONFIG_START, 1)[1].split(";</script>", 1)[0])
        return {t["external_id"]: t for t in config["initial"]["targets"]}

    def test_finished_targets_are_skipped_after_a_reload(self):
        self._post(self.staff, self._event("complete"))
        self._post(self.staff, self._event("punt", followup=self.private_fu, reason="ran_out_of_time"))
        targets = self._frame_targets()
        self.assertEqual(targets[str(self.open_fu.id)]["status"], "Observed")
        self.assertEqual(targets[str(self.private_fu.id)]["status"], "Punted")
        self.assertNotIn("status", targets[str(self.nophot_fu.id)])

    def test_other_nights_do_not_count(self):
        ObservedBlock.objects.create(
            night=self.half_night, followup=self.open_fu, kind="science", target_name="plannerbright",
            outcome="success", recorded_by=self.staff, source="admin", created_by=self.staff, modified_by=self.staff)
        self.assertNotIn("status", self._frame_targets()[str(self.open_fu.id)])

    def test_log_on_the_page_and_in_the_api(self):
        self._post(self.staff, self._event("fail", reason="weather", note="thick clouds"))
        self._post(self.staff, self._event("complete", followup=self.private_fu))
        url = reverse("night_planner", kwargs={"night_id": self.night.pk})
        with mock.patch(BAZIN, return_value={}):
            staff_page = self._client(self.staff).get(url).content.decode()
            outsider_page = self._client(self.outsider).get(url).content.decode()
        self.assertIn("thick clouds", staff_page)
        self.assertIn("plannerprivate", staff_page[staff_page.index("observing-log-table"):])
        self.assertNotIn("plannerprivate", outsider_page[outsider_page.index("observing-log-table"):])
        api = self._client(self.staff).get(reverse("api-night-observations", kwargs={"night_id": self.night.pk}))
        self.assertEqual([o["outcome"] for o in api.json()["observations"]][:2], ["success", "failed"])
