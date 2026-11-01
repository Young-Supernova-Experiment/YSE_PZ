"""Saved night plans: tables (#661), saving (#662), approval (#663), re-opening (#664) and
the target-list export (#77). Synthetic data only."""

import json
import re
from unittest import mock

from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import TestCase, override_settings
from django.urls import reverse

from YSE_App.models import NightPlan, PlannedBlock
from YSE_App.services import night_plans
from YSE_App.tests.deploy_checklist_helpers import create_principal_investigator
from YSE_App.tests.test_night_planner import BAZIN, _PlannerFixture

CONFIG_START = "window.ASTROSCHEDULER_CONFIG = "


class NightPlanMigrationTests(TestCase):
    def test_migrations_match_the_models(self):
        """makemigrations would create nothing for NightPlan or PlannedBlock."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(
            graph=loader.graph)
        mine = [
            op for migration in changes.get("YSE_App", []) for op in migration.operations
            if (getattr(op, "model_name", None) or getattr(op, "name", "")).lower() in ("nightplan", "plannedblock")
        ]
        self.assertEqual(mine, [], [op.describe() for op in mine])


@override_settings(ASTROSCHEDULER_ENABLED=True)
class _PlansFixture(_PlannerFixture):
    def _payload(self, parent=None, notes=None):
        sci = dict(kind="science", duration_minutes=30, priority=1.0, instrument="kast", exposure_source="lookup",
                   airmass_start=1.1, airmass_end=1.05, airmass_median=1.07, overhead={"slew_acq_min": 7},
                   exposures={"blue": {"exptime": 1237, "num": 1}, "red": {"exptime": 600, "num": 2}},
                   comment="Slew: 7m. Blue: 1x1237s, Red: 2x600s.")
        blocks = [
            dict(sci, target_name="plannerbright", external_id=str(self.open_fu.id),
                 start_time="2026-10-16T05:00:00+00:00", end_time="2026-10-16T05:30:00+00:00"),
            dict(sci, target_name="plannerprivate", external_id=str(self.private_fu.id), priority=2.0,
                 start_time="2026-10-16T05:31:00+00:00", end_time="2026-10-16T06:01:00+00:00"),
            {"target_name": "Feige 34", "external_id": None, "kind": "standard", "duration_minutes": 10,
             "priority": 0.0, "start_time": "2026-10-16T03:00:00+00:00", "end_time": "2026-10-16T03:10:00+00:00",
             "exposures": {"blue": {"exptime": 180, "num": 1}, "red": {"exptime": 100, "num": 1}}},
        ]
        targets = [
            {"name": "plannerbright", "external_id": str(self.open_fu.id), "ra": "01:00:00.00", "dec": "+20:00:00.0",
             "magnitude": 17.2, "mag_band": "r", "priority": 1.0, "warnings": ["synthetic warning"],
             "manual_start_time": "2026-10-16T05:00:00+00:00", "lock_type": "start", "not_a_field": "dropped"},
            {"name": "plannerprivate", "external_id": str(self.private_fu.id), "ra": "03:00:00.00",
             "dec": "+30:00:00.0", "magnitude": 18.9, "priority": 2.0},
        ]
        return {"result": {"blocks": blocks, "solver_version": "1.2.0"}, "targets": targets,
                "parent": parent, "notes": notes}

    def _save(self, user, **kwargs):
        response = self._client(user).post(reverse("api-night-plans", kwargs={"night_id": self.night.pk}),
                                           data=json.dumps(self._payload(**kwargs)), content_type="application/json")
        return response


class SavePlanTests(_PlansFixture):
    def test_save_creates_versions_with_blocks(self):
        first = self._save(self.staff, notes="first pass")
        self.assertEqual(first.status_code, 201, first.content[:300])
        plan = NightPlan.objects.get(pk=first.json()["id"])
        self.assertEqual((plan.version, plan.state, plan.author, plan.notes), (1, "draft", self.staff, "first pass"))
        self.assertEqual(plan.solver, "AstroScheduler 1.2.0")
        self.assertRegex(plan.solver_inputs["input_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(plan.solver_inputs["observatory"]["id"], "lick")
        self.assertNotIn("not_a_field", plan.solver_inputs["targets"][0])

        blocks = list(plan.blocks.order_by("order"))
        self.assertEqual([b.target_name for b in blocks], ["Feige 34", "plannerbright", "plannerprivate"])
        std, sci = blocks[0], blocks[1]
        self.assertEqual(std.kind, "standard")
        self.assertIsNone(std.followup)
        self.assertIsNotNone(std.ra)  # from AstroScheduler's standards list
        self.assertEqual((sci.followup, sci.transient), (self.open_fu, self.open_sn))
        self.assertEqual((sci.ra, sci.dec), (15.0, 20.0))
        self.assertEqual((sci.mag, sci.mag_band, sci.priority_at_plan), (17.2, "r", 1.0))
        self.assertEqual(sci.requested_sequence, [{"channel": "blue", "n": 1, "exptime_s": 1237.0},
                                                  {"channel": "red", "n": 2, "exptime_s": 600.0}])
        self.assertEqual(sci.time_with_overhead_s, 1800)
        self.assertEqual(sci.warnings, "synthetic warning")
        self.assertEqual(sci.settings["airmass_median"], 1.07)
        self.assertEqual(sci.start_ut.isoformat(), "2026-10-16T05:00:00+00:00")

        second = self._save(self.staff, parent=plan.id)
        again = NightPlan.objects.get(pk=second.json()["id"])
        self.assertEqual((again.version, again.parent), (2, plan))

    def test_links_only_followups_the_saver_can_see(self):
        response = self._save(self.outsider)
        self.assertEqual(response.status_code, 201)
        blocks = {b.target_name: b for b in NightPlan.objects.get(pk=response.json()["id"]).blocks.all()}
        self.assertEqual(blocks["plannerbright"].followup, self.open_fu)
        self.assertIsNone(blocks["plannerprivate"].followup)
        self.assertIsNone(blocks["plannerprivate"].transient)

    def test_bad_requests(self):
        url = reverse("api-night-plans", kwargs={"night_id": self.night.pk})
        other_night_plan = NightPlan.objects.create(
            night=self.half_night, version=1, author=self.staff, created_by=self.staff, modified_by=self.staff)
        too_many = self._payload()
        too_many["result"]["blocks"] = too_many["result"]["blocks"] * 200
        bad_parent = self._payload(parent=other_night_plan.id)
        for body in ({}, {"result": {"blocks": []}}, too_many, bad_parent, dict(self._payload(), notes=7)):
            response = self._client(self.staff).post(url, data=json.dumps(body), content_type="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertIn("detail", response.json())
        self.assertEqual(NightPlan.objects.filter(night=self.night).count(), 0)

    def test_list_counts_only_visible_blocks(self):
        self._save(self.staff)
        url = reverse("api-night-plans", kwargs={"night_id": self.night.pk})
        staff_view = self._client(self.staff).get(url).json()
        outsider_view = self._client(self.outsider).get(url).json()
        self.assertEqual(staff_view["plans"][0]["blocks"], 3)
        self.assertEqual(outsider_view["plans"][0]["blocks"], 2)
        self.assertTrue(staff_view["can_approve"])
        self.assertFalse(outsider_view["can_approve"])

    def test_flag_off(self):
        plan_id = self._save(self.staff).json()["id"]
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            client = self._client(self.staff)
            self.assertEqual(client.get(reverse("api-night-plans", kwargs={"night_id": self.night.pk})).status_code, 404)
            self.assertEqual(client.post(reverse("api-night-plan-approve", kwargs={"plan_id": plan_id})).status_code, 404)
            self.assertEqual(client.get(reverse("night_plan_target_list", kwargs={"plan_id": plan_id})).status_code, 404)


class ApprovePlanTests(_PlansFixture):
    def _approve(self, user, plan_id):
        return self._client(user).post(reverse("api-night-plan-approve", kwargs={"plan_id": plan_id}))

    def test_pi_and_staff_approve_and_the_old_approval_is_superseded(self):
        pi = create_principal_investigator(self.staff, name="planner-pi")
        pi.user = self.member
        pi.save()
        self.resource.principal_investigator = pi
        self.resource.save()

        v1 = self._save(self.staff).json()["id"]
        v2 = self._save(self.staff).json()["id"]
        self.assertEqual(self._approve(self.outsider, v1).status_code, 403)

        response = self._approve(self.member, v1)
        self.assertEqual(response.status_code, 200, response.content[:300])
        first = NightPlan.objects.get(pk=v1)
        self.assertEqual((first.state, first.approved_by), ("approved", self.member))

        self.assertEqual(self._approve(self.staff, v2).status_code, 200)
        self.assertEqual(NightPlan.objects.get(pk=v1).state, "superseded")
        self.assertEqual(NightPlan.objects.get(pk=v2).state, "approved")
        self.assertEqual(NightPlan.objects.filter(night=self.night, state="approved").count(), 1)


class ExportTests(_PlansFixture):
    def _export(self, user, plan_id, **params):
        response = self._client(user).get(reverse("night_plan_target_list", kwargs={"plan_id": plan_id}), params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("plan-v1.txt", response["Content-Disposition"])
        return response.content.decode()

    def test_star_list_in_planned_order(self):
        plan_id = self._save(self.staff).json()["id"]
        text = self._export(self.staff, plan_id)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("!Data {name %20}"))
        names = [line[:20].strip() for line in lines[1:]]
        self.assertEqual(names, ["Feige 34", "plannerbright", "plannerbright_os", "plannerprivate"])
        bright = lines[2]
        self.assertIn("01 00 00.00 +20 00 00.0 2000 mag = 17.20", bright)
        self.assertIn("comment = 2. UT 05:00-05:30 B 1x1237 R 2x600", bright)
        self.assertIn("needs a classification spectrum", bright)
        self.assertIn("raoffset=-36.00 decoffset=36.00", lines[3])
        self.assertIn("standard", lines[1])

    def test_export_hides_blocks_the_viewer_cannot_see(self):
        plan_id = self._save(self.staff).json()["id"]
        text = self._export(self.outsider, plan_id)
        self.assertIn("plannerbright", text)
        self.assertNotIn("plannerprivate", text)

    def test_finder_offsets_on_request(self):
        plan_id = self._save(self.staff).json()["id"]
        finder = mock.MagicMock()
        finder.return_value.finderchart_noview.return_value = (
            [{"id": "plannerprivate_S1", "ra": "03:00:10.0", "dec": "+30:01:00.0", "mag": 15.5,
              "ra_off": 12.0, "dec_off": -60.0}], "finder.png")
        with mock.patch("YSE_App.view_utils.finder", finder):
            text = self._export(self.staff, plan_id, finders="1")
        self.assertIn("plannerprivate_S1", text)
        self.assertIn("raoffset=12.00 decoffset=-60.00", text)
        finder.return_value.finderchart_noview.assert_called_once_with(self.private_sn.id)


class ReopenTests(_PlansFixture):
    def _frame_config(self, plan_id):
        url = reverse("night_planner_frame", kwargs={"night_id": self.night.pk})
        with mock.patch(BAZIN, return_value={}):
            response = self._client(self.staff).get(url, {"plan": plan_id})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        return json.loads(html.split(CONFIG_START, 1)[1].split(";</script>", 1)[0])

    def test_reopened_plan_seeds_the_solve_and_restores_edits(self):
        plan_id = self._save(self.staff).json()["id"]
        config = self._frame_config(plan_id)
        seed = config["initial"]["previous_schedule"]
        self.assertIn({"target_name": "plannerbright", "external_id": str(self.open_fu.id),
                       "start_time": "2026-10-16T05:00:00+00:00"}, seed)
        self.assertTrue(any(s["target_name"] == "Feige 34" and s["external_id"] is None for s in seed))
        bright = next(t for t in config["initial"]["targets"] if t["external_id"] == str(self.open_fu.id))
        self.assertEqual(bright["manual_start_time"], "2026-10-16T05:00:00+00:00")
        self.assertEqual(bright["lock_type"], "start")

    def test_plan_of_another_night_is_404(self):
        other = NightPlan.objects.create(
            night=self.half_night, version=1, author=self.staff, created_by=self.staff, modified_by=self.staff)
        for name in ("night_planner", "night_planner_frame"):
            with mock.patch(BAZIN, return_value={}):
                response = self._client(self.staff).get(
                    reverse(name, kwargs={"night_id": self.night.pk}), {"plan": other.id})
            self.assertEqual(response.status_code, 404, name)

    def test_page_lists_plans_and_offers_approval_to_those_allowed(self):
        plan_id = self._save(self.staff).json()["id"]
        url = reverse("night_planner", kwargs={"night_id": self.night.pk})
        with mock.patch(BAZIN, return_value={}):
            staff_page = self._client(self.staff).get(url, {"plan": plan_id}).content.decode()
            outsider_page = self._client(self.outsider).get(url).content.decode()
        self.assertIn('id="save-plan-btn"', staff_page)
        self.assertIn("Opened v1", staff_page)
        self.assertIn("frame/?plan=%d" % plan_id, staff_page)
        self.assertIn(reverse("night_plan_target_list", kwargs={"plan_id": plan_id}), staff_page)
        self.assertEqual(len(re.findall(r'class="btn btn-success btn-xs approve-plan-btn"', staff_page)), 1)
        self.assertNotIn("approve-plan-btn\" data-plan", outsider_page)


class PlannedBlockModelTests(_PlansFixture):
    def test_blocks_survive_a_deleted_followup_and_go_with_their_plan(self):
        plan = NightPlan.objects.get(pk=self._save(self.staff).json()["id"])
        self.open_fu.delete()
        block = plan.blocks.get(target_name="plannerbright")
        self.assertIsNone(block.followup)
        plan.delete()
        self.assertFalse(PlannedBlock.objects.filter(pk=block.pk).exists())

    def test_version_is_unique_per_night(self):
        from django.db import IntegrityError, transaction

        NightPlan.objects.create(night=self.night, version=1, author=self.staff,
                                 created_by=self.staff, modified_by=self.staff)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NightPlan.objects.create(night=self.night, version=1, author=self.staff,
                                     created_by=self.staff, modified_by=self.staff)

    def test_save_numbers_versions_after_existing_ones(self):
        NightPlan.objects.create(night=self.night, version=4, author=self.staff,
                                 created_by=self.staff, modified_by=self.staff)
        self.assertEqual(night_plans.save_plan(self.night, self.staff, self._payload()).version, 5)
