"""Observability page (#307): the ephemeris service, its cache, the page and the JSON endpoint.

The service is checked on known sites and nights with sanity bounds rather
than exact numbers, so a newer astropy ephemeris does not break the suite.
"""

import datetime
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App import observability_views
from YSE_App.models import Allocation, Observatory, Telescope
from YSE_App.services import observability as svc
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user
from YSE_App.tests.js_syntax_utils import bracket_imbalance, inline_script_bodies

NIGHT = datetime.date(2026, 9, 29)


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _telescope(pk=1, name="Keck", latitude=19.8283, longitude=-155.4783, elevation=4160.0, observatory="Maunakea"):
    return _Obj(pk=pk, name=name, latitude=latitude, longitude=longitude, elevation=elevation,
                observatory=_Obj(name=observatory) if observatory else None)


def _transient(pk=5, name="2026abc", ra=150.0, dec=2.0):
    return _Obj(pk=pk, name=name, ra=ra, dec=dec)


def _midnight_transiting_target(telescope, night):
    """A target at the zenith at local midnight: RA = LST then, Dec = latitude."""
    import astropy.units as u
    from astropy.time import Time

    observer = svc.observer_for(telescope)
    midnight = Time("%sT12:00:00" % night.isoformat()) - (telescope.longitude / 15.0) * u.hour + 12 * u.hour
    with svc.iers_quiet():
        ra = observer.local_sidereal_time(midnight).to_value(u.deg)
    return _transient(pk=7, name="zenith", ra=float(ra), dec=telescope.latitude)


class EphemerisServiceTests(SimpleTestCase):
    """compute_night_ephemeris on known sites; cache untouched."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.keck = svc.compute_night_ephemeris(_transient(), _telescope(), NIGHT)

    def test_night_is_the_local_evening_of_the_requested_date(self):
        night = self.keck["night"]
        # Hawaii evening of Sep 29 is Sep 30 UT; sunset ~04:00 UT, sunrise ~16:15 UT.
        self.assertEqual(night["date"], "2026-09-29")
        self.assertTrue(night["sunset"].startswith("2026-09-30T04"), night["sunset"])
        self.assertTrue(night["sunrise"].startswith("2026-09-30T16"), night["sunrise"])
        self.assertIsNone(night["note"])
        self.assertAlmostEqual(night["utc_offset_hours"], -10.37, places=2)

    def test_twilights_are_ordered_and_dark_is_astronomical(self):
        night = self.keck["night"]
        tw = night["twilight"]
        order = [night["sunset"], tw["evening_civil"], tw["evening_nautical"], tw["evening_astronomical"],
                 tw["morning_astronomical"], tw["morning_nautical"], tw["morning_civil"], night["sunrise"]]
        self.assertEqual(order, sorted(order))
        self.assertEqual(night["dark_start"], tw["evening_astronomical"])
        self.assertEqual(night["dark_end"], tw["morning_astronomical"])
        self.assertAlmostEqual(self.keck["summary"]["hours_dark"], 9.6, delta=0.3)

    def test_samples_cover_the_night_and_airmass_is_defined_only_above_the_horizon(self):
        samples = self.keck["samples"]
        for key in ("t", "alt", "az", "airmass", "moon_alt", "moon_sep", "sun_alt"):
            self.assertEqual(len(samples[key]), svc.N_SAMPLES, key)
        self.assertEqual(samples["t"], sorted(samples["t"]))
        self.assertLess(samples["t"][0], self.keck["night"]["sunset"])
        self.assertGreater(samples["t"][-1], self.keck["night"]["sunrise"])
        for alt, airmass in zip(samples["alt"], samples["airmass"]):
            if alt <= 0:
                self.assertIsNone(airmass)
            else:
                self.assertGreaterEqual(airmass, 1.0)
        # The sun is below the horizon between sunset and sunrise.
        night = self.keck["night"]
        for t, sun_alt in zip(samples["t"], samples["sun_alt"]):
            if night["sunset"] < t < night["sunrise"]:
                self.assertLess(sun_alt, 0.5, t)

    def test_morning_object_in_september_is_barely_observable_from_hawaii(self):
        s = self.keck["summary"]
        # RA 10h in late September rises in the morning twilight from Hawaii.
        self.assertFalse(s["never_rises"])
        self.assertLess(s["hours_observable"], 1.0)
        self.assertGreater(s["max_alt"], 30)
        # It is still climbing when the window closes after sunrise.
        self.assertIsNotNone(s["rise_30"])
        self.assertTrue(s["set_30_after_window"])
        self.assertFalse(s["rise_30_before_window"])
        self.assertLessEqual(s["hours_observable"], s["hours_dark"])
        self.assertLessEqual(s["hours_up_dark"], s["hours_dark"])
        self.assertEqual(round(s["moon_illum"], 1), round(s["moon_illum"], 1))
        self.assertTrue(0 <= s["moon_illum"] <= 1)
        self.assertTrue(0 <= s["moon_sep"] <= 180)

    def test_zenith_target_is_observable_most_of_the_night(self):
        tel = _telescope()
        payload = svc.compute_night_ephemeris(_midnight_transiting_target(tel, NIGHT), tel, NIGHT)
        s = payload["summary"]
        self.assertGreater(s["max_alt"], 85)
        self.assertLess(s["min_airmass"], 1.05)
        # +/- 4 h around transit above 30 deg at this declination, capped by the dark hours.
        self.assertGreater(s["hours_observable"], 6)
        self.assertLessEqual(s["hours_observable"], s["hours_dark"])
        self.assertLess(s["rise_30"], s["transit"])
        self.assertLess(s["transit"], s["set_30"])
        self.assertFalse(s["rise_30_before_window"])
        self.assertFalse(s["set_30_after_window"])
        self.assertTrue(s["transit"].startswith("2026-09-30T1"), s["transit"])
        # The airmass at the best sample is about the secant of the zenith distance.
        import math
        best = max(range(svc.N_SAMPLES), key=lambda i: payload["samples"]["alt"][i])
        alt = payload["samples"]["alt"][best]
        self.assertAlmostEqual(payload["samples"]["airmass"][best], 1 / math.sin(math.radians(alt)), delta=0.02)

    def test_target_that_never_rises_from_the_south(self):
        vlt = _telescope(pk=3, name="VLT", latitude=-24.6272, longitude=-70.4039, elevation=2635.0, observatory=None)
        payload = svc.compute_night_ephemeris(_transient(pk=6, name="north", ra=80.0, dec=80.0), vlt, NIGHT)
        s = payload["summary"]
        self.assertTrue(s["never_rises"])
        self.assertEqual(s["hours_observable"], 0.0)
        self.assertIsNone(s["min_airmass"])
        self.assertIsNone(s["transit"])
        self.assertIsNone(s["rise_30"])
        self.assertEqual(payload["telescope"]["observatory"], "")
        # Chile's evening of Sep 29 starts before midnight UT.
        self.assertTrue(payload["night"]["sunset"].startswith("2026-09-29T2"), payload["night"]["sunset"])

    def test_polar_site_falls_back_to_a_fixed_window_with_a_note(self):
        polar = _telescope(pk=9, name="Pole", latitude=89.0, longitude=0.0, elevation=0.0, observatory=None)
        payload = svc.compute_night_ephemeris(_transient(), polar, datetime.date(2026, 6, 21))
        self.assertIn("does not set", payload["night"]["note"])
        self.assertEqual(payload["night"]["sunset"], "2026-06-21T18:00")
        self.assertEqual(payload["night"]["sunrise"], "2026-06-22T06:00")
        self.assertEqual(payload["summary"]["hours_dark"], 12.0)

    def test_summary_rows_sort_by_hours_observable(self):
        rows = svc.summary_rows([
            {"summary": {"hours_observable": 1.0, "max_alt": 40}, "telescope": {"name": "B"}, "night": {}},
            {"summary": {"hours_observable": 5.0, "max_alt": 80}, "telescope": {"name": "C"}, "night": {}},
            {"summary": {"hours_observable": 1.0, "max_alt": 60}, "telescope": {"name": "A"}, "night": {}},
        ])
        self.assertEqual([r["telescope"]["name"] for r in rows], ["C", "A", "B"])

    def test_default_night_is_ut_now_minus_twelve_hours(self):
        self.assertEqual(svc.default_night_date(datetime.datetime(2026, 9, 29, 3, 0)), datetime.date(2026, 9, 28))
        self.assertEqual(svc.default_night_date(datetime.datetime(2026, 9, 29, 21, 0)), datetime.date(2026, 9, 29))
        self.assertEqual(svc.parse_night_date(" 2026-01-05 "), datetime.date(2026, 1, 5))
        self.assertIsInstance(svc.parse_night_date(""), datetime.date)
        with self.assertRaises(ValueError):
            svc.parse_night_date("tonight")


class EphemerisCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_second_call_is_a_cache_hit(self):
        tel, tr = _telescope(), _transient()
        with mock.patch.object(svc, "compute_night_ephemeris", wraps=svc.compute_night_ephemeris) as compute:
            first = svc.night_ephemeris(tr, tel, NIGHT)
            second = svc.night_ephemeris(tr, tel, NIGHT)
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(first, second)
            svc.night_ephemeris(tr, tel, NIGHT + datetime.timedelta(days=1))
            self.assertEqual(compute.call_count, 2)

    def test_key_is_versioned_and_follows_the_coordinates(self):
        tel, tr = _telescope(), _transient()
        key = svc.ephemeris_cache_key(tr, tel, NIGHT)
        self.assertTrue(key.startswith("observability_v%d_" % svc.EPHEMERIS_CACHE_VERSION))
        self.assertIn("2026-09-29", key)
        moved = _telescope(longitude=-70.0)
        self.assertNotEqual(key, svc.ephemeris_cache_key(tr, moved, NIGHT))
        self.assertNotEqual(key, svc.ephemeris_cache_key(_transient(ra=151.0), tel, NIGHT))

    def test_entries_expire_after_an_hour(self):
        tel, tr = _telescope(), _transient()
        with mock.patch.object(svc.cache, "set", wraps=svc.cache.set) as cache_set:
            svc.night_ephemeris(tr, tel, NIGHT)
        self.assertEqual(cache_set.call_args.kwargs.get("timeout"), 3600)

    def test_use_cache_false_recomputes(self):
        tel, tr = _telescope(), _transient()
        svc.night_ephemeris(tr, tel, NIGHT)
        with mock.patch.object(svc, "compute_night_ephemeris", wraps=svc.compute_night_ephemeris) as compute:
            svc.night_ephemeris(tr, tel, NIGHT, use_cache=False)
        self.assertEqual(compute.call_count, 1)


class ObservabilityViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("obs_staff", is_staff=True)
        cls.user = create_test_user("obs_user", is_staff=False)
        cls.other = create_test_user("obs_other", is_staff=False)
        audit = audit_fields(cls.staff)
        cls.transient = create_minimal_transient(cls.staff, name="obs-sn", ra=150.0, dec=2.0)
        maunakea = Observatory.objects.create(name="Maunakea", utc_offset=-10, tz_name="Pacific/Honolulu", **audit)
        paranal = Observatory.objects.create(name="Paranal", utc_offset=-4, tz_name="America/Santiago", **audit)
        cls.keck = Telescope.objects.create(name="Keck I", observatory=maunakea, latitude=19.8283,
                                            longitude=-155.4783, elevation=4160, **audit)
        cls.vlt = Telescope.objects.create(name="VLT UT1", observatory=paranal, latitude=-24.6272,
                                           longitude=-70.4039, elevation=2635, **audit)
        cls.lick = Telescope.objects.create(name="Shane 3m", observatory=maunakea, latitude=37.3414,
                                            longitude=-121.6429, elevation=1283, **audit)
        cls.group = Group.objects.create(name="obs-keck-users")
        cls.user.groups.add(cls.group)
        allocation = Allocation.objects.create(
            name="Keck 2026B", telescope=cls.keck, hours_allocated=10,
            start_date=timezone.now() - datetime.timedelta(days=30),
            end_date=timezone.now() + datetime.timedelta(days=150), **audit,
        )
        allocation.groups.add(cls.group)
        cls.page = reverse("observability", kwargs={"transient_id": cls.transient.pk})
        cls.data = reverse("observability_data", kwargs={"transient_id": cls.transient.pk})

    def setUp(self):
        cache.clear()
        self.client = Client()

    def test_page_and_endpoint_require_login(self):
        for url in (self.page, self.data):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])

    def test_page_lists_every_telescope_with_the_night_and_loader(self):
        self.client.force_login(self.user)
        response = self.client.get(self.page + "?date=2026-09-29")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for name in ("Keck I", "VLT UT1", "Shane 3m"):
            self.assertIn(name, html)
        self.assertIn('data-date="2026-09-29"', html)
        self.assertIn('data-url="%s"' % self.data, html)
        self.assertIn('id="obs_plot_%d"' % self.keck.pk, html)
        self.assertIn("bokeh-2.4.2.min.js", html)
        self.assertIn("?date=2026-09-28", html)
        self.assertIn("?date=2026-09-30", html)
        for body in inline_script_bodies(html):
            self.assertEqual(bracket_imbalance(body), [])

    def test_bad_date_on_the_page_falls_back_with_a_warning(self):
        self.client.force_login(self.user)
        response = self.client.get(self.page + "?date=tonight")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is not a YYYY-MM-DD date")
        self.assertContains(response, 'data-date="%s"' % svc.default_night_date().isoformat())

    def test_mine_keeps_the_telescopes_behind_the_users_allocations(self):
        self.client.force_login(self.user)
        html = self.client.get(self.page + "?date=2026-09-29&mine=1").content.decode()
        self.assertIn("Keck I", html)
        self.assertNotIn("VLT UT1", html)
        self.assertNotIn("Shane 3m", html)
        self.assertIn("checked", html)
        # A user with no allocations sees everything rather than an empty page.
        self.client.force_login(self.other)
        html = self.client.get(self.page + "?date=2026-09-29&mine=1").content.decode()
        self.assertIn("VLT UT1", html)
        self.assertEqual(svc.telescope_ids_for_user(self.other), set())
        self.assertEqual(svc.telescope_ids_for_user(self.user), {self.keck.pk})

    def test_data_endpoint_returns_one_entry_per_telescope_sorted_rows_and_plots(self):
        self.client.force_login(self.user)
        response = self.client.get(self.data + "?date=2026-09-29")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["date"], "2026-09-29")
        self.assertEqual(data["transient"]["name"], "obs-sn")
        self.assertEqual({r["telescope"]["id"] for r in data["results"]}, {self.keck.pk, self.vlt.pk, self.lick.pk})
        hours = [r["hours_observable"] for r in data["rows"]]
        self.assertEqual(hours, sorted(hours, reverse=True))
        for entry in data["results"]:
            self.assertEqual(entry["plot"]["target_id"], "obs_plot_%d" % entry["telescope"]["id"])
            self.assertIn("doc", entry["plot"])
            self.assertEqual(len(entry["samples"]["alt"]), svc.N_SAMPLES)
            self.assertEqual(entry["night"]["date"], "2026-09-29")

    def test_data_endpoint_filters_and_options(self):
        self.client.force_login(self.user)
        data = self.client.get(self.data + "?date=2026-09-29&telescope=%d&plots=0" % self.vlt.pk).json()
        self.assertEqual([r["telescope"]["name"] for r in data["results"]], ["VLT UT1"])
        self.assertNotIn("plot", data["results"][0])
        data = self.client.get(self.data + "?date=2026-09-29&mine=1&plots=0").json()
        self.assertEqual([r["telescope"]["name"] for r in data["results"]], ["Keck I"])
        self.assertEqual(self.client.get(self.data + "?date=nope").status_code, 400)
        self.assertEqual(self.client.post(self.data).status_code, 405)
        missing = reverse("observability_data", kwargs={"transient_id": 999999})
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.assertEqual(self.client.get(reverse("observability", kwargs={"transient_id": 999999})).status_code, 404)

    def test_data_endpoint_uses_the_cache_on_repeat(self):
        self.client.force_login(self.user)
        url = self.data + "?date=2026-09-29&telescope=%d&plots=0" % self.keck.pk
        with mock.patch.object(svc, "compute_night_ephemeris", wraps=svc.compute_night_ephemeris) as compute:
            self.client.get(url)
            self.client.get(url)
        self.assertEqual(compute.call_count, 1)

    def test_plot_json_marks_twilight_and_now(self):
        payload = svc.compute_night_ephemeris(self.transient, self.keck, NIGHT)
        item = observability_views._plot_json(payload)
        self.assertEqual(item["target_id"], "obs_plot_%d" % self.keck.pk)
        kinds = [ref["type"] for ref in item["doc"]["roots"]["references"]]
        self.assertGreaterEqual(kinds.count("BoxAnnotation"), 6)
        self.assertIn("Span", kinds)
        self.assertIn("HoverTool", kinds)

    def test_detail_page_links_to_the_observability_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="%s"' % self.page)
