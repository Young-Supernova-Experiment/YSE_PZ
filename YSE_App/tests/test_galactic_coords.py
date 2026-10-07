"""Stored galactic coordinates on Transient (#286): formula, save() upkeep, backfill, search."""

import math

from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from YSE_App.common.galactic import (
    backfill_galactic_coords,
    galactic_coords,
    galactic_latitude_expression,
    galactic_longitude_expression,
)
from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.models import Transient
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

# (ra, dec) J2000 -> (l, b) from astropy 5 (SkyCoord(...).galactic), 1e-4 deg.
REFERENCE = [
    ((0.0, 0.0), (96.3373, -60.1886)),
    ((10.0, 20.0), (119.2694, -42.7904)),
    ((266.4, -28.9), (0.0286, 0.0226)),
    ((192.85948, 27.12825), (340.7376, 90.0)),
    ((100.0, -40.0), (248.8719, -19.0936)),
    ((359.99, 89.9), (122.9069, 27.0308)),
]


def _wrap_diff(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class GalacticFormulaTests(TestCase):
    def test_python_form_matches_astropy(self):
        for (ra, dec), (l_ref, b_ref) in REFERENCE:
            l, b = galactic_coords(ra, dec)
            self.assertAlmostEqual(b, b_ref, places=3, msg=(ra, dec))
            # longitude is ill-defined at the pole; compare it on the sky
            self.assertLess(_wrap_diff(l, l_ref) * math.cos(math.radians(b)), 2e-4, (ra, dec))
            self.assertGreaterEqual(l, 0.0)
            self.assertLess(l, 360.0)

    def test_unusable_coordinates_give_none(self):
        self.assertEqual(galactic_coords(None, 1.0), (None, None))
        self.assertEqual(galactic_coords("ra", 1.0), (None, None))
        self.assertEqual(galactic_coords(float("nan"), 1.0), (None, None))

    def test_sql_form_matches_python_form(self):
        """The database expressions (used by the migration and the command) agree with save()."""
        user = create_test_user("gal_sql")
        for i, ((ra, dec), _ref) in enumerate(REFERENCE):
            create_minimal_transient(user, name="galsql%d" % i, ra=ra, dec=dec)
        rows = (
            Transient.objects.filter(name__startswith="galsql")
            .annotate(sql_l=galactic_longitude_expression(), sql_b=galactic_latitude_expression())
            .values_list("ra", "dec", "gal_l", "gal_b", "sql_l", "sql_b")
        )
        self.assertEqual(len(rows), len(REFERENCE))
        for ra, dec, gal_l, gal_b, sql_l, sql_b in rows:
            self.assertAlmostEqual(gal_b, sql_b, places=6, msg=(ra, dec))
            self.assertLess(_wrap_diff(gal_l, sql_l) * math.cos(math.radians(gal_b)), 1e-6, (ra, dec))
            self.assertGreaterEqual(sql_l, 0.0)
            self.assertLess(sql_l, 360.0)


class TransientSaveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("gal_save")

    def test_save_fills_and_tracks_ra_dec(self):
        t = create_minimal_transient(self.user, name="galsave1", ra=10.0, dec=20.0)
        t.refresh_from_db()
        self.assertAlmostEqual(t.gal_b, -42.7904, places=3)
        self.assertAlmostEqual(t.gal_l, 119.2694, places=3)
        t.ra, t.dec = 266.4, -28.9
        t.save()
        t.refresh_from_db()
        self.assertAlmostEqual(t.gal_b, 0.0226, places=3)

    def test_save_with_update_fields_naming_a_coordinate_recomputes(self):
        t = create_minimal_transient(self.user, name="galsave2", ra=10.0, dec=20.0)
        Transient.objects.filter(pk=t.pk).update(gal_b=None, gal_l=None)
        t.dec = -28.9
        t.ra = 266.4
        t.save(update_fields=["ra", "dec"])
        t.refresh_from_db()
        self.assertAlmostEqual(t.gal_b, 0.0226, places=3)
        self.assertAlmostEqual(t.gal_l, 0.0286, places=3)

    def test_save_with_unrelated_update_fields_leaves_the_columns_alone(self):
        t = create_minimal_transient(self.user, name="galsave3", ra=10.0, dec=20.0)
        Transient.objects.filter(pk=t.pk).update(gal_b=1.5, gal_l=2.5)
        t.redshift = 0.1
        t.save(update_fields=["redshift"])
        t.refresh_from_db()
        self.assertEqual((t.gal_l, t.gal_b), (2.5, 1.5))

    def test_api_exposes_the_columns_read_only(self):
        t = create_minimal_transient(self.user, name="galapi", ra=10.0, dec=20.0)
        self.client.force_login(self.user)
        response = self.client.get("/api/transients/%d/" % t.pk)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertAlmostEqual(data["gal_b"], -42.7904, places=3)
        self.assertAlmostEqual(data["gal_l"], 119.2694, places=3)


class BackfillTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("gal_backfill")
        cls.saved = create_minimal_transient(cls.user, name="galbf0", ra=10.0, dec=20.0)
        # bulk_create bypasses save(): the columns stay NULL, as for rows predating #286.
        template = Transient.objects.get(pk=cls.saved.pk)
        Transient.objects.bulk_create([
            Transient(name="galbf1", slug="galbf1", ra=266.4, dec=-28.9, status=template.status,
                      obs_group=template.obs_group, created_by=template.created_by, modified_by=template.modified_by),
            Transient(name="galbf2", slug="galbf2", ra=100.0, dec=-40.0, status=template.status,
                      obs_group=template.obs_group, created_by=template.created_by, modified_by=template.modified_by),
        ])

    def test_bulk_created_rows_have_no_coordinates(self):
        self.assertEqual(Transient.objects.filter(name__startswith="galbf", gal_b__isnull=True).count(), 2)

    def test_backfill_fills_only_null_rows_in_one_statement(self):
        Transient.objects.filter(pk=self.saved.pk).update(gal_b=1.0, gal_l=2.0)
        with CaptureQueriesContext(connection) as ctx:
            n = backfill_galactic_coords(Transient)
        self.assertEqual(n, 2)
        self.assertEqual(len(ctx.captured_queries), 1)
        by_name = {t.name: t for t in Transient.objects.filter(name__startswith="galbf")}
        self.assertAlmostEqual(by_name["galbf1"].gal_b, 0.0226, places=3)
        self.assertAlmostEqual(by_name["galbf2"].gal_l, 248.8719, places=3)
        self.assertEqual((by_name["galbf0"].gal_l, by_name["galbf0"].gal_b), (2.0, 1.0))

    def test_command_all_recomputes_every_row(self):
        Transient.objects.filter(pk=self.saved.pk).update(gal_b=1.0, gal_l=2.0)
        call_command("backfill_galactic_coords", "--all", verbosity=0)
        self.assertEqual(Transient.objects.filter(name__startswith="galbf", gal_b__isnull=True).count(), 0)
        self.saved.refresh_from_db()
        self.assertAlmostEqual(self.saved.gal_b, -42.7904, places=3)

    def test_command_default_reports_counts(self):
        from io import StringIO

        out = StringIO()
        call_command("backfill_galactic_coords", stdout=out)
        self.assertIn("updated 2 transient(s)", out.getvalue())
        self.assertIn("0 still without gal_b", out.getvalue())


class SearchOnStoredColumnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("gal_search")
        cls.plane = create_minimal_transient(cls.user, name="galplane", ra=266.4, dec=-28.9)   # b = +0.02
        cls.north = create_minimal_transient(cls.user, name="galnorth", ra=10.0, dec=20.0)     # b = -42.8
        cls.south = create_minimal_transient(cls.user, name="galsouth", ra=100.0, dec=-40.0)   # b = -19.1
        cls.pole = create_minimal_transient(cls.user, name="galpole", ra=192.85948, dec=27.12825)  # b = +90

    def _names(self, params):
        request = RequestFactory().get("/search/", params)
        request.user = self.user
        fs = TransientSearchFilterSet(request.GET, queryset=Transient.objects.filter(name__startswith="gal"), request=request)
        self.assertTrue(fs.is_valid(), fs.errors)
        return fs

    def test_abs_filters_are_range_predicates_on_the_column(self):
        fs = self._names({"gal_b_abs_min": "10"})
        self.assertEqual(set(fs.qs.values_list("name", flat=True)), {"galnorth", "galsouth", "galpole"})
        sql = str(fs.qs.query)
        self.assertNotIn("ABS(", sql.upper())
        self.assertNotIn("ASIN", sql.upper())
        self.assertIn("gal_b", sql)
        fs = self._names({"gal_b_abs_max": "30"})
        self.assertEqual(set(fs.qs.values_list("name", flat=True)), {"galplane", "galsouth"})
        fs = self._names({"gal_b_abs_min": "15", "gal_b_abs_max": "50"})
        self.assertEqual(set(fs.qs.values_list("name", flat=True)), {"galnorth", "galsouth"})

    def test_rows_without_coordinates_never_match_a_latitude_cut(self):
        Transient.objects.filter(pk=self.pole.pk).update(gal_b=None, gal_l=None)
        fs = self._names({"gal_b_abs_min": "0"})
        self.assertNotIn("galpole", set(fs.qs.values_list("name", flat=True)))
        fs = self._names({"gal_b_abs_max": "90"})
        self.assertNotIn("galpole", set(fs.qs.values_list("name", flat=True)))

    def test_ordering_by_gal_b_reads_the_column(self):
        fs = self._names({"ordering": "-gal_b"})
        self.assertEqual(list(fs.qs.values_list("name", flat=True)), ["galpole", "galplane", "galsouth", "galnorth"])
        self.assertNotIn("ASIN", str(fs.qs.query).upper())


class SearchPageColumnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("gal_page")
        create_minimal_transient(cls.user, name="galpg1", ra=10.0, dec=20.0)
        create_minimal_transient(cls.user, name="galpg2", ra=266.4, dec=-28.9)

    def setUp(self):
        self.client.force_login(self.user)

    def test_column_appears_only_with_a_latitude_filter_or_ordering(self):
        response = self.client.get("/search/?name_contains=galpg")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Gal. b (deg)")
        response = self.client.get("/search/?name_contains=galpg&gal_b_abs_min=10")
        self.assertContains(response, "Gal. b (deg)")
        self.assertContains(response, "-42.79")
        self.assertNotContains(response, "galpg2")
        response = self.client.get("/search/?name_contains=galpg&ordering=-gal_b")
        self.assertContains(response, "Gal. b (deg)")
        self.assertContains(response, "+0.02")
        body = response.content.decode()
        self.assertLess(body.index("galpg2"), body.index("galpg1"))
