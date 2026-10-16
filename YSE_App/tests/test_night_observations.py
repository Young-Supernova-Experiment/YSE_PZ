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
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

import datetime
import io

from YSE_App.models import NightPlan, ObservedBlock, OutcomeReason, TransientFollowup
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
        # Completed means data taken; the observation is judged later (#672).
        self.assertEqual((block.outcome, block.reason, block.source), ("observed", None, "astroscheduler"))
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
        block.outcome = "observed"
        block.full_clean()  # data taken needs no reason

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
        self.assertEqual([o["outcome"] for o in api.json()["observations"]][:2], ["observed", "failed"])
        self.assertIn("Data taken", staff_page)


class AssessTests(_ObservationsFixture):
    def _taken(self, followup=None):
        response = self._post(self.staff, self._event("complete", followup=followup))
        return response.json()["observed_block"]

    def _assess(self, user, block_id, **body):
        return self._client(user).post(reverse("api-observed-block-assess", kwargs={"block_id": block_id}),
                                       data=json.dumps(body), content_type="application/json")

    def test_good_observation_makes_the_request_successful(self):
        block_id = self._taken()
        self.assertEqual(self._status(), "InProcess")
        response = self._assess(self.staff, block_id, outcome="success", note="good S/N")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["followup_status"], "Successful")
        block = ObservedBlock.objects.get(pk=block_id)
        self.assertEqual(block.outcome, "success")
        self.assertIn("good S/N", block.comments)

    def test_failed_or_punted_judgement_needs_a_reason_and_leaves_the_request(self):
        block_id = self._taken()
        self.assertEqual(self._assess(self.staff, block_id, outcome="failed").status_code, 400)
        self.assertEqual(self._assess(self.staff, block_id, outcome="failed", reason="low_sn").status_code, 200)
        self.assertEqual(ObservedBlock.objects.get(pk=block_id).outcome, "failed")
        self.assertEqual(self._status(), "InProcess")
        self.assertEqual(self._assess(self.staff, block_id, outcome="punted", reason="other").status_code, 200)
        self.assertEqual(self._status(), "InProcess")

    def test_refusals(self):
        block_id = self._taken(followup=self.private_fu)
        self.assertEqual(self._assess(self.staff, block_id, outcome="observed").status_code, 400)
        self.assertEqual(self._assess(self.outsider, block_id, outcome="success").status_code, 403)
        self.assertEqual(self._status(self.private_fu), "InProcess")
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            self.assertEqual(self._assess(self.staff, block_id, outcome="success").status_code, 404)

    def test_page_offers_judgement_for_data_taken_rows(self):
        self._taken()
        with mock.patch(BAZIN, return_value={}):
            page = self._client(self.staff).get(reverse("night_planner", kwargs={"night_id": self.night.pk}))
        self.assertContains(page, 'class="btn btn-success btn-xs assess-btn"')


class ExpiryTests(_ObservationsFixture):
    NOW = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)
    SINCE = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)

    def _block(self, followup, outcome, reason=None):
        ObservedBlock.objects.create(
            night=self.night, followup=followup, kind="science", target_name=followup.transient.name,
            outcome=outcome, reason=reason, recorded_by=self.staff, source="admin",
            created_by=self.staff, modified_by=self.staff)

    def _expire(self, apply=True, since=None):
        return observations.expire_requests(self.NOW, apply=apply, since=since or self.SINCE, actor=self.staff)

    def test_outcomes_decide_the_request(self):
        observations.ensure_reasons(self.staff)
        weather = OutcomeReason.objects.get(code="weather")
        self._block(self.private_fu, "failed", weather)  # only a failed observation
        self._block(self.nophot_fu, "observed")  # data still to judge
        # open_fu: no observation at all
        result = self._expire()
        self.assertEqual(set(result["failed"]), {self.open_fu.id, self.private_fu.id})
        self.assertEqual(result["pending"], [self.nophot_fu.id])
        self.assertEqual(self._status(self.open_fu), "Failed")
        self.assertEqual(self._status(self.private_fu), "Failed")
        self.assertEqual(self._status(self.nophot_fu), "Requested")

    def test_a_good_observation_closes_as_successful(self):
        self._block(self.open_fu, "success")
        self.assertIn(self.open_fu.id, self._expire()["successful"])
        self.assertEqual(self._status(self.open_fu), "Successful")

    def test_dry_run_and_start_date(self):
        self.assertTrue(self._expire(apply=False)["failed"])
        self.assertEqual(self._status(), "Requested")
        later = datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc)  # after these windows ended
        self.assertEqual(self._expire(since=later)["failed"], [])
        self.assertEqual(self._status(), "Requested")

    def test_only_open_classical_requests_and_ended_windows(self):
        too = TransientFollowup.objects.create(
            transient=self.open_sn, status=self.status, valid_start=self.SINCE, valid_stop=self.SINCE + datetime.timedelta(days=2),
            requested_by=self.staff, created_by=self.staff, modified_by=self.staff)  # no classical resource
        running = TransientFollowup.objects.create(
            transient=self.open_sn, status=self.status, classical_resource=self.resource,
            valid_start=self.SINCE, valid_stop=self.NOW + datetime.timedelta(days=3),
            requested_by=self.staff, created_by=self.staff, modified_by=self.staff)  # window still open
        failed = self._expire()["failed"]
        self.assertNotIn(too.id, failed)
        self.assertNotIn(running.id, failed)
        self.assertEqual(self._status(too), "Requested")

    @override_settings(ASTROSCHEDULER_EXPIRE_AFTER="")
    def test_cron_and_command_are_off_without_the_setting(self):
        from YSE_App.data_ingest.Job_Queue import ExpireFollowupRequests

        self.assertEqual(ExpireFollowupRequests().do(), "disabled")
        buf = io.StringIO()
        call_command("expire_followup_requests", stdout=buf)
        self.assertIn("Off", buf.getvalue())
        self.assertEqual(self._status(), "Requested")

    @override_settings(ASTROSCHEDULER_EXPIRE_AFTER="2026-10-01")
    def test_command_reports_then_applies(self):
        buf = io.StringIO()
        call_command("expire_followup_requests", stdout=buf)
        self.assertIn("Would change", buf.getvalue())
        self.assertEqual(self._status(), "Requested")
        buf = io.StringIO()
        call_command("expire_followup_requests", "--apply", stdout=buf)
        self.assertIn("Changed", buf.getvalue())
        self.assertEqual(self._status(), "Failed")


class DataUploadTests(_ObservationsFixture):
    """New data prove the observation (#674)."""

    def _spectrum(self, transient, obs_date, followup=None):
        from YSE_App.models import TransientSpectrum
        from YSE_App.tests.fixtures_minimal import audit_fields, create_instrument_stack

        obs_group, instrument, _ = create_instrument_stack(self.staff, obs_group_name="planner-spec")
        return TransientSpectrum.objects.create(
            transient=transient, instrument=instrument, obs_group=obs_group, ra=transient.ra, dec=transient.dec,
            obs_date=obs_date, followup=followup, **audit_fields(self.staff))

    def _taken(self, followup=None):
        return self._post(self.staff, self._event("complete", followup=followup)).json()["observed_block"]

    def test_spectrum_from_that_night_makes_both_successful(self):
        block_id = self._taken()
        self._spectrum(self.open_sn, datetime.datetime(2026, 10, 16, 6, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(ObservedBlock.objects.get(pk=block_id).outcome, "success")
        self.assertEqual(self._status(), "Successful")

    def test_spectrum_naming_the_request(self):
        block_id = self._taken()
        self._spectrum(self.open_sn, datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc), followup=self.open_fu)
        self.assertEqual(ObservedBlock.objects.get(pk=block_id).outcome, "success")
        self.assertEqual(self._status(), "Successful")

    def test_request_named_by_the_upload_without_an_observation(self):
        self._spectrum(self.private_sn, datetime.datetime(2026, 10, 16, 6, tzinfo=datetime.timezone.utc),
                       followup=self.private_fu)
        self.assertEqual(self._status(self.private_fu), "Successful")

    def test_unrelated_data_change_nothing(self):
        block_id = self._taken()
        # Another night's spectrum of the same transient, and another transient's spectrum.
        self._spectrum(self.open_sn, datetime.datetime(2026, 10, 25, 6, tzinfo=datetime.timezone.utc))
        self._spectrum(self.private_sn, datetime.datetime(2026, 10, 16, 6, tzinfo=datetime.timezone.utc))
        self.assertEqual(ObservedBlock.objects.get(pk=block_id).outcome, "observed")
        self.assertEqual(self._status(), "InProcess")
        self.assertEqual(self._status(self.private_fu), "Requested")

    def test_off_when_the_planner_is_off(self):
        block_id = self._taken()
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            self._spectrum(self.open_sn, datetime.datetime(2026, 10, 16, 6, tzinfo=datetime.timezone.utc),
                           followup=self.open_fu)
        self.assertEqual(ObservedBlock.objects.get(pk=block_id).outcome, "observed")
        self.assertEqual(self._status(), "InProcess")

    def test_photometry_naming_the_request(self):
        from YSE_App.models import TransientPhotometry
        from YSE_App.tests.fixtures_minimal import audit_fields, create_instrument_stack

        block_id = self._taken()
        obs_group, instrument, _ = create_instrument_stack(self.staff, obs_group_name="planner-phot")
        TransientPhotometry.objects.create(transient=self.open_sn, instrument=instrument, obs_group=obs_group,
                                           followup=self.open_fu, **audit_fields(self.staff))
        self.assertEqual(ObservedBlock.objects.get(pk=block_id).outcome, "success")
        self.assertEqual(self._status(), "Successful")

    def test_upload_never_fails_on_a_hook_error(self):
        with mock.patch("YSE_App.services.observations.data_uploaded", side_effect=RuntimeError("boom")):
            spectrum = self._spectrum(self.open_sn, datetime.datetime(2026, 10, 16, 6, tzinfo=datetime.timezone.utc))
        self.assertIsNotNone(spectrum.pk)
