"""AstroScheduler night planner: flag (#654), targets API (#655) and server-side solve (#656).

Synthetic data only. The solver is the installed ``astroscheduler`` package (no HTTP);
the Bazin extrapolation is mocked so magnitudes are deterministic.
"""

import datetime
from unittest import mock

from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.authtoken.models import Token

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import (
    ClassicalObservingDate,
    ClassicalResource,
    FollowupStatus,
    Observatory,
    Telescope,
    TransientFollowup,
    TransientFollowupRequest,
)
from YSE_App.models.phot_stat_models import TransientPhotStat
from YSE_App.services import night_planner
from YSE_App.services.bazin import local_midnight_mjd
from YSE_App.tests.deploy_checklist_helpers import ensure_classical_night_type, iers_offline
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

UTC = datetime.timezone.utc
NIGHT = datetime.datetime(2026, 10, 15, tzinfo=UTC)
BAZIN = "YSE_App.services.bazin.extrapolated_mags"


@override_settings(ASTROSCHEDULER_ENABLED=True)
class _PlannerFixture(TestCase):
    """A Lick-like telescope with a full night and a first-half night. On the full night:
    a public follow-up, a group-restricted one, and one with no photometry."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("planner_staff", is_staff=True)
        cls.member = create_test_user("planner_member", is_staff=False)
        cls.outsider = create_test_user("planner_outsider", is_staff=False)
        cls.group = Group.objects.create(name="planner-group")
        public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        cls.member.groups.add(cls.group, public)
        cls.outsider.groups.add(public)
        audit = audit_fields(cls.staff)

        observatory = Observatory.objects.create(
            name="Synthetic Mt Hamilton", utc_offset=-8, tz_name="America/Los_Angeles", **audit
        )
        cls.telescope = Telescope.objects.create(
            name="Shane", observatory=observatory,
            latitude=37.3414, longitude=-121.6429, elevation=1283.0, **audit,
        )
        cls.resource = ClassicalResource.objects.create(
            telescope=cls.telescope,
            begin_date_valid=NIGHT - datetime.timedelta(days=30),
            end_date_valid=NIGHT + datetime.timedelta(days=30),
            **audit,
        )
        cls.night = ClassicalObservingDate.objects.create(
            resource=cls.resource, night_type=ensure_classical_night_type(cls.staff, "Full"),
            obs_date=NIGHT, **audit,
        )
        cls.half_night = ClassicalObservingDate.objects.create(
            resource=cls.resource, night_type=ensure_classical_night_type(cls.staff, "FirstHalf"),
            obs_date=NIGHT + datetime.timedelta(days=1), **audit,
        )
        cls.status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)

        # RA in degrees, as YSE_PZ stores it: 15 deg = 01h, 45 deg = 03h.
        cls.open_sn = create_minimal_transient(cls.staff, name="plannerbright", ra=15.0, dec=20.0)
        cls.private_sn = create_minimal_transient(cls.staff, name="plannerprivate", ra=45.0, dec=30.0)
        cls.nophot_sn = create_minimal_transient(cls.staff, name="plannernophot", ra=30.0, dec=25.0)
        for sn in (cls.open_sn, cls.private_sn):
            attach_synthetic_photometry(cls.staff, sn, n_points=2)

        window = dict(valid_start=NIGHT, valid_stop=NIGHT + datetime.timedelta(days=1))
        cls.open_fu = cls._followup(cls.open_sn, True, window, audit)
        cls.private_fu = cls._followup(cls.private_sn, False, window, audit)
        cls.private_fu.groups.add(cls.group)
        cls.nophot_fu = cls._followup(cls.nophot_sn, True, window, audit)
        cls.open_fu.offset_star_ra, cls.open_fu.offset_star_dec = 15.01, 20.01
        cls.open_fu.offset_north, cls.open_fu.offset_east = 36.0, -36.0
        cls.open_fu.save()

        TransientFollowupRequest.objects.create(
            followup=cls.open_fu, requestor=cls.staff, priority=2.0,
            comment="needs a classification spectrum", **audit,
        )
        TransientFollowupRequest.objects.create(
            followup=cls.open_fu, requestor=cls.member, priority=1.0, comment="", **audit,
        )

        cls.midnight = local_midnight_mjd(NIGHT, -8)
        TransientPhotStat.objects.update_or_create(
            transient=cls.open_sn,
            defaults={"last_detected_mag": 18.4, "last_detected_mjd": cls.midnight - 2.0},
        )
        # A detection 20 days before the night: stale.
        TransientPhotStat.objects.update_or_create(
            transient=cls.private_sn,
            defaults={"last_detected_mag": 18.9, "last_detected_mjd": cls.midnight - 20.0},
        )

    @classmethod
    def _followup(cls, transient, is_public, window, audit):
        return TransientFollowup.objects.create(
            transient=transient, status=cls.status, classical_resource=cls.resource,
            is_public=is_public, requested_by=cls.staff, **window, **audit,
        )

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _targets(self, user, night=None, bazin=None):
        url = reverse("api-planner-targets", kwargs={"night_id": (night or self.night).pk})
        with mock.patch(BAZIN, return_value=bazin or {}):
            response = self._client(user).get(url)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()


class PlannerFlagAndAuthTests(_PlannerFixture):
    def test_endpoints_404_when_flag_off(self):
        client = self._client(self.staff)
        with override_settings(ASTROSCHEDULER_ENABLED=False):
            self.assertFalse(night_planner.enabled())
            get = client.get(reverse("api-planner-targets", kwargs={"night_id": self.night.pk}))
            post = client.post(reverse("api-planner-solve", kwargs={"night_id": self.night.pk}),
                               data={}, content_type="application/json")
        self.assertEqual(get.status_code, 404)
        self.assertEqual(post.status_code, 404)

    def test_anonymous_is_refused(self):
        url = reverse("api-planner-targets", kwargs={"night_id": self.night.pk})
        self.assertIn(Client().get(url).status_code, (401, 403))

    def test_token_auth(self):
        token, _ = Token.objects.get_or_create(user=self.staff)
        url = reverse("api-planner-targets", kwargs={"night_id": self.night.pk})
        with mock.patch(BAZIN, return_value={}):
            response = Client().get(url, HTTP_AUTHORIZATION="Token %s" % token.key)
        self.assertEqual(response.status_code, 200)

    def test_unknown_night_404(self):
        response = self._client(self.staff).get(reverse("api-planner-targets", kwargs={"night_id": 999999}))
        self.assertEqual(response.status_code, 404)


class PlannerTargetsTests(_PlannerFixture):
    def test_target_schema(self):
        data = self._targets(self.staff)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["night"]["date"], "2026-10-15")
        self.assertEqual(data["instrument"], "kast")
        site = data["observatory"]
        self.assertEqual(site["id"], "lick")
        self.assertEqual(site["timezone"], "America/Los_Angeles")
        self.assertAlmostEqual(site["lat"], 37.3414)
        self.assertNotIn("telescope", site)

        by_id = {t["external_id"]: t for t in data["targets"]}
        target = by_id[str(self.open_fu.id)]
        self.assertEqual(target["name"], "plannerbright")
        self.assertEqual((target["ra"], target["dec"]), ("01:00:00.00", "+20:00:00.0"))
        self.assertEqual((target["ra_deg"], target["dec_deg"]), (15.0, 20.0))
        self.assertEqual(target["transient_id"], self.open_sn.id)
        self.assertEqual(target["priority"], 1.0)  # the minimum over the requestors
        self.assertIn("needs a classification spectrum", target["comment"])
        self.assertEqual(target["followup_status"], "Requested")
        self.assertEqual(target["offset_star"]["offset_north_arcsec"], 36.0)
        self.assertNotIn("status", target)  # YSE_PZ statuses must not reach the solver's status

    def test_visibility_follows_the_night_page(self):
        member = {t["name"] for t in self._targets(self.member)["targets"]}
        outsider = {t["name"] for t in self._targets(self.outsider)["targets"]}
        self.assertIn("plannerprivate", member)
        self.assertIn("plannerbright", outsider)
        self.assertNotIn("plannerprivate", outsider)

    def test_magnitude_sources_and_warnings(self):
        data = self._targets(self.staff, bazin={self.open_sn.id: (17.23, "r")})
        by_name = {t["name"]: t for t in data["targets"]}

        bright = by_name["plannerbright"]
        self.assertEqual((bright["magnitude"], bright["mag_band"], bright["mag_source"]), (17.23, "r", "bazin"))
        self.assertEqual(bright["mag_age_days"], 2.0)
        self.assertEqual(bright["warnings"], [])

        private = by_name["plannerprivate"]
        self.assertEqual((private["magnitude"], private["mag_source"]), (18.9, "last_detection"))
        self.assertTrue(any("20 days" in w for w in private["warnings"]))

        nophot = by_name["plannernophot"]
        self.assertEqual((nophot["magnitude"], nophot["mag_source"]), (19.0, "none"))
        self.assertTrue(nophot["warnings"])
        self.assertTrue(any(w.startswith("plannernophot:") for w in data["warnings"]))

    def test_bazin_is_asked_for_local_midnight_once(self):
        with mock.patch(BAZIN, return_value={}) as bazin:
            night_planner.night_targets(self.night, self.staff)
        bazin.assert_called_once()
        ids, at_mjd = bazin.call_args[0]
        self.assertEqual(set(ids), {self.open_sn.id, self.private_sn.id, self.nophot_sn.id})
        self.assertAlmostEqual(at_mjd, self.midnight)

    def test_query_count_does_not_grow_with_targets(self):
        def count():
            with mock.patch(BAZIN, return_value={}), CaptureQueriesContext(connection) as ctx:
                night_planner.night_targets(self.night, self.staff)
            return len(ctx.captured_queries)

        before = count()
        audit = audit_fields(self.staff)
        window = dict(valid_start=NIGHT, valid_stop=NIGHT + datetime.timedelta(days=1))
        for i in range(4):
            sn = create_minimal_transient(self.staff, name="plannerextra%d" % i, ra=20.0 + i, dec=10.0)
            fu = self._followup(sn, True, window, audit)
            TransientFollowupRequest.objects.create(followup=fu, requestor=self.staff, priority=3.0, **audit)
        self.assertEqual(count(), before)

    def test_unmapped_telescope_is_a_custom_site(self):
        self.telescope.name = "Synthetic 2m"
        self.telescope.save()
        data = self._targets(self.staff)
        self.assertEqual(data["observatory"]["id"], "yse-telescope-%d" % self.telescope.pk)
        self.assertEqual(data["observatory"]["telescope"], {"name": "Synthetic 2m"})
        self.assertTrue(any("ASTROSCHEDULER_SITE_MAP" in w for w in data["warnings"]))

    def test_site_map_rules(self):
        cases = {"Shane": ("lick", "kast"), "Keck II": ("keck2", "kcwi"), "Keck I": ("keck1", "lris")}
        for name, expected in cases.items():
            self.telescope.name = name
            self.assertEqual(night_planner.site_for_telescope(self.telescope)[:2], expected, name)

    def test_sexagesimal(self):
        self.assertEqual(night_planner.sexagesimal(359.999999, -0.5), ("00:00:00.00", "-00:30:00.0"))
        self.assertEqual(night_planner.sexagesimal(187.5, 12.391), ("12:30:00.00", "+12:23:27.6"))


class PlannerSolveTests(_PlannerFixture):
    def _payload(self, user=None):
        targets = self._targets(user or self.staff)["targets"]
        return {
            # A client cannot move the night, the site or the instrument.
            "date": "2030-01-01",
            "observatory": {"id": "keck1", "lat": 0.0, "lon": 0.0, "telescope": {"ha_limit_west": 5.6667}},
            "instrument": "lris",
            "targets": targets,
            "auto_standards": False,
        }

    def test_request_takes_site_date_and_instrument_from_the_night(self):
        with iers_offline():
            request = night_planner.build_solve_request(self.night, self._payload())
        self.assertEqual(request.date, datetime.date(2026, 10, 15))
        self.assertEqual(request.site.id, "lick")
        self.assertAlmostEqual(request.site.latitude, 37.3414)
        self.assertEqual(request.site.telescope, {"ha_limit_west": 5.6667})
        self.assertEqual(request.instrument, "kast")
        self.assertIsNone(request.night_start_override)
        self.assertIsNone(request.night_end_override)

    def test_first_half_night_ends_at_mid_night(self):
        with iers_offline():
            request = night_planner.build_solve_request(self.half_night, self._payload())
        self.assertIsNone(request.night_start_override)
        end = datetime.datetime.fromisoformat(request.night_end_override)
        # Mid-way between the 18-degree twilights at Lick in mid-October: about 08:00 UTC.
        self.assertEqual(end.date(), datetime.date(2026, 10, 17))
        self.assertTrue(7 <= end.hour <= 9, end)

    def test_solve_endpoint(self):
        url = reverse("api-planner-solve", kwargs={"night_id": self.night.pk})
        with iers_offline():
            response = self._client(self.staff).post(url, data=self._payload(), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:300])
        result = response.json()
        self.assertEqual(result["yse_pz"]["night_id"], self.night.pk)
        self.assertEqual(result["solver_version"], "1.0.0")
        planned = {b["external_id"] for b in result["blocks"] if b["kind"] == "science"}
        self.assertIn(str(self.open_fu.id), planned)

    def test_bad_payloads_are_400(self):
        url = reverse("api-planner-solve", kwargs={"night_id": self.night.pk})
        client = self._client(self.staff)
        too_many = {"targets": [{"name": "x%d" % i, "ra": "01:00:00", "dec": "+20:00:00", "magnitude": 17}
                                for i in range(night_planner.MAX_SOLVE_TARGETS + 1)]}
        for body in ({"targets": "nope"}, too_many, {"targets": [{"name": "nomag", "ra": "01:00:00", "dec": "+20:00:00"}]}):
            with iers_offline():
                response = client.post(url, data=body, content_type="application/json")
            self.assertEqual(response.status_code, 400, body if len(str(body)) < 200 else "too_many")
            self.assertIn("detail", response.json())
