"""AstroScheduler night planner page (#659), nights list (#658) and target links.

Synthetic data only; the Bazin extrapolation is mocked and the solver runs in-process.
"""

import datetime
import json
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django.urls import reverse

from YSE_App.models import ClassicalObservingDate, ClassicalResource, TransientFollowupRequest
from YSE_App.services import night_planner
from YSE_App.tests.deploy_checklist_helpers import ensure_classical_night_type, iers_offline, utc_days_from_now
from YSE_App.tests.fixtures_minimal import audit_fields
from YSE_App.tests.test_night_planner import BAZIN, _PlannerFixture

CONFIG_START = "window.ASTROSCHEDULER_CONFIG = "


def _frame_config(html):
    assert CONFIG_START in html, "no planner config in the frame"
    return json.loads(html.split(CONFIG_START, 1)[1].split(";</script>", 1)[0])


def _no_finder():
    finder = mock.MagicMock()
    finder.return_value.finderchart_noview.return_value = ([], "finder.png")
    return mock.patch("YSE_App.view_utils.finder", finder)


@override_settings(ASTROSCHEDULER_ENABLED=True)
class PlannerPageTests(_PlannerFixture):
    def _page(self, user, name, **kwargs):
        with mock.patch(BAZIN, return_value={}):
            return self._client(user).get(reverse(name, kwargs={"night_id": self.night.pk}), **kwargs)

    def test_off_means_404(self):
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            self.assertEqual(self._page(self.staff, "night_planner").status_code, 404)
            self.assertEqual(self._page(self.staff, "night_planner_frame").status_code, 404)

    def test_login_required(self):
        for name in ("night_planner", "night_planner_frame"):
            response = Client().get(reverse(name, kwargs={"night_id": self.night.pk}))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn("login", response["Location"])

    def test_page_in_the_shell_embeds_the_frame(self):
        response = self._page(self.staff, "night_planner")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('src="%s"' % reverse("night_planner_frame", kwargs={"night_id": self.night.pk}), body)
        self.assertIn("Shane", body)
        self.assertIn("no photometry", body)  # the feed's warnings are shown above the planner
        self.assertTemplateUsed(response, "YSE_App/base.html")

    def test_frame_config(self):
        response = self._page(self.staff, "night_planner_frame")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        config = _frame_config(response.content.decode())
        self.assertEqual(config["mode"], "embedded")
        self.assertEqual(config["apiBase"], reverse("night_planner", kwargs={"night_id": self.night.pk}).rstrip("/"))
        self.assertTrue(config["headers"]["X-CSRFToken"])
        self.assertTrue(config["staticBase"].rstrip("/").endswith("astroscheduler"))
        self.assertEqual(config["site"]["id"], "lick")
        self.assertEqual(config["initial"]["date"], "2026-10-15")
        self.assertEqual(config["initial"]["instrument"], "kast")
        by_id = {t["external_id"]: t for t in config["initial"]["targets"]}
        self.assertIn(str(self.open_fu.id), by_id)
        self.assertIn("/transient_detail/", by_id[str(self.open_fu.id)]["url"])

    def test_frame_escapes_target_text(self):
        TransientFollowupRequest.objects.create(
            followup=self.open_fu, requestor=self.staff, priority=3.0,
            comment="</script><script>alert(1)</script>", **audit_fields(self.staff),
        )
        html = self._page(self.staff, "night_planner_frame").content.decode()
        self.assertNotIn("<script>alert(1)", html)
        comments = [t["comment"] for t in _frame_config(html)["initial"]["targets"]]
        self.assertTrue(any("</script><script>alert(1)</script>" in c for c in comments))

    def test_embedded_solve_with_csrf(self):
        """The planner's POST to <apiBase>/api/schedule works with the frame's CSRF token,
        and is refused without it."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        with mock.patch(BAZIN, return_value={}):
            html = client.get(reverse("night_planner_frame", kwargs={"night_id": self.night.pk})).content.decode()
        config = _frame_config(html)
        url = config["apiBase"] + "/api/schedule"
        self.assertEqual(url, reverse("night-planner-solve", kwargs={"night_id": self.night.pk}))
        payload = json.dumps({"targets": config["initial"]["targets"], "auto_standards": False})

        refused = client.post(url, data=payload, content_type="application/json")
        self.assertEqual(refused.status_code, 403)
        with iers_offline():
            solved = client.post(url, data=payload, content_type="application/json",
                                 HTTP_X_CSRFTOKEN=config["headers"]["X-CSRFToken"])
        self.assertEqual(solved.status_code, 200, solved.content[:300])
        self.assertIn(str(self.open_fu.id), {b["external_id"] for b in solved.json()["blocks"]})

    def test_targets_api_links_transient_pages(self):
        data = self._targets(self.staff)
        target = next(t for t in data["targets"] if t["name"] == "plannerbright")
        self.assertTrue(target["url"].startswith("http"))
        self.assertIn("/transient_detail/", target["url"])

    def _observing_night(self):
        kwargs = {"telescope": "Shane", "obs_date": "2026-10-15", "pi_name": "None"}
        with iers_offline(), _no_finder(), mock.patch(BAZIN, return_value={}):
            response = self._client(self.staff).get(reverse("observing_night", kwargs=kwargs))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_observing_night_page_links_the_planner_only_when_on(self):
        link = reverse("night_planner", kwargs={"night_id": self.night.pk})
        self.assertIn(link, self._observing_night())
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            body = self._observing_night()
        self.assertNotIn(link, body)
        self.assertNotIn("plan-night-btn", body)


@override_settings(ASTROSCHEDULER_ENABLED=True)
class PlannerNightsTests(_PlannerFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        audit = audit_fields(cls.staff)
        cls.restricted = ClassicalResource.objects.create(
            telescope=cls.telescope,
            begin_date_valid=utc_days_from_now(-30, hour=0),
            end_date_valid=utc_days_from_now(90, hour=0),
            **audit,
        )
        cls.restricted.groups.add(Group.objects.get(name="planner-group"))
        night_type = ensure_classical_night_type(cls.staff, "Full")
        cls.soon = ClassicalObservingDate.objects.create(
            resource=cls.restricted, night_type=night_type, obs_date=utc_days_from_now(3, hour=0), **audit)
        cls.too_far = ClassicalObservingDate.objects.create(
            resource=cls.restricted, night_type=night_type, obs_date=utc_days_from_now(200, hour=0), **audit)

    def _nights(self, user, **params):
        response = self._client(user).get(reverse("api-planner-nights"), params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()["nights"]

    def test_member_sees_restricted_night_in_window(self):
        nights = {n["id"]: n for n in self._nights(self.member)}
        self.assertIn(self.soon.id, nights)
        self.assertNotIn(self.too_far.id, nights)
        night = nights[self.soon.id]
        self.assertEqual((night["telescope"], night["site"], night["instrument"]), ("Shane", "lick", "kast"))
        self.assertTrue(night["planner_targets_url"].endswith(
            reverse("api-planner-targets", kwargs={"night_id": self.soon.id})))
        self.assertEqual(set(night), {"id", "date", "telescope", "night_type", "site", "instrument",
                                      "planner_targets_url", "planner_url"})

    def test_outsider_does_not_see_restricted_night(self):
        self.assertNotIn(self.soon.id, {n["id"] for n in self._nights(self.outsider)})

    def test_window_parameters_are_capped(self):
        ids = {n["id"] for n in self._nights(self.member, days_ahead=100000)}
        self.assertIn(self.soon.id, ids)
        self.assertIn(self.too_far.id, ids)  # 200 days ahead is inside the 366-day cap

    def test_service_window(self):
        now = datetime.datetime(2026, 10, 14, 12, tzinfo=datetime.timezone.utc)
        ids = {n["id"] for n in night_planner.planner_nights(self.staff, days_back=0, days_ahead=1, now=now)}
        self.assertIn(self.night.id, ids)  # 2026-10-15
        self.assertNotIn(self.half_night.id, ids)  # 2026-10-16

    def test_flag_and_auth(self):
        self.assertIn(Client().get(reverse("api-planner-nights")).status_code, (401, 403))
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            response = self._client(self.staff).get(reverse("api-planner-nights"))
        self.assertEqual(response.status_code, 404)
