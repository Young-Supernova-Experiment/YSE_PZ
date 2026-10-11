"""Transient detail shell and summary page speed fixes (perf P5, P10, P20; #459, #460, #462)."""

import datetime
from unittest import mock

import requests
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from YSE_App import view_utils
from YSE_App.models import SurveyObservation, Transient, TransientPhotData
from YSE_App.templatetags.transient_detail_extras import galcoordsb, galcoordsl, tags_list
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_survey_field,
    create_minimal_transient,
    create_test_user,
    create_transient_with_synthetic_data,
)


def _legacy_disc_mag(photdata_qs):
    """The original implementation: walk every row in obs_date order."""
    firstphot = None
    for ph in photdata_qs.order_by("obs_date"):
        if ph.discovery_point:
            return ph
        if ph.mag:
            firstphot = ph
    return firstphot


class LikelyYSEFieldQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("detail_summary_field")

    def _query_count(self, transient):
        with CaptureQueriesContext(connection) as ctx:
            result = transient.LikelyYSEField()
        return result, len(ctx.captured_queries)

    def test_constant_queries_and_same_answer(self):
        few = create_minimal_transient(self.user, name="field-few", ra=10.0, dec=20.0)
        many = create_minimal_transient(self.user, name="field-many", ra=100.0, dec=-30.0)
        attach_synthetic_survey_field(self.user, few, n_observations=2)
        attach_synthetic_survey_field(self.user, many, n_observations=40)

        (field_few, ra_few, dec_few, since_few), n_few = self._query_count(few)
        (field_many, _ra, _dec, since_many), n_many = self._query_count(many)

        self.assertEqual(n_few, n_many)
        self.assertLessEqual(n_many, 2)
        self.assertEqual(field_few, "perf-field-field-few")
        self.assertEqual((ra_few, dec_few), (10.0, 20.0))
        # Newest observation is base MJD 60000 + (n - 1); both are long past.
        now_mjd = view_utils.date_to_mjd(datetime.datetime.utcnow())
        self.assertAlmostEqual(since_few, now_mjd - 60001.0, places=1)
        self.assertAlmostEqual(since_many, now_mjd - 60039.0, places=1)

    def test_field_without_observations_and_no_field(self):
        t = create_minimal_transient(self.user, name="field-unobserved", ra=200.0, dec=45.0)
        attach_synthetic_survey_field(self.user, t, n_observations=0)
        field_id, _ra, _dec, since = t.LikelyYSEField()
        self.assertEqual(field_id, "perf-field-field-unobserved")
        self.assertIsNone(since)
        self.assertEqual(SurveyObservation.objects.count(), 0)

        nowhere = create_minimal_transient(self.user, name="field-none", ra=300.0, dec=-60.0)
        self.assertEqual(nowhere.LikelyYSEField(), (None, None, None, None))

    def test_separation_uses_loaded_host(self):
        t = create_minimal_transient(self.user, name="sep-host")
        attach_synthetic_host(self.user, t)
        t = Transient.objects.select_related("host").get(pk=t.pk)
        with self.assertNumQueries(0):
            self.assertEqual(t.Separation(), "0.00")


class DiscoveryMagnitudeTests(TestCase):
    def test_matches_legacy_walk(self):
        user = create_test_user("detail_summary_disc")
        with_disc = create_transient_with_synthetic_data(user, name="disc-yes", n_phot_points=6)
        no_disc = create_transient_with_synthetic_data(user, name="disc-no", n_phot_points=6)
        zero_mag = create_transient_with_synthetic_data(user, name="disc-zero", n_phot_points=4)
        no_mag = create_transient_with_synthetic_data(user, name="disc-nomag", n_phot_points=3)

        TransientPhotData.objects.filter(photometry__transient=no_disc).update(discovery_point=False)
        TransientPhotData.objects.filter(photometry__transient=zero_mag).update(discovery_point=False)
        latest_zero = TransientPhotData.objects.filter(
            photometry__transient=zero_mag
        ).order_by("-obs_date").first()
        TransientPhotData.objects.filter(pk=latest_zero.pk).update(mag=0)
        TransientPhotData.objects.filter(photometry__transient=no_mag).update(
            discovery_point=False, mag=None
        )

        for transient in (with_disc, no_disc, zero_mag, no_mag):
            qs = TransientPhotData.objects.filter(photometry__transient=transient)
            expected = _legacy_disc_mag(qs)
            got = view_utils.get_disc_mag_from_photdata(qs)
            self.assertEqual(getattr(got, "pk", None), getattr(expected, "pk", None), transient.name)

        qs = TransientPhotData.objects.filter(photometry__transient=with_disc)
        with self.assertNumQueries(1):
            view_utils.get_disc_mag_from_photdata(qs)


class GalacticFilterTests(TestCase):
    def test_columns_are_read_without_parsing(self):
        user = create_test_user("detail_summary_gal")
        t = create_minimal_transient(user, name="gal-cols", ra=10.0, dec=20.0)
        t.refresh_from_db()
        self.assertIsNotNone(t.gal_l)
        self.assertEqual(galcoordsl(t), "%.7f" % t.gal_l)
        self.assertEqual(galcoordsb(t), "%.7f" % t.gal_b)
        with mock.patch(
            "YSE_App.templatetags.transient_detail_extras.SkyCoord",
            side_effect=AssertionError("sexagesimal parse should not run"),
        ):
            galcoordsl(t)
            galcoordsb(t)

    def test_same_formatting_and_value_as_sexagesimal_parse(self):
        user = create_test_user("detail_summary_gal2")
        for i, (ra, dec) in enumerate([(10.0, 20.0), (266.4, -28.9), (150.1, 2.2)]):
            t = create_minimal_transient(user, name="gal-legacy-%d" % i, ra=ra, dec=dec)
            t.refresh_from_db()
            legacy_l = galcoordsl(t.CoordString())
            legacy_b = galcoordsb(t.CoordString())
            for text in (galcoordsl(t), galcoordsb(t)):
                self.assertRegex(text, r"^-?\d+\.\d{7}$")
            # Sexagesimal coordinates are rounded to 0.01 s / 0.01 arcsec, so the
            # stored (exact) value differs from the old parse only far below an arcsec.
            self.assertAlmostEqual(float(galcoordsl(t)), float(legacy_l), delta=5e-4)
            self.assertAlmostEqual(float(galcoordsb(t)), float(legacy_b), delta=5e-4)

    def test_null_columns_fall_back_to_computing(self):
        user = create_test_user("detail_summary_gal3")
        t = create_minimal_transient(user, name="gal-null", ra=10.0, dec=20.0)
        Transient.objects.filter(pk=t.pk).update(gal_l=None, gal_b=None)
        t.refresh_from_db()
        self.assertIsNone(t.gal_l)
        self.assertEqual(galcoordsl(t), galcoordsl(t.CoordString()))
        self.assertEqual(galcoordsb(t), galcoordsb(t.CoordString()))


class TagsListTests(TestCase):
    def test_returns_names_from_prefetch(self):
        user = create_test_user("detail_summary_tags")
        t = create_minimal_transient(user, name="tags-one")
        t = Transient.objects.prefetch_related("tags").get(pk=t.pk)
        with self.assertNumQueries(0):
            self.assertEqual(list(tags_list(t.tags)), [])


class TransientSummaryPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = create_test_user("detail_summary_page")
        self.client = Client()
        self.client.force_login(self.user)

    def _page_queries(self, status_name="New"):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/transient_summary/%s/" % status_name)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_queries_do_not_grow_with_transients(self):
        for i in range(2):
            t = create_minimal_transient(self.user, name="sum-a-%d" % i)
            attach_synthetic_host(self.user, t)
        n_two = self._page_queries()
        for i in range(2, 6):
            t = create_minimal_transient(self.user, name="sum-a-%d" % i)
            attach_synthetic_host(self.user, t)
        n_six = self._page_queries()
        self.assertEqual(n_two, n_six)


class SummaryPlotCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = create_test_user("detail_summary_plots")
        self.client = Client()
        self.client.force_login(self.user)
        self.transient = create_transient_with_synthetic_data(
            self.user, name="summary-plots", n_phot_points=8
        )

    def test_lightcurve_summary_second_request_is_cached(self):
        url = "/lightcurveplot_summary/%d/" % self.transient.id
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.content)
        with CaptureQueriesContext(connection) as ctx:
            second = self.client.get(url)
        self.assertEqual(second.content, first.content)
        self.assertLessEqual(len(ctx.captured_queries), 3)

    def test_spectrum_summary_second_request_is_cached(self):
        url = "/spectrumplot_summary/%d/" % self.transient.id
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            second = self.client.get(url)
        self.assertEqual(second.content, first.content)
        self.assertLessEqual(len(ctx.captured_queries), 4)


PS1_PAGE = (
    '<table><tr><td><img src="//ps1images.stsci.edu/cache/abc.jpg" '
    'width="240" height="240" /></td></tr></table>'
)


class Ps1ImageCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = create_test_user("detail_summary_ps1")
        self.client = Client()
        self.client.force_login(self.user)
        self.transient = create_minimal_transient(self.user, name="ps1-cache")
        self.url = "/get_ps1_image/%d" % self.transient.id

    def _response(self, text="", status=200):
        resp = mock.Mock()
        resp.status_code = status
        resp.content = text.encode("utf-8")
        return resp

    def test_found_url_is_fetched_once(self):
        with mock.patch.object(view_utils.requests, "get", return_value=self._response(PS1_PAGE)) as get:
            first = self.client.get(self.url).json()
            second = self.client.get(self.url).json()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["jpegurl"], "http://ps1images.stsci.edu/cache/abc.jpg")

    def test_failure_is_cached_only_briefly(self):
        with mock.patch.object(
            view_utils.requests, "get", side_effect=requests.Timeout("slow")
        ), mock.patch.object(view_utils.cache, "set") as cache_set:
            data = self.client.get(self.url).json()
        self.assertEqual(data, {"jpegurl": "", "msg": "timeout"})
        self.assertEqual(cache_set.call_args[0][2], view_utils.PS1_FAILURE_CACHE_SECONDS)
        self.assertLess(view_utils.PS1_FAILURE_CACHE_SECONDS, 600)

    def test_found_url_is_cached_long(self):
        with mock.patch.object(
            view_utils.requests, "get", return_value=self._response(PS1_PAGE)
        ), mock.patch.object(view_utils.cache, "set") as cache_set:
            self.client.get(self.url)
        self.assertGreaterEqual(cache_set.call_args[0][2], 7 * 24 * 3600)

    def test_unknown_transient_is_404(self):
        with mock.patch.object(view_utils.requests, "get") as get:
            response = self.client.get("/get_ps1_image/99999999")
        self.assertEqual(response.status_code, 404)
        get.assert_not_called()
