"""SALT and Bazin fit tables with scheduled refits (#846). Synthetic data only; no fitter runs.

Pins:

* the post-save hook: a succeeded Bazin run writes one row per band (reference band, band key,
  peak-after-last-non-detection flag), a SALT run one row; failed runs, non-SALT sncosmo models
  and other services write nothing; saving again writes nothing; a hook error never breaks the run;
* subset runs (#847) are kept but never become the default; defaults come from the newest run;
  Bazin is the default reference epoch, SALT t0 the alternative;
* ``backfill_fit_tables`` (dry run by default, ``--apply``, idempotent);
* the refit rule (new detection, non-detection within 7 days after the last detection), the
  pass (job queue, idempotent, previous run's user and parameters), the ``FIT_REFIT_ENABLED`` gate;
* the Analysis tab's fit history only with the UI preview on;
* Explorer: the new tables are staff-only;
* migration 0064's ON DELETE list against the models, and raw-SQL deletes on MySQL.
"""

from __future__ import annotations

import datetime
import importlib
import io
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, models
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.analysis import bazin_fit
from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.models import (
    BazinFit,
    ExternalServiceRun,
    SaltFit,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.services import analysis_services as svc
from YSE_App.services import fit_tables
from YSE_App.services.photstat import PhotPoint
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

fit_migration = importlib.import_module("YSE_App.migrations.0064_fit_tables_fk_on_delete")


def bazin_result(peak_g=60010.0, peak_r=60011.0, last_nd=60000.0, reference="r"):
    def band(peak, mag):
        return {"A": 1.0, "t0": peak - 2.0, "t0_err": 0.3, "tau_rise": 3.0, "tau_rise_err": 0.4,
                "tau_fall": 20.0, "tau_fall_err": 1.5, "B": 0.0, "chi2": 8.0, "dof": 6, "reduced_chi2": 1.33,
                "n_points": 11, "method": "joint", "mjd_min": 60001.0, "mjd_max": 60040.0,
                "peak_mjd": peak, "peak_mag": mag}
    out = {"reference_band": reference, "bands": ["g-ZTF", "r"], "peak_mjd": peak_r,
           "per_band": {"g-ZTF": band(peak_g, 18.2), "r": band(peak_r, 18.0)}}
    if last_nd is not ...:
        out["last_non_detection_mjd"] = last_nd
    return out


def salt_result(model="salt3", t0=60012.0):
    return {"model": model, "model_version": "2.0", "z": 0.03, "z_source": "transient", "t0": t0, "t0_err": 0.1,
            "x0": 0.001, "x0_err": 0.0001, "x1": 0.5, "x1_err": 0.2, "c": 0.02, "c_err": 0.03, "mB": 18.1,
            "mB_err": 0.05, "chi2": 30.0, "ndof": 25, "reduced_chi2": 1.2, "n_points": 30, "bands": ["ztfg", "ztfr"],
            "mwebv": 0.02}


class FitBase(TestCase):
    def setUp(self):
        self.staff = create_test_user("fit_staff", is_superuser=True)
        self.member = create_test_user("fit_member", is_staff=False)
        self.transient = create_minimal_transient(self.staff, name="2099fit", obs_group_name="fit-group")
        self.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=self.staff, timeout_seconds=0)
        self.salt = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.staff, timeout_seconds=0)
        self.other = svc.register_service("other_fit", runner="ngsf", user=self.staff, timeout_seconds=0)

    def make_run(self, profile, result=None, *, status="succeeded", params=None, user=None, transient=None,
                 finished=None):
        user = user or self.staff
        run = ExternalServiceRun.objects.create(
            service=profile.service, transient=transient or self.transient, created_by=user, modified_by=user,
            request_payload={"analysis": True, "params": params or {}})
        if status == "succeeded":
            run.mark_succeeded(result=result or {})
        elif status == "failed":
            run.mark_failed("synthetic failure", result=result)
        if finished is not None:
            ExternalServiceRun.objects.filter(pk=run.pk).update(finished_at=finished)
            BazinFit.objects.filter(run=run).update(fit_date=finished)
            SaltFit.objects.filter(run=run).update(fit_date=finished)
            run.refresh_from_db()
        return run


class HookTests(FitBase):
    def test_bazin_run_writes_one_row_per_band(self):
        run = self.make_run(self.bazin, bazin_result())
        rows = {row.band: row for row in BazinFit.objects.filter(run=run)}
        self.assertEqual(set(rows), {"g-ZTF", "r"})
        self.assertTrue(rows["r"].is_reference_band)
        self.assertFalse(rows["g-ZTF"].is_reference_band)
        self.assertEqual(rows["g-ZTF"].band_key, "g")
        self.assertEqual(rows["r"].peak_mjd, 60011.0)
        self.assertEqual(rows["r"].peak_mag, 18.0)
        self.assertEqual(rows["r"].tau_rise, 3.0)
        self.assertEqual(rows["r"].tau_fall_err, 1.5)
        self.assertEqual(rows["r"].n_points, 11)
        self.assertEqual(rows["r"].dof, 6)
        self.assertAlmostEqual(rows["r"].reduced_chi2, 1.33)
        self.assertTrue(rows["r"].peak_after_last_non_detection)
        self.assertEqual(rows["r"].transient_id, self.transient.pk)
        self.assertFalse(rows["r"].is_subset)
        self.assertIsNotNone(rows["r"].fit_date)

    def test_peak_before_last_non_detection_is_flagged(self):
        run = self.make_run(self.bazin, bazin_result(peak_g=59990.0, last_nd=60000.0))
        self.assertFalse(BazinFit.objects.get(run=run, band="g-ZTF").peak_after_last_non_detection)
        run = self.make_run(self.bazin, bazin_result(last_nd=None))
        self.assertIsNone(BazinFit.objects.get(run=run, band="r").peak_after_last_non_detection)

    def test_older_result_falls_back_to_the_photometry_statistic(self):
        from YSE_App.models.phot_stat_models import TransientPhotStat

        TransientPhotStat.objects.update_or_create(transient=self.transient,
                                                   defaults={"last_non_detection_mjd": 60020.0})
        run = self.make_run(self.bazin, bazin_result(last_nd=...))
        row = BazinFit.objects.get(run=run, band="r")
        self.assertEqual(row.last_non_detection_mjd, 60020.0)
        self.assertFalse(row.peak_after_last_non_detection)

    def test_salt_run_writes_one_row(self):
        run = self.make_run(self.salt, salt_result())
        row = SaltFit.objects.get(run=run)
        self.assertEqual((row.model, row.model_version), ("salt3", "2.0"))
        self.assertEqual((row.t0, row.x1, row.c, row.m_b, row.z), (60012.0, 0.5, 0.02, 18.1, 0.03))
        self.assertEqual((row.x1_err, row.c_err, row.m_b_err), (0.2, 0.03, 0.05))
        self.assertEqual(row.dof, 25)
        self.assertEqual(row.bands, ["ztfg", "ztfr"])
        self.assertEqual(row.z_source, "transient")

    def test_failures_and_other_services_write_nothing(self):
        self.make_run(self.bazin, bazin_result(), status="failed")
        self.make_run(self.bazin, status="pending")
        self.make_run(self.salt, salt_result(model="nugent-sn1a"))
        self.make_run(self.other, bazin_result())
        self.make_run(self.bazin, {"reference_band": "r"})  # no per-band results
        self.assertEqual(BazinFit.objects.count(), 0)
        self.assertEqual(SaltFit.objects.count(), 0)

    def test_saving_again_writes_nothing_new(self):
        run = self.make_run(self.bazin, bazin_result())
        run.save()
        run.mark_succeeded(result=bazin_result())
        self.assertEqual(fit_tables.record_run(run), 0)
        self.assertEqual(BazinFit.objects.filter(run=run).count(), 2)

    def test_hook_error_does_not_break_the_run(self):
        with mock.patch.object(fit_tables, "record_run", side_effect=RuntimeError("boom")):
            run = self.make_run(self.bazin, bazin_result())
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(BazinFit.objects.count(), 0)

    def test_bazin_runner_reports_last_non_detection_before_first_detection(self):
        payload = {"photometry": [
            {"mjd": 59990.0, "upper_limit": True, "flagged": False},
            {"mjd": 59995.0, "upper_limit": True, "flagged": True},
            {"mjd": 60030.0, "upper_limit": True, "flagged": False},
            {"mjd": 60001.0, "upper_limit": False, "mag": 19.0, "mag_err": 0.1},
        ]}
        self.assertEqual(bazin_fit.last_non_detection_mjd(payload, 60001.0), 59990.0)
        self.assertEqual(bazin_fit.last_non_detection_mjd(payload, 60001.0, include_flagged=True), 59995.0)
        self.assertIsNone(bazin_fit.last_non_detection_mjd(payload, None))


class DefaultTests(FitBase):
    def test_defaults_come_from_the_newest_full_run(self):
        now = timezone.now()
        self.make_run(self.bazin, bazin_result(peak_r=60011.0), finished=now - datetime.timedelta(days=3))
        newest = self.make_run(self.bazin, bazin_result(peak_r=60013.0), finished=now - datetime.timedelta(days=1))
        subset = self.make_run(self.bazin, bazin_result(peak_r=60099.0), params={"selection": {"bands": ["r"]}},
                               finished=now)
        self.assertTrue(BazinFit.objects.filter(run=subset).first().is_subset)
        self.assertEqual(BazinFit.objects.filter(run=subset).first().selection, {"bands": ["r"]})
        self.assertEqual({row.run_id for row in fit_tables.latest_bazin(self.transient.pk)}, {newest.pk})
        self.assertEqual(fit_tables.latest_bazin_reference(self.transient.pk).peak_mjd, 60013.0)
        self.assertEqual(fit_tables.reference_epoch(self.transient.pk), (60013.0, "bazin"))

    def test_salt_is_the_alternative_reference(self):
        self.assertEqual(fit_tables.reference_epoch(self.transient.pk), (None, None))
        self.make_run(self.salt, salt_result(t0=60014.0))
        self.assertEqual(fit_tables.reference_epoch(self.transient.pk), (60014.0, "salt"))  # no Bazin fit
        self.make_run(self.bazin, bazin_result(peak_r=60011.0))
        self.assertEqual(fit_tables.reference_epoch(self.transient.pk), (60011.0, "bazin"))
        self.assertEqual(fit_tables.reference_epoch(self.transient.pk, prefer="salt"), (60014.0, "salt"))

    def test_history_marks_the_current_runs(self):
        now = timezone.now()
        old = self.make_run(self.bazin, bazin_result(), finished=now - datetime.timedelta(days=2))
        new = self.make_run(self.bazin, bazin_result(), finished=now - datetime.timedelta(days=1))
        self.make_run(self.salt, salt_result())
        history = fit_tables.history(self.transient.pk)
        self.assertEqual([e["run"].pk for e in history["bazin"]], [new.pk, old.pk])
        self.assertEqual([e["current"] for e in history["bazin"]], [True, False])
        self.assertTrue(history["bazin"][0]["rows"][0].is_reference_band)
        self.assertEqual(len(history["salt"]), 1)
        self.assertEqual(fit_tables.history(self.transient.pk, run_ids=[old.pk])["bazin"][0]["run"].pk, old.pk)


class BackfillTests(FitBase):
    def test_dry_run_then_apply(self):
        run = self.make_run(self.bazin, bazin_result())
        salt = self.make_run(self.salt, salt_result())
        self.make_run(self.bazin, bazin_result(), status="failed")
        BazinFit.objects.all().delete()
        SaltFit.objects.all().delete()

        out = io.StringIO()
        call_command("backfill_fit_tables", stdout=out)
        self.assertIn("dry run", out.getvalue())
        self.assertEqual(BazinFit.objects.count() + SaltFit.objects.count(), 0)

        call_command("backfill_fit_tables", "--apply", stdout=io.StringIO())
        self.assertEqual(BazinFit.objects.filter(run=run).count(), 2)
        self.assertEqual(SaltFit.objects.filter(run=salt).count(), 1)

        out = io.StringIO()
        call_command("backfill_fit_tables", "--apply", stdout=out)
        self.assertIn("2 already had rows", out.getvalue())
        self.assertEqual(BazinFit.objects.count(), 2)


class RefitRuleTests(SimpleTestCase):
    since = timezone.now()

    def point(self, mjd, *, detection):
        if detection:
            return PhotPoint(mjd=mjd, band_id=1, mag=19.0, mag_err=0.1)
        return PhotPoint(mjd=mjd, band_id=1, flux=1.0, flux_err=10.0, flux_zero_point=27.5)

    def reason(self, points, last_det=60010.0):
        later = self.since + datetime.timedelta(hours=1)
        return fit_tables.refit_reason([(later, p) for p in points], self.since, last_det, 7.0)

    def test_new_detection(self):
        self.assertEqual(self.reason([self.point(60020.0, detection=True)]), "new detection")

    def test_non_detection_within_the_window(self):
        self.assertIn("within 7 days", self.reason([self.point(60017.0, detection=False)]))

    def test_non_detection_outside_the_window_or_before_the_last_detection(self):
        self.assertIsNone(self.reason([self.point(60017.5, detection=False)]))
        self.assertIsNone(self.reason([self.point(60005.0, detection=False)]))
        self.assertIsNone(self.reason([self.point(60012.0, detection=False)], last_det=None))

    def test_points_stored_before_the_run_are_ignored(self):
        earlier = self.since - datetime.timedelta(hours=1)
        pts = [(earlier, self.point(60020.0, detection=True))]
        self.assertIsNone(fit_tables.refit_reason(pts, self.since, 60010.0, 7.0))


class RefitPassTests(FitBase):
    def setUp(self):
        super().setUp()
        self.obs_group, self.instrument, self.band = create_instrument_stack(self.staff, obs_group_name="fit-phot")
        self.photometry = TransientPhotometry.objects.create(transient=self.transient, instrument=self.instrument,
                                                             obs_group=self.obs_group, **audit_fields(self.staff))
        first = self.add_point(timezone.now() - datetime.timedelta(days=10), mag=19.0)
        TransientPhotData.objects.filter(pk=first.pk).update(created_date=timezone.now() - datetime.timedelta(days=2))
        self.run = self.make_run(self.bazin, bazin_result(), params={"extrapolate_days": 5.0})
        ExternalServiceRun.objects.filter(pk=self.run.pk).update(created_date=timezone.now() - datetime.timedelta(days=1))

    def add_point(self, obs_date, *, mag=None, flux=None, flux_err=None):
        return TransientPhotData.objects.create(photometry=self.photometry, band=self.band, obs_date=obs_date,
                                                mag=mag, mag_err=0.1 if mag is not None else None, flux=flux,
                                                flux_err=flux_err, flux_zero_point=27.5 if flux is not None else None,
                                                **audit_fields(self.staff))

    def test_nothing_new_means_no_refit(self):
        self.assertEqual(fit_tables.refit_candidates(), [])

    def test_new_detection_queues_one_refit_and_the_pass_is_idempotent(self):
        self.add_point(timezone.now() - datetime.timedelta(days=1), mag=18.5)
        candidates = fit_tables.refit_candidates()
        self.assertEqual([(c.transient_id, c.profile.pk, c.reason) for c in candidates],
                         [(self.transient.pk, self.bazin.pk, "new detection")])
        self.assertEqual(fit_tables.queue_refits(apply=False)["queued"], 0)
        with mock.patch("YSE_App.services.external_services.dispatch_run") as dispatch:
            summary = fit_tables.queue_refits(apply=True)
            self.assertEqual(summary["queued"], 1)
            self.assertEqual(dispatch.call_count, 1)
            new = ExternalServiceRun.objects.exclude(pk=self.run.pk).get()
            self.assertEqual(new.status, ExternalServiceRun.STATUS_PENDING)
            self.assertEqual(new.created_by, self.staff)
            self.assertTrue(new.request_payload["scheduled_refit"])
            self.assertEqual(new.request_payload["params"]["extrapolate_days"], 5.0)
            # a pending refit: the next pass queues nothing
            self.assertEqual(fit_tables.queue_refits(apply=True)["queued"], 0)
            # once it has finished, the data it saw are no longer new
            new.mark_succeeded(result=bazin_result())
            self.assertEqual(fit_tables.refit_candidates(), [])

    def test_non_detection_within_seven_days_after_the_last_detection(self):
        last = timezone.now() - datetime.timedelta(days=10)
        self.add_point(last + datetime.timedelta(days=8), flux=1.0, flux_err=10.0)
        self.assertEqual(fit_tables.refit_candidates(), [])
        self.add_point(last + datetime.timedelta(days=5), flux=1.0, flux_err=10.0)
        self.assertEqual(len(fit_tables.refit_candidates()), 1)

    def test_subset_runs_are_not_refitted_and_do_not_reset_the_clock(self):
        self.add_point(timezone.now() - datetime.timedelta(days=1), mag=18.5)
        self.make_run(self.bazin, bazin_result(), params={"selection": {"bands": ["r"]}})
        self.assertEqual(len(fit_tables.refit_candidates()), 1)

    @override_settings(FIT_REFIT_ENABLED=False)
    def test_flag_off(self):
        self.add_point(timezone.now() - datetime.timedelta(days=1), mag=18.5)
        with self.assertRaises(CommandError):
            call_command("refit_fits", "--apply", stdout=io.StringIO())
        out = io.StringIO()
        call_command("refit_fits", stdout=out)
        self.assertIn("1 refit(s) due", out.getvalue())
        self.assertEqual(fit_tables.refit_pass({}), {"skipped": "FIT_REFIT_ENABLED is off"})
        from YSE_App.data_ingest.Fit_Refit import FitRefit

        self.assertIn("disabled", FitRefit().do())
        self.assertEqual(ExternalServiceRun.objects.count(), 1)

    @override_settings(FIT_REFIT_ENABLED=True)
    def test_flag_on_command_queues(self):
        self.add_point(timezone.now() - datetime.timedelta(days=1), mag=18.5)
        out = io.StringIO()
        with mock.patch("YSE_App.services.external_services.dispatch_run"):
            call_command("refit_fits", "--apply", stdout=out)
        self.assertIn("1 queued", out.getvalue())


class FitHistoryViewTests(FitBase):
    def setUp(self):
        super().setUp()
        self.make_run(self.bazin, bazin_result())
        self.make_run(self.salt, salt_result())
        self.client = Client()
        self.client.force_login(self.staff)
        self.url = reverse("transient_detail_analysis_fragment", args=[self.transient.pk])

    def test_preview_off_page_unchanged(self):
        body = self.client.get(self.url).content.decode()
        self.assertNotIn("fit_history_card", body)

    def test_preview_on_shows_history(self):
        self.client.cookies[PREVIEW_COOKIE] = "1"
        body = self.client.get(self.url).content.decode()
        self.assertIn("fit_history_card", body)
        self.assertIn("tbl_fit_history_bazin", body)
        self.assertIn("tbl_fit_history_salt", body)
        self.assertIn("salt3 v2.0", body)


class ExplorerTests(TestCase):
    def test_fit_tables_are_staff_only(self):
        from YSE_App.services import explorer_scope as scope

        for table in ("YSE_App_bazinfit", "YSE_App_saltfit"):
            self.assertEqual(scope.classify(table, scope.TIER_MEMBER).kind, "refuse", table)
            self.assertEqual(scope.classify(table, scope.TIER_STAFF).kind, "pass", table)


class ForeignKeyListTests(SimpleTestCase):
    def test_every_foreign_key_of_the_new_tables_is_listed(self):
        expected = set()
        for model in (apps.get_model("YSE_App", "BazinFit"), apps.get_model("YSE_App", "SaltFit")):
            for field in model._meta.local_concrete_fields:
                if isinstance(field, models.ForeignKey):
                    self.assertIs(field.remote_field.on_delete, models.CASCADE)
                    expected.add((model._meta.db_table, field.column, field.related_model._meta.db_table,
                                  fit_migration.CASCADE))
        self.assertEqual(set(fit_migration.FOREIGN_KEYS), expected)


def _sql(statement, params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


class RawDeleteTests(TestCase):
    """Deletes as code that does not know the fit tables issues them (develop on the shared DB)."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")
        self.staff = create_test_user("fit_raw_staff", is_superuser=True)
        self.transient = create_minimal_transient(self.staff, name="2099fitraw", obs_group_name="fit-raw")
        self.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=self.staff, timeout_seconds=0)
        self.salt = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.staff, timeout_seconds=0)

    def _run(self, profile, result):
        run = ExternalServiceRun.objects.create(service=profile.service, transient=self.transient,
                                                created_by=self.staff, modified_by=self.staff)
        run.mark_succeeded(result=result)
        return run

    def test_constraints_carry_on_delete_cascade(self):
        from YSE_App.common.fk_on_delete import current_constraints

        for table, column, _, rule in fit_migration.FOREIGN_KEYS:
            self.assertEqual([row[3] for row in current_constraints(connection, table, column)], [rule])

    def test_run_delete_cascades(self):
        run = self._run(self.bazin, bazin_result())
        salt = self._run(self.salt, salt_result())
        self.assertEqual(BazinFit.objects.filter(run=run).count(), 2)
        _sql("DELETE FROM YSE_App_externalservicerun WHERE id = %s", [run.id])
        _sql("DELETE FROM YSE_App_externalservicerun WHERE id = %s", [salt.id])
        self.assertFalse(BazinFit.objects.exists())
        self.assertFalse(SaltFit.objects.exists())

    def test_transient_delete_cascades(self):
        run = self._run(self.bazin, bazin_result())
        self._run(self.salt, salt_result())
        new = {table for table, _, _, _ in fit_migration.FOREIGN_KEYS}
        with connection.cursor() as cursor:
            # What develop's Django does first for the tables it knows: SET_NULL on the runs,
            # delete the other children; the fit tables are left to MySQL.
            cursor.execute("UPDATE YSE_App_externalservicerun SET transient_id = NULL WHERE transient_id = %s",
                           [self.transient.id])
            cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                           "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'YSE_App_transient'")
            children = [(t, c) for t, c in cursor.fetchall() if t not in new and t != "YSE_App_externalservicerun"]
            for _attempt in range(3):  # a child row may itself have children: retry in a later pass
                for table, column in children:
                    try:
                        cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` = %s", [self.transient.id])
                    except IntegrityError:
                        pass
        _sql("DELETE FROM YSE_App_transient WHERE id = %s", [self.transient.id])
        self.assertFalse(BazinFit.objects.exists())
        self.assertFalse(SaltFit.objects.exists())
        self.assertTrue(ExternalServiceRun.objects.filter(pk=run.pk).exists())
