"""TransientPhotStat leftovers (umbrella #267).

* #269: photometry posted through the REST API recomputes each transient
  once per request (single rows and JSON lists); moving a photometry set to
  another transient refreshes both rows; the periodic ``photstat.refresh_stale``
  job and its cron repair missing / outdated rows.
* #270: the dashboard querysets carry no photometry subquery; the planning
  and saved-query tables filter on the stored last detection; the Explorer
  view ``yse_transient_photstat_v``.
* #271: the Bazin-extrapolated magnitude stored on the stat row, the
  ``photstat.bazin_extrapolate`` job, and a recompute leaving it alone.

The statistics rules (pre-detection limits only, a band with every limit,
the plot's detection rule) are pinned in ``test_photstat.py``; the view and
the API here return those same stored values.
"""

import datetime
import json
from unittest import mock

from django.db import connection
from django.db.utils import OperationalError
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from YSE_App.data_ingest.PhotStat_Jobs import PhotStatRefresh
from YSE_App.jobs import get_handler, run_pass
from YSE_App.models import (
    DataQuality,
    FollowupStatus,
    Job,
    Transient,
    TransientFollowup,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
)
from YSE_App.queries.yse_python_queries import annotate_recent_mag
from YSE_App.services import bazin, photstat, photstat_jobs
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_photstat import add_point, mjd_dt


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("photstat_left_user", is_superuser=True)
        cls.transient = create_minimal_transient(cls.user, name="2026psl")
        cls.obs_group, cls.instrument, cls.r = create_instrument_stack(cls.user, obs_group_name="psl-group")
        cls.photometry = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group,
            **audit_fields(cls.user),
        )
        with photstat.deferred_updates():
            # one pre-detection limit (23.74), then two detections
            add_point(cls.photometry, cls.r, 60003.0, flux=2.0, flux_err=10.0, zp=27.5, user=cls.user)
            add_point(cls.photometry, cls.r, 60006.0, mag=19.0, mag_err=0.1, user=cls.user)
            add_point(cls.photometry, cls.r, 60009.0, mag=18.0, mag_err=0.1, user=cls.user)
        cls.bad, _ = DataQuality.objects.get_or_create(name="Bad", defaults=audit_fields(cls.user))

    def stat(self, transient=None):
        return TransientPhotStat.objects.get(transient=transient or self.transient)


# --------------------------------------------------------------------- #269


class ApiBatchingTests(_Fixture):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _row(self, mjd, mag, *, flagged=False):
        return {
            "photometry": reverse("transientphotometry-detail", kwargs={"id": self.photometry.id}),
            "band": reverse("photometricband-detail", args=[self.r.pk]),
            "obs_date": mjd_dt(mjd).isoformat(),
            "mag": mag,
            "mag_err": 0.1,
            "data_quality": [reverse("dataquality-detail", args=[self.bad.pk])] if flagged else [],
        }

    def _post(self, body):
        with mock.patch.object(photstat, "recompute", wraps=photstat.recompute) as spy:
            response = self.client.post(
                "/api/transientphotdata/", data=json.dumps(body), content_type="application/json"
            )
        return response, spy.call_count

    def test_single_flagged_point_recomputes_once(self):
        # create + data_quality.set + save each fired a recompute before
        response, calls = self._post(self._row(60012.0, 17.0, flagged=True))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(calls, 1)
        self.assertEqual(self.stat().num_det_global, 2)  # the flagged point is ignored

    def test_list_of_points_recomputes_once_per_transient(self):
        rows = [self._row(60010.0 + i, 18.2 + 0.1 * i) for i in range(4)]
        response, calls = self._post(rows)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(response.json()), 4)
        self.assertEqual(calls, 1)
        self.assertEqual(TransientPhotData.objects.filter(photometry=self.photometry).count(), 7)
        stat = self.stat()
        self.assertEqual(stat.num_det_global, 6)
        self.assertAlmostEqual(stat.last_detected_mjd, 60013.0)

    def test_list_longer_than_the_cap_is_refused(self):
        with mock.patch("YSE_App.api_views.PhotStatBatchMixin.MAX_BATCH", 2):
            response, _calls = self._post([self._row(60010.0 + i, 18.0) for i in range(3)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TransientPhotData.objects.filter(photometry=self.photometry).count(), 3)


class ReassignmentTests(_Fixture):
    def test_moving_photometry_refreshes_both_transients(self):
        other = create_minimal_transient(self.user, name="2026pslgood")
        photstat.recompute(other.pk)
        self.assertEqual(self.stat(other).num_det_global, 0)

        self.photometry.transient = other
        self.photometry.save()

        self.assertEqual(self.stat(other).num_det_global, 2)
        self.assertAlmostEqual(self.stat(other).deepest_limit, 23.74, places=2)
        old = self.stat()
        self.assertEqual(old.num_det_global, 0)
        self.assertIsNone(old.deepest_limit)

    def test_duplicate_cleanup_move_then_delete(self):
        """QUB_data.CheckDuplicates: move inside deferred_updates, then delete the duplicate."""
        good = create_minimal_transient(self.user, name="2026pslkeep")
        with mock.patch.object(photstat, "recompute", wraps=photstat.recompute) as spy:
            with photstat.deferred_updates():
                self.photometry.transient = good
                self.photometry.save()
            self.assertEqual(spy.call_count, 2)  # one per transient
        self.transient.delete()
        self.assertEqual(self.stat(good).num_det_global, 2)
        self.assertFalse(TransientPhotStat.objects.filter(transient_id=self.transient.pk).exists())

    def test_saving_without_moving_costs_no_recompute(self):
        with mock.patch.object(photstat, "recompute", wraps=photstat.recompute) as spy:
            self.photometry.save()
            self.photometry.save(update_fields=["modified_date"])
        self.assertEqual(spy.call_count, 0)


@override_settings(JOB_RUNNER_INLINE=False)
class PeriodicRefreshTests(_Fixture):
    def _make_stale(self):
        missing = create_minimal_transient(self.user, name="2026pslmiss")
        TransientPhotStat.objects.filter(transient=missing).delete()
        TransientPhotStat.objects.filter(transient=self.transient).update(schema_version=0, peak_mag=1.0)
        return missing

    def test_job_refreshes_missing_and_outdated_rows(self):
        missing = self._make_stale()
        self.assertEqual(set(photstat.stale_transient_ids()), {self.transient.pk, missing.pk})
        result = get_handler(photstat_jobs.REFRESH_STALE_KIND)({})
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["created"], 1)
        stat = self.stat()
        self.assertEqual(stat.schema_version, photstat.SCHEMA_VERSION)
        self.assertAlmostEqual(stat.peak_mag, 18.0)
        self.assertTrue(TransientPhotStat.objects.filter(transient=missing).exists())
        self.assertEqual(photstat.stale_transient_ids(), [])

    def test_limit_bounds_one_pass(self):
        self._make_stale()
        self.assertEqual(get_handler(photstat_jobs.REFRESH_STALE_KIND)({"limit": 1})["processed"], 1)
        self.assertEqual(len(photstat.stale_transient_ids()), 1)

    def test_cron_queues_once_and_the_runner_refreshes_within_one_cycle(self):
        self._make_stale()
        summary = PhotStatRefresh().do()
        self.assertIn("photstat.refresh_stale queued job", summary)
        self.assertIn("photstat.bazin_extrapolate disabled", summary)
        self.assertIn("already queued", PhotStatRefresh().do())
        self.assertEqual(Job.objects.filter(kind=photstat_jobs.REFRESH_STALE_KIND).count(), 1)
        run_pass(kinds=[photstat_jobs.REFRESH_STALE_KIND])
        self.assertEqual(photstat.stale_transient_ids(), [])
        self.assertAlmostEqual(self.stat().peak_mag, 18.0)

    @override_settings(PHOTSTAT_REFRESH_CRON_ENABLED=False, PHOTSTAT_BAZIN_CRON_ENABLED=True)
    def test_cron_switches(self):
        summary = PhotStatRefresh().do()
        self.assertIn("photstat.refresh_stale disabled", summary)
        self.assertIn("photstat.bazin_extrapolate queued job", summary)
        self.assertFalse(Job.objects.filter(kind=photstat_jobs.REFRESH_STALE_KIND).exists())


# --------------------------------------------------------------------- #270


class StoredLastDetectionTests(_Fixture):
    def test_planning_tables_filter_on_the_stored_last_detection(self):
        # the newest point is a flagged, brighter one: not the last detection
        add_point(self.photometry, self.r, 60020.0, mag=12.0, mag_err=0.1, bad=True, user=self.user)
        qs = annotate_recent_mag(Transient.objects.filter(pk=self.transient.pk))
        with CaptureQueriesContext(connection) as ctx:
            row = qs.first()
        self.assertNotIn("yse_app_transientphotdata", ctx.captured_queries[0]["sql"].lower())
        self.assertAlmostEqual(row.recent_mag, self.stat().last_detected_mag)
        self.assertAlmostEqual(row.recent_mag, 18.0)
        self.assertTrue(qs.filter(recent_mag__lt=18.5).exists())
        self.assertFalse(qs.filter(recent_mag__lt=17.5).exists())


class ExplorerViewTests(_Fixture):
    def test_view_returns_the_stored_values_with_bands(self):
        if connection.vendor != "mysql":
            self.skipTest("the view is created on MySQL only")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT transient_name, num_det, num_limits, peak_mag, peak_band, deepest_limit, "
                "deepest_limit_band, last_non_detection_band, extrapolated_mag "
                "FROM yse_transient_photstat_v WHERE transient_id = %s",
                [self.transient.pk],
            )
            rows = cursor.fetchall()
        stat = self.stat()
        self.assertEqual(len(rows), 1)
        name, num_det, num_limits, peak_mag, peak_band, deepest, deepest_band, last_nd_band, xmag = rows[0]
        self.assertEqual(name, self.transient.name)
        self.assertEqual((num_det, num_limits), (stat.num_det_global, stat.num_limits_global))
        self.assertAlmostEqual(peak_mag, stat.peak_mag)
        self.assertEqual(peak_band, self.r.name)
        self.assertAlmostEqual(deepest, stat.deepest_limit)
        self.assertEqual((deepest_band, last_nd_band), (self.r.name, self.r.name))
        self.assertIsNone(xmag)

    def test_missing_privilege_is_a_warning_not_a_failed_deploy(self):
        import importlib

        migration = importlib.import_module("YSE_App.migrations.0034_transient_photstat_view")
        cursor = mock.MagicMock()
        cursor.__enter__.return_value.execute.side_effect = OperationalError(1142, "CREATE VIEW command denied")
        editor = mock.MagicMock()
        editor.connection.vendor = "mysql"
        editor.connection.cursor.return_value = cursor
        with self.assertLogs(migration.logger, level="WARNING"):
            migration.create_view(None, editor)
        cursor.__enter__.return_value.execute.side_effect = OperationalError(1054, "Unknown column")
        with self.assertRaises(OperationalError):
            migration.create_view(None, editor)
        editor.connection.vendor = "sqlite"
        editor.connection.cursor.reset_mock()
        migration.drop_view(None, editor)
        editor.connection.cursor.assert_not_called()


# --------------------------------------------------------------------- #271


class BazinWriteBackTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from YSE_App.tests.test_analysis_services import seed_bazin_lightcurve

        cls.user = create_test_user("photstat_bazin_user", is_superuser=True)
        cls.fitted = create_minimal_transient(cls.user, name="2026pslbaz")
        cls.phot, _truth, cls.now_mjd = seed_bazin_lightcurve(cls.user, cls.fitted)
        cls.unfit = create_minimal_transient(cls.user, name="2026pslnofit")
        photstat.recompute_many([cls.fitted.pk, cls.unfit.pk])

    def test_store_matches_the_bazin_column_and_keeps_the_band(self):
        at_mjd = self.now_mjd + 1.0
        expected = bazin.extrapolated_mags([self.fitted.pk], at_mjd)[self.fitted.pk]
        TransientPhotStat.objects.filter(transient=self.unfit).update(extrapolated_mag=19.0)
        result = bazin.store_extrapolations([self.fitted.pk, self.unfit.pk], at_mjd)
        self.assertEqual(result, {"fitted": 1, "cleared": 1})

        stat = TransientPhotStat.objects.select_related("extrapolated_band").get(transient=self.fitted)
        self.assertAlmostEqual(stat.extrapolated_mag, expected[0], places=6)
        self.assertEqual(stat.extrapolated_band.name, expected[1])
        self.assertAlmostEqual(stat.extrapolated_mjd, at_mjd)
        self.assertIsNotNone(stat.extrapolated_at)

        cleared = TransientPhotStat.objects.get(transient=self.unfit)
        self.assertIsNone(cleared.extrapolated_mag)
        self.assertIsNone(cleared.extrapolated_band_id)
        self.assertIsNotNone(cleared.extrapolated_at)

    def test_recompute_leaves_the_extrapolation_alone(self):
        bazin.store_extrapolations([self.fitted.pk], self.now_mjd)
        before = TransientPhotStat.objects.get(transient=self.fitted)
        band = self.phot.transientphotdata_set.first().band
        add_point(self.phot, band, self.now_mjd - 0.2, mag=18.5, mag_err=0.05, user=self.user)
        after = TransientPhotStat.objects.get(transient=self.fitted)
        self.assertNotEqual(after.num_det_global, before.num_det_global)
        self.assertEqual(
            (after.extrapolated_mag, after.extrapolated_band_id, after.extrapolated_mjd, after.extrapolated_at),
            (before.extrapolated_mag, before.extrapolated_band_id, before.extrapolated_mjd, before.extrapolated_at),
        )

    def test_job_defaults_to_transients_with_an_open_followup(self):
        audit = audit_fields(self.user)
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)
        now = timezone.now()
        TransientFollowup.objects.create(
            transient=self.fitted, status=status, valid_start=now - datetime.timedelta(days=1),
            valid_stop=now + datetime.timedelta(days=5), **audit,
        )
        TransientFollowup.objects.create(
            transient=self.unfit, status=status, valid_start=now - datetime.timedelta(days=20),
            valid_stop=now - datetime.timedelta(days=10), **audit,
        )
        self.assertEqual(photstat_jobs.open_followup_transient_ids(), [self.fitted.pk])
        result = get_handler(photstat_jobs.BAZIN_KIND)({})
        self.assertEqual(result["transients"], 1)
        self.assertEqual(result["fitted"], 1)
        self.assertIsNotNone(TransientPhotStat.objects.get(transient=self.fitted).extrapolated_mag)
        self.assertIsNone(TransientPhotStat.objects.get(transient=self.unfit).extrapolated_at)
