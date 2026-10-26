"""Exports and API filters (UX Tier 1: #808 SALT3 URLs, #809 DQ column + README, #810 query by id,
#811 API filter backends, #813 readable headers). Synthetic data only."""

import datetime
import io
import os
import zipfile
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from explorer.models import Query

from YSE_App.models import (
    DataAccessRule,
    DataQuality,
    TransientPhotData,
    TransientPhotometry,
    TransientSpecData,
    Unit,
)
from YSE_App.common.utilities import date_to_mjd
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

READER = "exports_ux_user"


def _results(response):
    data = response.json()
    return data["results"] if isinstance(data, dict) and "results" in data else data


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER])
class ExportsApiFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(READER)
        cls.t1 = create_minimal_transient(cls.user, name="2026uxa")
        cls.t2 = create_minimal_transient(cls.user, name="2026uxb")
        cls.phot1 = attach_synthetic_photometry(cls.user, cls.t1, n_points=4)
        cls.phot2 = attach_synthetic_photometry(cls.user, cls.t2, n_points=4)
        cls.spec1 = attach_synthetic_spectrum(cls.user, cls.t1)
        cls.spec2 = attach_synthetic_spectrum(cls.user, cls.t2)
        cls.spec1.spec_phase = 5.0
        cls.spec1.save()
        cls.spec2.spec_phase = 40.0
        cls.spec2.obs_date = timezone.now() - datetime.timedelta(days=100)
        cls.spec2.save()
        for spec in (cls.spec1, cls.spec2):
            TransientSpecData.objects.create(
                spectrum=spec, wavelength=5000.0, flux=1.0, flux_err=0.1, **audit_fields(cls.user)
            )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER])
class ApiFilterTests(ExportsApiFixture):
    def test_unfiltered_lists_are_unchanged(self):
        self.assertEqual(len(_results(self.client.get("/api/transientphotometry/"))), 2)
        self.assertEqual(len(_results(self.client.get("/api/transientphotdata/"))), 8)
        self.assertEqual(len(_results(self.client.get("/api/transientspectra/"))), 2)
        self.assertEqual(len(_results(self.client.get("/api/transientspecdata/"))), 2)

    def test_anonymous_still_refused(self):
        self.assertIn(Client().get("/api/transientphotdata/").status_code, (401, 403))

    def test_filter_by_transient_id_and_name(self):
        for url, n in (("transientphotometry", 1), ("transientphotdata", 4),
                       ("transientspectra", 1), ("transientspecdata", 1)):
            with self.subTest(url=url):
                by_id = _results(self.client.get("/api/%s/" % url, {"transient": self.t1.id}))
                by_name = _results(self.client.get("/api/%s/" % url, {"transient__name": self.t1.name}))
                self.assertEqual(len(by_id), n)
                self.assertEqual(len(by_name), n)
                self.assertEqual(len(_results(self.client.get("/api/%s/" % url, {"transient__name": "nope"}))), 0)

    def test_photdata_date_and_mjd_ranges(self):
        points = list(TransientPhotData.objects.filter(photometry=self.phot1).order_by("obs_date"))
        cut = points[2].obs_date
        got = _results(self.client.get(
            "/api/transientphotdata/", {"transient": self.t1.id, "obs_date__gte": cut.isoformat()}))
        self.assertEqual(len(got), 2)
        got = _results(self.client.get(
            "/api/transientphotdata/", {"transient": self.t1.id, "obs_date__lte": cut.isoformat()}))
        self.assertEqual(len(got), 3)
        mjd = float(date_to_mjd(cut))
        got = _results(self.client.get(
            "/api/transientphotdata/", {"transient": self.t1.id, "obs_mjd__gte": mjd - 0.001}))
        self.assertEqual(len(got), 2)
        got = _results(self.client.get(
            "/api/transientphotdata/", {"transient": self.t1.id, "obs_mjd__lte": mjd - 0.001}))
        self.assertEqual(len(got), 2)

    def test_photometry_header_date_range_matches_any_point(self):
        far_future = (timezone.now() + datetime.timedelta(days=30)).isoformat()
        self.assertEqual(len(_results(self.client.get(
            "/api/transientphotometry/", {"obs_date__gte": far_future}))), 0)
        self.assertEqual(len(_results(self.client.get(
            "/api/transientphotometry/", {"obs_date__lte": far_future}))), 2)

    def test_spectra_phase_and_date_ranges(self):
        for url in ("transientspectra", "transientspecdata"):
            with self.subTest(url=url):
                self.assertEqual(len(_results(self.client.get("/api/%s/" % url, {"spec_phase__gte": 10}))), 1)
                self.assertEqual(len(_results(self.client.get("/api/%s/" % url, {"spec_phase__lte": 10}))), 1)
                self.assertEqual(len(_results(self.client.get(
                    "/api/%s/" % url, {"spec_phase__gte": 1, "spec_phase__lte": 100}))), 2)
                recent = (timezone.now() - datetime.timedelta(days=10)).isoformat()
                self.assertEqual(len(_results(self.client.get("/api/%s/" % url, {"obs_date__gte": recent}))), 1)
        mjd = float(date_to_mjd(timezone.now() - datetime.timedelta(days=10)))
        self.assertEqual(len(_results(self.client.get("/api/transientspectra/", {"obs_mjd__lte": mjd}))), 1)

    def test_out_of_range_mjd_is_a_client_error(self):
        for url in ("transientphotometry", "transientphotdata", "transientspectra", "transientspecdata"):
            for param in ("obs_mjd__gte", "obs_mjd__lte"):
                for value in ("1e30", "-1e30", "99999999999"):
                    with self.subTest(url=url, param=param, value=value):
                        r = self.client.get("/api/%s/" % url, {param: value})
                        self.assertEqual(r.status_code, 400)
                        self.assertIn(param, r.json())


@override_settings(DATA_ACCESS_GRANTS_ENABLED=True)
class PhotometryHeaderPerPointFilterTests(TestCase):
    """A header's date-range match only considers the points the user may see (#811, per-point access)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("exports_ux_pp_admin", is_staff=True)
        cls.member = create_test_user("exports_ux_pp_member", is_staff=False)
        group, _ = Group.objects.get_or_create(name="exports-ux-pp-g")
        cls.member.groups.add(group)
        obs_group, instrument, band = create_instrument_stack(cls.admin, obs_group_name="exports-ux-pp")
        audit = audit_fields(cls.admin)
        transient = create_minimal_transient(cls.admin, name="2026uxpp")
        cls.header = TransientPhotometry.objects.create(
            transient=transient, instrument=instrument, obs_group=obs_group, **audit)
        cls.header.groups.add(group)
        now = timezone.now()
        cls.old_date = now - datetime.timedelta(days=100)
        cls.new_date = now - datetime.timedelta(days=1)
        cls.visible = TransientPhotData.objects.create(
            photometry=cls.header, band=band, mag=19.0, mag_err=0.05, obs_date=cls.old_date, **audit)
        cls.hidden = TransientPhotData.objects.create(
            photometry=cls.header, band=band, mag=18.0, mag_err=0.05, obs_date=cls.new_date, **audit)
        DataAccessRule.objects.create(
            transient_phot_data=cls.hidden, grantee_user=cls.member, effect="deny", granted_as="pi")

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.member)

    def _ids(self, params):
        # The serializer is hyperlinked (no "id" field): read the pk from each row's URL.
        r = self.client.get("/api/transientphotometry/", params)
        self.assertEqual(r.status_code, 200)
        return [int(str(row["url"]).rstrip("/").rsplit("/", 1)[-1]) for row in _results(r)]

    def test_header_is_listed_through_its_visible_point(self):
        self.assertIn(self.header.pk, self._ids({}))
        older = (self.old_date + datetime.timedelta(days=1)).isoformat()
        self.assertIn(self.header.pk, self._ids({"obs_date__lte": older}))

    def test_only_the_hidden_point_in_range_does_not_match(self):
        recent = (self.new_date - datetime.timedelta(hours=1)).isoformat()
        self.assertNotIn(self.header.pk, self._ids({"obs_date__gte": recent}))
        mjd = float(date_to_mjd(self.new_date)) - 0.01
        self.assertNotIn(self.header.pk, self._ids({"obs_mjd__gte": mjd}))


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER])
class Salt3UrlTests(ExportsApiFixture):
    def test_salt3_urls_resolve_to_the_plots(self):
        for name in ("salt3plot", "salt3fluxplot"):
            r = self.client.get(reverse(name, args=[self.t1.id, 0]))
            self.assertEqual(r.status_code, 200, name)

    def test_old_salt2_urls_redirect_permanently(self):
        for old, new in (("salt2plot", "salt3plot"), ("salt2fluxplot", "salt3fluxplot")):
            r = self.client.get(reverse(old, args=[self.t1.id, 1]) + "?width=400")
            self.assertEqual(r.status_code, 301)
            self.assertEqual(r["Location"], reverse(new, args=[self.t1.id, 1]) + "?width=400")

    def test_anonymous_is_sent_to_login(self):
        r = Client().get(reverse("salt2plot", args=[self.t1.id, 0]))
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("salt3plot", r["Location"])


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER])
class BulkPhotometryTests(ExportsApiFixture):
    def _zip(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return zipfile.ZipFile(io.BytesIO(r.content))

    def test_dq_column_and_readme_listing_skipped_points(self):
        dq = DataQuality.objects.create(name="exports-ux-dq", **audit_fields(self.user))
        points = list(TransientPhotData.objects.filter(photometry=self.phot1).order_by("obs_date"))
        points[0].data_quality.add(dq)
        # flux without a zero point and no magnitude: cannot be exported
        points[1].mag = None
        points[1].mag_err = None
        points[1].flux = 12.0
        points[1].flux_zero_point = None
        points[1].save()
        # flux without a zero point but with a magnitude: skipped as before, reason says so
        points[2].flux = 15.0
        points[2].flux_zero_point = None
        points[2].save()
        query = Query.objects.create(title="Search: status New; tags young", sql="select 1",
                                     created_by_user=self.user)
        with mock.patch("YSE_App.views.run_explorer_query_cached", return_value=[self.t1.name]):
            archive = self._zip(reverse("download_bulk_photometry_by_id", args=[query.id]))
        names = archive.namelist()
        self.assertIn("README.txt", names)
        self.assertIn(self.t1.name + ".csv", names)
        csv_lines = archive.read(self.t1.name + ".csv").decode().splitlines()
        header = [ln for ln in csv_lines if ln.startswith("MJD,")][0]
        self.assertTrue(header.endswith(",DQ"))
        rows = [ln for ln in csv_lines if ln and not ln.startswith(("#", "MJD,"))]
        self.assertEqual(len(rows), 2)  # four points, two skipped
        self.assertTrue(any(ln.endswith(",exports-ux-dq") for ln in rows))
        readme = archive.read("README.txt").decode()
        self.assertIn("Points skipped (no usable flux, or flux without a flux zero point): 2", readme)
        self.assertIn(self.t1.name, readme)
        self.assertIn("flux without a flux zero point and no magnitude", readme)
        self.assertIn("flux without a flux zero point (magnitude present but not used with flux)", readme)

    def test_by_id_unknown_is_404_and_old_title_url_still_works(self):
        self.assertEqual(
            self.client.get(reverse("download_bulk_photometry_by_id", args=[999999])).status_code, 404)
        Query.objects.create(title="old_style_title", sql="select 1", created_by_user=self.user)
        with mock.patch("YSE_App.views.run_explorer_query_cached", return_value=[self.t2.name]):
            archive = self._zip(reverse("download_bulk_photometry", args=["old_style_title"]))
        self.assertIn(self.t2.name + ".csv", archive.namelist())

    def test_dashboard_panel_links_the_download_by_id(self):
        from YSE_App.views import _pd_download_url
        query = Query.objects.create(title="Any title: with spaces", sql="select 1", created_by_user=self.user)
        uq = mock.Mock(query_id=query.id, python_query=None)
        self.assertEqual(_pd_download_url(uq), reverse("download_bulk_photometry_by_id", args=[query.id]))

    def test_dashboard_page_renders_the_download_button_deferred_and_not(self):
        from YSE_App.models import UserQuery
        query = Query.objects.create(title="Dash dl", sql="select 1", created_by_user=self.user)
        UserQuery.objects.create(user=self.user, query=query, **audit_fields(self.user))
        url = reverse("download_bulk_photometry_by_id", args=[query.id])
        for defer in ("1", "0"):
            with self.subTest(defer=defer), mock.patch.dict(os.environ, {"YSE_PERSONAL_DASHBOARD_DEFER": defer}), \
                    mock.patch("YSE_App.views.dashboard_sql_is_supported", return_value=True), \
                    mock.patch("YSE_App.views.run_explorer_query_cached", return_value=[]):
                response = self.client.get(reverse("personaldashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertIn(url, response.content.decode())


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER])
class ReadableHeaderTests(ExportsApiFixture):
    def test_snana_header_uses_readable_labels(self):
        r = self.client.get(reverse("download_photometry", args=[self.t1.slug]))
        self.assertEqual(r.status_code, 200)
        text = r.content.decode()
        # old upper-case keys are kept for existing parsers
        self.assertIn("# NAME: %s" % self.t1.name, text)
        for key in ("RA", "DEC", "REDSHIFT", "MW_EBV", "NON_DETECT_DATE", "NON_DETECT_LIMIT",
                    "NON_DETECT_BAND", "TNS_SPEC_CLASS"):
            self.assertIn("\n# %s: " % key, text)
        # one legend block explains them
        self.assertIn("# Header keys (legend)\n", text)
        self.assertIn("#   RA = right ascension (deg)\n", text)
        self.assertIn("#   NON_DETECT_LIMIT = limiting magnitude of the last non-detection (mag)\n", text)
        legend = [ln for ln in text.splitlines() if ln.startswith("#   ")]
        self.assertTrue(legend)
        self.assertFalse(any(":" in ln for ln in legend))
        self.assertNotIn("CREATED_BY", text.upper())
        self.assertNotIn("CANDIDATE_HOSTS", text.upper())
        self.assertIn("VARLIST:  MJD        FLT  FLUXCAL", text)

    def test_spectra_header_has_no_database_ids(self):
        unit = Unit.objects.create(name="exports-ux-flam", **audit_fields(self.user))
        self.spec1.unit = unit
        self.spec1.wavelength_medium = "vacuum"
        self.spec1.host_subtraction = "template"
        self.spec1.redshift_quality = True
        self.spec1.save()
        r = self.client.get(reverse("download_spectra", args=[self.t1.slug]))
        self.assertEqual(r.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        content = archive.read(archive.namelist()[0]).decode()
        self.assertIn("# Instrument:", content)
        self.assertIn("# Phase (days): 5.0", content)
        from YSE_App.common.utc_dates import utc_day
        self.assertIn("# Observation date (UTC): %s\n" % utc_day(self.spec1.obs_date), content)
        self.assertIn("# Wavelength medium: Vacuum\n", content)
        self.assertIn("# Host subtraction: Template\n", content)
        self.assertIn("# Quality mask: ", content)
        self.assertIn("# Redshift quality: True\n", content)
        self.assertIn("# Flux unit: exports-ux-flam\n", content)
        header = [ln for ln in content.splitlines() if ln.startswith("#")]
        self.assertFalse(any("_ID" in ln.upper() for ln in header))
        self.assertIn("wavelength,flux,fluxerr", content)
