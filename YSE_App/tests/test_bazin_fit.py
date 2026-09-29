"""Bazin light-curve fits (#225, joint fit #336): the fitter, the detail-page overlay and the scheduling column.

``YSE_App/services/bazin.py`` fits a Bazin curve to every band of a
transient at once in flux space, with per-band shape parameters tied by a
wavelength-correlated prior, and extrapolates it to a chosen MJD.  These
tests pin:

* the fitter recovers a synthetic Bazin curve with noise and its
  extrapolated magnitude, and returns ``None`` for too few points, a fitter
  failure or unphysical parameters;
* the joint fit recovers wavelength-smooth shape parameters across bands, a
  band with three points borrows its neighbours' shape and keeps fading
  after peak, and a declining-only band does not settle on a plateau;
* the magnitude conversion (non-positive flux has no magnitude) and the
  observing-night epoch (local midnight of the night's date);
* ``bazinplot/<id>/1/`` overlays the fit (solid fitted span, dashed
  extrapolation, one ``Bazin fit`` legend entry) and ``.../0/`` is the
  cached plain plot; the detail page carries the button in both defer modes;
* ``ObsNightFollowupTable`` / ``ToOFollowupTable`` render ``Bazin Mag``
  (``18.71 r``), blank without a fit, with a query count that does not grow
  with rows; ``download_target_list`` carries the same value.
"""

import datetime
import math
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from YSE_App import view_utils
from YSE_App.models import (
    ClassicalNightType,
    ClassicalObservingDate,
    ClassicalResource,
    DataQuality,
    FollowupStatus,
    Instrument,
    Observatory,
    PhotometricBand,
    Telescope,
    ToOResource,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.services import bazin
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_lightcurve_legend import bokeh_doc_from_html, legend_grid

TRUTH = (2000.0, 60012.0, 25.0, 4.0, 50.0)  # A, t0, tau_fall, tau_rise, B


def synthetic_curve(n=15, step=3.0, start=60000.0, truth=TRUTH, noise=0.05, seed=3):
    """(mjd, mag, magerr) sampled from a Bazin curve with Gaussian magnitude noise."""
    rng = np.random.default_rng(seed)
    mjd = start + step * np.arange(n)
    mag = bazin.flux_to_mag(bazin.bazin_flux(mjd, *truth))
    magerr = np.full(n, noise)
    if noise:
        mag = mag + rng.normal(0, noise, n)
    return mjd, mag, magerr


def attach_bazin_photometry(user, transient, *, band, n_points=12, mjd_start=None,
                            flagged_first=False, with_upper_limit=False):
    """Bazin-shaped detections in ``band`` ending about now, plus optional junk rows."""
    audit = audit_fields(user)
    photometry = TransientPhotometry.objects.create(
        transient=transient, instrument=band.instrument, obs_group=transient.obs_group, **audit
    )
    now_mjd = bazin.datetime_to_mjd(timezone.now())
    if mjd_start is None:
        mjd_start = now_mjd - 3.0 * n_points  # last point ~today
    truth = (TRUTH[0], mjd_start + 12.0, TRUTH[2], TRUTH[3], TRUTH[4])
    mjd, mag, magerr = synthetic_curve(n=n_points, start=mjd_start, truth=truth, noise=0.0)
    rows = []
    for i, (m, y, e) in enumerate(zip(mjd, mag, magerr)):
        rows.append(TransientPhotData(
            photometry=photometry, band=band,
            obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(m)),
            mag=float(y), mag_err=float(e), discovery_point=(i == 0), **audit,
        ))
    if with_upper_limit:  # flux/flux_err < 3 and no mag: never a detection
        rows.append(TransientPhotData(
            photometry=photometry, band=band,
            obs_date=bazin._MJD_EPOCH + datetime.timedelta(days=float(mjd[0]) - 4),
            mag=None, mag_err=None, flux=10.0, flux_err=20.0, flux_zero_point=27.5, **audit,
        ))
    TransientPhotData.objects.bulk_create(rows)
    if flagged_first:
        bad, _ = DataQuality.objects.get_or_create(name="Bad", defaults=audit)
        first = TransientPhotData.objects.filter(photometry=photometry, mag__isnull=False).order_by("obs_date").first()
        first.mag = 10.0  # wildly off: only harmless if the fit ignores it
        first.save()
        first.data_quality.add(bad)
    return photometry, truth


# ------------------------------------------------------------------ pure helpers


class BazinModelTests(TestCase):
    def test_fit_recovers_synthetic_curve_and_extrapolated_mag(self):
        mjd, mag, magerr = synthetic_curve()
        fit = bazin.fit_bazin(mjd, mag, magerr)
        self.assertIsNotNone(fit)
        A, t0, tau_fall, tau_rise, B = fit.params
        self.assertAlmostEqual(t0, TRUTH[1], delta=2.0)
        self.assertAlmostEqual(tau_fall, TRUTH[2], delta=5.0)
        self.assertAlmostEqual(tau_rise, TRUTH[3], delta=1.5)
        self.assertGreater(A, 0)
        self.assertEqual(fit.n_points, 15)
        self.assertEqual(fit.dof, 10)
        self.assertLess(fit.reduced_chi2, 5.0)
        truth_mag = bazin.flux_to_mag(bazin.bazin_flux(TRUTH[1] + 20, *TRUTH))
        self.assertAlmostEqual(fit.mag_at(TRUTH[1] + 20), truth_mag, delta=0.05)
        # noiseless curve: parameters essentially exact
        exact = bazin.fit_bazin(*synthetic_curve(noise=0.0))
        for got, want in zip(exact.params, TRUTH):
            self.assertAlmostEqual(got, want, delta=abs(want) * 0.01 + 0.01)

    def test_rejections(self):
        mjd, mag, magerr = synthetic_curve()
        self.assertIsNone(bazin.fit_bazin(mjd[:4], mag[:4], magerr[:4]))
        self.assertIsNone(bazin.fit_bazin(mjd, np.full_like(mag, np.nan), magerr))
        self.assertIsNone(bazin.fit_bazin(np.full_like(mjd, 60000.0), mag, magerr))
        self.assertIsNone(bazin.fit_bazin([], [], []))
        with mock.patch("scipy.optimize.least_squares", side_effect=ValueError("synthetic failure")):
            self.assertIsNone(bazin.fit_bazin(mjd, mag, magerr))  # joint and shared solves both fail
        # x = [A, B, t0, log tau_rise, log tau_fall]: a zero amplitude or a NaN drops the band
        for x in (np.array([0.0, 0.0, 60012.0, 0.6, 1.4]), np.array([1.0, 0.0, np.nan, 0.6, 1.4])):
            result = mock.MagicMock(status=1, x=x, jac=np.eye(5))
            with mock.patch("scipy.optimize.least_squares", return_value=result):
                self.assertIsNone(bazin.fit_bazin(mjd, mag, magerr))
        failed = mock.MagicMock(status=-1, x=np.array([1.0, 0.0, 60012.0, 0.6, 1.4]), jac=np.eye(5))
        with mock.patch("scipy.optimize.least_squares", return_value=failed):
            self.assertIsNone(bazin.fit_bazin(mjd, mag, magerr))

    def test_fit_is_bounded_and_capped(self):
        mjd, mag, magerr = synthetic_curve()
        real = __import__("scipy.optimize", fromlist=["least_squares"]).least_squares
        with mock.patch("scipy.optimize.least_squares", wraps=real) as ls:
            fit = bazin.fit_bazin(mjd, mag, magerr)
        self.assertEqual(ls.call_count, 1)  # the joint solve succeeded; no shared fallback
        self.assertEqual(fit.method, "joint")
        args, kwargs = ls.call_args
        lower, upper = kwargs["bounds"]
        self.assertEqual(kwargs["max_nfev"], bazin.MAX_NFEV)
        # x = [A, B, t0, log10 tau_rise, log10 tau_fall] for one band
        self.assertEqual(lower[0], 0.0)
        peak = float(bazin.mag_to_flux(mag).max())
        self.assertEqual((lower[1], upper[1]), (0.0, bazin.B_MAX_FRACTION * peak))
        self.assertEqual((lower[2], upper[2]), (mjd.min() - bazin.T0_PAD_DAYS, mjd.max() + bazin.T0_PAD_DAYS))
        self.assertAlmostEqual(10 ** lower[3], bazin.TAU_RISE_BOUNDS[0])
        self.assertAlmostEqual(10 ** upper[3], bazin.TAU_RISE_BOUNDS[1])
        self.assertAlmostEqual(10 ** lower[4], bazin.TAU_FALL_BOUNDS[0])
        self.assertAlmostEqual(10 ** upper[4], bazin.TAU_FALL_BOUNDS[1])
        x0 = args[1]
        self.assertEqual(x0[2], mjd[np.argmin(mag)])  # brightest point = max flux
        self.assertAlmostEqual(10 ** x0[3], 10.0)
        self.assertAlmostEqual(10 ** x0[4], 30.0)

    def test_shared_shape_fallback_when_the_joint_solve_fails(self):
        mjd, mag, magerr = synthetic_curve()
        real = __import__("scipy.optimize", fromlist=["least_squares"]).least_squares
        calls = []

        def flaky(fun, x0, **kwargs):
            calls.append(len(x0))
            if len(calls) == 1:
                raise ValueError("joint solve blew up")
            return real(fun, x0, **kwargs)

        points = {"g": list(zip(mjd, mag, magerr)), "r": list(zip(mjd + 1, mag + 0.1, magerr))}
        with mock.patch("scipy.optimize.least_squares", side_effect=flaky):
            fits = bazin.fit_bazin_joint(points, {"g": 4770.0, "r": 6230.0})
        self.assertEqual(calls, [10, 7])  # 2 bands: 5 params each, then 2 x (A, B) + one shared shape
        self.assertEqual(set(fits), {"g", "r"})
        self.assertTrue(all(fit.method == "shared" for fit in fits.values()))
        self.assertAlmostEqual(fits["g"].params[1], fits["r"].params[1])  # same t0
        self.assertAlmostEqual(fits["g"].params[2], fits["r"].params[2])  # same tau_fall

    def test_mag_conversion(self):
        self.assertAlmostEqual(bazin.flux_to_mag(1.0), 27.5)
        self.assertAlmostEqual(float(bazin.mag_to_flux(27.5)), 1.0)
        self.assertIsNone(bazin.flux_to_mag(0.0))
        self.assertIsNone(bazin.flux_to_mag(-3.0))
        arr = bazin.flux_to_mag(np.array([1.0, 0.0, -1.0]))
        self.assertAlmostEqual(arr[0], 27.5)
        self.assertTrue(np.isnan(arr[1]) and np.isnan(arr[2]))
        self.assertAlmostEqual(float(bazin.flux_err_from_mag_err(100.0, 0.1)), 100 * 0.1 * 0.4 * math.log(10))
        # a fit whose curve drops below zero flux has no magnitude out there
        faint = bazin.BazinFit(params=(100.0, 60000.0, 10.0, 2.0, -20.0), cov=(), chi2=0.0, dof=1,
                               n_points=6, mjd_min=59990.0, mjd_max=60010.0)
        self.assertAlmostEqual(bazin.bazin_mag_at(faint, 60000.0), bazin.flux_to_mag(30.0))
        self.assertIsNone(bazin.bazin_mag_at(faint, 60200.0))
        self.assertIsNone(bazin.bazin_mag_at(None, 60000.0))

    def test_usable_detection_rule(self):
        self.assertTrue(bazin.is_usable_detection(18.0, 0.1))
        self.assertTrue(bazin.is_usable_detection(18.0, 0.5))  # no flux info: kept, like the plot
        self.assertFalse(bazin.is_usable_detection(18.0, 0.5, flux=10.0, flux_err=5.0))
        self.assertTrue(bazin.is_usable_detection(18.0, 0.36, flux=10.0, flux_err=5.0))
        self.assertFalse(bazin.is_usable_detection(None, 0.1))
        self.assertFalse(bazin.is_usable_detection(18.0, None))
        self.assertFalse(bazin.is_usable_detection(18.0, 0.1, flagged=True))
        self.assertFalse(bazin.is_usable_detection(float("nan"), 0.1))

    def test_local_midnight_mjd(self):
        night = datetime.datetime(2026, 9, 29, 0, 0, tzinfo=datetime.timezone.utc)
        day = bazin.datetime_to_mjd(night)
        self.assertAlmostEqual(bazin.local_midnight_mjd(night, -10), day + 1 + 10 / 24)  # Hawaii
        self.assertAlmostEqual(bazin.local_midnight_mjd(night, -4), day + 1 + 4 / 24)  # Chile
        self.assertAlmostEqual(bazin.local_midnight_mjd(night, 8), day + 16 / 24)  # east
        self.assertAlmostEqual(bazin.local_midnight_mjd(night.date(), None), day + 1)
        self.assertAlmostEqual(bazin.datetime_to_mjd(datetime.datetime(1858, 11, 17)), 0.0)
        self.assertAlmostEqual(bazin.datetime_to_mjd(datetime.datetime(2000, 1, 1, 12)), 51544.5)

    def test_pick_prefers_named_band_then_most_recent(self):
        early = bazin.fit_bazin(*synthetic_curve(start=60000.0, noise=0.0))
        late = bazin.fit_bazin(*synthetic_curve(start=60010.0, noise=0.0))
        fits = {"g": early, "r": late}
        self.assertEqual(bazin.pick_extrapolated_mag(fits, 60050.0)[1], "r")
        self.assertEqual(bazin.pick_extrapolated_mag(fits, 60050.0, ["g"])[1], "g")
        self.assertEqual(bazin.pick_extrapolated_mag(fits, 60050.0, ["z"])[1], "r")
        self.assertIsNone(bazin.pick_extrapolated_mag({}, 60050.0))
        # a band whose extrapolation has no flux is skipped for the next one
        faint = bazin.BazinFit(params=(100.0, 60030.0, 10.0, 2.0, -50.0), cov=(), chi2=0.0, dof=1,
                               n_points=6, mjd_min=60020.0, mjd_max=60060.0)
        self.assertEqual(bazin.pick_extrapolated_mag({"g": early, "i": faint}, 60300.0)[1], "g")

    def test_plot_grid_spans_lead_in_and_extrapolation(self):
        fit = bazin.fit_bazin(*synthetic_curve(noise=0.0))
        fitted, extrapolated = bazin.bazin_plot_grid(fit, today_mjd=60100.0)
        self.assertAlmostEqual(fitted[0], fit.mjd_min - bazin.GRID_LEAD_DAYS)
        self.assertAlmostEqual(fitted[-1], fit.mjd_max)
        self.assertAlmostEqual(extrapolated[0], fit.mjd_max)
        self.assertAlmostEqual(extrapolated[-1], 60100.0 + bazin.EXTRAPOLATION_DAYS)
        _fitted, past = bazin.bazin_plot_grid(fit, today_mjd=59000.0)  # data newer than "today"
        self.assertAlmostEqual(past[-1], fit.mjd_max + bazin.EXTRAPOLATION_DAYS)

    def test_fit_bands_keeps_short_bands_and_needs_five_points_in_total(self):
        mjd, mag, magerr = synthetic_curve(noise=0.0)
        fits = bazin.fit_bands({
            "r": list(zip(mjd, mag, magerr)),
            "g": list(zip(mjd[:3], mag[:3], magerr[:3])),
            "empty": [],
        })
        self.assertEqual(set(fits), {"r", "g"})  # the 3-point band borrows r's shape
        self.assertEqual(fits["g"].n_points, 3)
        self.assertEqual(bazin.fit_bands({"g": list(zip(mjd[:2], mag[:2], magerr[:2])),
                                          "r": list(zip(mjd[:2], mag[:2], magerr[:2]))}), {})
        self.assertEqual(bazin.fit_bands({}), {})


# ------------------------------------------------------------- joint fit (#336)


LAMBDA = {"g": 4770.0, "r": 6230.0, "i": 7630.0, "z": 9050.0}


def smooth_truth(band, t0=60012.0):
    """Bazin parameters that vary smoothly with log wavelength (redder = slower)."""
    dl = math.log10(LAMBDA[band]) - math.log10(LAMBDA["g"])
    return (2000.0 * (1 - 0.3 * dl), t0 + 4 * dl, 20.0 * (1 + 1.5 * dl), 4.0 * (1 + 0.8 * dl), 0.0)


def band_points(band, mjd, noise=0.04, seed=5):
    rng = np.random.default_rng(seed)
    mag = bazin.flux_to_mag(bazin.bazin_flux(mjd, *smooth_truth(band))) + rng.normal(0, noise, len(mjd))
    return list(zip(mjd, mag, np.full(len(mjd), noise)))


def truth_mag(band, mjd):
    return bazin.flux_to_mag(bazin.bazin_flux(mjd, *smooth_truth(band)))


class BazinJointFitTests(TestCase):
    def test_wavelength_lookup_by_alias_family_and_default(self):
        from YSE_App.common.filter_display import DEFAULT_EFFECTIVE_WAVELENGTH_AA, band_effective_wavelength

        self.assertEqual(band_effective_wavelength("g"), 4770.0)
        self.assertEqual(band_effective_wavelength("r-ZTF"), 6440.0)
        self.assertEqual(band_effective_wavelength("orange-ATLAS"), 6790.0)
        self.assertEqual(band_effective_wavelength("y-LSST"), 9710.0)
        self.assertEqual(band_effective_wavelength("UVW2"), 2030.0)
        self.assertEqual(band_effective_wavelength("r-bazin-db"), 6230.0)  # family fallback
        self.assertEqual(band_effective_wavelength("H"), 16620.0)
        self.assertEqual(band_effective_wavelength("Q-unknown"), DEFAULT_EFFECTIVE_WAVELENGTH_AA)
        self.assertEqual(band_effective_wavelength(None), DEFAULT_EFFECTIVE_WAVELENGTH_AA)

    def test_shape_correlation_is_strong_for_neighbours_and_floored_for_distant_bands(self):
        gr = bazin.shape_correlation(LAMBDA["g"], LAMBDA["r"])
        ri = bazin.shape_correlation(LAMBDA["r"], LAMBDA["i"])
        uy = bazin.shape_correlation(3560.0, 9620.0)
        self.assertGreater(ri, gr)
        self.assertGreater(gr, 0.5)
        self.assertEqual(uy, bazin.CORRELATION_FLOOR)
        self.assertEqual(bazin.shape_correlation(6000.0, 6000.0), 1.0)
        self.assertEqual(bazin.shape_correlation(None, 6000.0), bazin.CORRELATION_FLOOR)

    def test_joint_fit_recovers_wavelength_smooth_shapes(self):
        mjd = 60000.0 + 3.0 * np.arange(15)
        fits = bazin.fit_bazin_joint({b: band_points(b, mjd) for b in LAMBDA}, LAMBDA)
        self.assertEqual(set(fits), set(LAMBDA))
        for band, fit in fits.items():
            A, t0, tau_fall, tau_rise, B = fit.params
            tA, tt0, tfall, trise, _tB = smooth_truth(band)
            self.assertEqual(fit.method, "joint")
            self.assertAlmostEqual(t0, tt0, delta=1.5, msg=band)
            self.assertAlmostEqual(tau_fall, tfall, delta=0.15 * tfall, msg=band)
            self.assertAlmostEqual(tau_rise, trise, delta=0.2 * trise, msg=band)
            self.assertLess(B, bazin.B_MAX_FRACTION * A)
            for dt in (10, 25, 40):
                self.assertAlmostEqual(fit.mag_at(tt0 + dt), truth_mag(band, tt0 + dt), delta=0.15, msg=band)
        # redder bands fall slower, as in the truth
        self.assertLess(fits["g"].params[2], fits["z"].params[2])

    def test_three_point_band_borrows_shape_and_keeps_fading(self):
        mjd = 60000.0 + 3.0 * np.arange(15)
        fits = bazin.fit_bazin_joint(
            {"r": band_points("r", mjd), "g": band_points("g", np.array([60006.0, 60009.0, 60012.0]))}, LAMBDA
        )
        g = fits["g"]
        self.assertEqual(g.n_points, 3)
        peak = smooth_truth("g")[1]
        mags = [g.mag_at(peak + dt) for dt in (10, 20, 30, 40)]
        # no plateau: each further 10 d is at least 0.3 mag fainter
        for earlier, later in zip(mags, mags[1:]):
            self.assertGreater(later - earlier, 0.3)
        for dt, mag in zip((10, 20, 30, 40), mags):
            self.assertAlmostEqual(mag, truth_mag("g", peak + dt), delta=0.5)
        self.assertLess(g.params[4], 1e-6 * g.params[0])  # baseline pinned for a band with < 3 points

    def test_declining_only_band_does_not_plateau(self):
        """2025aarm-like: one band seen only on the decline sits on its neighbour's tail."""
        mjd = 60000.0 + 3.0 * np.arange(15)
        fits = bazin.fit_bazin_joint(
            {"r": band_points("r", mjd), "i": band_points("i", np.array([60020.0, 60024.0, 60028.0, 60032.0]))}, LAMBDA
        )
        i = fits["i"]
        peak = smooth_truth("i")[1]
        mags = [i.mag_at(peak + dt) for dt in (20, 40, 60, 80)]
        for earlier, later in zip(mags, mags[1:]):
            self.assertGreater(later - earlier, 0.5)  # keeps fading at ~0.04 mag/d
        for dt, mag in zip((20, 40, 60, 80), mags):
            self.assertAlmostEqual(mag, truth_mag("i", peak + dt), delta=0.5)
        self.assertLess(i.params[4], 0.01 * i.params[0])  # baseline ~0
        # even alone, the bounded baseline stops the tail from flattening at the last point
        alone = bazin.fit_bazin_joint(
            {"i": band_points("i", np.array([60020.0, 60024.0, 60028.0, 60032.0, 60036.0]))}, LAMBDA
        )["i"]
        self.assertGreater(alone.mag_at(peak + 80) - alone.mag_at(peak + 40), 1.0)

    def test_unknown_wavelengths_fall_back_to_the_default(self):
        mjd = 60000.0 + 3.0 * np.arange(12)
        fits = bazin.fit_bazin_joint({"a": band_points("g", mjd), "b": band_points("r", mjd)})
        self.assertEqual(set(fits), {"a", "b"})
        self.assertAlmostEqual(fits["a"].params[1], fits["b"].params[1], delta=1.0)


# ------------------------------------------------------------- database layer


class BazinDatabaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("bazin_db_user")
        cls.transient = create_minimal_transient(cls.user, name="2026bazindb")
        cls.obs_group, instrument, cls.r_band = create_instrument_stack(cls.user, obs_group_name="bazin-db")
        audit = audit_fields(cls.user)
        cls.g_band = PhotometricBand.objects.create(
            name="g", instrument=instrument, disp_color="#00ff00", disp_symbol="circle", **audit
        )
        cls.i_band = PhotometricBand.objects.create(
            name="i", instrument=instrument, disp_color="#0000ff", disp_symbol="circle", **audit
        )
        _phot, cls.r_truth = attach_bazin_photometry(
            cls.user, cls.transient, band=cls.r_band, n_points=12, flagged_first=True, with_upper_limit=True
        )
        # g: fitted too, but its last detection is older than r's
        _phot, cls.g_truth = attach_bazin_photometry(
            cls.user, cls.transient, band=cls.g_band, n_points=10,
            mjd_start=bazin.datetime_to_mjd(timezone.now()) - 60.0,
        )
        # i: too few detections for a fit
        attach_bazin_photometry(cls.user, cls.transient, band=cls.i_band, n_points=3)
        cls.empty = create_minimal_transient(cls.user, name="2026bazinempty")

    def setUp(self):
        cache.clear()

    def test_detections_exclude_flags_and_limits_and_take_one_query(self):
        with CaptureQueriesContext(connection) as ctx:
            found = bazin.detections_by_transient([self.transient.id, self.empty.id, None])
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual(set(found), {self.transient.id})
        bands = found[self.transient.id]["bands"]
        self.assertEqual({name for name, _pts in bands.values()}, {"r-bazin-db", "g", "i"})
        self.assertEqual(len(bands[self.r_band.id][1]), 11)  # 12 - flagged point; limit never counted
        self.assertEqual(len(bands[self.g_band.id][1]), 10)
        self.assertEqual(len(bands[self.i_band.id][1]), 3)
        self.assertRegex(found[self.transient.id]["token"], r"^24:\d{4}-")
        self.assertEqual(bazin.detections_by_transient([]), {})

    def test_fits_for_transients_uses_most_recent_band_and_caches(self):
        with mock.patch.object(bazin, "fit_bazin_joint", wraps=bazin.fit_bazin_joint) as fitter:
            fits = bazin.fits_for_transients([self.transient.id, self.empty.id])
            self.assertEqual(fitter.call_count, 1)  # one joint solve for the transient
            # r, g and the 3-point i band (which borrows its shape) all get a fit
            self.assertEqual(set(fits[self.transient.id]), {self.r_band.id, self.g_band.id, self.i_band.id})
            wavelengths = fitter.call_args.args[1]
            self.assertEqual(wavelengths[self.r_band.id], 6230.0)  # 'r-bazin-db' -> r family
            self.assertEqual(wavelengths[self.g_band.id], 4770.0)
            self.assertEqual(fits.get(self.empty.id, {}), {})
            again = bazin.fits_for_transients([self.transient.id])
            self.assertEqual(fitter.call_count, 1)  # served from the cache
        self.assertEqual(again[self.transient.id][self.r_band.id][1].params,
                         fits[self.transient.id][self.r_band.id][1].params)

        now = bazin.datetime_to_mjd(timezone.now())
        # the 3-point i band ends today too and now has a (borrowed-shape) fit, so it is
        # the most recently observed fitted band; r is available by preference
        picked = bazin.extrapolated_mag(self.transient, now + 3.0)
        self.assertEqual(picked[1], "i")
        mag, band_name = bazin.extrapolated_mag(self.transient, now + 3.0, ["r-bazin-db"])
        self.assertEqual(band_name, "r-bazin-db")
        truth = bazin.flux_to_mag(bazin.bazin_flux(now + 3.0, *self.r_truth))
        self.assertAlmostEqual(mag, truth, delta=0.1)
        self.assertEqual(bazin.extrapolated_mag(self.transient, now, ["g"])[1], "g")
        self.assertIsNone(bazin.extrapolated_mag(self.empty, now))
        self.assertEqual(bazin.extrapolated_mags([self.empty.id], now), {})

    def test_new_photometry_invalidates_the_cached_fit(self):
        with mock.patch.object(bazin, "fit_bazin_joint", wraps=bazin.fit_bazin_joint) as fitter:
            bazin.fits_for_transients([self.transient.id])
            before = fitter.call_count
            photometry = TransientPhotometry.objects.filter(transient=self.transient).first()
            TransientPhotData.objects.create(
                photometry=photometry, band=self.i_band, obs_date=timezone.now(),
                mag=19.0, mag_err=0.1, **audit_fields(self.user),
            )
            bazin.fits_for_transients([self.transient.id])
            self.assertGreater(fitter.call_count, before)

    def test_cache_outage_does_not_break_fits(self):
        with mock.patch.object(cache, "get_many", side_effect=RuntimeError("redis down")), \
             mock.patch.object(cache, "set_many", side_effect=RuntimeError("redis down")):
            fits = bazin.fits_for_transients([self.transient.id])
        self.assertIn(self.r_band.id, fits[self.transient.id])


# ------------------------------------------------------------- detail page


class BazinPlotViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("bazin_view_user", is_staff=True)
        cls.transient = create_minimal_transient(cls.user, name="2026bazinplot")
        _group, _instrument, band = create_instrument_stack(cls.user, obs_group_name="bazin-plot")
        attach_bazin_photometry(cls.user, cls.transient, band=band, n_points=12, with_upper_limit=True)
        cls.short = create_minimal_transient(cls.user, name="2026bazinshort")
        attach_synthetic_photometry(cls.user, cls.short, n_points=3)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        cache.clear()

    def _doc(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response, bokeh_doc_from_html(response.content.decode())

    @staticmethod
    def _lines(doc):
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        lines = []
        for ref in refs.values():
            if ref["type"] == "GlyphRenderer":
                glyph = refs[ref["attributes"]["glyph"]["id"]]
                if glyph["type"] == "Line":
                    lines.append(glyph["attributes"])
        return lines

    def test_overlay_adds_dashed_extrapolation_and_one_legend_entry(self):
        with mock.patch.dict("os.environ", {"YSE_PLOT_HTML_CACHE": "0"}):
            _plain, plain_doc = self._doc(reverse("bazinplot", args=[self.transient.id, 0]))
            response, doc = self._doc(reverse("bazinplot", args=[self.transient.id, 1]))
        labels = [label for row in legend_grid(doc) for label in row]
        self.assertIn(view_utils.BAZIN_LEGEND_LABEL, labels)
        self.assertTrue(labels[-1].startswith("today ("))
        plain_labels = [label for row in legend_grid(plain_doc) for label in row]
        self.assertNotIn(view_utils.BAZIN_LEGEND_LABEL, plain_labels)
        dashed = [line for line in self._lines(doc) if line.get("line_dash") == [6]]
        def width(line):
            value = line.get("line_width")
            return value.get("value") if isinstance(value, dict) else value
        solid_fit = [line for line in self._lines(doc) if width(line) == 2 and line.get("line_dash") != [6]]
        self.assertEqual(len(dashed), 1)
        self.assertEqual(len(solid_fit), 1)
        self.assertEqual(len([l for l in self._lines(plain_doc) if l.get("line_dash") == [6]]), 0)
        body = response.content.decode()
        self.assertIn("Bazin fit (dashed = extrapolated)", body)
        self.assertIn("today", body)
        self.assertNotIn(view_utils.BAZIN_FIT_UNAVAILABLE_TEXT, body)
        # x range reaches the end of the extrapolation (today + 15 d)
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        plot = refs[doc["roots"]["root_ids"][0]]["attributes"]
        x_end = refs[plot["x_range"]["id"]]["attributes"]["end"]
        today = bazin.datetime_to_mjd(datetime.datetime.utcnow())
        self.assertGreaterEqual(x_end, today + bazin.EXTRAPOLATION_DAYS - 1)
        refs_plain = {ref["id"]: ref for ref in plain_doc["roots"]["references"]}
        plain_plot = refs_plain[plain_doc["roots"]["root_ids"][0]]["attributes"]
        plain_x_end = refs_plain[plain_plot["x_range"]["id"]]["attributes"]["end"]
        self.assertGreater(x_end, plain_x_end)

    def test_overlay_hides_with_the_band_in_the_legend(self):
        with mock.patch.dict("os.environ", {"YSE_PLOT_HTML_CACHE": "0"}):
            _response, doc = self._doc(reverse("bazinplot", args=[self.transient.id, 1]))
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        callbacks = [ref for ref in refs.values() if ref["type"] == "CustomJS"]
        followers = sum(len(cb["attributes"]["args"]["followers"]) for cb in callbacks)
        # error bars + upper limit from the plot, solid + dashed fit from the overlay
        self.assertEqual(followers, 4)

    def test_too_few_points_notes_unavailable_fit(self):
        with mock.patch.dict("os.environ", {"YSE_PLOT_HTML_CACHE": "0"}):
            response = self.client.get(reverse("bazinplot", args=[self.short.id, 1]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(view_utils.BAZIN_FIT_UNAVAILABLE_TEXT, response.content.decode())

    def test_fitter_failure_does_not_break_the_plot(self):
        with mock.patch.dict("os.environ", {"YSE_PLOT_HTML_CACHE": "0"}), \
             mock.patch.object(bazin, "fit_bazin_joint", return_value={}):
            response = self.client.get(reverse("bazinplot", args=[self.transient.id, 1]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(view_utils.BAZIN_FIT_UNAVAILABLE_TEXT, response.content.decode())

    def test_zero_flag_is_the_cached_plain_plot_and_one_flag_is_cached_separately(self):
        plain_url = reverse("lightcurveplot_detail", args=[self.transient.id]) + "?w=800"
        off_url = reverse("bazinplot", args=[self.transient.id, 0]) + "?w=800"
        on_url = reverse("bazinplot", args=[self.transient.id, 1]) + "?w=800"
        with mock.patch.dict("os.environ", {"YSE_PLOT_HTML_CACHE": "1"}):
            plain = self.client.get(plain_url).content
            with CaptureQueriesContext(connection) as ctx:
                off = self.client.get(off_url).content
            self.assertEqual(off, plain)  # served from the plot cache
            self.assertLessEqual(len(ctx.captured_queries), 4)  # session/user + cache token, no photometry
            with mock.patch.object(bazin, "fit_bazin_joint", wraps=bazin.fit_bazin_joint) as fitter:
                on = self.client.get(on_url).content
                self.assertEqual(fitter.call_count, 1)
                on_again = self.client.get(on_url).content
                self.assertEqual(fitter.call_count, 1)  # cached under its own key
            self.assertEqual(on, on_again)
            self.assertNotEqual(on, plain)
            self.assertIn(view_utils.BAZIN_LEGEND_LABEL, on.decode())

    def test_detail_page_has_the_button_in_both_defer_modes(self):
        for defer in ("1", "0"):
            with self.subTest(defer=defer), mock.patch.dict("os.environ", {"YSE_TRANSIENT_DETAIL_DEFER": defer}):
                response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('id="bazinplot"', html)
                self.assertIn("Show Bazin Fit", html)
                self.assertIn("Show SALT3 Fit", html)
                self.assertLess(html.index('id="salt2plot"'), html.index('id="bazinplot"'))
                self.assertIn(reverse("bazinplot", args=[self.transient.id, 1]), html)
                self.assertIn(reverse("bazinplot", args=[self.transient.id, 0]), html)
                self.assertIn("$('.plotBazin').on('click'", html)


# ------------------------------------------------------------ scheduling table


class BazinTableTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("bazin_table_user")
        audit = audit_fields(cls.user)
        cls.obs_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cls.obs_group, instrument, cls.band = create_instrument_stack(cls.user, obs_group_name="bazin-night")
        observatory = instrument.telescope.observatory
        observatory.utc_offset = -10
        observatory.save()
        night_type, _ = ClassicalNightType.objects.get_or_create(name="Full", defaults=audit)
        cls.resource = ClassicalResource.objects.create(
            telescope=instrument.telescope,
            begin_date_valid=cls.obs_date - datetime.timedelta(days=3),
            end_date_valid=cls.obs_date + datetime.timedelta(days=3),
            **audit,
        )
        cls.night = ClassicalObservingDate.objects.create(
            resource=cls.resource, night_type=night_type, obs_date=cls.obs_date, **audit
        )
        cls.too = ToOResource.objects.create(
            telescope=instrument.telescope,
            begin_date_valid=cls.obs_date - datetime.timedelta(days=3),
            end_date_valid=cls.obs_date + datetime.timedelta(days=3),
            **audit,
        )
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)
        cls.transients, cls.truths = [], {}
        for i in range(6):
            t = create_minimal_transient(cls.user, name=f"bznight{i}", ra=10.0 + i, dec=-5.0 + i)
            if i < 4:
                _phot, truth = attach_bazin_photometry(cls.user, t, band=cls.band, n_points=10 + i)
                cls.truths[t.id] = truth
            elif i == 4:
                attach_synthetic_photometry(cls.user, t, n_points=2)  # recent mag, no fit
            for resource_kw in ({"classical_resource": cls.resource}, {"too_resource": cls.too}):
                create_or_attach_request(
                    cls.user, t, status=status,
                    valid_start=cls.obs_date - datetime.timedelta(days=4),
                    valid_stop=cls.obs_date + datetime.timedelta(days=4),
                    comment="bazin", **resource_kw,
                )
            cls.transients.append(t)

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)

    def _night_qs(self, n=None):
        ids = [t.id for t in self.transients[: n or len(self.transients)]]
        return self.resource.transientfollowup_set.filter(transient_id__in=ids).select_related("transient")

    def test_column_renders_mag_and_band_at_local_midnight(self):
        from YSE_App.table_utils import ObsNightFollowupTable

        table = ObsNightFollowupTable(self._night_qs(), classical_obs_date=self.night)
        cells = {row.record.transient_id: row.get_cell("bazin_mag") for row in table.rows}
        night_mjd = bazin.local_midnight_mjd(self.obs_date, -10)
        self.assertAlmostEqual(table._bazin_mjd, night_mjd)
        for t in self.transients[:4]:
            expected = bazin.flux_to_mag(bazin.bazin_flux(night_mjd, *self.truths[t.id]))
            mag_str, band = cells[t.id].split()
            self.assertEqual(band, "r")  # 'r-bazin-night' shortened like the plot legend
            self.assertAlmostEqual(float(mag_str), expected, delta=0.05)
        self.assertEqual(cells[self.transients[4].id], "")
        self.assertEqual(cells[self.transients[5].id], "")
        header = [str(col.header) for col in table.columns]
        self.assertIn("Bazin Mag @ Night", header)
        self.assertEqual(header.index("Bazin Mag @ Night"), header.index("Recent Mag") + 1)
        self.assertFalse(table.columns["bazin_mag"].orderable)

    def test_too_table_extrapolates_to_now(self):
        from YSE_App.table_utils import ToOFollowupTable

        qs = self.too.transientfollowup_set.select_related("transient")
        table = ToOFollowupTable(qs, too_resource=self.too)
        now = bazin.datetime_to_mjd(timezone.now())
        self.assertAlmostEqual(table._bazin_mjd, now, delta=1 / 24)
        cells = {row.record.transient_id: row.get_cell("bazin_mag") for row in table.rows}
        t = self.transients[0]
        expected = bazin.flux_to_mag(bazin.bazin_flux(now, *self.truths[t.id]))
        self.assertAlmostEqual(float(cells[t.id].split()[0]), expected, delta=0.05)
        self.assertIn("Bazin Mag Now", [str(col.header) for col in table.columns])

    def test_query_count_is_flat_in_rows_and_fits_run_once_per_transient(self):
        from YSE_App.table_utils import ObsNightFollowupTable

        def render(n):
            cache.clear()
            with CaptureQueriesContext(connection) as ctx, \
                 mock.patch.object(bazin, "fit_bazin_joint", wraps=bazin.fit_bazin_joint) as fitter:
                table = ObsNightFollowupTable(self._night_qs(n), classical_obs_date=self.night)
                cells = [row.get_cell("bazin_mag") for row in table.rows]
            self.assertEqual(len(cells), n)
            return len(ctx.captured_queries), fitter.call_count

        q2, fits2 = render(2)
        q6, fits6 = render(6)
        self.assertEqual(q2, q6)
        self.assertEqual(fits2, 2)
        self.assertEqual(fits6, 5)  # one joint solve per transient with detections (the 2-point one yields {})

    def test_observing_night_page_shows_the_column(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        url = reverse(
            "observing_night",
            kwargs={
                "telescope": self.resource.telescope.name.replace(" ", "_"),
                "obs_date": self.obs_date.strftime("%Y-%m-%d"),
                "pi_name": "None",
            },
        )
        with iers_offline():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Bazin Mag @ Night", html)
        night_mjd = bazin.local_midnight_mjd(self.obs_date, -10)
        t = self.transients[0]
        expected = bazin.flux_to_mag(bazin.bazin_flux(night_mjd, *self.truths[t.id]))
        self.assertIn("%.2f r" % expected, html)

    def test_download_target_list_carries_the_bazin_mag(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        url = reverse(
            "download_target_list",
            kwargs={
                "telescope": self.resource.telescope.name.replace(" ", "_"),
                "obs_date": self.obs_date.strftime("%Y-%m-%d"),
            },
        )
        with iers_offline(), mock.patch.object(
                bazin, "detections_by_transient", wraps=bazin.detections_by_transient) as detections:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(detections.call_count, 1)  # one photometry query for the whole list
        body = response.content.decode()
        night_mjd = bazin.local_midnight_mjd(self.obs_date, -10)
        for t in self.transients[:4]:
            expected = bazin.flux_to_mag(bazin.bazin_flux(night_mjd, *self.truths[t.id]))
            line = next(line for line in body.splitlines() if line.startswith(t.name))
            self.assertIn("mag = %.2f bazin_mag = %.2f r comment = bazin" % (float(t.recent_mag()), expected), line)
        no_fit = next(line for line in body.splitlines() if line.startswith(self.transients[4].name))
        self.assertNotIn("bazin_mag", no_fit)
        self.assertIn("mag = %.2f comment = bazin" % float(self.transients[4].recent_mag()), no_fit)
        no_phot = next(line for line in body.splitlines() if line.startswith(self.transients[5].name))
        self.assertIn("2000 comment = bazin", no_phot)
