"""Summary-tab Refit / stored SALT3 fit, the NGSF runner and the has_jwst flag (#315, #328 follow-up).

* ``salt2plot`` / ``salt2fluxplot`` overlay the last successful ``sncosmo_fit``
  run (its ``model_curves.json`` and parameters) and never call sncosmo in the
  request; without a stored run the plot says so;
* the Summary-tab fragments (``salt_fit_fragment``, ``ngsf_fragment``) show the
  stored fit, the not-registered / not-installed / queued / failed states, and
  the Refit button starts a run through the Analysis tab's run endpoint;
* the NGSF runner: parameters written from the payload and the deployment's
  base ``parameters.json``, the subprocess boundary (fully mocked: nothing is
  spawned), CSV parsing and ranking, the not-installed / no-spectrum /
  non-zero-exit / timeout errors, end to end through the inline queue;
* ``Transient.has_jwst``: set by the JWST status lookup (HST / Chandra too),
  searchable as ``has_jwst`` and ``legacy.has_jwst``, shown as an Archives badge.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import tempfile
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import view_utils
from YSE_App.analysis import ngsf
from YSE_App.analysis.base import AnalysisError
from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.models import ExternalServiceRun, Transient, TransientSpecData, TransientSpectrum
from YSE_App.services import analysis_services as svc
from YSE_App.services import fit_status
from YSE_App.tests import test_jwst_tab
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user
from YSE_App.tests.test_analysis_services import MEDIA, SYNTH_SOURCE, _gpc1_bands, seed_sncosmo_lightcurve

# A recorded NGSF results CSV (the column names NGSF writes; two types among the top matches).
NGSF_CSV = """SPECTRUM,GALAXY,CONST_SN,CONST_GAL,Z,A_v,Phase,Band,Frac(SN),Frac(gal),CHI2/dof,CHI2/dof2
bank/original_resolution/sne/Ia/sn2011fe/sn2011fe-visit3.dat,E,1.2,0.3,0.031,0.2,3.0,r,0.81,0.19,1.18,1.20
bank/original_resolution/sne/Ia/sn1994D/sn1994D-19940304.dat,S0,1.1,0.2,0.030,0.0,1.0,r,0.79,0.21,1.25,1.31
bank/original_resolution/sne/Ic/sn1994I/sn1994I-19940405.dat,Sc,0.9,0.4,0.028,0.4,-2.0,r,0.66,0.34,1.90,1.95
"""
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c636000020000050001"
    "0d0a2db40000000049454e44ae426082"
)


def _fake_ngsf_run(csv_text=NGSF_CSV, pngs=2, returncode=0, stdout="NGSF done", stderr=""):
    """A stand-in for ``subprocess.run`` that behaves like NGSF: reads parameters.json, writes results."""

    def fake(argv, cwd=None, env=None, capture_output=True, text=True, timeout=None, check=False):
        fake.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout, "env": dict(env or {})})
        params_path = argv[-1]
        with open(params_path, encoding="utf-8") as fh:
            parameters = json.load(fh)
        fake.parameters.append(parameters)
        out_dir = parameters["saving_results_path"]
        base = os.path.splitext(os.path.basename(parameters["object_to_fit"]))[0]
        if returncode == 0:
            with open(os.path.join(out_dir, base + ".csv"), "w", encoding="utf-8") as fh:
                fh.write(csv_text)
            for i in range(pngs):
                with open(os.path.join(out_dir, "%s_%d.png" % (base, i)), "wb") as fh:
                    fh.write(PNG_1x1)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    fake.calls = []
    fake.parameters = []
    return fake


def _spectrum_payload(n=200, spectrum_id=7, mjd=61300.5, z=0.031):
    wave = np.linspace(3500.0, 9000.0, n)
    flux = 1e-16 * (1.0 + 0.2 * np.sin(wave / 300.0))
    return {
        "transient": {"id": 1, "name": "2026ngsf", "redshift": z, "redshift_source": "transient" if z else ""},
        "redshift": z,
        "photometry": [],
        "spectra": [{"id": spectrum_id, "mjd": mjd, "instrument": "LRIS", "obs_group": "YSE", "redshift": None,
                     "n_points": n, "wavelength": wave.tolist(), "flux": flux.tolist(), "flux_err": None}],
        "photstat": None,
        "params": {},
    }


NGSF_OK = {"available": True, "command": "/usr/bin/python3 /opt/NGSF/run.py", "home": "", "reason": ""}


class NgsfRunnerTests(TestCase):
    """The runner with the subprocess boundary mocked; nothing is spawned."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ngsf-home-")
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_availability_states(self):
        with override_settings(NGSF_COMMAND=""):
            a = ngsf.availability()
            self.assertFalse(a["available"])
            self.assertIn("NGSF_COMMAND is empty", a["reason"])
        with override_settings(NGSF_COMMAND="definitely-not-a-program-xyz"):
            a = ngsf.availability()
            self.assertFalse(a["available"])
            self.assertIn("was not found", a["reason"])
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py", NGSF_HOME=os.path.join(self.home, "missing")):
            a = ngsf.availability()
            self.assertFalse(a["available"])
            self.assertIn("NGSF_HOME", a["reason"])
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py", NGSF_HOME=self.home):
            a = ngsf.availability()
            self.assertTrue(a["available"], a)
            self.assertTrue(a["command"].endswith("/opt/NGSF/run.py"))

    def test_build_argv_appends_or_substitutes_the_parameters_path(self):
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py"):
            argv = ngsf.build_argv("/tmp/p.json")
            self.assertEqual(argv[1:], ["/opt/NGSF/run.py", "/tmp/p.json"])
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py --params {params} --quiet"):
            argv = ngsf.build_argv("/tmp/p.json")
            self.assertEqual(argv[1:], ["/opt/NGSF/run.py", "--params", "/tmp/p.json", "--quiet"])
        with override_settings(NGSF_COMMAND=""):
            with self.assertRaises(AnalysisError):
                ngsf.build_argv("/tmp/p.json")

    def test_not_installed_is_a_clear_analysis_error(self):
        with override_settings(NGSF_COMMAND=""):
            with self.assertRaises(AnalysisError) as ctx:
                ngsf.run(_spectrum_payload(), {})
        self.assertIn("not installed", str(ctx.exception))
        self.assertIn("docs/ngsf.md", str(ctx.exception))

    def test_no_spectrum_and_bad_spectrum_id(self):
        with mock.patch.object(ngsf, "availability", return_value=NGSF_OK):
            payload = _spectrum_payload()
            payload["spectra"] = []
            with self.assertRaises(AnalysisError) as ctx:
                ngsf.run(payload, {})
            self.assertIn("no spectrum", str(ctx.exception))
            with self.assertRaises(AnalysisError) as ctx:
                ngsf.run(_spectrum_payload(), {"spectrum_id": "999"})
            self.assertIn("999", str(ctx.exception))
            with self.assertRaises(AnalysisError) as ctx:
                ngsf.run(_spectrum_payload(n=10), {})
            self.assertIn("usable points", str(ctx.exception))

    def test_choose_spectrum_prefers_the_most_recent_with_data(self):
        payload = _spectrum_payload(spectrum_id=1, mjd=61000.0)
        newer = dict(payload["spectra"][0], id=2, mjd=61010.0)
        empty_newest = {"id": 3, "mjd": 61020.0, "wavelength": [], "flux": [], "flux_err": None}
        payload["spectra"] = [payload["spectra"][0], newer, empty_newest]
        self.assertEqual(ngsf.choose_spectrum(payload)["id"], 2)
        self.assertEqual(ngsf.choose_spectrum(payload, "1")["id"], 1)

    def test_parse_results_csv_ranks_and_splits_templates(self):
        rows = ngsf.parse_results_csv(NGSF_CSV)
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["type"], "Ia")
        self.assertEqual(rows[0]["template"], "sn2011fe")
        self.assertEqual(rows[0]["galaxy"], "E")
        self.assertAlmostEqual(rows[0]["chi2_dof"], 1.18)
        self.assertAlmostEqual(rows[0]["phase"], 3.0)
        self.assertEqual(rows[2]["type"], "Ic")
        # unsorted input comes out sorted by chi2/dof
        shuffled = "\n".join([NGSF_CSV.splitlines()[0]] + NGSF_CSV.splitlines()[1:][::-1]) + "\n"
        self.assertEqual([r["template"] for r in ngsf.parse_results_csv(shuffled)], ["sn2011fe", "sn1994D", "sn1994I"])
        self.assertEqual(ngsf.split_template("Ia/sn2011fe"), ("Ia", "sn2011fe"))
        self.assertEqual(ngsf.split_template("sn2011fe.dat"), ("", "sn2011fe"))
        summary = ngsf.summarise(rows, 5)
        self.assertEqual(summary["best_type"], "Ia")
        self.assertEqual(summary["type_votes"], {"Ia": 2, "Ic": 1})

    def test_run_writes_parameters_and_reads_results(self):
        with open(os.path.join(self.home, "parameters.json"), "w", encoding="utf-8") as fh:
            json.dump({"temp_sn_tr": ["Ia", "Ib", "Ic", "II"], "temp_gal_tr": ["E", "S0", "Sc"],
                       "lower_lam": 3000, "upper_lam": 10000, "z_int": 0.002, "n": 20}, fh)
        fake = _fake_ngsf_run()
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py", NGSF_HOME=self.home, NGSF_SUBPROCESS_TIMEOUT=321), \
                mock.patch.object(ngsf.subprocess, "run", fake):
            result = ngsf.run(_spectrum_payload(), {"n": 5, "how_many_plots": 1, "mask_telluric": True})
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["cwd"], self.home)
        self.assertEqual(call["timeout"], 321)
        self.assertEqual(call["env"].get("MPLBACKEND"), "Agg")
        self.assertTrue(call["argv"][-1].endswith("parameters.json"))
        params = fake.parameters[0]
        # the deployment's base parameters survive, ours override
        self.assertEqual(params["temp_sn_tr"], ["Ia", "Ib", "Ic", "II"])
        self.assertEqual(params["z_int"], 0.002)
        self.assertEqual(params["n"], 5)
        self.assertAlmostEqual(params["z_start"], 0.011)
        self.assertAlmostEqual(params["z_end"], 0.051)
        self.assertEqual(params["use_exact_z"], 0)
        self.assertEqual(params["mask_telluric"], 1)
        self.assertEqual(params["show_plot"], 0)
        self.assertTrue(params["object_to_fit"].endswith("2026ngsf_spec7.dat"))
        # results
        r = result.results
        self.assertEqual(r["best_type"], "Ia")
        self.assertEqual(r["best_template"], "sn2011fe")
        self.assertAlmostEqual(r["best_chi2_dof"], 1.18)
        self.assertEqual(r["type_votes"], {"Ia": 2, "Ic": 1})
        self.assertEqual(r["spectrum_id"], 7)
        self.assertEqual(r["n_matches"], 3)
        self.assertEqual(len(r["matches"]), 3)
        self.assertEqual(result.summary, "NGSF: Ia, sn2011fe, phase +3 d, chi2/dof 1.18")
        self.assertEqual([p.name for p in result.plots], ["ngsf_match_1.png"])
        self.assertEqual(sorted(f.name for f in result.files), ["ngsf_log.txt", "ngsf_results.csv", "parameters.json"])
        log = [f for f in result.files if f.name == "ngsf_log.txt"][0].data.decode()
        self.assertIn("NGSF done", log)
        # the scratch directory is gone
        self.assertFalse(os.path.exists(os.path.dirname(params["object_to_fit"])))

    def test_unknown_redshift_uses_zero_to_z_max_and_exact_z_when_window_is_zero(self):
        fake = _fake_ngsf_run()
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py", NGSF_HOME=self.home), \
                mock.patch.object(ngsf.subprocess, "run", fake):
            ngsf.run(_spectrum_payload(z=None), {"z_max": 0.2})
            ngsf.run(_spectrum_payload(z=0.05), {"z_window": 0})
        self.assertEqual((fake.parameters[0]["z_start"], fake.parameters[0]["z_end"]), (0.0, 0.2))
        self.assertEqual(fake.parameters[1]["use_exact_z"], 1)
        self.assertAlmostEqual(fake.parameters[1]["z_exact"], 0.05)

    def test_non_zero_exit_timeout_and_missing_csv(self):
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py", NGSF_HOME=self.home):
            with mock.patch.object(ngsf.subprocess, "run", _fake_ngsf_run(returncode=2, stderr="Traceback: boom")):
                with self.assertRaises(AnalysisError) as ctx:
                    ngsf.run(_spectrum_payload(), {})
            self.assertIn("status 2", str(ctx.exception))
            self.assertIn("boom", str(ctx.exception))
            with mock.patch.object(ngsf.subprocess, "run", side_effect=subprocess.TimeoutExpired("ngsf", 12)):
                with self.assertRaises(AnalysisError) as ctx:
                    ngsf.run(_spectrum_payload(), {})
            self.assertIn("12 s", str(ctx.exception))
            with mock.patch.object(ngsf.subprocess, "run", _fake_ngsf_run(csv_text="", pngs=0)):
                with self.assertRaises(AnalysisError) as ctx:
                    ngsf.run(_spectrum_payload(), {})
            self.assertIn("no template match", str(ctx.exception))
            fake = _fake_ngsf_run()
            original = fake

            def no_csv(argv, **kw):
                proc = original(argv, **kw)
                out_dir = original.parameters[-1]["saving_results_path"]
                for name in os.listdir(out_dir):
                    if name.endswith(".csv"):
                        os.remove(os.path.join(out_dir, name))
                return proc

            with mock.patch.object(ngsf.subprocess, "run", no_csv):
                with self.assertRaises(AnalysisError) as ctx:
                    ngsf.run(_spectrum_payload(), {})
            self.assertIn("no results CSV", str(ctx.exception))

    def test_registers_as_a_builtin(self):
        staff = create_test_user("ngsf_reg", is_superuser=True)
        profile = svc.register_service("ngsf", runner="ngsf", user=staff)
        self.assertEqual(profile.input_spec, ["spectra", "redshift"])
        self.assertEqual(profile.output_spec, ["results", "plots", "files"])
        self.assertIn("spectrum_id", profile.param_schema)
        self.assertEqual(profile.summary_keys, ["best_type", "best_template", "best_phase", "best_chi2_dof"])
        self.assertEqual(profile.service.name, ngsf.NAME)


@override_settings(MEDIA_ROOT=MEDIA)
class SummaryRefitBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.staff = create_test_user("refit_staff", is_superuser=True)
        self.user = create_test_user("refit_user", is_staff=False)
        self.transient = create_minimal_transient(self.staff, name="2026rft", obs_group_name="rft-group")
        self.transient.redshift = 0.03
        self.transient.save(update_fields=["redshift"])
        self.client = Client()
        self.client.force_login(self.staff)
        view_utils._load_heavy_plot_stack()

    def register_sncosmo(self, **kwargs):
        return svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.staff, timeout_seconds=0,
                                    default_params={"source": SYNTH_SOURCE}, **kwargs)

    def stored_fit(self):
        """A real succeeded sncosmo run on the synthetic source (nothing downloaded)."""
        seed_sncosmo_lightcurve(self.staff, self.transient, t0_offset=10)
        profile = self.register_sncosmo()
        with override_settings(JOB_RUNNER_INLINE=True):
            run = svc.start_analysis(profile, self.transient, self.staff, {"mw_extinction": False})
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED, run.error)
        return run

    def add_spectrum(self, n=120):
        inst, _bands = _gpc1_bands(self.staff)
        audit = audit_fields(self.staff)
        spec = TransientSpectrum.objects.create(transient=self.transient, instrument=inst, obs_group=self.transient.obs_group,
                                                ra=10.0, dec=20.0, obs_date=timezone.now() - datetime.timedelta(days=2), **audit)
        TransientSpecData.objects.bulk_create([
            TransientSpecData(spectrum=spec, wavelength=3500.0 + 40 * i, flux=1.0 + 0.01 * i, flux_err=0.1, **audit) for i in range(n)
        ])
        return spec


class StoredSaltOverlayTests(SummaryRefitBase):
    def _line_count(self, html):
        return html.count('"type":"Line"') + html.count('"type": "Line"')

    def test_plots_never_call_sncosmo_and_say_so_without_a_stored_fit(self):
        seed_sncosmo_lightcurve(self.staff, self.transient, t0_offset=10)
        model = mock.MagicMock(name="Model")
        fit_lc = mock.MagicMock(name="fit_lc")
        with mock.patch.multiple(view_utils.sncosmo, Model=model, fit_lc=fit_lc):
            for name in ("salt2plot", "salt2fluxplot"):
                with self.subTest(name=name):
                    response = self.client.get(reverse(name, args=[self.transient.id, 1]))
                    self.assertEqual(response.status_code, 200)
                    body = response.content.decode()
                    self.assertIn(view_utils.SALT_FIT_NONE_TEXT, body)
                    self.assertNotIn(view_utils.SALT_FIT_UNAVAILABLE_TEXT, body)
        self.assertFalse(model.called)
        self.assertFalse(fit_lc.called)

    def test_plots_overlay_the_stored_run(self):
        run = self.stored_fit()
        curves = fit_status.model_curves(run)
        self.assertEqual(sorted(curves["bands"]), ["sdssg", "sdssi", "sdssr"])
        self.assertEqual(len(curves["mjd"]), len(curves["bands"]["sdssr"]))
        labels = fit_status.salt_fit_labels(run, today_mjd=61500.0)
        self.assertTrue(labels[0].startswith("phase = "))
        self.assertTrue(any("₀" in text for text in labels), labels)  # t0 line
        self.assertIn(SYNTH_SOURCE + " fit", labels[-1])
        self.assertIn("by refit_staff", labels[-1])

        with mock.patch.object(view_utils.sncosmo, "fit_lc") as fit_lc:
            without = self.client.get(reverse("salt2plot", args=[self.transient.id, 0])).content.decode()
            with_fit = self.client.get(reverse("salt2plot", args=[self.transient.id, 1])).content.decode()
            flux = self.client.get(reverse("salt2fluxplot", args=[self.transient.id, 1])).content.decode()
        self.assertFalse(fit_lc.called)
        self.assertNotIn(view_utils.SALT_FIT_NONE_TEXT, with_fit)
        self.assertNotIn(view_utils.SALT_FIT_NONE_TEXT, flux)
        self.assertIn(" fit ", with_fit)
        # one model line per band (three bands) on top of the plain plot; Bokeh
        # serialises each line glyph the same number of times
        extra = self._line_count(with_fit) - self._line_count(without)
        self.assertGreaterEqual(extra, 3)
        self.assertEqual(extra % 3, 0, extra)
        self.assertIn("by refit_staff", flux)

    def test_stored_fit_helpers_pick_the_newest_success(self):
        self.assertIsNone(fit_status.stored_salt_fit(self.transient))
        run = self.stored_fit()
        self.assertEqual(fit_status.stored_salt_fit(self.transient).pk, run.pk)
        profile = self.register_sncosmo()
        failed = svc.start_analysis(profile, self.transient, self.user, {}, dispatch=False)
        failed.mark_failed("no good")
        self.assertEqual(fit_status.stored_salt_fit(self.transient).pk, run.pk)
        self.assertEqual(fit_status.latest_run(self.transient, "sncosmo_fit").pk, failed.pk)
        self.assertEqual(fit_status.model_curves(failed), {"mjd": [], "bands": {}})

    def test_detail_plot_cache_key_carries_the_run(self):
        seed_sncosmo_lightcurve(self.staff, self.transient, t0_offset=10)
        with override_settings(YSE_PLOT_HTML_CACHE=True), mock.patch.object(view_utils, "_plot_html_cache_enabled", return_value=True), \
                mock.patch.object(view_utils.cache, "set", wraps=view_utils.cache.set) as cache_set:
            self.client.get(reverse("salt2plot", args=[self.transient.id, 1]))
        keys = [c.args[0] for c in cache_set.call_args_list if str(c.args[0]).startswith("lc_detail")]
        self.assertTrue(keys and keys[0].endswith("_salt0"), keys)


class SaltFitFragmentTests(SummaryRefitBase):
    def url(self):
        return reverse("transient_detail_salt_fit_fragment", args=[self.transient.id])

    def test_not_registered_states(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("not available on this server", html)
        self.assertIn("register_analysis_service --builtin sncosmo_fit", html)
        self.assertNotIn("data-fit-action", html)
        self.client.force_login(self.user)
        html = self.client.get(self.url()).content.decode()
        self.assertIn("not available on this server", html)
        self.assertNotIn("register_analysis_service", html)

    def test_stored_fit_and_refit_button(self):
        run = self.stored_fit()
        html = self.client.get(self.url()).content.decode()
        self.assertIn("SALT3 fit:", html)
        self.assertIn("<em>t0</em>", html)
        self.assertIn("<em>z</em>", html)
        self.assertIn("by refit_staff", html)
        self.assertIn('data-fit-action="run"', html)
        self.assertIn('data-service="sncosmo_fit"', html)
        self.assertIn('data-active="0"', html)
        self.assertNotIn("data-status-url", html)
        self.assertIn(reverse("transient_analysis_run", args=[self.transient.id]), html)
        # the fragment URL and the run's t0 agree with the stored result
        self.assertIn("%d" % round(run.result["t0"]), html.replace(",", ""))

    def test_refit_through_the_run_endpoint_then_polls_and_shows_failure(self):
        seed_sncosmo_lightcurve(self.staff, self.transient, t0_offset=10)
        self.register_sncosmo()
        response = self.client.post(reverse("transient_analysis_run", args=[self.transient.id]),
                                    data=json.dumps({"service": "sncosmo_fit", "params": {}}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        run = ExternalServiceRun.objects.get(uuid=response.json()["run"]["uuid"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_PENDING)
        html = self.client.get(self.url()).content.decode()
        self.assertIn('data-active="1"', html)
        self.assertIn(reverse("analysis_run_status", args=[run.uuid]), html)
        self.assertIn("refit queued", html)
        self.assertIn("disabled", html)  # no second refit while one is queued
        run.mark_failed("could not load sncosmo source 'salt3'")
        html = self.client.get(self.url()).content.decode()
        self.assertIn("last refit failed", html)
        self.assertIn("could not load sncosmo source", html)
        self.assertIn('data-active="0"', html)
        self.assertNotIn(' disabled', html.split('data-fit-action="run"')[1].split(">")[0])

    def test_cap_reached_disables_the_button(self):
        self.stored_fit()
        profile = self.register_sncosmo(max_runs_per_user_per_day=1)
        self.client.force_login(self.user)
        svc.start_analysis(profile, self.transient, self.user, {}, dispatch=False)
        html = self.client.get(self.url()).content.decode()
        self.assertIn("Daily limit reached", html)
        self.assertIn("disabled", html)

    def test_group_restricted_service_hides_the_button_for_outsiders(self):
        from django.contrib.auth.models import Group

        self.stored_fit()
        grp = Group.objects.create(name="fitters")
        self.register_sncosmo(groups=[grp])
        self.client.force_login(self.user)
        html = self.client.get(self.url()).content.decode()
        self.assertIn("your groups may not run it", html)
        self.assertNotIn("data-fit-action", html)

    def test_summary_tab_has_both_shells_and_the_driver(self):
        response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        html = response.content.decode()
        self.assertIn('id="salt_fit_status"', html)
        self.assertIn('id="ngsf_status"', html)
        self.assertIn(self.url(), html)
        self.assertIn(reverse("transient_detail_ngsf_fragment", args=[self.transient.id]), html)
        self.assertIn("yseFitStatusLoad('salt_fit_status')", html)
        self.assertIn('id="archive_flags"', html)


class NgsfFragmentAndQueueTests(SummaryRefitBase):
    def url(self):
        return reverse("transient_detail_ngsf_fragment", args=[self.transient.id])

    def test_states(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("NGSF classification:", html)
        self.assertIn("not available on this server", html)
        self.assertIn("--builtin ngsf", html)
        svc.register_service("ngsf", runner="ngsf", user=self.staff)
        with override_settings(NGSF_COMMAND="definitely-not-a-program-xyz"):
            html = self.client.get(self.url()).content.decode()
        self.assertIn("NGSF is not installed on this server", html)
        self.assertIn("NGSF_COMMAND", html)
        self.assertNotIn("data-fit-action", html)
        self.client.force_login(self.user)
        with override_settings(NGSF_COMMAND="definitely-not-a-program-xyz"):
            html = self.client.get(self.url()).content.decode()
        self.assertIn("NGSF is not installed on this server", html)
        self.assertNotIn("NGSF_COMMAND", html)
        self.client.force_login(self.staff)
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py"):
            html = self.client.get(self.url()).content.decode()
        self.assertIn("none yet", html)
        self.assertIn('data-fit-action="run"', html)
        self.assertIn('data-service="ngsf"', html)

    def test_run_ngsf_end_to_end_inline_and_the_fragment_shows_it(self):
        self.add_spectrum()
        svc.register_service("ngsf", runner="ngsf", user=self.staff, timeout_seconds=0)
        fake = _fake_ngsf_run()
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py", JOB_RUNNER_INLINE=True), \
                mock.patch.object(ngsf.subprocess, "run", fake):
            response = self.client.post(reverse("transient_analysis_run", args=[self.transient.id]),
                                        data=json.dumps({"service": "ngsf", "params": {"n": 3}}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        run = ExternalServiceRun.objects.get(uuid=response.json()["run"]["uuid"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED, run.error)
        self.assertEqual(run.result["best_type"], "Ia")
        self.assertEqual(run.result["spectrum_id"], TransientSpectrum.objects.get(transient=self.transient).pk)
        self.assertEqual(sorted(run.result["_files"]), ["ngsf_log.txt", "ngsf_match_1.png", "ngsf_match_2.png", "ngsf_results.csv", "parameters.json"])
        self.assertEqual(fake.parameters[0]["n"], 3)
        self.assertEqual(len(fake.parameters[0]["object_to_fit"]) > 0, True)
        self.assertEqual([k for k, _v in svc.summary_pairs(run)], ["best_type", "best_template", "best_phase", "best_chi2_dof"])
        with override_settings(NGSF_COMMAND="python3 /opt/NGSF/run.py"):
            html = self.client.get(self.url()).content.decode()
        self.assertIn('class="badge badge-primary yse-ngsf-type">Ia<', html)
        self.assertIn("sn2011fe", html)
        self.assertIn("Ia &times;2", html)
        self.assertIn("by refit_staff", html)
        # the Analysis tab lists the run with its plots
        tab = self.client.get(reverse("transient_detail_analysis_fragment", args=[self.transient.id])).content.decode()
        self.assertIn("ngsf_match_1.png", tab)
        self.assertIn("NGSF: Ia, sn2011fe", tab)

    def test_run_without_ngsf_installed_fails_with_the_reason(self):
        self.add_spectrum()
        svc.register_service("ngsf", runner="ngsf", user=self.staff, timeout_seconds=0)
        with override_settings(NGSF_COMMAND="", JOB_RUNNER_INLINE=True):
            response = self.client.post(reverse("transient_analysis_run", args=[self.transient.id]),
                                        data=json.dumps({"service": "ngsf", "params": {}}), content_type="application/json")
        run = ExternalServiceRun.objects.get(uuid=response.json()["run"]["uuid"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("NGSF_COMMAND is empty", run.error)
        with override_settings(NGSF_COMMAND=""):
            html = self.client.get(self.url()).content.decode()
        self.assertIn("last run failed", html)


class HasJwstFlagTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = create_test_user("jwst_flag_user")
        self.transient = create_minimal_transient(self.user, name="2026jwstflag")
        self.client = Client()
        self.client.force_login(self.user)

    def _status(self, name):
        return self.client.get(reverse(name, args=[self.transient.id])).json()

    def test_field_defaults_to_unknown_and_is_in_the_archive_flags(self):
        self.assertIsNone(self.transient.has_jwst)
        self.assertEqual([label for label, _flag in self.transient.archive_flags], ["HST", "JWST", "Chandra", "Spitzer"])

    def test_jwst_status_records_the_flag_without_touching_modified_date(self):
        before = Transient.objects.get(pk=self.transient.pk).modified_date
        with mock.patch("YSE_App.common.mast_query.jwstObservations", test_jwst_tab._FakeJwst):
            payload = self._status("get_jwst_status")
        self.assertEqual(payload["has_data"], True)
        t = Transient.objects.get(pk=self.transient.pk)
        self.assertIs(t.has_jwst, True)
        self.assertEqual(t.modified_date, before)
        cache.clear()
        with mock.patch("YSE_App.common.mast_query.jwstObservations", test_jwst_tab._EmptyJwst):
            payload = self._status("get_jwst_status")
        self.assertEqual(payload["has_data"], False)
        self.assertIs(Transient.objects.get(pk=self.transient.pk).has_jwst, False)

    def test_failed_lookup_leaves_the_flag_alone(self):
        Transient.objects.filter(pk=self.transient.pk).update(has_jwst=True)
        with mock.patch("YSE_App.common.mast_query.jwstObservations", test_jwst_tab._BrokenJwst):
            payload = self._status("get_jwst_status")
        self.assertIsNone(payload["has_data"])
        self.assertIs(Transient.objects.get(pk=self.transient.pk).has_jwst, True)

    def test_cached_answer_does_not_rewrite(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", test_jwst_tab._FakeJwst):
            self._status("get_jwst_status")
        Transient.objects.filter(pk=self.transient.pk).update(has_jwst=None)
        with mock.patch("YSE_App.common.mast_query.jwstObservations", test_jwst_tab._FakeJwst), \
                mock.patch.object(view_utils, "_record_archive_flag") as record:
            self._status("get_jwst_status")
        self.assertFalse(record.called)
        self.assertEqual(view_utils._record_archive_flag(self.transient.pk, "has_jwst", None), False)
        self.assertEqual(view_utils._record_archive_flag(self.transient.pk, "has_jwst", True), True)
        self.assertEqual(view_utils._record_archive_flag(self.transient.pk, "has_jwst", True), False)

    def test_hst_and_chandra_status_record_their_flags_too(self):
        hst = mock.MagicMock()
        hst.return_value.Nimages = 3
        with mock.patch("YSE_App.common.mast_query.hstImages", hst):
            self.assertEqual(self._status("get_hst_status")["has_data"], True)
        self.assertIs(Transient.objects.get(pk=self.transient.pk).has_hst, True)
        chandra = mock.MagicMock()
        chandra.return_value.n_obsid = 0
        with mock.patch("YSE_App.common.chandra_query.chandraImages", chandra):
            self.assertEqual(self._status("get_chandra_status")["has_data"], False)
        self.assertIs(Transient.objects.get(pk=self.transient.pk).has_chandra, False)

    def test_search_filters_and_legacy_key(self):
        other = create_minimal_transient(self.user, name="2026nojwst")
        unknown = create_minimal_transient(self.user, name="2026unknownjwst")
        Transient.objects.filter(pk=self.transient.pk).update(has_jwst=True)
        Transient.objects.filter(pk=other.pk).update(has_jwst=False)
        factory = RequestFactory()

        def names(params):
            request = factory.get("/search/", params)
            request.user = self.user
            fs = TransientSearchFilterSet(request.GET, queryset=Transient.objects.filter(
                pk__in=[self.transient.pk, other.pk, unknown.pk]), request=request)
            self.assertTrue(fs.is_valid(), fs.errors)
            return set(fs.qs.values_list("name", flat=True))

        self.assertEqual(names({"has_jwst": "true"}), {"2026jwstflag"})
        self.assertEqual(names({"has_jwst": "false"}), {"2026nojwst"})
        self.assertEqual(names({}), {"2026jwstflag", "2026nojwst", "2026unknownjwst"})
        self.assertEqual(names({"annotation_origin": "legacy", "annotation_key": "has_jwst", "annotation_value_eq": "true"}),
                         {"2026jwstflag"})
        self.assertEqual(names({"annotation_origin": "legacy", "annotation_key": "has_jwst"}), {"2026jwstflag", "2026nojwst"})
        self.assertEqual(names({"annotation_origin": "legacy", "annotation_key": "has_jwst", "annotation_value_min": "1"}),
                         {"2026jwstflag"})
        page = self.client.get(reverse("search"), {"has_jwst": "true"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Has JWST data", page.content.decode())

    def test_legacy_annotation_and_api_expose_the_flag(self):
        from YSE_App.services.annotations import legacy_data

        Transient.objects.filter(pk=self.transient.pk).update(has_jwst=True)
        self.transient.refresh_from_db()
        self.assertEqual(legacy_data(self.transient).get("has_jwst"), True)
        response = self.client.get("/api/transients/%d/" % self.transient.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["has_jwst"], True)
        response = self.client.get("/api/transients/", {"has_jwst": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["name"] for r in response.json()["results"]], ["2026jwstflag"])

    def test_summary_tab_shows_archive_badges(self):
        Transient.objects.filter(pk=self.transient.pk).update(has_jwst=True, has_hst=False)
        html = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug})).content.decode()
        self.assertIn('data-archive="JWST" data-flag="1"', html)
        self.assertIn('data-archive="HST" data-flag="0"', html)
        self.assertIn('data-archive="Chandra" data-flag="unknown"', html)
        self.assertIn(">HST: no<", html)
        self.assertIn(">Chandra: ?<", html)
