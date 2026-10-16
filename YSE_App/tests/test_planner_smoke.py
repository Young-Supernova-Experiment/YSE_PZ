"""AstroScheduler night planner: end-to-end smoke test (#35) and query counts (#675).

Synthetic data only. The solver is the installed ``astroscheduler`` package, run in-process
(no HTTP); the Bazin extrapolation is mocked so magnitudes are deterministic.
"""

import datetime
import json
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from YSE_App.models import (
    NightPlan,
    ObservedBlock,
    TransientFollowup,
    TransientFollowupRequest,
    TransientSpectrum,
)
from YSE_App.services import observations
from YSE_App.tests.deploy_checklist_helpers import iers_offline
from YSE_App.tests.fixtures_minimal import audit_fields, create_instrument_stack, create_minimal_transient
from YSE_App.tests.test_night_plans import CONFIG_START, _PlansFixture
from YSE_App.tests.test_night_planner import BAZIN

UTC = datetime.timezone.utc


def _config(html):
    return json.loads(html.split(CONFIG_START, 1)[1].split(";</script>", 1)[0])


@override_settings(ASTROSCHEDULER_ENABLED=True)
class PlannerSmokeTests(_PlansFixture):
    """One night from request to closed requests, through the same endpoints the pages use."""

    def test_night_end_to_end(self):
        client = self._client(self.staff)
        night_kwargs = {"night_id": self.night.pk}

        # 1. The observing-night page links the planner; the planner page and frame load.
        with iers_offline(), mock.patch(BAZIN, return_value={}), \
                mock.patch("YSE_App.view_utils.finder") as finder:
            finder.return_value.finderchart_noview.return_value = ([], "finder.png")
            night_page = client.get(reverse("observing_night", kwargs={
                "telescope": "Shane", "obs_date": "2026-10-15", "pi_name": "None"}))
        self.assertContains(night_page, reverse("night_planner", kwargs=night_kwargs))
        with mock.patch(BAZIN, return_value={}):
            self.assertEqual(client.get(reverse("night_planner", kwargs=night_kwargs)).status_code, 200)
            config = _config(client.get(reverse("night_planner_frame", kwargs=night_kwargs)).content.decode())
        self.assertEqual(config["site"]["id"], "lick")

        # 2. Recalculate: a real solve through the URL the embedded planner uses.
        payload = {"targets": config["initial"]["targets"], "auto_standards": True}
        with iers_offline():
            solved = client.post(config["apiBase"] + "/api/schedule", data=json.dumps(payload),
                                 content_type="application/json")
        self.assertEqual(solved.status_code, 200, solved.content[:300])
        result = solved.json()
        planned_ids = {b["external_id"] for b in result["blocks"] if b["kind"] == "science"}
        self.assertIn(str(self.open_fu.id), planned_ids)
        self.assertLess(result["yse_pz"]["solve_seconds"], 120)

        # 3. Save as v1, approve it, export it.
        saved = client.post(reverse("api-night-plans", kwargs=night_kwargs), content_type="application/json",
                            data=json.dumps({"result": result, "targets": config["initial"]["targets"]}))
        self.assertEqual(saved.status_code, 201, saved.content[:300])
        plan = NightPlan.objects.get(pk=saved.json()["id"])
        self.assertEqual(client.post(reverse("api-night-plan-approve", kwargs={"plan_id": plan.id})).status_code, 200)
        export = client.get(reverse("night_plan_target_list", kwargs={"plan_id": plan.id})).content.decode()
        self.assertIn("plannerbright", export)
        self.assertIn("comment = ", export)

        # 4. Re-open v1: the first solve starts from its blocks.
        with mock.patch(BAZIN, return_value={}):
            reopened = _config(client.get(reverse("night_planner_frame", kwargs=night_kwargs),
                                          {"plan": plan.id}).content.decode())
        self.assertTrue(reopened["initial"]["previous_schedule"])

        # 5. At the telescope: start and finish the bright target (data taken).
        event = {"target_name": "plannerbright", "external_id": str(self.open_fu.id), "plan": plan.id}
        url = reverse("api-night-observations", kwargs=night_kwargs)
        for action, when in (("start", "2026-10-16T05:00:00+00:00"), ("complete", "2026-10-16T05:30:00+00:00")):
            response = client.post(url, content_type="application/json",
                                   data=json.dumps(dict(event, action=action, time_utc=when)))
            self.assertEqual(response.status_code, 201, response.content[:300])
        block = ObservedBlock.objects.get(followup=self.open_fu)
        self.assertEqual(block.outcome, "observed")
        self.assertEqual(block.planned_block.plan, plan)
        plan.refresh_from_db()
        self.assertEqual(plan.state, "executing")
        self.assertEqual(self._status(self.open_fu), "InProcess")

        # 6. The spectrum is uploaded: observation and request are successful.
        obs_group, instrument, _ = create_instrument_stack(self.staff, obs_group_name="planner-smoke-spec")
        TransientSpectrum.objects.create(
            transient=self.open_sn, instrument=instrument, obs_group=obs_group, ra=self.open_sn.ra,
            dec=self.open_sn.dec, obs_date=datetime.datetime(2026, 10, 16, 5, 20, tzinfo=UTC),
            **audit_fields(self.staff))
        block.refresh_from_db()
        self.assertEqual(block.outcome, "success")
        self.assertEqual(self._status(self.open_fu), "Successful")

        # 7. After the window: requests that got no good data close as Failed.
        result = observations.expire_requests(datetime.datetime(2026, 10, 20, tzinfo=UTC), apply=True,
                                              since=datetime.datetime(2026, 10, 1, tzinfo=UTC), actor=self.staff)
        self.assertEqual(set(result["failed"]), {self.private_fu.id, self.nophot_fu.id})
        self.assertEqual(self._status(self.private_fu), "Failed")
        self.assertEqual(self._status(self.open_fu), "Successful")

        # 8. Tonight's log shows the success.
        log = client.get(url).json()["observations"]
        self.assertEqual([(o["target_name"], o["outcome"]) for o in log], [("plannerbright", "success")])

    def _status(self, followup):
        followup.refresh_from_db()
        return followup.status.name


@override_settings(ASTROSCHEDULER_ENABLED=True)
class PlannerQueryCountTests(_PlansFixture):
    """The planner's views make a fixed number of queries however much data there is (#675)."""

    def setUp(self):
        super().setUp()
        # Seed the outcome reasons up front, so the first measured request doesn't seed them.
        observations.ensure_reasons(self.staff)

    def _count(self, user, method, url, **kwargs):
        client = self._client(user)
        with mock.patch(BAZIN, return_value={}), CaptureQueriesContext(connection) as ctx:
            response = getattr(client, method)(url, **kwargs)
        self.assertLess(response.status_code, 400, response.content[:300])
        return len(ctx.captured_queries)

    def _more_followups(self, n):
        audit = audit_fields(self.staff)
        window = dict(valid_start=self.night.obs_date, valid_stop=self.night.obs_date + datetime.timedelta(days=1))
        made = []
        for i in range(n):
            sn = create_minimal_transient(self.staff, name="plannermore%d" % i, ra=10.0 + i, dec=15.0)
            fu = TransientFollowup.objects.create(
                transient=sn, status=self.status, classical_resource=self.resource, is_public=True,
                requested_by=self.staff, **window, **audit)
            TransientFollowupRequest.objects.create(followup=fu, requestor=self.staff, priority=3.0,
                                                    comment="synthetic request %d" % i, **audit)
            made.append(fu)
        return made

    def _plan_with(self, followups):
        payload = self._payload()
        for i, fu in enumerate(followups):
            payload["result"]["blocks"].append({
                "target_name": fu.transient.name, "external_id": str(fu.id), "kind": "science",
                "start_time": "2026-10-16T%02d:00:00+00:00" % (7 + i % 5), "duration_minutes": 20,
                "exposures": {"blue": {"exptime": 600, "num": 1}}})
        return self._client(self.staff).post(reverse("api-night-plans", kwargs={"night_id": self.night.pk}),
                                             data=json.dumps(payload), content_type="application/json").json()["id"]

    def test_plans_list(self):
        url = reverse("api-night-plans", kwargs={"night_id": self.night.pk})
        self._save(self.staff)
        for user in (self.staff, self.outsider):
            one = self._count(user, "get", url)
            for _ in range(3):
                self._save(self.staff)
            self.assertEqual(self._count(user, "get", url), one, user.username)

    def test_export(self):
        few = self._plan_with([])
        many = self._plan_with(self._more_followups(5))
        for user in (self.staff, self.outsider):
            base = self._count(user, "get", reverse("night_plan_target_list", kwargs={"plan_id": few}))
            more = self._count(user, "get", reverse("night_plan_target_list", kwargs={"plan_id": many}))
            self.assertEqual(more, base, user.username)

    def test_observations_log(self):
        url = reverse("api-night-observations", kwargs={"night_id": self.night.pk})

        def record(fu):
            self._client(self.staff).post(url, content_type="application/json", data=json.dumps({
                "action": "fail", "external_id": str(fu.id), "target_name": fu.transient.name,
                "reasons": ["weather", "low_sn"], "time_utc": "2026-10-16T06:00:00+00:00"}))

        record(self.open_fu)
        base = self._count(self.outsider, "get", url)
        for fu in self._more_followups(4):
            record(fu)
        self.assertEqual(self._count(self.outsider, "get", url), base)

    def test_planner_page_and_frame(self):
        page = reverse("night_planner", kwargs={"night_id": self.night.pk})
        frame = reverse("night_planner_frame", kwargs={"night_id": self.night.pk})
        self._save(self.staff)
        base_page = self._count(self.staff, "get", page)
        base_frame = self._count(self.staff, "get", frame)
        self._more_followups(5)
        self._save(self.staff)
        self.assertEqual(self._count(self.staff, "get", page), base_page)
        self.assertEqual(self._count(self.staff, "get", frame), base_frame)
