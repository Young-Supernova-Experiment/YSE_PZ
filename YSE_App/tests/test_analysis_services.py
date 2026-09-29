"""Analysis-service framework (#312: #313, #314, first slice of #315).

Pins:

* the registry (``register_service``: in-process and webhook, spec validation,
  the management command) and visibility by group;
* the payload builder (photometry with sncosmo band names, upper limits,
  redshift fallback to the host, spectra);
* both in-process runners on synthetic light curves: the Bazin fit recovers
  the peak, the sncosmo fit recovers ``t0`` with a synthetic source and
  bandpass registered in-process (nothing is downloaded; the real SALT3 path
  is skipped unless the model is cached locally);
* the job-queue path end to end (``run_pass`` -> ``execute_run`` -> runner ->
  stored plots / files / results), failures with a clear error, timeouts;
* the webhook runner with mocked HTTP (token minted, payload shape, bearer
  from the credential, HTTP and transport errors, synchronous replies) and
  completion through the callback endpoint with base64 and multipart
  attachments;
* the Analysis tab fragment (empty, with runs, a failed run's error text,
  other users' runs hidden without data access), the run action, the daily
  cap (429, staff exempt), status polling, file download access, delete;
* the DRF endpoints.
"""

from __future__ import annotations

import base64
import datetime
import io
import json
import os
import shutil
import tempfile
import time
from unittest import mock

import numpy as np
from cryptography.fernet import Fernet
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.analysis import bazin_fit, runners, sncosmo_fit
from YSE_App.analysis.base import AnalysisError, AnalysisResult
from YSE_App.jobs import run_pass
from YSE_App.models import (
    AnalysisResultFile,
    AnalysisService,
    EncryptedCredential,
    ExternalService,
    ExternalServiceRun,
    Host,
    Instrument,
    Job,
    Observatory,
    PhotometricBand,
    Telescope,
    TransientPhotData,
    TransientPhotometry,
    TransientSpecData,
    TransientSpectrum,
)
from YSE_App.services import analysis_payload, analysis_services as svc, bazin
from YSE_App.services import external_services as runs
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user

KEY = Fernet.generate_key().decode()
MEDIA = tempfile.mkdtemp(prefix="yse-analysis-media-")
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
SYNTH_SOURCE = "yse-test-gauss"


def _register_synthetic_sncosmo():
    """A Gaussian time-series source and a GPC1-r bandpass under the names bandpassdict expects (no download)."""
    import sncosmo

    phase = np.linspace(-30.0, 80.0, 111)
    wave = np.linspace(3000.0, 10000.0, 71)
    flux = (np.exp(-0.5 * (phase[:, None] / 12.0) ** 2) * np.exp(-0.5 * ((wave[None, :] - 5800.0) / 1800.0) ** 2)) * 1e-15
    source = sncosmo.TimeSeriesSource(phase, wave, flux, name=SYNTH_SOURCE)
    sncosmo.register(source, SYNTH_SOURCE, force=True)
    for name, centre in (("sdssg", 4770.0), ("sdssr", 6230.0), ("sdssi", 7620.0)):
        band = sncosmo.Bandpass([centre - 700.0, centre - 350.0, centre, centre + 350.0, centre + 700.0],
                                [0.0, 0.8, 1.0, 0.8, 0.0], name=name)
        sncosmo.register(band, name, force=True)
    return source


def _salt3_available() -> bool:
    """True when sncosmo already has the SALT3 model cached (never triggers a download)."""
    try:
        from astropy.config.paths import get_cache_dir

        root = os.path.join(get_cache_dir(), "sncosmo")
    except Exception:  # pragma: no cover
        return False
    if not os.path.isdir(root):
        return False
    for dirpath, _dirs, files in os.walk(root):
        if any("salt3" in f.lower() for f in files) or "salt3" in dirpath.lower():
            return True
    return False


def _gpc1_bands(user):
    audit = audit_fields(user)
    obs, _ = Observatory.objects.get_or_create(name="AnalysisObs", defaults={"utc_offset": -10, "tz_name": "US/Hawaii", **audit})
    tel, _ = Telescope.objects.get_or_create(name="Pan-STARRS1", defaults={"observatory": obs, "latitude": 20.7, "longitude": -156.25, "elevation": 3052.0, **audit})
    inst, _ = Instrument.objects.get_or_create(name="GPC1", defaults={"telescope": tel, **audit})
    bands = {}
    for n in ("g", "r", "i"):
        bands[n], _ = PhotometricBand.objects.get_or_create(name=n, instrument=inst, defaults={"disp_color": "#888888", "disp_symbol": "circle", **audit})
    return inst, bands


def seed_bazin_lightcurve(user, transient, *, peak_offset=12.0, group=None, n=10, step=3.0):
    """Three GPC1 bands following a Bazin curve peaking ``peak_offset`` days ago, one upper limit per band."""
    inst, bands = _gpc1_bands(user)
    audit = audit_fields(user)
    phot = TransientPhotometry.objects.create(transient=transient, instrument=inst, obs_group=transient.obs_group, **audit)
    if group is not None:
        phot.groups.add(group)
    now_mjd = bazin.datetime_to_mjd(timezone.now())
    rng = np.random.default_rng(11)
    rows = []
    truth = {}
    for bname, A, shift, tf, tr in (("g", 2500, -1.5, 22, 4), ("r", 3000, 0.0, 30, 5), ("i", 2600, 1.5, 36, 6)):
        start = now_mjd - peak_offset - 9
        mjd = start + step * np.arange(n) + rng.uniform(-0.3, 0.3, n)
        mjd = mjd[mjd < now_mjd - 0.5]
        params = (A, now_mjd - peak_offset + shift, tf, tr, 20.0)
        truth[bname] = params
        mag = bazin.flux_to_mag(bazin.bazin_flux(mjd, *params)) + rng.normal(0, 0.03, len(mjd))
        for i, (m, y) in enumerate(zip(mjd, mag)):
            rows.append(TransientPhotData(photometry=phot, band=bands[bname], obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(m)),
                                          mag=float(y), mag_err=0.05, discovery_point=(i == 0 and bname == "r"), **audit))
        rows.append(TransientPhotData(photometry=phot, band=bands[bname], obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(mjd[0]) - 3),
                                      mag=None, mag_err=None, flux=10.0, flux_err=20.0, flux_zero_point=27.5, **audit))
    TransientPhotData.objects.bulk_create(rows)
    return phot, truth, now_mjd


def seed_sncosmo_lightcurve(user, transient, *, t0_offset=15.0, z=0.03, amplitude=0.2):
    """g/r/i points drawn from the synthetic sncosmo source at ``z`` peaking ``t0_offset`` days ago."""
    import sncosmo

    _register_synthetic_sncosmo()
    inst, bands = _gpc1_bands(user)
    audit = audit_fields(user)
    phot = TransientPhotometry.objects.create(transient=transient, instrument=inst, obs_group=transient.obs_group, **audit)
    now_mjd = bazin.datetime_to_mjd(timezone.now())
    t0 = now_mjd - t0_offset
    model = sncosmo.Model(source=SYNTH_SOURCE)
    model.set(z=z, t0=t0, amplitude=amplitude)  # 0.2 puts the peak near 18th mag on the 27.5 zero point
    rng = np.random.default_rng(5)
    rows = []
    for bname, sband in (("g", "sdssg"), ("r", "sdssr"), ("i", "sdssi")):
        mjd = t0 + np.arange(-12.0, 30.0, 3.0) + rng.uniform(-0.2, 0.2, 14)
        flux = model.bandflux(sband, mjd, zp=27.5, zpsys="ab")
        mag = -2.5 * np.log10(flux) + 27.5 + rng.normal(0, 0.03, len(mjd))
        for m, y in zip(mjd, mag):
            rows.append(TransientPhotData(photometry=phot, band=bands[bname], obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(m)),
                                          mag=float(y), mag_err=0.04, **audit))
    TransientPhotData.objects.bulk_create(rows)
    return t0


def _slow_runner_module(sleep_seconds):
    import types

    mod = types.ModuleType("yse_test_slow_runner")

    def run(payload, params):
        time.sleep(sleep_seconds)
        return AnalysisResult(results={"slept": sleep_seconds})

    mod.run = run
    return mod


class _FakeResponse:
    def __init__(self, status_code=202, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@override_settings(CREDENTIALS_KEY=KEY, MEDIA_ROOT=MEDIA, JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False)
class AnalysisBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.staff = create_test_user("an_staff", is_superuser=True)
        self.user = create_test_user("an_user", is_staff=False)
        self.other = create_test_user("an_other", is_staff=False)
        self.transient = create_minimal_transient(self.staff, name="2026anl", obs_group_name="an-group")
        self.transient.redshift = 0.03
        self.transient.mw_ebv = 0.02
        self.transient.save(update_fields=["redshift", "mw_ebv"])
        self.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=self.staff, timeout_seconds=0)
        self.sncosmo = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.staff, timeout_seconds=0,
                                            default_params={"source": SYNTH_SOURCE})
        self.client = Client()

    def _run_queue(self):
        return run_pass(worker_id="test")


class RegistryTests(AnalysisBase):
    def test_register_inprocess_copies_module_metadata(self):
        self.assertEqual(self.bazin.service.kind, ExternalService.KIND_ANALYSIS)
        self.assertEqual(self.bazin.runner_kind, AnalysisService.RUNNER_INPROCESS)
        self.assertEqual(self.bazin.service.name, bazin_fit.NAME)
        self.assertEqual(self.bazin.input_spec, ["photometry"])
        self.assertIn("plots", self.bazin.output_spec)
        self.assertIn("extrapolate_days", self.bazin.param_schema)
        self.assertEqual(svc.load_runner_module("bazin_fit"), bazin_fit)
        self.assertEqual(svc.load_runner_module("YSE_App.analysis.sncosmo_fit"), sncosmo_fit)

    def test_register_webhook_and_update(self):
        cred = EncryptedCredential(name="NGSF", service="ngsf", kind="analysis", created_by=self.staff, modified_by=self.staff)
        cred.set_secret({"api_token": "abc"})
        cred.save()
        grp = Group.objects.create(name="Spectra people")
        p = svc.register_service("ngsf", "NGSF", base_url="https://ngsf.example.org/run", user=self.staff,
                                 input_spec=["spectra", "redshift"], output_spec=["results", "plots", "files"],
                                 param_schema={"n_templates": {"type": "integer", "default": 5}},
                                 timeout_seconds=1800, max_runs_per_user_per_day=3, groups=[grp], credential=cred)
        self.assertTrue(p.is_webhook)
        self.assertEqual(p.service.base_url, "https://ngsf.example.org/run")
        self.assertEqual(p.service.max_runs_per_user_per_day, 3)
        self.assertEqual(list(p.service.groups.all()), [grp])
        self.assertEqual(p.default_params(), {"n_templates": 5})
        # a second call updates in place
        p2 = svc.register_service("ngsf", "NGSF v2", base_url="https://ngsf.example.org/v2", user=self.staff)
        self.assertEqual(p2.pk, p.pk)
        self.assertEqual(ExternalService.objects.filter(slug="ngsf").count(), 1)
        self.assertEqual(p2.service.name, "NGSF v2")

    def test_register_validates(self):
        with self.assertRaises(svc.AnalysisConfigError):
            svc.register_service("x", runner="bazin_fit", base_url="https://x")
        with self.assertRaises(svc.AnalysisConfigError):
            svc.register_service("x")
        with self.assertRaises(svc.AnalysisConfigError):
            svc.register_service("x", runner="no.such.module.here")
        with self.assertRaises(svc.AnalysisConfigError):
            svc.register_service("x", runner="bazin_fit", input_spec=["cubes"])

    def test_management_command(self):
        out = io.StringIO()
        call_command("register_analysis_service", "--builtin", "bazin_fit", "--cap", "7", stdout=out)
        self.assertIn("bazin_fit", out.getvalue())
        self.assertEqual(ExternalService.objects.get(slug="bazin_fit").max_runs_per_user_per_day, 7)
        call_command("register_analysis_service", "hook", "Hook", "--url", "https://h.example.org/run",
                     "--input", "spectra", "--output", "results", "--timeout", "60", stdout=out)
        p = AnalysisService.objects.get(service__slug="hook")
        self.assertTrue(p.is_webhook)
        self.assertEqual(p.input_spec, ["spectra"])
        self.assertEqual(p.timeout_seconds, 60)
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("register_analysis_service", "bad", "--url", "https://x", "--builtin", "bazin_fit")

    def test_services_for_user_respects_groups_and_enabled(self):
        grp = Group.objects.create(name="fitters")
        self.sncosmo.service.groups.add(grp)
        self.assertEqual({p.slug for p in svc.services_for_user(self.user)}, {"bazin_fit"})
        self.user.groups.add(grp)
        self.assertEqual({p.slug for p in svc.services_for_user(self.user)}, {"bazin_fit", "sncosmo_fit"})
        self.assertEqual({p.slug for p in svc.services_for_user(self.staff)}, {"bazin_fit", "sncosmo_fit"})
        self.bazin.service.enabled = False
        self.bazin.service.save()
        self.assertEqual({p.slug for p in svc.services_for_user(self.user)}, {"sncosmo_fit"})
        self.assertEqual({p.slug for p in svc.services_for_user(self.staff, include_disabled=True)}, {"bazin_fit", "sncosmo_fit"})

    def test_coerce_params(self):
        clean = svc.coerce_params(self.sncosmo, {"minsnr": "4", "fit_redshift": "true", "source": SYNTH_SOURCE})
        self.assertEqual(clean["minsnr"], 4.0)
        self.assertIs(clean["fit_redshift"], True)
        self.assertEqual(clean["window_before"], 20.0)  # default filled in
        self.assertEqual(svc.coerce_params(self.sncosmo, {"source": "salt2"})["source"], "salt2")
        with self.assertRaises(svc.InvalidParams) as ctx:
            svc.coerce_params(self.sncosmo, {"minsnr": "abc", "source": "nope"})
        self.assertIn("minsnr", ctx.exception.errors)
        self.assertIn("source", ctx.exception.errors)

    def test_form_fields(self):
        fields = {f["name"]: f for f in svc.form_fields(self.sncosmo)}
        self.assertEqual(fields["source"]["type"], "choice")
        self.assertEqual(fields["source"]["default"], SYNTH_SOURCE)  # service default_params override the schema
        self.assertEqual(fields["fit_redshift"]["type"], "boolean")


class PayloadTests(AnalysisBase):
    def test_photometry_payload(self):
        seed_bazin_lightcurve(self.staff, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"], {"a": 1})
        self.assertEqual(payload["redshift"], 0.03)
        self.assertEqual(payload["transient"]["redshift_source"], "transient")
        self.assertEqual(payload["transient"]["mw_ebv"], 0.02)
        self.assertEqual(payload["params"], {"a": 1})
        rows = payload["photometry"]
        n_rows = TransientPhotData.objects.filter(photometry__transient=self.transient).count()
        self.assertEqual(len(rows), n_rows)
        limits = [r for r in rows if r["upper_limit"]]
        self.assertEqual(len(limits), 3)
        dets = analysis_payload.detections(payload)
        self.assertEqual(len(dets), n_rows - 3)
        self.assertEqual({r["sncosmo_band"] for r in dets}, {"sdssg", "sdssr", "sdssi"})
        self.assertTrue(all(r["flux"] > 0 and r["flux_err"] > 0 and r["zp"] == 27.5 for r in dets))
        self.assertEqual(payload["spectra"], [])
        self.assertTrue(all(r["mjd"] == r["mjd"] for r in rows))

    def test_redshift_falls_back_to_host_and_spectra_included(self):
        self.transient.redshift = None
        audit = audit_fields(self.staff)
        host = Host.objects.create(ra=10.0, dec=20.0, redshift=0.055, **audit)
        self.transient.host = host
        self.transient.save()
        inst, _bands = _gpc1_bands(self.staff)
        spec = TransientSpectrum.objects.create(transient=self.transient, instrument=inst, obs_group=self.transient.obs_group,
                                                ra=10.0, dec=20.0, obs_date=timezone.now(), **audit)
        TransientSpecData.objects.bulk_create([
            TransientSpecData(spectrum=spec, wavelength=4000.0 + 10 * i, flux=1.0 + i, flux_err=0.1, **audit) for i in range(5)
        ])
        payload = analysis_payload.build_payload(self.transient, self.staff, ["spectra", "redshift"])
        self.assertEqual(payload["redshift"], 0.055)
        self.assertEqual(payload["transient"]["redshift_source"], "host")
        self.assertEqual(payload["transient"]["host"]["redshift"], 0.055)
        self.assertEqual(payload["photometry"], [])
        self.assertEqual(len(payload["spectra"]), 1)
        self.assertEqual(payload["spectra"][0]["n_points"], 5)
        self.assertEqual(payload["spectra"][0]["wavelength"][0], 4000.0)
        self.assertEqual(payload["spectra"][0]["flux_err"], [0.1] * 5)

    def test_payload_respects_photometry_visibility(self):
        grp = Group.objects.create(name="private-phot")
        seed_bazin_lightcurve(self.staff, self.transient, group=grp)
        self.other.groups.add(grp)
        n_rows = TransientPhotData.objects.filter(photometry__transient=self.transient).count()
        self.assertEqual(len(analysis_payload.build_payload(self.transient, self.other)["photometry"]), n_rows)
        self.assertEqual(len(analysis_payload.build_payload(self.transient, self.user)["photometry"]), 0)


class InProcessRunnerTests(AnalysisBase):
    def test_bazin_runner_recovers_peak(self):
        _phot, truth, now_mjd = seed_bazin_lightcurve(self.staff, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry"])
        result = bazin_fit.run(payload, {"extrapolate_days": 5})
        self.assertIsInstance(result, AnalysisResult)
        r = result.results
        self.assertEqual(sorted(r["bands"]), ["g", "i", "r"])
        self.assertIn(r["reference_band"], ("g", "r", "i"))
        A, t0, tf, tr, _B = truth[r["reference_band"]]
        true_peak = t0 + tr * np.log(tf / tr - 1.0)
        self.assertLess(abs(r["peak_mjd"] - true_peak), 2.0, r)
        self.assertIsNotNone(r["mag_extrapolated"])
        self.assertAlmostEqual(r["extrapolate_mjd"], max(f["mjd_max"] for f in r["per_band"].values()) + 5, places=3)
        self.assertEqual([p.name for p in result.plots], ["lightcurve.png"])
        self.assertTrue(result.plots[0].data.startswith(b"\x89PNG"))
        self.assertEqual({f.name for f in result.files}, {"bazin_fit.json", "model_curves.json"})
        fit_json = json.loads([f for f in result.files if f.name == "bazin_fit.json"][0].data)
        self.assertEqual(len(fit_json["bands"]["r"]["cov"]), 5)

    def test_bazin_runner_too_few_points(self):
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry"])
        with self.assertRaises(AnalysisError) as ctx:
            bazin_fit.run(payload, {})
        self.assertIn("at least", str(ctx.exception))

    def test_sncosmo_runner_with_synthetic_source(self):
        t0 = seed_sncosmo_lightcurve(self.staff, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        result = sncosmo_fit.run(payload, {"source": SYNTH_SOURCE, "mw_extinction": False})
        r = result.results
        self.assertEqual(r["model"], SYNTH_SOURCE)
        self.assertEqual(r["z"], 0.03)
        self.assertEqual(r["z_source"], "transient")
        self.assertLess(abs(r["t0"] - t0), 1.0, r)
        self.assertIn("t0_err", r)
        self.assertIn("amplitude", r)
        self.assertGreater(r["n_points"], 20)
        self.assertEqual(sorted(r["bands"]), ["sdssg", "sdssi", "sdssr"])
        self.assertIsNotNone(r["reduced_chi2"])
        names = [p.name for p in result.plots]
        self.assertEqual(names, ["lightcurve.png", "corner.png"])
        self.assertEqual({f.name for f in result.files}, {"fit_result.json", "model_curves.json"})
        fit_json = json.loads([f for f in result.files if f.name == "fit_result.json"][0].data)
        self.assertEqual(fit_json["vparam_names"], ["t0", "amplitude"])
        self.assertEqual(len(fit_json["covariance"]), 2)
        curves = json.loads([f for f in result.files if f.name == "model_curves.json"][0].data)
        self.assertIn("sdssr", curves["bands"])

    def test_sncosmo_runner_with_mw_extinction(self):
        seed_sncosmo_lightcurve(self.staff, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        result = sncosmo_fit.run(payload, {"source": SYNTH_SOURCE, "mw_extinction": True})
        self.assertEqual(result.results["mwebv"], 0.02)
        self.assertIn("t0", result.results)

    def test_sncosmo_runner_needs_redshift_or_fits_it(self):
        seed_sncosmo_lightcurve(self.staff, self.transient)
        self.transient.redshift = None
        self.transient.save(update_fields=["redshift"])
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        with self.assertRaises(AnalysisError) as ctx:
            sncosmo_fit.run(payload, {"source": SYNTH_SOURCE})
        self.assertIn("redshift", str(ctx.exception))
        result = sncosmo_fit.run(payload, {"source": SYNTH_SOURCE, "fit_redshift": True, "z_max": 0.2, "mw_extinction": False})
        self.assertEqual(result.results["z_source"], "fitted")
        self.assertIn("z_err", result.results)
        self.assertGreater(result.results["z"], 0.0)

    def test_sncosmo_runner_unknown_source_is_an_analysis_error(self):
        seed_sncosmo_lightcurve(self.staff, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        import sncosmo

        with mock.patch.object(sncosmo, "Model", side_effect=OSError("no download")):
            with self.assertRaises(AnalysisError) as ctx:
                sncosmo_fit.run(payload, {"source": "salt3"})
        self.assertIn("salt3", str(ctx.exception))

    def test_sncosmo_runner_reports_missing_bandpasses(self):
        # a band bandpassdict does not know
        audit = audit_fields(self.staff)
        inst, _ = _gpc1_bands(self.staff)
        weird, _ = PhotometricBand.objects.get_or_create(name="w-PS1", instrument=inst, defaults={"disp_color": "#000", "disp_symbol": "circle", **audit})
        phot = TransientPhotometry.objects.create(transient=self.transient, instrument=inst, obs_group=self.transient.obs_group, **audit)
        TransientPhotData.objects.bulk_create([
            TransientPhotData(photometry=phot, band=weird, obs_date=timezone.now() - datetime.timedelta(days=d), mag=19.0, mag_err=0.05, **audit)
            for d in range(6)
        ])
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        with self.assertRaises(AnalysisError) as ctx:
            sncosmo_fit.run(payload, {"source": SYNTH_SOURCE})
        self.assertIn("GPC1 w-PS1", str(ctx.exception))

    def test_salt3_fit_when_model_cached(self):
        if not _salt3_available():
            self.skipTest("SALT3 model not cached locally; the sncosmo download is not attempted in tests")
        seed_sncosmo_lightcurve(self.staff, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        result = sncosmo_fit.run(payload, {"source": "salt3", "mw_extinction": False})
        self.assertIn("x1", result.results)
        self.assertIn("mB", result.results)


class QueueExecutionTests(AnalysisBase):
    def test_bazin_run_end_to_end_through_the_queue(self):
        seed_bazin_lightcurve(self.staff, self.transient)
        run = svc.start_analysis(self.bazin, self.transient, self.user, {"extrapolate_days": "3"})
        self.assertEqual(run.status, ExternalServiceRun.STATUS_PENDING)
        self.assertEqual(run.request_payload["params"]["extrapolate_days"], 3.0)
        self.assertEqual(Job.objects.filter(kind=runs.JOB_KIND).count(), 1)
        result = self._run_queue()
        self.assertEqual(len(result.jobs), 1)
        job = Job.objects.get(kind=runs.JOB_KIND)
        self.assertEqual(job.status, Job.DONE, job.error)
        self.assertEqual(job.result["handled"], True)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED, run.error)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertIn("peak_mjd", run.result)
        self.assertEqual(sorted(run.result["_files"]), ["bazin_fit.json", "lightcurve.png", "model_curves.json"])
        files = {f.name: f for f in run.files.all()}
        self.assertEqual(files["lightcurve.png"].kind, AnalysisResultFile.KIND_PLOT)
        self.assertEqual(files["bazin_fit.json"].kind, AnalysisResultFile.KIND_INFERENCE)
        self.assertEqual(files["model_curves.json"].kind, AnalysisResultFile.KIND_DATA)
        self.assertTrue(files["lightcurve.png"].is_image)
        self.assertGreater(files["lightcurve.png"].size, 1000)
        path = files["lightcurve.png"].file.path
        self.assertTrue(path.startswith(MEDIA))
        self.assertIn("service_runs/%s/" % run.uuid, path.replace(os.sep, "/"))
        self.assertTrue(os.path.exists(path))
        self.assertEqual([k for k, _v in svc.summary_pairs(run)], ["peak_mjd", "peak_mag", "tau_rise", "tau_fall"])

    def test_sncosmo_run_end_to_end_inline(self):
        seed_sncosmo_lightcurve(self.staff, self.transient)
        with override_settings(JOB_RUNNER_INLINE=True):
            run = svc.start_analysis(self.sncosmo, self.transient, self.user, {"mw_extinction": False})
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED, run.error)
        self.assertEqual(run.result["model"], SYNTH_SOURCE)
        self.assertEqual(run.files.filter(kind=AnalysisResultFile.KIND_PLOT).count(), 2)

    def test_run_fails_with_analysis_error_text(self):
        run = svc.start_analysis(self.bazin, self.transient, self.user)  # no photometry
        self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("at least", run.error)
        self.assertEqual(Job.objects.get(kind=runs.JOB_KIND).status, Job.DONE)

    def test_run_fails_when_runner_raises_unexpectedly(self):
        seed_bazin_lightcurve(self.staff, self.transient)
        run = svc.start_analysis(self.bazin, self.transient, self.user)
        with mock.patch.object(bazin_fit, "run", side_effect=ZeroDivisionError("boom")):
            self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("ZeroDivisionError", run.error)

    def test_run_times_out(self):
        self.bazin.timeout_seconds = 1
        self.bazin.save()
        run = svc.start_analysis(self.bazin, self.transient, self.user)
        with mock.patch.object(svc, "load_runner_module", return_value=_slow_runner_module(3.0)):
            self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("timed out", run.error)

    def test_no_timeout_runs_inline_thread_free(self):
        finished, value, exc = runners.call_with_timeout(lambda a: a + 1, (1,), 0)
        self.assertEqual((finished, value, exc), (True, 2, None))
        finished, value, exc = runners.call_with_timeout(lambda a: 1 / a, (0,), 5)
        self.assertTrue(finished)
        self.assertIsInstance(exc, ZeroDivisionError)

    def test_bad_runner_path_fails_run(self):
        self.bazin.runner_path = "no.such.module"
        self.bazin.save()
        run = svc.start_analysis(self.bazin, self.transient, self.user)
        self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("cannot import", run.error)

    def test_analysis_service_without_profile_is_left_alone(self):
        plain = ExternalService.objects.create(name="Plain", slug="plain", kind=ExternalService.KIND_ANALYSIS,
                                               created_by=self.staff, modified_by=self.staff)
        run, _ = runs.start_run(plain, self.user, transient=self.transient)
        self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_PENDING)
        self.assertEqual(Job.objects.get(kind=runs.JOB_KIND).result["handled"], False)

    def test_fail_timed_out_runs(self):
        self.bazin.timeout_seconds = 60
        self.bazin.save()
        run = svc.start_analysis(self.bazin, self.transient, self.user, dispatch=False)
        run.mark_running()
        ExternalServiceRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - datetime.timedelta(seconds=120))
        self.assertEqual(svc.fail_timed_out_runs(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("timed out", run.error)
        fresh = svc.start_analysis(self.bazin, self.transient, self.user, dispatch=False)
        self.assertEqual(svc.fail_timed_out_runs(), 0)
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, ExternalServiceRun.STATUS_PENDING)

    def test_attachment_size_cap(self):
        run = svc.start_analysis(self.bazin, self.transient, self.user, dispatch=False)
        with override_settings(ANALYSIS_MAX_ATTACHMENT_BYTES=10):
            with self.assertRaises(ValueError):
                svc.store_attachment(run, "big.bin", b"x" * 11)
        row = svc.store_attachment(run, "../../evil name.png", PNG_1x1, "image/png")
        self.assertEqual(row.name, "evil_name.png")
        self.assertEqual(row.kind, AnalysisResultFile.KIND_PLOT)
        row2 = svc.store_attachment(run, "trace.nc", b"\x00\x01")
        self.assertEqual(row2.kind, AnalysisResultFile.KIND_INFERENCE)
        # same name replaces
        svc.store_attachment(run, "evil_name.png", PNG_1x1, "image/png")
        self.assertEqual(run.files.count(), 2)


class WebhookTests(AnalysisBase):
    def setUp(self):
        super().setUp()
        self.cred = EncryptedCredential(name="Hook token", service="hook", kind="analysis", created_by=self.staff, modified_by=self.staff)
        self.cred.set_secret({"api_token": "s3cret"})
        self.cred.save()
        self.hook = svc.register_service("hook", "Hook fitter", base_url="https://hook.example.org/run", user=self.staff,
                                         input_spec=["photometry", "redshift"], output_spec=["results", "plots", "files"],
                                         credential=self.cred, timeout_seconds=600,
                                         param_schema={"iterations": {"type": "integer", "default": 100}})
        seed_bazin_lightcurve(self.staff, self.transient)

    def _dispatch(self, response, side_effect=None):
        run = svc.start_analysis(self.hook, self.transient, self.user, {"iterations": 5})
        with mock.patch("requests.post", side_effect=side_effect, return_value=response) as post:
            self._run_queue()
        run.refresh_from_db()
        return run, post

    def test_posts_payload_with_callback_and_token(self):
        run, post = self._dispatch(_FakeResponse(202, {"id": "job-42"}))
        self.assertEqual(run.status, ExternalServiceRun.STATUS_RUNNING)
        self.assertEqual(run.external_id, "job-42")
        self.assertTrue(post.called)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://hook.example.org/run")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer s3cret")
        body = json.loads(kwargs["data"])
        self.assertEqual(body["run_id"], str(run.uuid))
        self.assertEqual(body["service"], "hook")
        self.assertTrue(body["callback_url"].endswith(runs.callback_path(run)))
        self.assertEqual(body["params"], {"iterations": 5})
        self.assertEqual(body["redshift"], 0.03)
        self.assertEqual(len(body["photometry"]), TransientPhotData.objects.filter(photometry__transient=self.transient).count())
        self.assertEqual(body["output_spec"], ["results", "plots", "files"])
        token = body["callback_token"]
        self.assertEqual(kwargs["headers"]["X-Run-Token"], token)
        self.assertTrue(runs.verify_callback_token(run, token))
        # the callback completes the run with a base64 plot and an opaque inference file
        payload = {
            "status": "succeeded", "result": {"chi2": 1.5, "n": 3},
            "plots": [{"name": "fit.png", "content_type": "image/png", "data": base64.b64encode(PNG_1x1).decode()}],
            "files": [{"name": "posterior.joblib", "kind": "inference", "data": base64.b64encode(b"\x80\x04joblib").decode()},
                      {"name": "bad"}],
        }
        resp = self.client.post(runs.callback_path(run), data=json.dumps(payload), content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["files"], ["fit.png", "posterior.joblib"])
        self.assertEqual(len(resp.json()["attachment_errors"]), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(run.result["chi2"], 1.5)
        self.assertEqual(run.result["_files"], ["fit.png", "posterior.joblib"])
        files = {f.name: f for f in run.files.all()}
        self.assertEqual(files["fit.png"].kind, AnalysisResultFile.KIND_PLOT)
        self.assertEqual(files["posterior.joblib"].kind, AnalysisResultFile.KIND_INFERENCE)
        self.assertEqual(files["posterior.joblib"].file.read(), b"\x80\x04joblib")
        # a second completion is refused and stores nothing more
        resp = self.client.post(runs.callback_path(run), data=json.dumps(payload), content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(run.files.count(), 2)

    def test_callback_multipart(self):
        run, post = self._dispatch(_FakeResponse(200, {"external_id": "m-1"}))
        token = json.loads(post.call_args[1]["data"])["callback_token"]
        resp = self.client.post(runs.callback_path(run), data={
            "status": "succeeded", "result": json.dumps({"score": 0.9}), "kinds": json.dumps({"trace": "inference"}),
            "plot": SimpleUploadedFile("corner.png", PNG_1x1, content_type="image/png"),
            "trace": SimpleUploadedFile("trace.nc", b"CDF\x01", content_type="application/x-netcdf"),
        }, HTTP_X_RUN_TOKEN=token)
        self.assertEqual(resp.status_code, 200, resp.content)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(run.result["score"], 0.9)
        files = {f.name: f for f in run.files.all()}
        self.assertEqual(files["corner.png"].kind, AnalysisResultFile.KIND_PLOT)
        self.assertEqual(files["trace.nc"].kind, AnalysisResultFile.KIND_INFERENCE)
        self.assertEqual(files["trace.nc"].meta, {"field": "trace"})

    def test_callback_wrong_token_and_failed_status(self):
        run, post = self._dispatch(_FakeResponse(202, {}))
        resp = self.client.post(runs.callback_path(run), data=json.dumps({"status": "failed", "error": "diverged"}),
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(resp.status_code, 403)
        token = json.loads(post.call_args[1]["data"])["callback_token"]
        resp = self.client.post(runs.callback_path(run), data=json.dumps({"status": "failed", "error": "diverged", "token": token}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertEqual(run.error, "diverged")

    def test_http_error_fails_run(self):
        run, _ = self._dispatch(_FakeResponse(500, None, text="kaboom"))
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("answered 500", run.error)
        self.assertIn("kaboom", run.error)

    def test_transport_error_fails_run(self):
        import requests

        run, _ = self._dispatch(None, side_effect=requests.ConnectionError("refused"))
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("refused", run.error)

    def test_synchronous_reply_completes_run(self):
        reply = {"status": "succeeded", "result": {"answer": 42},
                 "plots": [{"name": "p.png", "content_type": "image/png", "data": base64.b64encode(PNG_1x1).decode()}]}
        run, _ = self._dispatch(_FakeResponse(200, reply))
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(run.result["answer"], 42)
        self.assertEqual([f.name for f in run.files.all()], ["p.png"])

    def test_missing_base_url_fails(self):
        self.hook.service.base_url = ""
        self.hook.service.save()
        run, post = self._dispatch(_FakeResponse(200, {}))
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertFalse(post.called)

    def test_no_credential_no_bearer(self):
        self.hook.service.credential = None
        self.hook.service.save()
        _run, post = self._dispatch(_FakeResponse(202, {}))
        self.assertNotIn("Authorization", post.call_args[1]["headers"])


class AnalysisTabTests(AnalysisBase):
    def setUp(self):
        super().setUp()
        seed_bazin_lightcurve(self.staff, self.transient)
        self.fragment_url = reverse("transient_detail_analysis_fragment", args=[self.transient.id])
        self.run_url = reverse("transient_analysis_run", args=[self.transient.id])

    def test_detail_page_has_tab_and_loader(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('href="#analysis_tab"', html)
        self.assertIn('id="analysis_container"', html)
        self.assertIn(self.fragment_url, html)
        self.assertIn("yseLoadAnalysisTab", html)

    def test_fragment_without_runs(self):
        self.client.force_login(self.user)
        resp = self.client.get(self.fragment_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("No analysis has been run", html)
        self.assertIn('id="analysis_run_form"', html)
        self.assertIn('<option value="bazin_fit"', html)
        self.assertIn('<option value="sncosmo_fit"', html)
        services = json.loads(html.split('id="yse_analysis_services_json">')[1].split("</script>")[0])
        self.assertEqual({s["slug"] for s in services}, {"bazin_fit", "sncosmo_fit"})
        self.assertEqual(services[0]["cap"]["limit"], None)

    def test_fragment_without_services(self):
        AnalysisService.objects.all().delete()
        self.client.force_login(self.user)
        resp = self.client.get(self.fragment_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No analysis service is enabled", resp.content.decode())

    def test_fragment_with_completed_and_failed_runs(self):
        import sncosmo

        with override_settings(JOB_RUNNER_INLINE=True):
            good = svc.start_analysis(self.bazin, self.transient, self.user)
            with mock.patch.object(sncosmo, "Model", side_effect=OSError("no download")):
                bad = svc.start_analysis(self.sncosmo, self.transient, self.user, {"source": "salt3"})
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.status, "succeeded", good.error)
        self.assertEqual(bad.status, "failed")
        self.client.force_login(self.other)
        resp = self.client.get(self.fragment_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(html.count('class="analysis-run-row"'), 2)
        self.assertIn('data-status="succeeded"', html)
        self.assertIn('data-status="failed"', html)
        self.assertIn("could not load sncosmo source", html)  # the failed run's error text
        self.assertIn('class="analysis-plot img-fluid"', html)
        self.assertIn("bazin_fit.json", html)
        self.assertIn("peak_mjd", html)
        self.assertIn("lightcurve.png", html)
        # other users cannot delete a run that is not theirs
        self.assertNotIn('data-analysis-action="delete"', html)
        self.client.force_login(self.user)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn('data-analysis-action="delete"', html)
        self.assertEqual(html.count('data-active="0"'), 1)

    def test_fragment_hides_other_runs_without_data_access(self):
        grp = Group.objects.create(name="hidden-phot")
        TransientPhotometry.objects.filter(transient=self.transient).first().groups.add(grp)
        mine = svc.start_analysis(self.bazin, self.transient, self.other, dispatch=False)
        svc.start_analysis(self.bazin, self.transient, self.user, dispatch=False)
        self.client.force_login(self.other)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertEqual(html.count('class="analysis-run-row"'), 1)
        self.assertIn(str(mine.uuid), html)
        self.assertIn("do not have access", html)
        self.client.force_login(self.staff)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertEqual(html.count('class="analysis-run-row"'), 2)

    def test_run_action_and_status_polling(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.run_url, data=json.dumps({"service": "bazin_fit", "params": {"extrapolate_days": 4}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["run"]["status"], "pending")
        uuid = body["run"]["uuid"]
        status_url = reverse("analysis_run_status", args=[uuid])
        self.assertEqual(self.client.get(status_url).json()["is_finished"], False)
        self._run_queue()
        js = self.client.get(status_url).json()
        self.assertTrue(js["is_finished"])
        self.assertEqual(js["status"], "succeeded", js["error"])
        self.assertEqual(js["params"], {"extrapolate_days": 4.0, "band": "", "include_flagged": False})
        self.assertEqual(len(js["files"]), 3)
        plot = [f for f in js["files"] if f["is_image"]][0]
        self.assertTrue(plot["url"].startswith("/analysis_runs/%s/files/" % uuid))
        img = self.client.get(plot["url"])
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img["Content-Type"], "image/png")
        self.assertTrue(b"".join(img.streaming_content).startswith(b"\x89PNG"))
        # form-encoded works too, unknown keys become params
        resp = self.client.post(self.run_url, data={"service": "bazin_fit", "band": "r"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["run"]["params"]["band"], "r")

    def test_run_action_errors(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.run_url, data={"service": "nope"}).status_code, 400)
        resp = self.client.post(self.run_url, data=json.dumps({"service": "sncosmo_fit", "params": {"minsnr": "x"}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("minsnr", resp.json()["errors"])
        grp = Group.objects.create(name="only-them")
        self.bazin.service.groups.add(grp)
        self.assertEqual(self.client.post(self.run_url, data={"service": "bazin_fit"}).status_code, 400)
        self.client.logout()
        self.assertIn(self.client.post(self.run_url, data={"service": "bazin_fit"}).status_code, (302, 403))

    def test_daily_cap_returns_429_and_staff_exempt(self):
        self.bazin.service.max_runs_per_user_per_day = 1
        self.bazin.service.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.run_url, data={"service": "bazin_fit"}).status_code, 200)
        resp = self.client.post(self.run_url, data={"service": "bazin_fit"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["limit"], 1)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn("0 of 1 left today", html)
        self.assertIn('data-capped="1"', html)
        self.client.force_login(self.staff)
        for _ in range(2):
            self.assertEqual(self.client.post(self.run_url, data={"service": "bazin_fit"}).status_code, 200)
        self.assertIn("no cap for staff", self.client.get(self.fragment_url).content.decode())
        cap = svc.cap_status(self.bazin.service, self.user)
        self.assertEqual((cap["limit"], cap["used"], cap["remaining"], cap["exempt"]), (1, 1, 0, False))

    def test_file_access_delete_and_cancel(self):
        with override_settings(JOB_RUNNER_INLINE=True):
            run = svc.start_analysis(self.bazin, self.transient, self.user)
        run.refresh_from_db()
        f = run.files.filter(kind=AnalysisResultFile.KIND_PLOT).get()
        url = reverse("analysis_run_file", kwargs={"run_uuid": str(run.uuid), "file_id": f.pk, "name": f.name})
        self.assertIn(self.client.get(url).status_code, (302, 403))  # anonymous
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 200)  # sees the photometry, so sees the plot
        grp = Group.objects.create(name="hidden-phot2")
        TransientPhotometry.objects.filter(transient=self.transient).first().groups.add(grp)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse("analysis_run_delete", args=[run.uuid])).status_code, 403)
        self.client.force_login(self.user)  # the requester keeps access
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url.replace(f.name, "other.png")).status_code, 200)  # the name is cosmetic
        self.assertEqual(self.client.get(url.replace("/%d/" % f.pk, "/999999/")).status_code, 404)
        path = f.file.path
        self.assertTrue(os.path.exists(path))
        pending = svc.start_analysis(self.bazin, self.transient, self.user, dispatch=False)
        resp = self.client.post(reverse("analysis_run_cancel", args=[pending.uuid]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["run"]["status"], "cancelled")
        resp = self.client.post(reverse("analysis_run_delete", args=[run.uuid]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ExternalServiceRun.objects.filter(pk=run.pk).exists())
        self.assertFalse(AnalysisResultFile.objects.filter(pk=f.pk).exists())
        self.assertFalse(os.path.exists(path))

    def test_status_of_unknown_run_is_404(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("analysis_run_status", args=["00000000-0000-0000-0000-000000000000"])).status_code, 404)

    def test_staff_run_page_still_renders(self):
        with override_settings(JOB_RUNNER_INLINE=True):
            run = svc.start_analysis(self.bazin, self.transient, self.user)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("external_service_run_detail", args=[run.uuid])).status_code, 200)

    def test_admin_pages_render(self):
        self.client.force_login(self.staff)
        for name in ("YSE_App_analysisservice_changelist", "YSE_App_analysisresultfile_changelist"):
            self.assertEqual(self.client.get(reverse("admin:" + name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("admin:YSE_App_analysisservice_change", args=[self.bazin.pk])).status_code, 200)


class AnalysisApiTests(AnalysisBase):
    def setUp(self):
        super().setUp()
        seed_bazin_lightcurve(self.staff, self.transient)

    def test_services_endpoint(self):
        self.client.force_login(self.user)
        resp = self.client.get("/api/analysisservices/")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        by_slug = {r["slug"]: r for r in rows}
        self.assertEqual(set(by_slug), {"bazin_fit", "sncosmo_fit"})
        self.assertEqual(by_slug["bazin_fit"]["runner_kind"], "inprocess")
        self.assertEqual({f["name"] for f in by_slug["bazin_fit"]["param_fields"]}, {"band", "extrapolate_days", "include_flagged"})
        self.assertEqual(by_slug["bazin_fit"]["cap"]["limit"], None)
        self.assertFalse(by_slug["bazin_fit"]["has_credential"])
        self.assertEqual(self.client.get(by_slug["bazin_fit"]["url"]).status_code, 200)

    def test_runs_endpoint_create_list_retrieve_delete(self):
        self.client.force_login(self.user)
        resp = self.client.post("/api/analysisruns/", data=json.dumps({"service": "bazin_fit", "transient": self.transient.name,
                                                                      "params": {"extrapolate_days": 2}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        uuid = resp.json()["uuid"]
        self.assertEqual(resp.json()["status"], "pending")
        self.assertEqual(resp.json()["params"]["extrapolate_days"], 2.0)
        self._run_queue()
        resp = self.client.get("/api/analysisruns/%s/" % uuid)
        self.assertEqual(resp.status_code, 200)
        js = resp.json()
        self.assertEqual(js["status"], "succeeded", js["error"])
        self.assertEqual(js["service"], "bazin_fit")
        self.assertEqual(js["transient_name"], self.transient.name)
        self.assertEqual(len(js["files"]), 3)
        self.assertTrue(js["files"][0]["url"].startswith("http"))
        self.assertIn("peak_mjd", js["result"])
        resp = self.client.get("/api/analysisruns/?transient=%d&status=succeeded" % self.transient.id)
        rows = resp.json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([r["uuid"] for r in rows], [uuid])
        rows = self.client.get("/api/analysisruns/?service=sncosmo_fit").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(rows, [])
        # errors
        self.assertEqual(self.client.post("/api/analysisruns/", data=json.dumps({"service": "bazin_fit", "transient": "nope"}),
                                          content_type="application/json").status_code, 404)
        self.assertEqual(self.client.post("/api/analysisruns/", data=json.dumps({"service": "zzz", "transient": self.transient.id}),
                                          content_type="application/json").status_code, 400)
        self.bazin.service.max_runs_per_user_per_day = 1
        self.bazin.service.save()
        self.assertEqual(self.client.post("/api/analysisruns/", data=json.dumps({"service": "bazin_fit", "transient": self.transient.id}),
                                          content_type="application/json").status_code, 429)
        # another user may not delete it; the requester may
        self.client.force_login(self.other)
        self.assertEqual(self.client.delete("/api/analysisruns/%s/" % uuid).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.delete("/api/analysisruns/%s/" % uuid).status_code, 204)
        self.assertFalse(ExternalServiceRun.objects.filter(uuid=uuid).exists())

    def test_runs_endpoint_hides_invisible_transients(self):
        grp = Group.objects.create(name="api-hidden")
        TransientPhotometry.objects.filter(transient=self.transient).first().groups.add(grp)
        svc.start_analysis(self.bazin, self.transient, self.staff, dispatch=False)
        self.client.force_login(self.other)
        rows = self.client.get("/api/analysisruns/").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(rows, [])
        self.client.force_login(self.staff)
        rows = self.client.get("/api/analysisruns/").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(len(rows), 1)
