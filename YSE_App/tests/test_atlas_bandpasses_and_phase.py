"""ATLAS bandpasses for sncosmo fits (#963) and the "Phase today" frame (#964).

Pins:

* every ATLAS cyan / orange / c / o key in ``bandpassdict`` maps to sncosmo's
  ``atlasc`` / ``atlaso``, never to an SDSS or Bessell curve; ATLAS w has no
  entry, so fits skip it with the "no bandpass" hint;
* the pinned sncosmo registers ``atlasc`` / ``atlaso`` (checked in the loader
  metadata, nothing is downloaded);
* a SALT-style fit on synthetic ATLAS c / o points fits in ``atlasc`` /
  ``atlaso`` (stand-in curves are registered under those names for the test
  and removed afterwards, so nothing is downloaded);
* ``phase_today`` stays observer-frame; ``phase_today_rest`` is
  ``phase_today / (1 + z)`` and only present with a redshift; labels say which
  frame, and the rest-frame row comes first on the Analysis tab.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import types

import numpy as np
from django.test import SimpleTestCase, TestCase

from YSE_App.analysis import bazin_fit, sncosmo_fit
from YSE_App.analysis.base import AnalysisError
from YSE_App.common.bandpassdict import bandpassdict
from YSE_App.models import Instrument, Observatory, PhotometricBand, Telescope, TransientPhotData, TransientPhotometry
from YSE_App.services import analysis_payload, analysis_services as svc, bazin
from YSE_App.templatetags.analysis_extras import analysis_label
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user
from YSE_App.tests.test_analysis_services import SYNTH_SOURCE, _register_synthetic_sncosmo, seed_bazin_lightcurve

ATLAS_BAND_NAMES = {"c": "atlasc", "o": "atlaso", "cyan": "atlasc", "orange": "atlaso",
                    "cyan-ATLAS": "atlasc", "orange-ATLAS": "atlaso"}


def _split(key):
    """``'Band: <instrument> - <band>'`` -> ``(instrument, band)``."""
    inst, band = key[len("Band: "):].rsplit(" - ", 1)
    return inst, band


class AtlasBandpassDictTests(SimpleTestCase):
    def test_every_atlas_cyan_orange_key_maps_to_the_atlas_bandpass(self):
        seen = 0
        for key, value in bandpassdict.items():
            inst, band = _split(key)
            is_atlas = "ATLAS" in band or inst.startswith("ATLAS") or (inst == "ACAM1" and band in ("cyan", "orange"))
            if is_atlas and band in ATLAS_BAND_NAMES:
                seen += 1
                self.assertEqual(value, ATLAS_BAND_NAMES[band], key)
        self.assertGreaterEqual(seen, 20)

    def test_no_atlas_key_uses_an_sdss_or_bessell_curve(self):
        for key, value in bandpassdict.items():
            inst, band = _split(key)
            if "ATLAS" in key or inst == "ACAM1":
                self.assertFalse(value.startswith(("sdss", "bessell")), "%s -> %s" % (key, value))

    def test_atlas_w_has_no_bandpass(self):
        for key in bandpassdict:
            inst, band = _split(key)
            if inst.startswith("ATLAS") or inst == "ACAM1":
                self.assertNotIn(band, ("w", "wide", "w-ATLAS", "wide-ATLAS"), key)

    def test_lookups_through_the_payload_helper(self):
        cases = [
            ("ACAM1", "cyan", "atlasc"), ("ACAM1", "orange", "atlaso"),
            ("ACAM1", "cyan-ATLAS", "atlasc"), ("ACAM1", "orange-ATLAS", "atlaso"),
            ("Unknown", "cyan-ATLAS", "atlasc"), ("Unknown", "orange-ATLAS", "atlaso"),
            ("Gaia-Photometric", "orange-ATLAS", "atlaso"), ("GPC1", "orange-ATLAS", "atlaso"),
            ("ATLAS-01", "o", "atlaso"), ("ATLAS-03", "c", "atlasc"),
            ("ATLAS-05", "c", "atlasc"), ("ATLAS-05", "o", "atlaso"), ("ATLAS-08", "o", "atlaso"),
            ("ATLAS-05", "w", None), ("ATLAS-01", "w", None),
            # non-ATLAS entries are unchanged
            ("GPC1", "r", "sdssr"), ("ZTF-Cam", "g", "sdssg"), ("LSSTCam", "i", "lssti"),
        ]
        for inst, band, expected in cases:
            self.assertEqual(analysis_payload.sncosmo_band_name(inst, band), expected, (inst, band))

    def test_pinned_sncosmo_registers_the_atlas_bandpasses(self):
        from sncosmo.bandpasses import _BANDPASSES

        names = {m["name"] for m in _BANDPASSES.get_loaders_metadata()}
        self.assertIn("atlasc", names)
        self.assertIn("atlaso", names)


class _StandInAtlasBandpasses:
    """Register Gaussian-ish curves as ``atlasc`` / ``atlaso`` for one test; restore the registry after."""

    CENTRES = {"atlasc": 5330.0, "atlaso": 6790.0}

    def __enter__(self):
        import sncosmo
        from sncosmo.bandpasses import _BANDPASSES

        self._registry = _BANDPASSES
        self._saved = {name: _BANDPASSES._instances.get((name, None)) for name in self.CENTRES}
        for name, centre in self.CENTRES.items():
            band = sncosmo.Bandpass([centre - 1300.0, centre - 650.0, centre, centre + 650.0, centre + 1300.0],
                                    [0.0, 0.8, 1.0, 0.8, 0.0], name=name)
            sncosmo.register(band, name, force=True)
        return self

    def __exit__(self, *exc):
        for name, inst in self._saved.items():
            if inst is None:
                self._registry._instances.pop((name, None), None)
            else:
                self._registry._instances[(name, None)] = inst
        return False


class AtlasSaltFitTests(TestCase):
    def setUp(self):
        self.user = create_test_user("atlas_fit_staff", is_superuser=True)
        self.transient = create_minimal_transient(self.user, name="2026atl", obs_group_name="atlas-fit-group")
        self.transient.redshift = 0.03
        self.transient.save(update_fields=["redshift"])

    def _atlas_bands(self):
        audit = audit_fields(self.user)
        obs, _ = Observatory.objects.get_or_create(name="AtlasTestObs", defaults={"utc_offset": -10, "tz_name": "US/Hawaii", **audit})
        tel, _ = Telescope.objects.get_or_create(name="AtlasTestTel", defaults={"observatory": obs, "latitude": 19.5, "longitude": -155.6, "elevation": 3400.0, **audit})
        inst, _ = Instrument.objects.get_or_create(name="ATLAS-05", defaults={"telescope": tel, **audit})
        bands = {}
        for n in ("c", "o", "w"):
            bands[n], _ = PhotometricBand.objects.get_or_create(name=n, instrument=inst, defaults={"disp_color": "#888888", "disp_symbol": "circle", **audit})
        return inst, bands

    def _seed(self, names=("c", "o", "w")):
        """ATLAS points drawn from the synthetic source through the stand-in curves (w uses the o curve)."""
        import sncosmo

        _register_synthetic_sncosmo()
        inst, bands = self._atlas_bands()
        audit = audit_fields(self.user)
        phot = TransientPhotometry.objects.create(transient=self.transient, instrument=inst, obs_group=self.transient.obs_group, **audit)
        t0 = bazin.datetime_to_mjd(datetime.datetime.now(datetime.timezone.utc)) - 15.0
        model = sncosmo.Model(source=SYNTH_SOURCE)
        model.set(z=0.03, t0=t0, amplitude=0.2)
        rng = np.random.default_rng(7)
        rows = []
        for bname in names:
            sband = {"c": "atlasc", "o": "atlaso", "w": "atlaso"}[bname]
            mjd = t0 + np.arange(-12.0, 30.0, 2.0) + rng.uniform(-0.2, 0.2, 21)
            mag = -2.5 * np.log10(model.bandflux(sband, mjd, zp=27.5, zpsys="ab")) + 27.5 + rng.normal(0, 0.03, len(mjd))
            for m, y in zip(mjd, mag):
                rows.append(TransientPhotData(photometry=phot, band=bands[bname], obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(m)),
                                              mag=float(y), mag_err=0.04, **audit))
        TransientPhotData.objects.bulk_create(rows)
        return t0

    def test_fit_uses_atlasc_and_atlaso_and_skips_w(self):
        with _StandInAtlasBandpasses():
            t0 = self._seed()
            payload = analysis_payload.build_payload(self.transient, self.user, ["photometry", "redshift"])
            by_band = {r["band"]: r["sncosmo_band"] for r in payload["photometry"]}
            self.assertEqual(by_band, {"c": "atlasc", "o": "atlaso", "w": None})
            result = sncosmo_fit.run(payload, {"source": SYNTH_SOURCE, "mw_extinction": False})
        r = result.results
        self.assertEqual(r["bands"], ["atlasc", "atlaso"])
        self.assertLess(abs(r["t0"] - t0), 1.0, r)

    def test_only_w_points_fail_with_the_no_bandpass_hint(self):
        with _StandInAtlasBandpasses():
            self._seed(names=("w",))
            payload = analysis_payload.build_payload(self.transient, self.user, ["photometry", "redshift"])
            with self.assertRaises(AnalysisError) as ctx:
                sncosmo_fit.run(payload, {"source": SYNTH_SOURCE})
        self.assertIn("ATLAS-05 w", str(ctx.exception))

    def test_salt_phase_today_in_both_frames(self):
        with _StandInAtlasBandpasses():
            self._seed()
            payload = analysis_payload.build_payload(self.transient, self.user, ["photometry", "redshift"])
            r = sncosmo_fit.run(payload, {"source": SYNTH_SOURCE, "mw_extinction": False}).results
        self.assertAlmostEqual(r["phase_today"], 15.0, delta=1.5)
        self.assertAlmostEqual(r["phase_today_rest"], r["phase_today"] / 1.03, places=6)


class BazinPhaseTodayTests(TestCase):
    def setUp(self):
        self.user = create_test_user("phase_staff", is_superuser=True)
        self.transient = create_minimal_transient(self.user, name="2026phs", obs_group_name="phase-group")

    def test_rest_frame_phase_with_redshift(self):
        self.transient.redshift = 0.1
        self.transient.save(update_fields=["redshift"])
        seed_bazin_lightcurve(self.user, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.user, ["photometry"])
        r = bazin_fit.run(payload, {}).results
        today = bazin.datetime_to_mjd(datetime.datetime.now(datetime.timezone.utc))
        self.assertAlmostEqual(r["phase_today"], today - r["peak_mjd"], delta=0.01)
        self.assertAlmostEqual(r["phase_today_rest"], r["phase_today"] / 1.1, places=6)

    def test_observer_frame_only_without_redshift(self):
        seed_bazin_lightcurve(self.user, self.transient)
        payload = analysis_payload.build_payload(self.transient, self.user, ["photometry"])
        payload["redshift"] = None
        r = bazin_fit.run(payload, {}).results
        self.assertIsNotNone(r["phase_today"])
        self.assertNotIn("phase_today_rest", r)


class PhaseHelpersTests(SimpleTestCase):
    def test_rest_frame_phase(self):
        self.assertAlmostEqual(bazin_fit.rest_frame_phase(11.0, 0.1), 10.0)
        self.assertAlmostEqual(bazin_fit.rest_frame_phase(-5.0, 0.0), -5.0)
        self.assertIsNone(bazin_fit.rest_frame_phase(None, 0.1))
        self.assertIsNone(bazin_fit.rest_frame_phase(10.0, None))
        self.assertIsNone(bazin_fit.rest_frame_phase(10.0, float("nan")))
        self.assertIsNone(bazin_fit.rest_frame_phase(10.0, -0.5))

    def test_labels_name_the_frame(self):
        self.assertEqual(analysis_label("phase_today"), "Phase today (observer-frame days)")
        self.assertEqual(analysis_label("phase_today_rest"), "Phase today (rest-frame days)")

    def test_rest_frame_row_comes_first(self):
        run = types.SimpleNamespace(result={"peak_mjd": 60000.0, "phase_today": 11.0, "phase_today_rest": 10.0, "z": 0.1})
        keys = [k for k, _v in svc.result_rows(run)]
        self.assertLess(keys.index("phase_today_rest"), keys.index("phase_today"))
        run = types.SimpleNamespace(result={"phase_today": 11.0})
        self.assertEqual([k for k, _v in svc.result_rows(run)], ["phase_today"])
