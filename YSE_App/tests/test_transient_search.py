"""The shared transient search filters (``TransientSearchFilterSet``, #284 / #285 / #286).

One fixture, one assertion per filter: the cone (bounding box plus exact
separation, decimal or sexagesimal centre), galactic latitude (checked against
astropy), names and aliases, TNS-style names, time windows, the photometry
filters on the stored ``TransientPhotStat`` row, classification and
non-classification, redshift (own or host), status / group / survey, tags
(any and all), spectrum / follow-up / comment / host relations and
``visible_to_group``; that each filter alone costs one query; that the search
page and ``/api/transients/`` accept the same parameters and return the same
rows; that the legacy API names still work; and that the page's query count
does not grow with the number of rows.
"""

import datetime
import re

from django.contrib.auth.models import Group
from django.db import connection
from django.http import QueryDict
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from YSE_App.filters.transient_search import (
    DEFAULT_RADIUS_ARCSEC,
    TransientSearchFilterSet,
    annotate_gal_b,
    parse_coordinate_pair,
    parse_quick_search,
    quick_search_params,
)
from YSE_App.models import (
    AlternateTransientNames,
    FollowupStatus,
    InternalSurvey,
    Log,
    Transient,
    TransientClass,
    TransientFollowup,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
    TransientSpectrum,
    TransientTag,
    WebAppColor,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.js_syntax_utils import html_script_problems

PRIVATE_GROUP = "search-private-group"


def _point(photometry, band, days_ago, *, mag=None, flux=None, flux_err=None, zp=None, user=None):
    return TransientPhotData.objects.create(
        photometry=photometry, band=band,
        obs_date=timezone.now() - datetime.timedelta(days=days_ago),
        mag=mag, mag_err=0.05 if mag is not None else None,
        flux=flux, flux_err=flux_err, flux_zero_point=zp, **audit_fields(user),
    )


class SearchFixture(TestCase):
    """Five transients that differ in every filtered property."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("search_staff")
        cls.member = create_test_user("search_member", is_staff=False)
        cls.outsider = create_test_user("search_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=PRIVATE_GROUP)
        cls.member.groups.add(cls.group)
        audit = audit_fields(cls.user)
        cls.audit = audit
        now = timezone.now()

        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.user, obs_group_name="YSE")
        other_group, _, _ = create_instrument_stack(cls.user, obs_group_name="ZTF")
        survey, _ = InternalSurvey.objects.get_or_create(name="YSE", defaults=audit)
        cls.sn_ia, _ = TransientClass.objects.get_or_create(name="SN Ia", defaults=audit)
        cls.sn_ii, _ = TransientClass.objects.get_or_create(name="SN II", defaults=audit)
        color, _ = WebAppColor.objects.get_or_create(color="blue", defaults=audit)
        cls.tag_young, _ = TransientTag.objects.get_or_create(name="young", defaults={"color": color, **audit})
        cls.tag_host, _ = TransientTag.objects.get_or_create(name="has-host", defaults={"color": color, **audit})
        cls.tag_old, _ = TransientTag.objects.get_or_create(name="old", defaults={"color": color, **audit})
        requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)

        # A: the "everything" transient.
        a = create_minimal_transient(cls.user, name="2026sea", status_name="Following", obs_group_name="YSE", ra=10.0, dec=20.0)
        a.disc_date = now - datetime.timedelta(days=10)
        a.redshift = 0.02
        a.best_spec_class = cls.sn_ia
        a.internal_survey = survey
        a.save()
        a.tags.add(cls.tag_young, cls.tag_host)
        attach_synthetic_host(cls.user, a)  # host redshift 0.05
        TransientSpectrum.objects.create(
            transient=a, instrument=cls.instrument, obs_group=cls.obs_group, ra=a.ra, dec=a.dec,
            obs_date=now, **audit)
        Log.objects.create(transient=a, comment="worth a look", **audit)
        TransientFollowup.objects.create(
            transient=a, status=requested, valid_start=now, valid_stop=now + datetime.timedelta(days=3), **audit)
        phot = TransientPhotometry.objects.create(transient=a, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        _point(phot, cls.band, 12, flux=10.0, flux_err=20.0, zp=27.5, user=cls.user)  # limit 22.89
        for days_ago, mag in ((9, 18.5), (7, 18.0), (5, 17.5), (3, 17.0), (1, 17.4)):
            _point(phot, cls.band, days_ago, mag=mag, user=cls.user)
        cls.a = a

        # B: 34" east of A, internal name with a ZTF alias, photometric class only, faint and old.
        b = create_minimal_transient(cls.user, name="PS26abc", status_name="New", obs_group_name="ZTF", ra=10.01, dec=20.0)
        b.disc_date = now - datetime.timedelta(days=100)
        b.photo_class = cls.sn_ii
        b.save()
        b.tags.add(cls.tag_old)
        AlternateTransientNames.objects.create(transient=b, name="ZTF26aaaaaaa", obs_group=other_group, **audit)
        phot = TransientPhotometry.objects.create(transient=b, instrument=cls.instrument, obs_group=other_group, **audit)
        _point(phot, cls.band, 50, mag=20.5, user=cls.user)
        _point(phot, cls.band, 48, mag=20.6, user=cls.user)
        cls.b = b

        # C: in the galactic plane, spectroscopic SN II, no photometry at all.
        c = create_minimal_transient(cls.user, name="2026zzz", status_name="Watch", obs_group_name="YSE", ra=266.4, dec=-28.9)
        c.best_spec_class = cls.sn_ii
        c.save()
        cls.c = c

        # D: photometry and a spectrum shared only with the private group.
        d = create_minimal_transient(cls.user, name="2026prv", status_name="Ignore", obs_group_name="YSE", ra=100.0, dec=-40.0)
        phot = TransientPhotometry.objects.create(transient=d, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        phot.groups.add(cls.group)
        _point(phot, cls.band, 2, mag=19.0, user=cls.user)
        spec = TransientSpectrum.objects.create(
            transient=d, instrument=cls.instrument, obs_group=cls.obs_group, ra=d.ra, dec=d.dec,
            obs_date=now, **audit)
        spec.groups.add(cls.group)
        cls.d = d

        # E: public photometry near D, redshift only from the host.
        e = create_minimal_transient(cls.user, name="2026pub", status_name="New", obs_group_name="YSE", ra=100.1, dec=-40.0)
        host = attach_synthetic_host(cls.user, e)
        host.redshift = 0.3
        host.save()
        phot = TransientPhotometry.objects.create(transient=e, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        _point(phot, cls.band, 4, mag=19.5, user=cls.user)
        cls.e = e

        cls.all_names = {"2026sea", "PS26abc", "2026zzz", "2026prv", "2026pub"}
        cls.factory = RequestFactory()

    # ------------------------------------------------------------------
    def filterset(self, params, user=None):
        request = self.factory.get("/search/", params)
        request.user = user or self.user
        return TransientSearchFilterSet(request.GET, queryset=Transient.objects.all(), request=request)

    def names(self, params, user=None):
        fs = self.filterset(params, user=user)
        self.assertTrue(fs.is_valid(), fs.errors)
        return set(fs.qs.values_list("name", flat=True))

    def assertNames(self, params, expected, user=None):
        self.assertEqual(self.names(params, user=user), set(expected), params)


class FixtureSanityTests(SearchFixture):
    def test_phot_stats_exist_for_the_photometry(self):
        stat = TransientPhotStat.objects.get(transient=self.a)
        self.assertAlmostEqual(stat.peak_mag, 17.0)
        self.assertEqual(stat.num_det_global, 5)
        self.assertAlmostEqual(stat.rise_rate, 0.25, places=2)
        self.assertAlmostEqual(stat.decay_rate, 0.2, places=2)
        self.assertAlmostEqual(stat.deepest_limit, 22.89, places=2)
        self.assertFalse(TransientPhotStat.objects.filter(transient=self.c).exists())

    def test_no_filter_returns_everything(self):
        self.assertNames({}, self.all_names)


class PositionFilterTests(SearchFixture):
    def test_cone_search_decimal(self):
        self.assertNames({"ra": "10.0", "dec": "20.0", "radius_arcsec": "5"}, {"2026sea"})
        self.assertNames({"ra": "10.0", "dec": "20.0", "radius_arcsec": "60"}, {"2026sea", "PS26abc"})

    def test_cone_search_default_radius_and_sexagesimal(self):
        self.assertEqual(DEFAULT_RADIUS_ARCSEC, 5.0)
        self.assertNames({"ra": "10.0", "dec": "20.0"}, {"2026sea"})
        self.assertNames({"ra": "00:40:00", "dec": "+20:00:00", "radius_arcsec": "60"}, {"2026sea", "PS26abc"})
        self.assertNames({"ra": "0h40m00s", "dec": "20d00m00s", "radius_arcsec": "60"}, {"2026sea", "PS26abc"})

    def test_cone_search_annotates_and_orders_by_separation(self):
        fs = self.filterset({"ra": "10.0", "dec": "20.0", "radius_arcsec": "60"})
        rows = list(fs.qs.values_list("name", "separation"))
        self.assertEqual([r[0] for r in rows], ["2026sea", "PS26abc"])
        self.assertAlmostEqual(rows[0][1] * 3600, 0.0, places=3)
        self.assertAlmostEqual(rows[1][1] * 3600, 33.8, delta=0.2)
        fs = self.filterset({"ra": "10.0", "dec": "20.0", "radius_arcsec": "60", "ordering": "-separation"})
        self.assertEqual(list(fs.qs.values_list("name", flat=True)), ["PS26abc", "2026sea"])

    def test_cone_search_across_the_ra_wrap(self):
        wrap = create_minimal_transient(self.user, name="2026wrap", ra=359.9995, dec=5.0)
        self.assertNames({"ra": "0.0005", "dec": "5.0", "radius_arcsec": "10"}, {"2026wrap"})
        self.assertNames({"ra": "359.9995", "dec": "5.0", "radius_arcsec": "1"}, {"2026wrap"})
        wrap.delete()

    def test_cone_search_needs_both_coordinates(self):
        fs = self.filterset({"ra": "10.0"})
        self.assertFalse(fs.is_valid())
        self.assertIn("both RA and Dec", str(fs.errors))
        fs = self.filterset({"ra": "not", "dec": "coords"})
        self.assertFalse(fs.is_valid())

    def test_ordering_by_separation_without_a_cone_is_ignored(self):
        self.assertNames({"ordering": "separation"}, self.all_names)

    def test_ra_dec_ranges(self):
        self.assertNames({"ra_gte": "9", "ra_lte": "11"}, {"2026sea", "PS26abc"})
        self.assertNames({"dec_lte": "-30"}, {"2026prv", "2026pub"})

    def test_galactic_latitude_matches_astropy(self):
        from astropy.coordinates import SkyCoord

        rows = annotate_gal_b(Transient.objects.filter(name__in=self.all_names)).values_list("ra", "dec", "gal_b")
        self.assertEqual(len(rows), 5)
        for ra, dec, gal_b in rows:
            expected = SkyCoord(ra, dec, unit="deg").galactic.b.deg
            self.assertAlmostEqual(gal_b, expected, places=3, msg=(ra, dec))

    def test_galactic_latitude_filters(self):
        # C sits about 0.05 deg from the galactic centre.
        self.assertNames({"gal_b_abs_max": "5"}, {"2026zzz"})
        self.assertNames({"gal_b_abs_min": "10"}, self.all_names - {"2026zzz"})
        fs = self.filterset({"ordering": "gal_b"})
        values = list(fs.qs.values_list("gal_b", flat=True))
        self.assertEqual(values, sorted(values))
        self.assertEqual(list(fs.qs.values_list("name", flat=True))[-1], "2026zzz")  # largest b (+0.02)


class NameFilterTests(SearchFixture):
    def test_name_exact_and_contains(self):
        self.assertNames({"name": "2026sea"}, {"2026sea"})
        self.assertNames({"name": "2026se"}, set())
        self.assertNames({"name_contains": "26"}, self.all_names)
        self.assertNames({"name_contains": "SEA"}, {"2026sea"})

    def test_alias_matches_name_or_alternate_name(self):
        self.assertNames({"alias": "ztf26"}, {"PS26abc"})
        self.assertNames({"alias": "sea"}, {"2026sea"})
        self.assertNames({"alias": "nomatch"}, set())

    def test_has_tns_name(self):
        self.assertNames({"has_tns_name": "true"}, self.all_names - {"PS26abc"})
        self.assertNames({"has_tns_name": "false"}, {"PS26abc"})


class TimeFilterTests(SearchFixture):
    def test_discovery_date_window(self):
        cut = (timezone.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        self.assertNames({"disc_date_after": cut}, {"2026sea"})
        self.assertNames({"disc_date_before": cut}, {"PS26abc"})
        self.assertNames({"days_since_disc_max": "30"}, {"2026sea"})
        self.assertNames({"days_since_disc_max": "365"}, {"2026sea", "PS26abc"})

    def test_created_and_modified_windows_accept_dates_and_iso_datetimes(self):
        yesterday = (timezone.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertNames({"created_after": yesterday}, self.all_names)
        self.assertNames({"created_before": yesterday}, set())
        self.assertNames({"modified_after": yesterday + "T00:00:00Z"}, self.all_names)
        self.assertNames({"modified_before": "2000-01-01"}, set())
        # legacy API names
        self.assertNames({"created_date_gte": yesterday}, self.all_names)
        self.assertNames({"modified_date_gte": "2099-01-01T00:00:00Z"}, set())


class PhotometryFilterTests(SearchFixture):
    def test_peak_and_latest_magnitude(self):
        self.assertNames({"peak_mag_max": "18"}, {"2026sea"})
        self.assertNames({"peak_mag_min": "20"}, {"PS26abc"})
        self.assertNames({"latest_mag_max": "19.2"}, {"2026sea", "2026prv"})
        self.assertNames({"latest_mag_min": "20"}, {"PS26abc"})
        # legacy names
        self.assertNames({"peak_mag_lte": "18"}, {"2026sea"})
        self.assertNames({"last_det_mag_gte": "20"}, {"PS26abc"})

    def test_detection_count(self):
        self.assertNames({"num_det_min": "3"}, {"2026sea"})
        self.assertNames({"num_det_max": "2"}, {"PS26abc", "2026prv", "2026pub"})
        self.assertNames({"num_det_gte": "5"}, {"2026sea"})

    def test_detection_dates_and_recency(self):
        cut = (timezone.now() - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
        self.assertNames({"first_det_after": cut}, {"2026sea", "2026prv", "2026pub"})
        self.assertNames({"first_det_before": cut}, {"PS26abc"})
        self.assertNames({"last_det_before": cut}, {"PS26abc"})
        self.assertNames({"last_det_after": cut, "days_since_last_det_max": "3"}, {"2026sea", "2026prv"})

    def test_rates_and_deepest_limit(self):
        self.assertNames({"rise_rate_min": "0.2"}, {"2026sea"})
        self.assertNames({"rise_rate_max": "0.1"}, set())
        self.assertNames({"decay_rate_min": "0.1", "decay_rate_max": "0.3"}, {"2026sea"})
        self.assertNames({"deepest_limit_min": "22"}, {"2026sea"})
        self.assertNames({"deepest_limit_max": "22"}, set())
        self.assertNames({"rise_rate_gte": "0.2"}, {"2026sea"})


class ClassificationFilterTests(SearchFixture):
    def test_spec_phot_either_and_exclude(self):
        self.assertNames({"spec_class": "SN Ia"}, {"2026sea"})
        self.assertNames({"photo_class": "SN II"}, {"PS26abc"})
        self.assertNames({"classification": "SN II"}, {"PS26abc", "2026zzz"})
        self.assertNames({"classification": ["SN II", "SN Ia"]}, {"2026sea", "PS26abc", "2026zzz"})
        self.assertNames({"exclude_class": "SN II"}, {"2026sea", "2026prv", "2026pub"})
        self.assertNames({"exclude_class": ["SN II", "SN Ia"]}, {"2026prv", "2026pub"})

    def test_unknown_class_is_a_form_error_not_a_crash(self):
        fs = self.filterset({"spec_class": "SN Nope"})
        self.assertFalse(fs.is_valid())

    def test_redshift_own_or_host(self):
        self.assertNames({"has_redshift": "true"}, {"2026sea", "2026pub"})
        self.assertNames({"has_redshift": "false"}, {"PS26abc", "2026zzz", "2026prv"})
        self.assertNames({"redshift_min": "0.015", "redshift_max": "0.1"}, {"2026sea"})
        self.assertNames({"redshift_min": "0.2"}, {"2026pub"})
        fs = self.filterset({"ordering": "-best_redshift", "has_redshift": "true"})
        self.assertEqual(list(fs.qs.values_list("name", flat=True)), ["2026pub", "2026sea"])


class RelationFilterTests(SearchFixture):
    def test_status_group_and_survey(self):
        self.assertNames({"status": "New"}, {"PS26abc", "2026pub"})
        self.assertNames({"status": ["New", "Watch"]}, {"PS26abc", "2026pub", "2026zzz"})
        self.assertNames({"status_in": "New,Watch"}, {"PS26abc", "2026pub", "2026zzz"})
        self.assertNames({"obs_group": "ZTF"}, {"PS26abc"})
        self.assertNames({"internal_survey": "YSE"}, {"2026sea"})

    def test_tags_any_all_and_legacy(self):
        self.assertNames({"tags": "young"}, {"2026sea"})
        self.assertNames({"tags": ["young", "old"]}, {"2026sea", "PS26abc"})
        self.assertNames({"tags_all": ["young", "has-host"]}, {"2026sea"})
        self.assertNames({"tags_all": ["young", "old"]}, set())
        self.assertNames({"tag_in": "young,old"}, {"2026sea", "PS26abc"})

    def test_spectrum_respects_group_access(self):
        self.assertNames({"has_spectrum": "true"}, {"2026sea", "2026prv"})
        self.assertNames({"has_spectrum": "true"}, {"2026sea", "2026prv"}, user=self.member)
        self.assertNames({"has_spectrum": "true"}, {"2026sea"}, user=self.outsider)
        self.assertNames({"has_spectrum": "false"}, {"PS26abc", "2026zzz", "2026prv", "2026pub"}, user=self.outsider)

    def test_followup_comment_host(self):
        self.assertNames({"has_followup": "true"}, {"2026sea"})
        self.assertNames({"has_followup": "false"}, self.all_names - {"2026sea"})
        self.assertNames({"followup_status": "Requested"}, {"2026sea"})
        self.assertNames({"has_comment": "true"}, {"2026sea"})
        self.assertNames({"has_host": "true"}, {"2026sea", "2026pub"})
        self.assertNames({"has_host": "false"}, {"PS26abc", "2026zzz", "2026prv"})
        self.assertNames({"host_redshift_min": "0.1"}, {"2026pub"})
        self.assertNames({"host_redshift_max": "0.1"}, {"2026sea"})

    def test_visible_to_group(self):
        self.assertNames({"visible_to_group": PRIVATE_GROUP}, {"2026prv"})
        self.assertNames({"visible_to_group": PRIVATE_GROUP}, {"2026prv"}, user=self.member)
        self.assertNames({"visible_to_group": PRIVATE_GROUP}, set(), user=self.outsider)
        self.assertNames({"visible_to_group": "no-such-group"}, set())
        fs = TransientSearchFilterSet(QueryDict("visible_to_group=%s" % PRIVATE_GROUP), queryset=Transient.objects.all())
        self.assertEqual(list(fs.qs), [])


class QueryCountTests(SearchFixture):
    ONE_QUERY_PARAMS = (
        {"ra": "10.0", "dec": "20.0", "radius_arcsec": "60", "ordering": "separation"},
        {"gal_b_abs_min": "10", "ordering": "-gal_b"},
        {"alias": "ztf"},
        {"has_tns_name": "true"},
        {"disc_date_after": "2020-01-01"},
        {"peak_mag_max": "19", "num_det_min": "1", "days_since_last_det_max": "30"},
        {"classification": "SN II"},
        {"exclude_class": "SN II"},
        {"redshift_min": "0.01"},
        {"status": ["New", "Watch"]},
        {"tags": ["young", "old"]},
        {"tags_all": ["young", "has-host"]},
        {"has_spectrum": "true"},
        {"has_followup": "true", "has_comment": "true", "has_host": "true"},
        {"visible_to_group": PRIVATE_GROUP},
        {"ra": "10.0", "dec": "20.0", "radius_arcsec": "60", "gal_b_abs_min": "1", "has_spectrum": "true",
         "tags": "young", "status": "Following", "peak_mag_max": "19", "redshift_max": "1", "ordering": "-peak_mag"},
    )

    def test_each_filter_is_one_query(self):
        for params in self.ONE_QUERY_PARAMS:
            fs = self.filterset(params)
            self.assertTrue(fs.is_valid(), fs.errors)
            qs = fs.qs
            with self.assertNumQueries(1, msg=params):
                list(qs.values_list("pk", flat=True))

    def test_non_staff_has_spectrum_is_one_query(self):
        fs = self.filterset({"has_spectrum": "true"}, user=self.outsider)
        self.assertTrue(fs.is_valid())
        qs = fs.qs
        with self.assertNumQueries(1):
            list(qs.values_list("pk", flat=True))


class QuickSearchTests(TestCase):
    def test_parse_quick_search(self):
        self.assertEqual(parse_quick_search("2026abc"), ("name", "2026abc"))
        self.assertEqual(parse_quick_search("  "), ("name", ""))
        kind, ra, dec, radius = parse_quick_search("10.5 -20.25")
        self.assertEqual((kind, ra, dec, radius), ("cone", 10.5, -20.25, DEFAULT_RADIUS_ARCSEC))
        kind, ra, dec, radius = parse_quick_search("10.5, -20.25, 30")
        self.assertEqual((kind, radius), ("cone", 30.0))
        kind, ra, dec, radius = parse_quick_search("00:42:00 +41:16:00")
        self.assertEqual(kind, "cone")
        self.assertAlmostEqual(ra, 10.5, places=4)
        self.assertAlmostEqual(dec, 41.2667, places=3)
        kind, ra, dec, radius = parse_quick_search("00 42 00 +41 16 00 12")
        self.assertEqual((kind, radius), ("cone", 12.0))
        self.assertAlmostEqual(ra, 10.5, places=4)
        self.assertEqual(parse_quick_search("SN 2026abc")[0], "name")
        self.assertEqual(parse_quick_search("10.5 95.0"), ("name", "10.5 95.0"))

    def test_parse_coordinate_pair(self):
        self.assertEqual(parse_coordinate_pair("370", "10"), (10.0, 10.0))
        with self.assertRaises(ValueError):
            parse_coordinate_pair("10", "100")
        with self.assertRaises(ValueError):
            parse_coordinate_pair("abc", "def")

    def test_quick_search_params_expands_q_without_overriding_explicit_values(self):
        data = quick_search_params(QueryDict("q=10.0 20.0"))
        self.assertEqual((data["ra"], data["dec"], data["radius_arcsec"]), ("10.000000", "20.000000", "5"))
        data = quick_search_params(QueryDict("q=10.0 20.0&radius_arcsec=30"))
        self.assertEqual(data["radius_arcsec"], "30")
        data = quick_search_params(QueryDict("q=2026abc"))
        self.assertEqual(data["name_contains"], "2026abc")
        self.assertNotIn("ra", data)
        data = quick_search_params(QueryDict("name_contains=x"))
        self.assertEqual(data["name_contains"], "x")


class SearchPageTests(SearchFixture):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _names_in_table(self, html):
        """Names linked from the results table (the links carry the lower-case slug)."""
        slugs = re.findall(r'/transient_detail/([^/"]+)/', html)
        by_slug = {t.slug: t.name for t in Transient.objects.filter(slug__in=slugs)}
        return set(by_slug[s] for s in slugs)

    def _order_in_table(self, html):
        slugs = re.findall(r'/transient_detail/([^/"]+)/', html)
        by_slug = {t.slug: t.name for t in Transient.objects.filter(slug__in=slugs)}
        return [by_slug[s] for s in slugs]

    def test_page_renders_form_results_and_chips(self):
        response = self.client.get("/search/?status=New&status=Watch&has_spectrum=false")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="transient-search-form"', html)
        self.assertIn('id="search-active-filters"', html)
        self.assertIn("3 matches", html)
        self.assertEqual(self._names_in_table(html), {"2026zzz", "2026pub", "PS26abc"})
        self.assertIn('id="search-group-relations"', html)
        self.assertIn('badge badge-primary">2<', html)  # two active filters in Relations
        self.assertIn("/api/transients/?", html)
        self.assertEqual(html_script_problems(html), [])
        # the status dropdown has choices
        self.assertIn('class="transientStatusChange"', html)

    def test_header_quick_search_name_and_cone(self):
        html = self.client.get("/search/?q=PS26").content.decode()
        self.assertEqual(self._names_in_table(html), {"PS26abc"})
        html = self.client.get("/search/?q=10.0+20.0+60").content.decode()
        self.assertEqual(self._names_in_table(html), {"2026sea", "PS26abc"})
        self.assertIn('Sep. (arcsec)', html)
        self.assertIn("33.8", html)
        html = self.client.get("/search/?q=10.0+20.0").content.decode()
        self.assertEqual(self._names_in_table(html), {"2026sea"})

    def test_empty_search_lists_everything_without_a_separation_column(self):
        html = self.client.get("/search/").content.decode()
        self.assertEqual(self._names_in_table(html), self.all_names)
        self.assertNotIn('Sep. (arcsec)', html)
        self.assertIn("5 matches", html)

    def test_invalid_input_shows_an_error_and_still_renders(self):
        response = self.client.get("/search/?ra=10")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="search-errors"', html)
        self.assertIn("both RA and Dec", html)
        response = self.client.get("/search/?peak_mag_max=bright&status=Nope&per_page=7&page=99")
        self.assertEqual(response.status_code, 200)

    def test_table_sort_and_per_page_from_the_query_string(self):
        html = self.client.get("/search/?sort=-peak_mag&per_page=50").content.decode()
        order = self._order_in_table(html)
        self.assertEqual(order[0], "PS26abc")  # peak 20.5 is the largest number
        self.assertIn('<option value="50" selected>', html)
        html = self.client.get("/search/?ordering=name").content.decode()
        order = self._order_in_table(html)
        self.assertEqual(order, sorted(order))

    def test_legacy_parameters_are_carried_as_hidden_inputs(self):
        html = self.client.get("/search/?status_in=New,Watch").content.decode()
        self.assertIn('<input type="hidden" name="status_in" value="New,Watch">', html)
        self.assertEqual(self._names_in_table(html), {"2026zzz", "2026pub", "PS26abc"})

    def test_query_count_does_not_grow_with_rows(self):
        def count(url):
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        few = count("/search/?status=Following")
        for i in range(12):
            t = create_minimal_transient(self.user, name="2026bulk%02d" % i, status_name="Following", ra=50.0 + i, dec=1.0)
            t.tags.add(self.tag_young)
        many = count("/search/?status=Following")
        self.assertEqual(many, few)
        self.assertLessEqual(few, 25)

    def test_login_required(self):
        response = Client().get("/search/?q=2026")
        self.assertEqual(response.status_code, 302)


class ApiParityTests(SearchFixture):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def api_names(self, query):
        response = self.client.get("/api/transients/?" + query)
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        return [r["name"] for r in rows]

    def test_api_and_page_return_the_same_cone(self):
        api = self.api_names("ra=10.0&dec=20.0&radius_arcsec=60")
        html = self.client.get("/search/?ra=10.0&dec=20.0&radius_arcsec=60").content.decode()
        slugs = re.findall(r'/transient_detail/([^/"]+)/', html)
        by_slug = {t.slug: t.name for t in Transient.objects.filter(slug__in=slugs)}
        self.assertEqual(api, [by_slug[s] for s in slugs])
        self.assertEqual(api, ["2026sea", "PS26abc"])

    def test_api_accepts_every_filter_family(self):
        self.assertEqual(set(self.api_names("has_spectrum=true&has_tns_name=true")), {"2026sea", "2026prv"})
        self.assertEqual(self.api_names("gal_b_abs_max=5"), ["2026zzz"])
        self.assertEqual(self.api_names("classification=SN%20II&ordering=name"), ["2026zzz", "PS26abc"])
        self.assertEqual(self.api_names("tags_all=young&tags_all=has-host"), ["2026sea"])
        self.assertEqual(self.api_names("visible_to_group=%s" % PRIVATE_GROUP), ["2026prv"])
        self.assertEqual(self.api_names("alias=ztf26"), ["PS26abc"])

    def test_api_legacy_parameters_still_work(self):
        self.assertEqual(set(self.api_names("status_in=New,Watch")), {"PS26abc", "2026pub", "2026zzz"})
        self.assertEqual(self.api_names("tag_in=young"), ["2026sea"])
        self.assertEqual(self.api_names("name=2026sea"), ["2026sea"])
        self.assertEqual(self.api_names("peak_mag_lte=18"), ["2026sea"])
        self.assertEqual(self.api_names("ra_gte=9&ra_lte=11&dec_gte=19&dec_lte=21&ordering=-peak_mag"), ["PS26abc", "2026sea"])
        self.assertEqual(self.api_names("created_date_gte=2020-01-01T00:00:00Z&num_det_gte=5"), ["2026sea"])

    def test_api_rejects_bad_input_with_400(self):
        response = self.client.get("/api/transients/?ra=10")
        self.assertEqual(response.status_code, 400)
        self.assertIn("both RA and Dec", response.content.decode())
        response = self.client.get("/api/transients/?status=Nope")
        self.assertEqual(response.status_code, 400)
