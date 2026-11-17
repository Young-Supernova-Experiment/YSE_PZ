"""Fit a subset of the data to recreate earlier circumstances (#847). Synthetic data only; no fitter runs.

Pins:

* the selection: dates as MJD or UTC ``YYYYMMDD.ddd``, bands, telescopes, quality levels, its
  canonical form (so a stored selection normalizes to itself) and the errors;
* the payload: a subset filters the points correctly (date cut, bands, telescopes, quality) and
  only ever narrows the points the requesting user may see;
* run parameters: the selection is validated and stored canonically, and "Repeat this fit"
  (the tab's run endpoint with the run's own parameters) reproduces the same selection;
* subset runs never supply defaults (Bazin, SALT, the Summary-tab SALT fit) and never trigger
  the spectral-phase recomputation; full runs still do;
* the Analysis tab and the transient page with the UI preview off (unchanged) and on.
"""

from __future__ import annotations

import datetime
import json
import os
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App.common import quality_mask as qm
from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.models import (
    ExternalServiceRun,
    Instrument,
    Observatory,
    PhotometricBand,
    SaltFit,
    Telescope,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.services import analysis_payload, fit_status, fit_subset, fit_tables
from YSE_App.services import analysis_services as svc
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user

MJD_EPOCH = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)


def at_mjd(mjd):
    return MJD_EPOCH + datetime.timedelta(days=float(mjd))


def bazin_result(peak=60011.0):
    band = {"A": 1.0, "t0": peak - 2.0, "tau_rise": 3.0, "tau_fall": 20.0, "B": 0.0, "chi2": 8.0, "dof": 6,
            "reduced_chi2": 1.33, "n_points": 11, "method": "joint", "peak_mjd": peak, "peak_mag": 18.0}
    return {"reference_band": "r", "bands": ["r"], "peak_mjd": peak, "per_band": {"r": band},
            "last_non_detection_mjd": 60000.0}


def salt_result(t0=60012.0):
    return {"model": "salt3", "model_version": "2.0", "z": 0.03, "z_source": "transient", "t0": t0, "t0_err": 0.1,
            "x0": 0.001, "x1": 0.5, "c": 0.02, "mB": 18.1, "chi2": 30.0, "ndof": 25, "n_points": 30,
            "bands": ["ztfg"]}


class SelectionTests(SimpleTestCase):
    def test_until_as_mjd_or_utc_date(self):
        self.assertEqual(fit_subset.parse_until("60500.25"), 60500.25)
        self.assertEqual(fit_subset.parse_until(60500), 60500.0)
        expected = (datetime.datetime(2024, 7, 15, 12, tzinfo=datetime.timezone.utc) - MJD_EPOCH).total_seconds() / 86400
        self.assertAlmostEqual(fit_subset.parse_until("20240715.5"), expected, places=5)
        self.assertIsNone(fit_subset.parse_until(""))
        self.assertIsNone(fit_subset.parse_until(None))
        for bad in ("abc", "20241345.1", "-5", "nan", True, "200000"):
            with self.assertRaises(fit_subset.SelectionError, msg=bad):
                fit_subset.parse_until(bad)

    def test_canonical_form_round_trips(self):
        raw = {"until": "20240715.5", "bands": "r, g,g", "telescopes": ["Tel-B", "Tel-A", ""],
               "quality": ["reduction", "best"]}
        clean = fit_subset.normalize_selection(raw)
        self.assertEqual(set(clean), {"until_mjd", "bands", "telescopes", "quality"})
        self.assertEqual(clean["bands"], ["g", "r"])
        self.assertEqual(clean["telescopes"], ["Tel-A", "Tel-B"])
        self.assertEqual(clean["quality"], ["best", "reduction"])  # level order, not the order given
        self.assertEqual(fit_subset.normalize_selection(clean), clean)
        self.assertEqual(fit_subset.normalize_selection(json.loads(json.dumps(clean))), clean)

    def test_no_selection_and_errors(self):
        for empty in (None, "", {}):
            self.assertIsNone(fit_subset.normalize_selection(empty))
        for bad in ({"bands": [], "until": ""}, {"colour": ["red"]}, {"quality": ["shiny"]}, ["r"],
                    {"bands": ["x" * 100]}, {"bands": 5}):
            with self.assertRaises(fit_subset.SelectionError, msg=repr(bad)):
                fit_subset.normalize_selection(bad)

    def test_quality_levels(self):
        cases = {
            0: "best", int(qm.PhotFlag.NOT_PUBLISHED): "best", int(qm.PhotFlag.APERTURE): "method",
            int(qm.PhotFlag.POOR_SEEING): "reduction", int(qm.PhotFlag.QUICKLOOK): "quicklook",
            int(qm.PhotFlag.CR): "artefact", int(qm.PhotFlag.OTHER): "artefact",
            int(qm.PhotFlag.REJECTED): "rejected",
            int(qm.PhotFlag.APERTURE | qm.PhotFlag.CR): "artefact", None: "best",
        }
        for mask, level in cases.items():
            self.assertEqual(fit_subset.quality_level(mask), level, mask)

    def test_point_matches(self):
        sel = {"until_mjd": 60010.0, "bands": ["r"], "telescopes": ["Tel-A"], "quality": ["best"]}
        ok = {"mjd": 60010.0, "band": "r", "telescope": "Tel-A", "mask": 0}
        self.assertTrue(fit_subset.point_matches(sel, **ok))  # the date cut is inclusive
        self.assertFalse(fit_subset.point_matches(sel, **dict(ok, mjd=60010.01)))
        self.assertFalse(fit_subset.point_matches(sel, **dict(ok, mjd=None)))
        self.assertFalse(fit_subset.point_matches(sel, **dict(ok, band="g")))
        self.assertFalse(fit_subset.point_matches(sel, **dict(ok, telescope="Tel-B")))
        self.assertFalse(fit_subset.point_matches(sel, **dict(ok, mask=int(qm.PhotFlag.QUICKLOOK))))
        self.assertTrue(fit_subset.point_matches(None, **dict(ok, band="g")))

    def test_summary_and_as_of(self):
        line = fit_subset.summary({"until_mjd": 60500.25, "bands": ["g", "r"], "telescopes": ["Tel-A"],
                                   "quality": ["best", "method"]})
        self.assertEqual(line, "points to MJD 60500.250 (20240709.250 UTC); bands g, r; telescopes Tel-A; "
                               "quality best, measurement-method caveat")
        self.assertEqual(fit_subset.summary(None), "")
        self.assertEqual(fit_subset.as_of_mjd({"selection": {"until_mjd": 60000.0}}, 61000.0), 60000.0)
        self.assertEqual(fit_subset.as_of_mjd({"selection": {"until_mjd": 62000.0}}, 61000.0), 61000.0)
        self.assertEqual(fit_subset.as_of_mjd({}, 61000.0), 61000.0)


class SubsetBase(TestCase):
    """Two telescopes with g and r bands; points on MJD 60001..60008 in two photometry sets."""

    def setUp(self):
        self.staff = create_test_user("subset_staff", is_superuser=True)
        self.member = create_test_user("subset_member", is_staff=False)
        self.transient = create_minimal_transient(self.staff, name="2099sub", obs_group_name="subset-group")
        audit = audit_fields(self.staff)
        obs = Observatory.objects.create(name="SubsetObs", utc_offset=0, tz_name="UTC", **audit)
        self.group_a = Group.objects.create(name="subset-a")
        self.group_b = Group.objects.create(name="subset-b")
        self.points = {}
        for tel_name, group in (("Tel-A", self.group_a), ("Tel-B", self.group_b)):
            tel = Telescope.objects.create(name=tel_name, observatory=obs, latitude=0.0, longitude=0.0,
                                           elevation=0.0, **audit)
            inst = Instrument.objects.create(name=tel_name + "-cam", telescope=tel, **audit)
            phot = TransientPhotometry.objects.create(transient=self.transient, instrument=inst,
                                                      obs_group=self.transient.obs_group, **audit)
            phot.groups.add(group)
            for band_name in ("g", "r"):
                band = PhotometricBand.objects.create(name=band_name, instrument=inst, disp_color="#888888",
                                                      disp_symbol="circle", **audit)
                for day in range(1, 9):
                    mask = int(qm.PhotFlag.POOR_SEEING) if day % 4 == 0 else 0
                    point = TransientPhotData.objects.create(
                        photometry=phot, band=band, obs_date=at_mjd(60000 + day), mag=18.0 + 0.1 * day,
                        mag_err=0.05, quality_mask=mask, **audit)
                    self.points[(tel_name, band_name, day)] = point.pk
        # Staff see only the data their groups allow, like everyone else.
        self.staff.groups.add(self.group_a, self.group_b)
        self.member.groups.add(self.group_a)

    def rows(self, user, selection):
        params = {"selection": selection} if selection is not None else {}
        return analysis_payload.build_payload(self.transient, user, ["photometry"], params)["photometry"]

    def ids(self, keys):
        return {self.points[k] for k in keys}


class PayloadFilterTests(SubsetBase):
    def test_no_selection_is_every_visible_point(self):
        self.assertEqual(len(self.rows(self.staff, None)), 32)

    def test_date_cut(self):
        rows = self.rows(self.staff, {"until_mjd": 60003.0})
        self.assertEqual({r["id"] for r in rows},
                         self.ids((t, b, d) for t in ("Tel-A", "Tel-B") for b in ("g", "r") for d in (1, 2, 3)))
        self.assertTrue(all(r["mjd"] <= 60003.0 for r in rows))
        same = self.rows(self.staff, fit_subset.normalize_selection({"until": fit_subset._utc(60003.0)}))
        self.assertEqual({r["id"] for r in same}, {r["id"] for r in rows})  # UTC date form, same cut

    def test_bands_and_telescopes(self):
        rows = self.rows(self.staff, {"bands": ["r"], "telescopes": ["Tel-B"]})
        self.assertEqual({r["id"] for r in rows}, self.ids(("Tel-B", "r", d) for d in range(1, 9)))

    def test_quality(self):
        rows = self.rows(self.staff, {"quality": ["reduction"]})
        self.assertEqual({r["id"] for r in rows},
                         self.ids((t, b, d) for t in ("Tel-A", "Tel-B") for b in ("g", "r") for d in (4, 8)))
        self.assertEqual(len(self.rows(self.staff, {"quality": ["best"]})), 24)

    def test_combined(self):
        rows = self.rows(self.staff, {"until_mjd": 60005.0, "bands": ["g"], "telescopes": ["Tel-A"],
                                      "quality": ["best"]})
        self.assertEqual({r["id"] for r in rows}, self.ids(("Tel-A", "g", d) for d in (1, 2, 3, 5)))

    def test_access_is_respected(self):
        visible = {r["id"] for r in self.rows(self.member, None)}
        self.assertEqual(visible, self.ids((t, b, d) for t in ("Tel-A",) for b in ("g", "r") for d in range(1, 9)))
        # Naming the other telescope only narrows: it never adds points the user may not see.
        self.assertEqual(self.rows(self.member, {"telescopes": ["Tel-A", "Tel-B"]}), self.rows(self.member, None))
        self.assertEqual(self.rows(self.member, {"telescopes": ["Tel-B"]}), [])
        self.assertTrue({r["id"] for r in self.rows(self.member, {"until_mjd": 60008.0})} <= visible)

    def test_form_options_are_from_visible_points(self):
        self.assertEqual(fit_subset.options(self.member, self.transient)["telescopes"], ["Tel-A"])
        opts = fit_subset.options(self.staff, self.transient)
        self.assertEqual(opts["bands"], ["g", "r"])
        self.assertEqual(opts["telescopes"], ["Tel-A", "Tel-B"])
        self.assertEqual([key for key, _label in opts["quality"]], ["best", "reduction"])

    def test_bad_stored_selection_fails_the_payload(self):
        with self.assertRaises(fit_subset.SelectionError):
            self.rows(self.staff, {"quality": ["shiny"]})


@override_settings(JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False)
class RunParamsTests(SubsetBase):
    def setUp(self):
        super().setUp()
        self.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=self.staff, timeout_seconds=0)
        self.salt = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.staff, timeout_seconds=0)

    def test_selection_is_stored_canonically(self):
        clean = svc.coerce_params(self.bazin, {"selection": {"until": "60003", "bands": "r"}})
        self.assertEqual(clean["selection"], {"until_mjd": 60003.0, "bands": ["r"]})
        self.assertEqual(clean["extrapolate_days"], 7.0)  # the service defaults still apply
        self.assertNotIn("selection", svc.coerce_params(self.bazin, {"selection": {}}))
        with self.assertRaises(svc.InvalidParams) as ctx:
            svc.coerce_params(self.bazin, {"selection": {"until": "yesterday"}})
        self.assertIn("selection", ctx.exception.errors)

    def test_repeat_reproduces_the_selection(self):
        client = Client()
        client.force_login(self.staff)
        url = reverse("transient_analysis_run", args=[self.transient.pk])
        with mock.patch("YSE_App.services.external_services.dispatch_run"):
            first = client.post(url, json.dumps({"service": "bazin_fit", "params": {
                "selection": {"until": "20230301.5", "quality": ["best"], "telescopes": ["Tel-A"]}}}),
                content_type="application/json")
            self.assertEqual(first.status_code, 200, first.content)
            run1 = ExternalServiceRun.objects.get(uuid=first.json()["run"]["uuid"])
            # "Repeat this fit" posts the run's own parameters unchanged.
            again = client.post(url, json.dumps({"service": "bazin_fit", "params": svc.run_params(run1)}),
                                content_type="application/json")
            self.assertEqual(again.status_code, 200, again.content)
        run2 = ExternalServiceRun.objects.get(uuid=again.json()["run"]["uuid"])
        self.assertNotEqual(run1.pk, run2.pk)
        self.assertEqual(svc.run_params(run2), svc.run_params(run1))
        self.assertEqual(fit_tables.selection_of(run2), fit_tables.selection_of(run1))
        rows1 = analysis_payload.build_payload(self.transient, self.staff, ["photometry"], svc.run_params(run1))
        rows2 = analysis_payload.build_payload(self.transient, self.staff, ["photometry"], svc.run_params(run2))
        self.assertEqual(rows1["photometry"], rows2["photometry"])
        self.assertTrue(rows1["photometry"])

    def test_invalid_selection_is_a_400(self):
        client = Client()
        client.force_login(self.staff)
        url = reverse("transient_analysis_run", args=[self.transient.pk])
        with mock.patch("YSE_App.services.external_services.dispatch_run") as dispatch:
            response = client.post(url, json.dumps({"service": "bazin_fit", "params": {"selection": {"bands": []}}}),
                                   content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("selection", response.json()["errors"])
        dispatch.assert_not_called()


class DefaultsAndPhaseTests(SubsetBase):
    def setUp(self):
        super().setUp()
        self.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=self.staff, timeout_seconds=0)
        self.salt = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.staff, timeout_seconds=0)

    def make_run(self, profile, result, params=None):
        run = ExternalServiceRun.objects.create(
            service=profile.service, transient=self.transient, created_by=self.staff, modified_by=self.staff,
            request_payload={"analysis": True, "params": params or {}})
        run.mark_succeeded(result=result)
        return run

    def test_subset_runs_never_trigger_phase_recomputation(self):
        with mock.patch("YSE_App.services.spectral_phase.on_fits_recorded") as recompute:
            self.make_run(self.bazin, bazin_result(), {"selection": {"until_mjd": 60003.0}})
            self.make_run(self.salt, salt_result(), {"selection": {"bands": ["g"]}})
            recompute.assert_not_called()
            self.make_run(self.bazin, bazin_result())
            recompute.assert_called_once_with(self.transient.pk)

    def test_subset_runs_are_never_the_default(self):
        full_bazin = self.make_run(self.bazin, bazin_result(peak=60011.0))
        full_salt = self.make_run(self.salt, salt_result(t0=60012.0))
        sub_bazin = self.make_run(self.bazin, bazin_result(peak=60050.0), {"selection": {"until_mjd": 60003.0}})
        sub_salt = self.make_run(self.salt, salt_result(t0=60060.0), {"selection": {"until_mjd": 60003.0}})
        self.assertTrue(SaltFit.objects.get(run=sub_salt).is_subset)
        self.assertEqual(SaltFit.objects.get(run=sub_salt).selection, {"until_mjd": 60003.0})
        self.assertEqual({row.run_id for row in fit_tables.latest_bazin(self.transient.pk)}, {full_bazin.pk})
        self.assertEqual(fit_tables.latest_salt(self.transient.pk).run_id, full_salt.pk)
        self.assertEqual(fit_tables.reference_epoch(self.transient.pk), (60011.0, "bazin"))
        # The Summary tab's stored SALT fit and its status line ignore subset runs too.
        self.assertEqual(fit_status.stored_salt_fit(self.transient).pk, full_salt.pk)
        self.assertEqual(fit_status.latest_run(self.transient, "sncosmo_fit").pk, full_salt.pk)
        history = fit_tables.history(self.transient.pk)
        self.assertEqual([e["current"] for e in history["bazin"]], [False, True])
        self.assertEqual(history["bazin"][0]["run"].pk, sub_bazin.pk)


@override_settings(UI_PREVIEW_DEFAULT=False)
class PreviewTests(SubsetBase):
    def setUp(self):
        super().setUp()
        self.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=self.staff, timeout_seconds=0)
        run = ExternalServiceRun.objects.create(
            service=self.bazin.service, transient=self.transient, created_by=self.staff, modified_by=self.staff,
            request_payload={"analysis": True, "params": {"selection": {"until_mjd": 60003.0, "bands": ["r"]}}})
        run.mark_succeeded(result=bazin_result())
        self.url = reverse("transient_detail_analysis_fragment", args=[self.transient.pk])

    def client_for(self, preview):
        client = Client()
        client.force_login(self.staff)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client

    def test_preview_off_tab_unchanged(self):
        body = self.client_for(False).get(self.url).content.decode()
        for marker in ("fit_subset_card", "fit_subset_form", "fit-subset-repeat", "fit-subset-summary",
                       "Fit part of the data"):
            self.assertNotIn(marker, body)
        self.assertIn("analysis_run_form", body)

    def test_preview_on_shows_form_history_line_and_repeat(self):
        body = self.client_for(True).get(self.url).content.decode()
        self.assertIn('id="fit_subset_form"', body)
        self.assertIn('value="Tel-B"', body)
        self.assertIn('id="fit_subset_q_reduction"', body)
        self.assertIn("fit-subset-summary", body)
        self.assertIn("points to MJD 60003.000", body)
        self.assertIn("bands r", body)
        self.assertIn("fit-subset-repeat", body)
        self.assertIn('data-service="bazin_fit"', body)
        self.assertIn("&quot;until_mjd&quot;: 60003.0", body)

    def test_detail_page_toolbar_and_script(self):
        url = reverse("transient_detail", kwargs={"slug": self.transient.slug})
        with mock.patch.dict(os.environ, {"YSE_TRANSIENT_DETAIL_DEFER": "1"}):
            off = self.client_for(False).get(url).content.decode()
            on = self.client_for(True).get(url).content.decode()
        self.assertNotIn("fit-subset.js", off)
        self.assertNotIn("yse-fit-subset-open", off)
        self.assertIn("fit-subset.js?v=subset-fits-1", on)
        self.assertIn("yse-fit-subset-open", on)
