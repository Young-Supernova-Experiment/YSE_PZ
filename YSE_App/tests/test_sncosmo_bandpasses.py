"""SALT3 fit bands: ATLAS, DECam and GOTO map to sncosmo bandpasses (hotfix).

A SALT3 run on a transient with only ATLAS-04 c, DECam g/r/i/z and GOTO-4 L
photometry used to fail with "0 usable detection(s) with a known bandpass".
GOTO (and ATLAS on sncosmo < 2.8) curves ship in ``common/bandpasses/``; the
tests never download anything.
"""

import datetime

import numpy as np
from django.test import SimpleTestCase
from django.utils import timezone

from YSE_App.analysis import sncosmo_fit
from YSE_App.common import sncosmo_bandpasses
from YSE_App.common.bandpassdict import bandpassdict
from YSE_App.models import Instrument, Observatory, PhotometricBand, Telescope, TransientPhotData, TransientPhotometry
from YSE_App.services import analysis_payload, bazin
from YSE_App.tests.fixtures_minimal import audit_fields
from YSE_App.tests.test_analysis_services import SYNTH_SOURCE, AnalysisBase, _register_synthetic_sncosmo


def _sncosmo_has_builtin_atlas():
    import sncosmo

    major, minor = (int(x) for x in sncosmo.__version__.split(".")[:2])
    return (major, minor) >= (2, 8)


class BandpassDictTests(SimpleTestCase):
    def test_reported_bands_have_bandpasses(self):
        expected = {
            ("ATLAS-04", "c"): "atlasc",
            ("ATLAS-04", "o"): "atlaso",
            ("DECam", "g"): "desg",
            ("DECam", "r"): "desr",
            ("DECam", "i"): "desi",
            ("DECam", "z"): "desz",
            ("GOTO-4", "L"): "gotol",
            # facility-queue ATLAS forced photometry lands on instrument "ATLAS"
            ("ATLAS", "c"): "atlasc",
            ("ATLAS", "o"): "atlaso",
        }
        for (instrument, band), name in expected.items():
            self.assertEqual(analysis_payload.sncosmo_band_name(instrument, band), name, (instrument, band))

    def test_every_atlas_unit_and_goto_band(self):
        for unit in range(1, 9):
            self.assertEqual(bandpassdict["Band: ATLAS-%02i - c" % unit], "atlasc")
            self.assertEqual(bandpassdict["Band: ATLAS-%02i - o" % unit], "atlaso")
        for unit in range(1, 5):
            self.assertEqual(bandpassdict["Band: GOTO-%i - L" % unit], "gotol")
            self.assertEqual(bandpassdict["Band: GOTO-%i - g" % unit], "gotog")


class LocalBandpassTests(SimpleTestCase):
    def test_curves_load(self):
        # (name, lower wavelength, upper wavelength) in Angstrom
        for name, lo, hi in (("gotol", 3900, 7200), ("gotor", 5500, 7300), ("gotog", 4000, 6000), ("gotob", 3600, 5300),
                             ("atlasc", 4100, 6600), ("atlaso", 5500, 8300)):
            fname = {**sncosmo_bandpasses.LOCAL_BANDPASSES, **sncosmo_bandpasses.FALLBACK_BANDPASSES}[name]
            band = sncosmo_bandpasses._load("%s/%s" % (sncosmo_bandpasses.BANDPASS_DIR, fname), name=name)
            self.assertGreaterEqual(band.minwave(), lo - 100, name)
            self.assertLessEqual(band.maxwave(), hi + 100, name)
            self.assertTrue(lo < band.wave_eff < hi, (name, band.wave_eff))

    def test_register_is_idempotent_and_resolves_goto(self):
        import sncosmo

        sncosmo_bandpasses.register()
        sncosmo_bandpasses.register()
        self.assertAlmostEqual(sncosmo.get_bandpass("gotol").wave_eff, 5500, delta=200)
        if not _sncosmo_has_builtin_atlas():
            self.assertAlmostEqual(sncosmo.get_bandpass("atlaso").wave_eff, 6800, delta=300)


class GotoFitTests(AnalysisBase):
    def test_fit_runs_on_goto_l_only(self):
        """Before the fix this raised '0 usable detection(s) with a known bandpass'."""
        import sncosmo

        _register_synthetic_sncosmo()
        sncosmo_bandpasses.register()
        audit = audit_fields(self.staff)
        obs, _ = Observatory.objects.get_or_create(name="GOTO-N obs", defaults={"utc_offset": 0, "tz_name": "UTC", **audit})
        tel, _ = Telescope.objects.get_or_create(name="GOTO-N", defaults={"observatory": obs, "latitude": 28.76, "longitude": -17.88, "elevation": 2300.0, **audit})
        inst, _ = Instrument.objects.get_or_create(name="GOTO-4", defaults={"telescope": tel, **audit})
        band, _ = PhotometricBand.objects.get_or_create(name="L", instrument=inst, defaults={"disp_color": "#888888", "disp_symbol": "circle", **audit})
        phot = TransientPhotometry.objects.create(transient=self.transient, instrument=inst, obs_group=self.transient.obs_group, **audit)
        t0 = bazin.datetime_to_mjd(timezone.now()) - 15.0
        model = sncosmo.Model(source=SYNTH_SOURCE)
        model.set(z=0.03, t0=t0, amplitude=0.2)
        mjd = t0 + np.arange(-12.0, 30.0, 3.0)
        mag = -2.5 * np.log10(model.bandflux("gotol", mjd, zp=27.5, zpsys="ab")) + 27.5
        TransientPhotData.objects.bulk_create([
            TransientPhotData(photometry=phot, band=band, obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(m)),
                              mag=float(y), mag_err=0.04, **audit)
            for m, y in zip(mjd, mag)
        ])
        payload = analysis_payload.build_payload(self.transient, self.staff, ["photometry", "redshift"])
        result = sncosmo_fit.run(payload, {"source": SYNTH_SOURCE, "mw_extinction": False})
        self.assertEqual(result.results["bands"], ["gotol"])
        self.assertAlmostEqual(result.results["t0"], t0, delta=1.0)
