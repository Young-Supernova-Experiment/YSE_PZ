""""Observable tonight" on a shortlist (#854, UI preview only).

Covers the vectorised service on synthetic coordinates with known visibility
(a target at the zenith at local midnight, one that rises but never reaches
airmass 2, one that never rises), its agreement with the per-object
observability page (#307), the cache per (site, night, object set, limit), the
500-object cap, no network use, the JSON endpoint (preview only, listing rule)
and the three shortlists that carry the control: the follow-up list, the
preview dashboard and a filtered search. With the preview off those pages are
unchanged.

Synthetic users, telescopes and coordinates only.
"""

import base64
import datetime
import re
import socket
import warnings
from unittest import mock

from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import ClassicalResource, FollowupStatus, Observatory, Telescope, UserPreference
from YSE_App.services import observability as obs_svc
from YSE_App.services import observable_tonight as tonight
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

PREVIEW_COOKIE = "yse-ui-preview"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
NIGHT = datetime.date(2026, 9, 29)
UTC_DAY = re.compile(r"^\d{8}\.\d{3}$")


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _site(pk=1, name="Synthetic Maunakea"):
    return _Obj(pk=pk, name=name, latitude=19.8283, longitude=-155.4783, elevation=4160.0, observatory=None)


def _local_midnight(site, night):
    import astropy.units as u
    from astropy.time import Time

    return Time("%sT12:00:00" % night.isoformat()) - (site.longitude / 15.0) * u.hour + 12 * u.hour


def _zenith_at_midnight(site, night):
    """(ra, dec) at the zenith at local midnight: RA = LST then, Dec = latitude."""
    import astropy.units as u

    midnight = _local_midnight(site, night)
    midnight.delta_ut1_utc = 0.0  # UT1 = UTC to a second is plenty and needs no IERS table
    with obs_svc.iers_quiet(), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ra = midnight.sidereal_time("mean", longitude=site.longitude * u.deg).to_value(u.deg)
    return float(ra), site.latitude


def _minutes(iso_a, iso_b):
    fmt = "%Y-%m-%dT%H:%M"
    return abs((datetime.datetime.strptime(iso_a, fmt) - datetime.datetime.strptime(iso_b, fmt)).total_seconds()) / 60


class VisibilityServiceTests(SimpleTestCase):
    """The vectorised computation on known geometry; cache untouched."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.site = _site()
        ra, dec = _zenith_at_midnight(cls.site, NIGHT)
        cls.zenith = (11, ra, dec)
        cls.low = (12, ra, -60.0)      # culminates ~10 deg up: rises, never reaches airmass 2
        cls.never = (13, ra, -80.0)    # culminates below the horizon
        cls.payload = tonight.compute_visibility([cls.zenith, cls.low, cls.never], cls.site, NIGHT)

    def test_every_object_has_an_answer(self):
        self.assertEqual(set(self.payload["results"]), {"11", "12", "13"})
        self.assertEqual(self.payload["airmass_limit"], 2.0)
        self.assertEqual(self.payload["night"], "20260929")
        self.assertIsNone(self.payload["note"])

    def test_zenith_target_is_up_around_midnight_inside_astronomical_night(self):
        entry = self.payload["results"]["11"]
        self.assertTrue(entry["up"])
        self.assertLess(entry["min_airmass"], 1.01)
        midnight = _local_midnight(self.site, NIGHT).utc.isot[:16]
        self.assertLess(entry["start"]["iso"], midnight)
        self.assertGreater(entry["end"]["iso"], midnight)
        # Hour angle ~4.7 h at the dark-window edges puts it below airmass 2 then.
        self.assertGreater(entry["start"]["iso"], self.payload["dark_start"]["iso"])
        self.assertLess(entry["end"]["iso"], self.payload["dark_end"]["iso"])
        self.assertTrue(4 < entry["hours"] < 9.6, entry["hours"])
        self.assertTrue(0 <= entry["moon_sep"] <= 180)

    def test_times_are_printed_in_the_utc_date_format(self):
        entry = self.payload["results"]["11"]
        for when in (entry["start"], entry["end"], self.payload["dark_start"], self.payload["dark_end"]):
            self.assertRegex(when["utc"], UTC_DAY)
            self.assertTrue(when["utc"].startswith(when["iso"][:10].replace("-", "")))
            self.assertRegex(when["mjd"], r"^\d{5}\.\d{3}$")
        # Keck's astronomical night of Sep 29 (local) is Sep 30 UT, ~05:30 to ~15:00.
        self.assertTrue(self.payload["dark_start"]["utc"].startswith("20260930"))

    def test_low_target_rises_but_is_never_below_the_limit(self):
        entry = self.payload["results"]["12"]
        self.assertFalse(entry["up"])
        self.assertIsNone(entry["start"])
        self.assertEqual(entry["hours"], 0.0)
        self.assertGreater(entry["min_airmass"], 2.0)

    def test_target_below_the_horizon_has_no_airmass(self):
        entry = self.payload["results"]["13"]
        self.assertFalse(entry["up"])
        self.assertIsNone(entry["min_airmass"])

    def test_a_tighter_limit_shortens_the_window(self):
        tight = tonight.compute_visibility([self.zenith], self.site, NIGHT, airmass_limit=1.2)
        self.assertTrue(tight["results"]["11"]["up"])
        self.assertLess(tight["results"]["11"]["hours"], self.payload["results"]["11"]["hours"])
        self.assertGreater(tight["results"]["11"]["start"]["iso"], self.payload["results"]["11"]["start"]["iso"])

    def test_agrees_with_the_per_object_observability_page(self):
        target = _Obj(pk=11, name="zenith", ra=self.zenith[1], dec=self.zenith[2])
        page = obs_svc.compute_night_ephemeris(target, self.site, NIGHT)["summary"]
        entry = self.payload["results"]["11"]
        self.assertAlmostEqual(entry["min_airmass"], page["min_airmass"], delta=0.02)
        # Airmass 2 is 30 degrees; the two sample grids differ by a few minutes.
        self.assertLess(_minutes(entry["start"]["iso"], page["rise_30"]), 15)
        self.assertLess(_minutes(entry["end"]["iso"], page["set_30"]), 15)

    def test_one_transform_for_all_objects(self):
        from astropy.coordinates import SkyCoord

        with mock.patch.object(SkyCoord, "transform_to", autospec=True,
                               side_effect=SkyCoord.transform_to) as transform:
            tonight.compute_visibility([self.zenith, self.low, self.never], self.site, NIGHT)
        shapes = [call.args[0].shape for call in transform.call_args_list]
        # One (objects x 1) transform for the shortlist, plus the moon's.
        self.assertIn((3, 1), shapes)
        self.assertEqual(len([s for s in shapes if s == (3, 1)]), 1)

    def test_empty_shortlist(self):
        payload = tonight.compute_visibility([], self.site, NIGHT)
        self.assertEqual(payload["results"], {})

    def test_more_than_the_cap_is_refused(self):
        targets = [(i, 10.0, 20.0) for i in range(tonight.MAX_OBJECTS + 1)]
        with self.assertRaises(tonight.ShortlistTooLong):
            tonight.compute_visibility(targets, self.site, NIGHT)
        with self.assertRaises(tonight.ShortlistTooLong):
            tonight.shortlist_visibility(targets, self.site, NIGHT)

    def test_no_network_for_a_night_past_the_bundled_tables(self):
        def _no_network(*args, **kwargs):
            raise AssertionError("astropy tried to open a network connection")

        with mock.patch.object(socket, "create_connection", side_effect=_no_network):
            payload = tonight.compute_visibility([self.zenith], self.site, datetime.date(2031, 6, 1))
        self.assertIn("11", payload["results"])

    def test_parse_ids(self):
        self.assertEqual(tonight.parse_ids(["3,1", "3", " 2 ,"]), [3, 1, 2])
        with self.assertRaises(ValueError):
            tonight.parse_ids(["1,x"])


class VisibilityCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.site = _site()
        self.targets = [(1, 10.0, 20.0), (2, 200.0, -30.0)]

    def tearDown(self):
        cache.clear()

    def _call(self, targets=None, site=None, night=NIGHT, limit=2.0):
        return tonight.shortlist_visibility(targets or self.targets, site or self.site, night, limit)

    def test_cached_per_site_night_object_set_and_limit(self):
        with mock.patch.object(tonight, "compute_visibility", wraps=tonight.compute_visibility) as compute:
            first = self._call()
            self.assertEqual(self._call(), first)
            self._call(targets=list(reversed(self.targets)))  # same set, other order: cached
            self.assertEqual(compute.call_count, 1)
            self._call(targets=self.targets[:1])
            self._call(limit=1.5)
            self._call(night=NIGHT + datetime.timedelta(days=1))
            self._call(site=_site(pk=2))
        self.assertEqual(compute.call_count, 5)

    def test_key_holds_the_coordinates(self):
        moved = [(1, 10.5, 20.0), (2, 200.0, -30.0)]
        self.assertNotEqual(tonight.cache_key(self.targets, self.site, NIGHT, 2.0),
                            tonight.cache_key(moved, self.site, NIGHT, 2.0))


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS, DATA_SERVICE_READER_USERNAMES=[], API_SERVICE_USERNAMES=[],
                   UI_PREVIEW_DEFAULT=False)
class ObservableTonightBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("obstonight_owner")
        cls.viewer = create_test_user("obstonight_viewer", is_staff=False)
        audit = audit_fields(cls.owner)
        observatory = Observatory.objects.create(name="Synthetic Obs", utc_offset=-10, tz_name="UTC", **audit)
        # Named so they sort first in the telescope list (the default site).
        cls.site = Telescope.objects.create(name="AAA Synthetic 4m", observatory=observatory, latitude=19.8283,
                                            longitude=-155.4783, elevation=4160, **audit)
        Telescope.objects.create(name="ZZZ Synthetic 1m", observatory=observatory, latitude=-30.0,
                                 longitude=-70.0, elevation=2000, **audit)
        # TNS-style names are listed for every signed-in user.
        cls.listed = [create_minimal_transient(cls.owner, name="2026obs%s" % c, status_name="Following",
                                               ra=10.0 + i, dec=20.0) for i, c in enumerate("abc")]
        cls.private = create_minimal_transient(cls.owner, name="private-obstonight", status_name="Following")

    def setUp(self):
        cache.clear()

    def client_for(self, user, preview=True):
        client = Client()
        client.force_login(user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client

    def data(self, client, ids, **params):
        query = {"site": self.site.pk, "ids": ",".join(str(i) for i in ids), "date": NIGHT.isoformat()}
        query.update(params)
        return client.get(reverse("observable_tonight_data"), query)


class EndpointTests(ObservableTonightBase):
    def test_preview_off_is_not_found(self):
        response = self.data(self.client_for(self.viewer, preview=False), [self.listed[0].pk])
        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        response = self.data(Client(), [self.listed[0].pk])
        self.assertEqual(response.status_code, 302)

    def test_basic_auth_scripts_never_get_it(self):
        client = self.client_for(self.viewer)
        basic = "Basic " + base64.b64encode(b"synthetic:synthetic").decode("ascii")
        response = client.get(reverse("observable_tonight_data"),
                              {"site": self.site.pk, "ids": str(self.listed[0].pk)}, HTTP_AUTHORIZATION=basic)
        self.assertIn(response.status_code, (302, 401, 403, 404))

    def test_answers_each_listed_object_and_leaves_out_the_rest(self):
        ids = [t.pk for t in self.listed] + [self.private.pk]
        response = self.data(self.client_for(self.viewer), ids)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body["results"]), {str(t.pk) for t in self.listed})
        self.assertEqual(body["telescope"]["name"], "AAA Synthetic 4m")
        self.assertEqual(body["date"], NIGHT.isoformat())
        for entry in body["results"].values():
            self.assertEqual(set(entry), {"up", "start", "end", "hours", "min_airmass", "moon_sep"})

    def test_cached_for_the_same_site_night_and_set(self):
        client = self.client_for(self.viewer)
        ids = [t.pk for t in self.listed]
        with mock.patch.object(tonight, "compute_visibility", wraps=tonight.compute_visibility) as compute:
            self.data(client, ids)
            self.data(client, list(reversed(ids)))
        self.assertEqual(compute.call_count, 1)

    def test_cap(self):
        ids = list(range(1, tonight.MAX_OBJECTS + 2))
        with mock.patch.object(tonight, "compute_visibility") as compute:
            response = self.data(self.client_for(self.viewer), ids)
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most %d objects" % tonight.MAX_OBJECTS, response.json()["error"])
        compute.assert_not_called()

    def test_needs_a_shortlist(self):
        response = self.data(self.client_for(self.viewer), [])
        self.assertEqual(response.status_code, 400)
        self.assertIn("shortlist", response.json()["error"])

    def test_bad_input(self):
        client = self.client_for(self.viewer)
        pk = self.listed[0].pk
        self.assertEqual(self.data(client, [pk], site="").status_code, 400)
        self.assertEqual(self.data(client, [pk], site="999999").status_code, 400)
        self.assertEqual(self.data(client, [pk], airmass="9").status_code, 400)
        self.assertEqual(self.data(client, [pk], airmass="x").status_code, 400)
        self.assertEqual(self.data(client, [pk], date="tonight").status_code, 400)
        response = client.get(reverse("observable_tonight_data"), {"site": self.site.pk, "ids": "1,x"})
        self.assertEqual(response.status_code, 400)


class ShortlistPageTests(ObservableTonightBase):
    CONTROL = 'class="yse-obs-tonight form-inline"'

    def _followup(self):
        _obs_group, instrument, _band = create_instrument_stack(self.owner, obs_group_name="obstonight-res")
        now = timezone.now()
        resource = ClassicalResource.objects.create(
            telescope=instrument.telescope, begin_date_valid=now - datetime.timedelta(days=1),
            end_date_valid=now + datetime.timedelta(days=3), creator_only=True, **audit_fields(self.owner))
        from YSE_App.services.followup_requests import create_or_attach_request

        requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.owner))
        create_or_attach_request(self.owner, self.listed[0], status=requested, valid_start=now,
                                 valid_stop=now + datetime.timedelta(days=3), classical_resource=resource)

    def test_followup_list_preview_off_unchanged(self):
        self._followup()
        response = self.client_for(self.owner, preview=False).get(reverse("followup"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "yse-obs-tonight")
        # Favourite stars carry data-transient-id already; the rows must not.
        self.assertNotRegex(response.content.decode(), r"<tr\s[^>]*data-transient-id")
        self.assertNotContains(response, "observable-tonight.js")

    def test_followup_list_preview_on(self):
        self._followup()
        response = self.client_for(self.owner).get(reverse("followup"))
        self.assertContains(response, self.CONTROL)
        self.assertRegex(response.content.decode(), r'<tr\s[^>]*data-transient-id="%d"' % self.listed[0].pk)
        self.assertContains(response, 'data-id-selector="tr[data-transient-id]"')
        self.assertContains(response, "observable-tonight.js?v=obs-tonight-2")

    def test_site_list_starts_with_the_first_telescope(self):
        self._followup()
        html = self.client_for(self.owner).get(reverse("followup")).content.decode()
        select = html[html.index('class="form-control input-sm form-control-sm yse-obs-site"'):]
        first = re.search(r'<option value="(\d+)">([^<]+)</option>', select)
        names = list(Telescope.objects.exclude(latitude=None).order_by("name").values_list("name", flat=True))
        self.assertEqual(first.group(2), names[0])
        self.assertNotIn("selected", select[:select.index("</select>")])

    def test_dashboard_preview_has_the_control_and_old_dashboard_does_not(self):
        on = self.client_for(self.viewer).get(reverse("dashboard"))
        self.assertTemplateUsed(on, "YSE_App/dashboard_v2.html")
        self.assertContains(on, self.CONTROL)
        self.assertContains(on, 'data-id-selector=".yse-dash-select"')
        off = self.client_for(self.viewer, preview=False).get(reverse("dashboard"))
        self.assertTemplateUsed(off, "YSE_App/dashboard.html")
        self.assertNotContains(off, "yse-obs-tonight")

    def test_search_preview_off_unchanged(self):
        response = self.client_for(self.viewer, preview=False).get(reverse("search"), {"name_contains": "2026obs"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["obs_tonight"])
        self.assertNotContains(response, "yse-obs-tonight")

    def test_search_without_filters_is_blocked(self):
        response = self.client_for(self.viewer).get(reverse("search"))
        self.assertNotContains(response, self.CONTROL)
        self.assertContains(response, "set at least one filter first")

    def test_search_ordering_alone_is_not_a_filter(self):
        response = self.client_for(self.viewer).get(reverse("search"), {"ordering": "name"})
        self.assertNotContains(response, self.CONTROL)
        self.assertContains(response, "set at least one filter first")

    def test_filtered_search_has_the_control(self):
        response = self.client_for(self.viewer).get(reverse("search"), {"name_contains": "2026obs"})
        self.assertContains(response, self.CONTROL)
        self.assertContains(response, 'data-id-selector=".search-select-row"')
        self.assertContains(response, 'class="search-select-row"')

    def test_filtered_search_over_the_cap_is_blocked_with_a_message(self):
        with mock.patch.object(tonight, "MAX_OBJECTS", 2):
            response = self.client_for(self.viewer).get(reverse("search"), {"name_contains": "2026obs"})
        self.assertNotContains(response, self.CONTROL)
        self.assertContains(response, "this search has 3 matches; it works on at most 2.")


class DefaultSiteTests(ObservableTonightBase):
    """Per-user default site (#959): saved on the server, used before the browser's last choice."""

    def post(self, client, site):
        return client.post(reverse("observable_tonight_default_site"), {"site": site})

    def control(self, user):
        html = self.client_for(user).get(reverse("dashboard")).content.decode()
        start = html.index('class="yse-obs-tonight form-inline"')
        return html[start:html.index("</div>", start)]

    def other_site(self):
        return Telescope.objects.get(name="ZZZ Synthetic 1m")

    def test_preview_off_is_not_found_and_anonymous_is_sent_to_login(self):
        self.assertEqual(self.post(self.client_for(self.viewer, preview=False), self.site.pk).status_code, 404)
        self.assertEqual(self.post(Client(), self.site.pk).status_code, 302)
        self.assertFalse(UserPreference.objects.filter(user=self.viewer).exists())

    def test_post_only_and_csrf_checked(self):
        client = self.client_for(self.viewer)
        self.assertEqual(client.get(reverse("observable_tonight_default_site")).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.viewer)
        strict.cookies[PREVIEW_COOKIE] = "1"
        self.assertEqual(self.post(strict, self.site.pk).status_code, 403)

    def test_save_shows_it_selected_and_clear_removes_it(self):
        client = self.client_for(self.viewer)
        other = self.other_site()
        before = self.control(self.viewer)
        self.assertIn('data-default-site=""', before)
        self.assertNotIn(" selected>", before)
        self.assertRegex(before, r'<button type="button" class="btn btn-link btn-sm yse-obs-make-default">'
                                 r'Make this my default</button>')

        response = self.post(client, other.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"default_site": other.pk, "name": other.name})
        self.assertEqual(UserPreference.objects.get(user=self.viewer).observing_default_site_id, other.pk)
        after = self.control(self.viewer)
        self.assertIn('data-default-site="%d"' % other.pk, after)
        self.assertIn('<option value="%d" selected>' % other.pk, after)
        self.assertIn('yse-obs-make-default" hidden>', after)
        self.assertRegex(after, r'<span class="yse-obs-default-note small text-muted">your default site</span>')
        # another user's control is unchanged
        self.assertIn('data-default-site=""', self.control(self.owner))

        response = self.post(client, "")
        self.assertEqual(response.json(), {"default_site": None, "name": ""})
        self.assertIsNone(UserPreference.objects.get(user=self.viewer).observing_default_site_id)
        self.assertIn('data-default-site=""', self.control(self.viewer))

    def test_bad_sites_are_refused(self):
        client = self.client_for(self.viewer)
        no_coords = Telescope.objects.create(name="Synthetic no coordinates", observatory=self.site.observatory,
                                             **audit_fields(self.owner))
        for value in ("x", "999999", str(no_coords.pk)):
            with self.subTest(site=value):
                self.assertEqual(self.post(client, value).status_code, 400)
        self.assertFalse(UserPreference.objects.filter(user=self.viewer, observing_default_site__isnull=False).exists())

    def test_deleted_default_falls_back(self):
        audit = audit_fields(self.owner)
        doomed = Telescope.objects.create(name="Synthetic doomed", observatory=self.site.observatory,
                                          latitude=0.0, longitude=0.0, elevation=0.0, **audit)
        self.post(self.client_for(self.viewer), doomed.pk)
        doomed.delete()
        pref = UserPreference.objects.get(user=self.viewer)
        self.assertIsNone(pref.observing_default_site_id)
        self.assertIn('data-default-site=""', self.control(self.viewer))

    def test_existing_preferences_are_kept(self):
        pref = UserPreference.objects.create(user=self.viewer, comment_default_public=True)
        self.post(self.client_for(self.viewer), self.site.pk)
        pref.refresh_from_db()
        self.assertTrue(pref.comment_default_public)
        self.assertEqual(pref.observing_default_site_id, self.site.pk)
