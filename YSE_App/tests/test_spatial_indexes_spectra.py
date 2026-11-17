"""Performance audit 2026-10-10: positional/time indexes (#477, #478) and
spectrum plotting without one ORM object per pixel (#479)."""

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from YSE_App import view_utils
from YSE_App.models import (
    Candidate,
    Host,
    SurveyField,
    SurveyObservation,
    Transient,
    TransientSpecData,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_spectrum,
    create_minimal_transient,
    create_test_user,
)

# model -> {index name: fields}
EXPECTED_INDEXES = {
    Transient: {
        "yse_transient_dec_ra_idx": ["dec", "ra"],
        "yse_transient_created_idx": ["created_date"],
    },
    Host: {"yse_host_dec_ra_idx": ["dec", "ra"]},
    Candidate: {"yse_candidate_dec_ra_idx": ["dec", "ra"]},
    SurveyField: {"yse_surveyfield_dec_ra_idx": ["dec_cen", "ra_cen"]},
    SurveyObservation: {
        "yse_surveyobs_obs_mjd_idx": ["obs_mjd"],
        "yse_surveyobs_mjd_req_idx": ["mjd_requested"],
    },
}


class SpatialAndTimeIndexTests(TestCase):
    def test_model_meta_declares_indexes(self):
        for model, expected in EXPECTED_INDEXES.items():
            declared = {ix.name: list(ix.fields) for ix in model._meta.indexes}
            for name, fields in expected.items():
                self.assertEqual(declared.get(name), fields, f"{model.__name__}.{name}")
                self.assertLessEqual(len(name), 30, name)

    def test_migration_state_matches_models(self):
        """Index sets in the migration graph equal Meta.indexes (makemigrations clean)."""
        state = MigrationLoader(None, ignore_no_migrations=True).project_state()
        for model in EXPECTED_INDEXES:
            model_state = state.models[(model._meta.app_label, model._meta.model_name)]
            migrated = {ix.name: list(ix.fields) for ix in model_state.options.get("indexes", [])}
            declared = {ix.name: list(ix.fields) for ix in model._meta.indexes}
            self.assertEqual(migrated, declared, model.__name__)

    def test_indexes_exist_in_database(self):
        with connection.cursor() as cursor:
            for model, expected in EXPECTED_INDEXES.items():
                constraints = connection.introspection.get_constraints(
                    cursor, model._meta.db_table
                )
                for name, fields in expected.items():
                    self.assertIn(name, constraints, f"{model._meta.db_table}.{name}")
                    columns = [model._meta.get_field(f).column for f in fields]
                    self.assertEqual(constraints[name]["columns"], columns)


class SpectrumPointsValuesListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("spec_values_list_user")

    def _spectrum_with_points(self, transient, n):
        spectrum = attach_synthetic_spectrum(self.user, transient)
        TransientSpecData.objects.bulk_create([
            TransientSpecData(
                spectrum=spectrum,
                wavelength=4000.0 + 10.0 * i,
                flux=1.0 + 0.1 * i,
                created_by=self.user,
                modified_by=self.user,
            )
            for i in range(n)
        ])
        return spectrum

    def test_points_grouped_per_spectrum_one_query_per_storage(self):
        transient = create_minimal_transient(self.user, name="2026specvlgroup")
        a = self._spectrum_with_points(transient, 5)
        b = self._spectrum_with_points(transient, 3)
        empty = attach_synthetic_spectrum(self.user, transient)
        # One query per storage (#492): arrays rows, then pixel rows for the spectra without one.
        with self.assertNumQueries(2):
            points = view_utils._spec_points_by_spectrum([a.id, b.id, empty.id])
        self.assertEqual(set(points), {a.id, b.id})
        wave, flux = points[a.id]
        self.assertEqual(list(wave), [4000.0, 4010.0, 4020.0, 4030.0, 4040.0])
        self.assertAlmostEqual(flux[-1], 1.4)
        self.assertEqual(len(points[b.id][0]), 3)

    def test_no_spectra_runs_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(view_utils._spec_points_by_spectrum([]), {})

    def test_spectrumplot_queries_do_not_scale_with_pixels(self):
        client = Client()
        client.force_login(self.user)
        counts = []
        for name, n in (("2026specvlsmall", 20), ("2026specvllarge", 400)):
            transient = create_minimal_transient(self.user, name=name)
            self._spectrum_with_points(transient, n)
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(reverse("spectrumplot", args=[transient.id]))
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"<script", response.content)
            counts.append(len(ctx.captured_queries))
        self.assertEqual(counts[0], counts[1])
