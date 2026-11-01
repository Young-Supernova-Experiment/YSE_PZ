"""Display redshifts with the full precedence list (#736).

The SQL expression behind list columns, sorting, filters and exports must equal
``redshift_precedence.best_redshift()``. Synthetic rows only.
"""

from __future__ import annotations

import csv
import io

from django.test import TestCase
from django.urls import reverse

from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.models import Host, Transient
from YSE_App.services.redshift_precedence import best_redshift
from YSE_App.services.redshift_sql import annotate_best_redshift
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

# name: (transient z, source, override), (host z, source, override) or None, host photo-z
CASES = {
    "t-override-beats-ned": ((0.10, "manual", True), (0.20, "NED", False), None),
    "t-tns-loses-to-host-ned": ((0.05, "TNS", False), (0.06, "NED", False), None),
    "t-desi-loses-to-host-ned": ((0.07, "DESI DR1", False), (0.08, "NED", False), None),
    "t-own-loses-to-host-desi": ((0.03, "YSE spectrum", False), (0.04, "DESI DR1", False), None),
    "t-own-tie-transient-wins": ((0.03, "manual", False), (0.04, "YSE spectrum", False), None),
    "t-legacy-loses-to-host-catalogue": ((0.05, None, False), (0.06, "Sherlock", False), None),
    "t-host-override-wins": ((0.01, "NED", False), (0.02, "manual", True), None),
    "t-host-photoz-only": ((None, None, False), (None, None, False), 0.30),
    "t-photo-source-loses-to-tns": ((0.50, "Prost photo-z", False), (0.40, "TNS", False), None),
    "t-spec-beats-host-photoz": ((0.05, "TNS", False), (None, None, False), 0.30),
    "t-no-host": ((0.07, "YSE spectrum", False), None, None),
    "t-nothing": ((None, None, False), None, None),
    "t-padded-case-source": ((0.90, "  ned ", False), (0.80, "YSE spectrum", False), None),
    "t-catalogue-tie": ((0.11, "Sherlock", False), (0.12, "ANTARES cat", False), None),
    "t-bare-desi": ((0.21, "desi", False), (0.22, "TNS", False), None),
    "t-host-only-legacy": ((None, None, False), (0.33, None, False), 0.30),
    "t-zero-redshift": ((0.0, "NED", False), (0.5, "TNS", False), None),
}


class BestRedshiftDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("best_z_user", is_staff=True)
        for i, (name, (t, h, photo)) in enumerate(CASES.items()):
            transient = create_minimal_transient(cls.user, name=name, ra=10.0 + i, dec=20.0)
            transient.redshift, transient.redshift_source, transient.redshift_override = t
            if h is not None:
                host = Host.objects.create(
                    ra=10.0 + i, dec=20.0, name="host-" + name, created_by=cls.user, modified_by=cls.user,
                    redshift=h[0], redshift_source=h[1], redshift_override=h[2], photo_z=photo,
                )
                transient.host = host
            transient.save()

    def test_sql_expression_equals_best_redshift(self):
        qs = annotate_best_redshift(Transient.objects.filter(name__in=list(CASES)).select_related("host"))
        rows = {t.name: t for t in qs}
        self.assertEqual(set(rows), set(CASES))
        for name, transient in rows.items():
            expected = best_redshift(transient).value
            if expected is None:
                self.assertIsNone(transient.best_redshift, name)
            else:
                self.assertAlmostEqual(transient.best_redshift, expected, places=9, msg=name)

    def test_expected_winners(self):
        want = {
            "t-override-beats-ned": 0.10, "t-tns-loses-to-host-ned": 0.06, "t-desi-loses-to-host-ned": 0.08,
            "t-own-loses-to-host-desi": 0.04, "t-own-tie-transient-wins": 0.03,
            "t-legacy-loses-to-host-catalogue": 0.06, "t-host-override-wins": 0.02,
            "t-host-photoz-only": 0.30, "t-photo-source-loses-to-tns": 0.40, "t-spec-beats-host-photoz": 0.05,
            "t-no-host": 0.07, "t-nothing": None, "t-padded-case-source": 0.90, "t-catalogue-tie": 0.11,
            "t-bare-desi": 0.21, "t-host-only-legacy": 0.33, "t-zero-redshift": 0.0,
        }
        qs = annotate_best_redshift(Transient.objects.filter(name__in=list(CASES)).select_related("host"))
        for transient in qs:
            expected = want[transient.name]
            for got in (transient.best_redshift, transient.z_or_hostz()):
                if expected is None:
                    self.assertIsNone(got, transient.name)
                else:
                    self.assertAlmostEqual(got, expected, places=9, msg=transient.name)

    def test_annotation_is_idempotent_and_adds_no_query(self):
        qs = Transient.objects.filter(name__in=list(CASES)).select_related("host")
        twice = annotate_best_redshift(annotate_best_redshift(qs))
        with self.assertNumQueries(1):
            rows = list(twice)
            for t in rows:
                t.z_or_hostz()  # reads the joined host, no lazy load

    def test_filters_and_sorting_use_the_best_redshift(self):
        names = lambda qs: set(qs.values_list("name", flat=True))  # noqa: E731
        base = Transient.objects.filter(name__in=list(CASES))
        hit = TransientSearchFilterSet({"redshift_min": 0.35}, queryset=base).qs
        self.assertEqual(names(hit), {"t-padded-case-source", "t-photo-source-loses-to-tns"})
        hit = TransientSearchFilterSet({"redshift_max": 0.015}, queryset=base).qs
        self.assertEqual(names(hit), {"t-zero-redshift"})
        has = TransientSearchFilterSet({"has_redshift": "true"}, queryset=base).qs
        self.assertEqual(names(has), set(CASES) - {"t-nothing"})
        ordered = list(annotate_best_redshift(base).filter(best_redshift__isnull=False)
                       .order_by("best_redshift", "pk").values_list("name", flat=True))
        self.assertEqual(ordered[0], "t-zero-redshift")
        self.assertEqual(ordered[-1], "t-padded-case-source")

    def test_search_csv_has_best_redshift_column(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("search_export_csv"))
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertIn("best_redshift", rows[0])
