"""Bazin light-curve fits for extrapolated magnitudes (#225, joint fit #336).

The Bazin function (Bazin et al. 2009)::

    flux(t) = A * exp(-(t - t0) / tau_fall) / (1 + exp(-(t - t0) / tau_rise)) + B

is fitted to all bands of a transient at once, in flux space (zero point
``FLUX_ZERO_POINT``, the convention of ``lightcurveplot_flux`` and the SALT3
block), weighted by the flux error, with ``scipy.optimize.least_squares``.
Each band keeps its own amplitude ``A_b`` and a bounded baseline ``B_b``;
the shape ``(t0_b, log tau_rise_b, log tau_fall_b)`` is per band too, but
tied to the other bands by a wavelength-correlated penalty
``sum_{b<b'} w(b,b') ||theta_b - theta_b'||^2 / sigma^2`` with
``w = exp(-(dlog10 lambda / l)^2)``: neighbouring filters (g-r, r-i) share
their shape almost exactly, distant ones (u-y) may differ, and a band with
only a few detections borrows its shape from its neighbours instead of
being dropped.  The fitted curve is then evaluated at a chosen MJD and
converted back to a magnitude.  Two surfaces use it: the "Show Bazin Fit"
overlay on the transient detail page and the "Bazin Mag @ Night" column of
the classical observing-night table.

Only detections are fitted: rows with a magnitude and a magnitude error,
no upper limits, no rows carrying a ``data_quality`` flag (the same
exclusion as ``_recent_phot_subqueries`` / ``Transient.recent_mag()``), and
the light-curve plot's ``mag_err <= MAX_MAG_ERR`` cut when flux and flux
error are present.  A transient needs ``MIN_DETECTIONS`` such points in
total.

The fit is bounded so a page never hangs on a pathological light curve:
``tau_rise`` in ``TAU_RISE_BOUNDS``, ``tau_fall`` in ``TAU_FALL_BOUNDS``,
``t0`` within the data span padded by ``T0_PAD_DAYS``, ``A > 0``,
``0 <= B_b <= B_MAX_FRACTION * peak_b`` and ``MAX_NFEV`` function
evaluations.  When the joint solve fails a shared-shape fit (one ``t0`` and
``tau`` pair for every band) is tried; when that fails too there is no fit.
Extrapolated fluxes that are not positive have no magnitude and yield
``None``.

Everything above the "database access" marker is pure (numpy + scipy) so
both surfaces and the tests can call it without Bokeh or Django models.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import math
import warnings
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from YSE_App.common.filter_display import band_effective_wavelength

logger = logging.getLogger(__name__)

FLUX_ZERO_POINT = 27.5
MIN_DETECTIONS = 5
# The light-curve plot hides detections with mag_err > 0.36 when flux info
# exists (they are effectively upper limits); the fit ignores them too.
MAX_MAG_ERR = 0.36
MAG_ERR_FLOOR = 0.01
TAU_RISE_BOUNDS = (0.5, 50.0)
TAU_FALL_BOUNDS = (1.0, 300.0)
T0_PAD_DAYS = 50.0
MAX_NFEV = 4000
# Plot grid: this many days before the first detection and after the later
# of today / the last detection.
GRID_LEAD_DAYS = 5.0
EXTRAPOLATION_DAYS = 15.0
GRID_STEP_DAYS = 0.5

# Fit results are cached per (transient, photometry token); the token changes
# whenever a detection is added or edited, so the timeout only bounds growth.
FIT_CACHE_TIMEOUT = 7 * 24 * 3600
FIT_CACHE_VERSION = 'v2'  # v2: joint wavelength-correlated fit (#336)

_MJD_EPOCH = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)
_LN10_OVER_2P5 = 0.4 * math.log(10.0)


@dataclasses.dataclass(frozen=True)
class BazinFit:
    """One band's fitted Bazin curve.

    ``params`` is ``(A, t0, tau_fall, tau_rise, B)`` in flux units of
    ``FLUX_ZERO_POINT``; ``cov`` the 5x5 covariance (nested tuples; entries
    may be ``inf`` when a parameter is unconstrained); ``chi2`` / ``dof`` the
    weighted residual sum and ``n_points - 5``; ``mjd_min`` / ``mjd_max`` the
    span of the fitted detections.
    """

    params: Tuple[float, float, float, float, float]
    cov: Tuple[Tuple[float, ...], ...]
    chi2: float
    dof: int
    n_points: int
    mjd_min: float
    mjd_max: float
    # 'joint' (wavelength-correlated shape), 'shared' (one shape for all bands, the fallback)
    method: str = 'joint'

    @property
    def reduced_chi2(self) -> Optional[float]:
        return self.chi2 / self.dof if self.dof > 0 else None

    def flux_at(self, mjd):
        return bazin_flux(np.asarray(mjd, dtype=float), *self.params)

    def mag_at(self, mjd) -> Optional[float]:
        return bazin_mag_at(self, mjd)


# --------------------------------------------------------------------- model


def bazin_flux(t, A, t0, tau_fall, tau_rise, B):
    """Bazin flux at ``t`` (scalar or array); overflow in the exponentials is masked."""
    t = np.asarray(t, dtype=float)
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        return A * (np.exp(-(t - t0) / tau_fall) / (1.0 + np.exp(-(t - t0) / tau_rise))) + B


def mag_to_flux(mag, zp: float = FLUX_ZERO_POINT):
    return 10.0 ** (-0.4 * (np.asarray(mag, dtype=float) - zp))


def flux_err_from_mag_err(flux, mag_err):
    """Linearised flux error: ``flux * mag_err * 0.4 ln 10`` (0.921 in the reference script)."""
    return np.asarray(flux, dtype=float) * np.asarray(mag_err, dtype=float) * _LN10_OVER_2P5


def flux_to_mag(flux, zp: float = FLUX_ZERO_POINT):
    """Magnitude for ``flux``; non-positive fluxes become ``nan`` (arrays) / ``None`` (scalars)."""
    arr = np.asarray(flux, dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        mags = np.where(arr > 0, -2.5 * np.log10(np.where(arr > 0, arr, 1.0)) + zp, np.nan)
    if np.ndim(flux) == 0:
        value = float(mags)
        return None if not math.isfinite(value) else value
    return mags


def datetime_to_mjd(value) -> float:
    """MJD of a ``datetime`` (naive values are taken as UTC); floats pass through."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return (value - _MJD_EPOCH).total_seconds() / 86400.0


def local_midnight_mjd(obs_date, utc_offset) -> float:
    """MJD of local midnight during the night whose calendar date is ``obs_date``.

    A classical night dated ``D`` is the evening of ``D`` at the observatory
    (see ``services/classical_nights.py``), so its midnight is ``D+1 00:00``
    local time, i.e. ``D + 1 day - utc_offset`` in UT.  Hawaii (``-10``):
    ``D+1 10:00 UT``; Chile (``-4``): ``D+1 04:00 UT``; an eastern site
    (``+8``): ``D 16:00 UT``.
    """
    if isinstance(obs_date, datetime.datetime):
        obs_date = obs_date.date()
    day = datetime_to_mjd(obs_date)
    try:
        offset_hours = float(utc_offset or 0)
    except (TypeError, ValueError):
        offset_hours = 0.0
    return day + 1.0 - offset_hours / 24.0


# ----------------------------------------------------------------- selection


def is_usable_detection(mag, mag_err, flux=None, flux_err=None, flagged: bool = False) -> bool:
    """The light-curve plot's detection rule, minus flagged rows.

    A point counts when it has a finite magnitude and error, is not
    ``data_quality``-flagged, and either has no flux information or a
    magnitude error at or below ``MAX_MAG_ERR``.
    """
    if flagged or mag is None or mag_err is None:
        return False
    try:
        mag, mag_err = float(mag), float(mag_err)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(mag) and math.isfinite(mag_err)):
        return False
    if flux is not None and flux_err is not None and mag_err > MAX_MAG_ERR:
        return False
    return True


# ----------------------------------------------------------------------- fit

# Baseline B_b is bounded to [0, B_MAX_FRACTION * peak flux of the band]; a
# band with fewer than B_FREE_MIN_POINTS detections has it pinned at 0.  A
# free baseline is what let a declining tail settle onto a plateau (#336).
B_MAX_FRACTION = 0.1
B_FREE_MIN_POINTS = 3
# Wavelength prior on the per-band shape (t0, log10 tau_rise, log10 tau_fall):
# pair weight w = max(exp(-(dlog10 lambda / CORRELATION_LENGTH_DEX)^2), CORRELATION_FLOOR),
# penalty residuals sqrt(w) * dtheta / sigma.  g-r (0.11 dex apart) get
# w = 0.58, r-i (0.09 dex) 0.70, u-y (0.43 dex) the floor: neighbours share
# their shape to within ~2.6 d / 0.13 dex, the ends of the optical window
# only to within ~14 d / 0.7 dex.
CORRELATION_LENGTH_DEX = 0.15
CORRELATION_FLOOR = 0.02
SHAPE_PRIOR_SIGMA_T0 = 2.0       # days
SHAPE_PRIOR_SIGMA_LOGTAU = 0.1   # dex
DEFAULT_WAVELENGTH_AA = 6000.0
_LOG10 = math.log(10.0)


def shape_correlation(wavelength_a: float, wavelength_b: float) -> float:
    """Pair weight of the wavelength prior for two effective wavelengths (Angstrom)."""
    try:
        dlog = math.log10(float(wavelength_a)) - math.log10(float(wavelength_b))
    except (TypeError, ValueError):
        return CORRELATION_FLOOR
    return max(math.exp(-(dlog / CORRELATION_LENGTH_DEX) ** 2), CORRELATION_FLOOR)


class _BandData:
    __slots__ = ('key', 'mjd', 'flux', 'flux_err', 'wavelength', 'n')

    def __init__(self, key, mjd, mag, magerr, wavelength):
        order = np.argsort(mjd)
        self.key = key
        self.mjd = mjd[order]
        flux = mag_to_flux(mag[order])
        self.flux = flux
        self.flux_err = flux_err_from_mag_err(flux, np.maximum(magerr[order], MAG_ERR_FLOOR))
        self.wavelength = float(wavelength) if wavelength else DEFAULT_WAVELENGTH_AA
        self.n = int(len(self.mjd))


def _clean_points(points):
    arr = np.asarray(list(points), dtype=float).reshape(-1, 3)
    keep = np.all(np.isfinite(arr), axis=1)
    return arr[keep]


def _solve_joint(bands: List[_BandData], shared: bool):
    """Bounded least squares over all bands; ``shared`` uses one shape for every band.

    Returns ``(x, jac, unpack)`` where ``unpack(x)`` yields per-band
    ``(A, B, t0, log10 tau_rise, log10 tau_fall)``, or ``None`` on failure.
    """
    from scipy.optimize import least_squares

    nb = len(bands)
    all_mjd = np.concatenate([b.mjd for b in bands])
    all_flux = np.concatenate([b.flux for b in bands])
    t0_guess = float(all_mjd[int(np.argmax(all_flux))])
    t_lo, t_hi = float(all_mjd.min()) - T0_PAD_DAYS, float(all_mjd.max()) + T0_PAD_DAYS
    ltr_lo, ltr_hi = math.log10(TAU_RISE_BOUNDS[0]), math.log10(TAU_RISE_BOUNDS[1])
    ltf_lo, ltf_hi = math.log10(TAU_FALL_BOUNDS[0]), math.log10(TAU_FALL_BOUNDS[1])
    ltr0 = min(max(1.0, ltr_lo), ltr_hi)          # tau_rise = 10 d
    ltf0 = min(max(math.log10(30.0), ltf_lo), ltf_hi)  # tau_fall = 30 d
    n_shape = 1 if shared else nb

    x0, lo, hi = [], [], []
    for b in bands:
        peak = float(b.flux.max())
        x0.append(peak); lo.append(0.0); hi.append(np.inf)
    for b in bands:
        peak = float(b.flux.max())
        b_max = B_MAX_FRACTION * peak if b.n >= B_FREE_MIN_POINTS else 1e-6 * peak
        x0.append(0.01 * b_max); lo.append(0.0); hi.append(b_max)
    x0 += [t0_guess] * n_shape; lo += [t_lo] * n_shape; hi += [t_hi] * n_shape
    x0 += [ltr0] * n_shape; lo += [ltr_lo] * n_shape; hi += [ltr_hi] * n_shape
    x0 += [ltf0] * n_shape; lo += [ltf_lo] * n_shape; hi += [ltf_hi] * n_shape
    x0, lo, hi = np.array(x0), np.array(lo), np.array(hi)

    def unpack(x):
        out = []
        for i in range(nb):
            j = 0 if shared else i
            out.append((x[i], x[nb + i], x[2 * nb + j], x[2 * nb + n_shape + j], x[2 * nb + 2 * n_shape + j]))
        return out

    pairs = []
    if not shared:
        for i in range(nb):
            for j in range(i + 1, nb):
                pairs.append((i, j, math.sqrt(shape_correlation(bands[i].wavelength, bands[j].wavelength))))

    def residuals(x):
        per_band = unpack(x)
        parts = []
        for b, (A, B, t0, ltr, ltf) in zip(bands, per_band):
            model = bazin_flux(b.mjd, A, t0, 10.0 ** ltf, 10.0 ** ltr, B)
            parts.append((b.flux - model) / b.flux_err)
        for i, j, sw in pairs:
            _Ai, _Bi, t0i, ltri, ltfi = per_band[i]
            _Aj, _Bj, t0j, ltrj, ltfj = per_band[j]
            parts.append(np.array([
                sw * (t0i - t0j) / SHAPE_PRIOR_SIGMA_T0,
                sw * (ltri - ltrj) / SHAPE_PRIOR_SIGMA_LOGTAU,
                sw * (ltfi - ltfj) / SHAPE_PRIOR_SIGMA_LOGTAU,
            ]))
        res = np.concatenate(parts)
        return np.where(np.isfinite(res), res, 1e6)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        result = least_squares(
            residuals, x0, bounds=(lo, hi), method='trf', x_scale='jac', max_nfev=MAX_NFEV,
        )
    if result.status < 0 or not np.all(np.isfinite(result.x)):
        return None
    return result.x, result.jac, unpack


def _covariance(jac):
    jtj = jac.T @ jac
    try:
        return np.linalg.pinv(jtj)
    except Exception:
        return np.full(jtj.shape, np.inf)


def fit_bazin_joint(
    points_by_band: Dict[object, Iterable[Tuple[float, float, float]]],
    wavelengths: Optional[Dict[object, float]] = None,
) -> Dict[object, BazinFit]:
    """Joint Bazin fit of every band in ``{key: [(mjd, mag, magerr), ...]}`` (#336).

    Per-band amplitude and (bounded) baseline are free; the shape parameters
    ``(t0, log tau_rise, log tau_fall)`` are per band but tied by the
    wavelength prior (``wavelengths`` maps key -> effective wavelength in
    Angstrom; unknown bands use ``DEFAULT_WAVELENGTH_AA``), so a band with
    two or three detections borrows its shape from its neighbours.  Needs
    ``MIN_DETECTIONS`` detections in total and a non-zero time span.  Falls
    back to a shared-shape fit (one ``t0``/``tau`` for all bands) when the
    joint solve fails; returns ``{}`` when both fail.  Bands whose amplitude
    collapses to zero are left out of the result.
    """
    wavelengths = wavelengths or {}
    bands: List[_BandData] = []
    for key, points in points_by_band.items():
        arr = _clean_points(points)
        if len(arr) == 0:
            continue
        bands.append(_BandData(key, arr[:, 0], arr[:, 1], arr[:, 2], wavelengths.get(key)))
    total = sum(b.n for b in bands)
    if not bands or total < MIN_DETECTIONS:
        return {}
    all_mjd = np.concatenate([b.mjd for b in bands])
    if all_mjd.max() - all_mjd.min() <= 0:
        return {}

    solved, method = None, 'joint'
    for shared in (False, True):
        try:
            solved = _solve_joint(bands, shared=shared)
        except Exception as exc:  # ValueError, LinAlgError, ...
            logger.debug("Bazin %s fit failed: %s: %s", 'shared' if shared else 'joint', type(exc).__name__, exc)
            solved = None
        if solved is not None:
            method = 'shared' if shared else 'joint'
            break
    if solved is None:
        return {}
    x, jac, unpack = solved
    cov = _covariance(jac)
    nb = len(bands)
    n_shape = 1 if method == 'shared' else nb

    fits: Dict[object, BazinFit] = {}
    for i, (b, (A, B, t0, ltr, ltf)) in enumerate(zip(bands, unpack(x))):
        tau_rise, tau_fall = 10.0 ** ltr, 10.0 ** ltf
        A, B, t0 = float(A), float(B), float(t0)
        if not all(math.isfinite(v) for v in (A, B, t0, tau_rise, tau_fall)) or A <= 0:
            continue
        model = bazin_flux(b.mjd, A, t0, tau_fall, tau_rise, B)
        if not np.all(np.isfinite(model)):
            continue
        j = 0 if method == 'shared' else i
        idx = [i, 2 * nb + j, 2 * nb + 2 * n_shape + j, 2 * nb + n_shape + j, nb + i]  # A, t0, ltf, ltr, B
        scale = np.array([1.0, 1.0, tau_fall * _LOG10, tau_rise * _LOG10, 1.0])
        block = cov[np.ix_(idx, idx)] * np.outer(scale, scale)
        fits[b.key] = BazinFit(
            params=(A, t0, float(tau_fall), float(tau_rise), B),
            cov=tuple(tuple(float(v) for v in row) for row in block),
            chi2=float(np.sum(((b.flux - model) / b.flux_err) ** 2)),
            dof=int(b.n - 5),
            n_points=b.n,
            mjd_min=float(b.mjd.min()),
            mjd_max=float(b.mjd.max()),
            method=method,
        )
    return fits


def fit_bazin(mjd: Sequence[float], mag: Sequence[float], magerr: Sequence[float]) -> Optional[BazinFit]:
    """Fit a single band on its own (the joint fit with one band); ``None`` when unusable."""
    mjd = np.asarray(mjd, dtype=float).ravel()
    mag = np.asarray(mag, dtype=float).ravel()
    magerr = np.asarray(magerr, dtype=float).ravel()
    if not (len(mjd) == len(mag) == len(magerr)) or len(mjd) == 0:
        return None
    return fit_bazin_joint({'band': list(zip(mjd, mag, magerr))}).get('band')


def bazin_mag_at(fit: Optional[BazinFit], mjd) -> Optional[float]:
    """Magnitude of the fitted curve at ``mjd``; ``None`` when there is no fit or the flux is not positive."""
    if fit is None:
        return None
    flux = float(bazin_flux(float(mjd), *fit.params))
    if not math.isfinite(flux) or flux <= 0:
        return None
    return flux_to_mag(flux)


def bazin_plot_grid(fit: BazinFit, today_mjd: float):
    """``(fitted_mjd, extrapolated_mjd)`` grids for the overlay.

    The fitted part runs from ``GRID_LEAD_DAYS`` before the first detection
    to the last detection; the extrapolated part from the last detection to
    ``EXTRAPOLATION_DAYS`` after the later of today and the last detection.
    """
    start = fit.mjd_min - GRID_LEAD_DAYS
    end = max(float(today_mjd), fit.mjd_max) + EXTRAPOLATION_DAYS
    fitted = np.arange(start, fit.mjd_max + GRID_STEP_DAYS / 2, GRID_STEP_DAYS)
    extrapolated = np.arange(fit.mjd_max, end + GRID_STEP_DAYS / 2, GRID_STEP_DAYS)
    return fitted, extrapolated


def fit_bands(
    points_by_band: Dict[object, Iterable[Tuple[float, float, float]]],
    wavelengths: Optional[Dict[object, float]] = None,
) -> Dict[object, BazinFit]:
    """Per-band fits from one joint solve (see :func:`fit_bazin_joint`); bands without a usable fit are left out."""
    return fit_bazin_joint(points_by_band, wavelengths)


def pick_extrapolated_mag(
    fits: Dict[object, BazinFit], at_mjd: float, band_preference: Optional[Sequence[object]] = None,
) -> Optional[Tuple[float, object]]:
    """``(mag, band_key)`` at ``at_mjd`` from the preferred band, else the most recently observed one.

    Bands named in ``band_preference`` are tried first, in order; the rest
    follow by latest detection (newest first).  A band whose extrapolated
    flux is not positive is skipped.
    """
    if not fits:
        return None
    ordered: List[object] = []
    for key in band_preference or ():
        if key in fits and key not in ordered:
            ordered.append(key)
    for key in sorted(fits, key=lambda k: (-fits[k].mjd_max, str(k))):
        if key not in ordered:
            ordered.append(key)
    for key in ordered:
        mag = bazin_mag_at(fits[key], at_mjd)
        if mag is not None:
            return mag, key
    return None


# ------------------------------------------------------------ database access


def detections_by_transient(transient_ids: Iterable[int]):
    """One query: usable detections for ``transient_ids`` grouped per transient and band.

    Returns ``{transient_id: {'token': str, 'bands': {band_id: (band_name, [(mjd, mag, magerr), ...])}}}``.
    ``token`` (``"<n usable rows>:<latest modified_date>"``) identifies the
    photometry state the way ``_transient_phot_cache_token`` does, so cached
    fits invalidate as soon as a detection is added or edited.
    """
    from YSE_App.models.phot_models import TransientPhotData

    ids = list({int(t) for t in transient_ids if t is not None})
    result: Dict[int, dict] = {}
    if not ids:
        return result
    rows = (
        TransientPhotData.objects.filter(
            photometry__transient_id__in=ids, mag__isnull=False, mag_err__isnull=False,
        )
        .exclude(data_quality__isnull=False)
        .values_list(
            'photometry__transient_id', 'band_id', 'band__name', 'obs_date',
            'mag', 'mag_err', 'flux', 'flux_err', 'modified_date',
        )
    )
    latest: Dict[int, Optional[datetime.datetime]] = {}
    counts: Dict[int, int] = {}
    for tid, band_id, band_name, obs_date, mag, mag_err, flux, flux_err, modified in rows:
        if not is_usable_detection(mag, mag_err, flux, flux_err):
            continue
        entry = result.setdefault(tid, {'token': '', 'bands': {}})
        band = entry['bands'].setdefault(band_id, (band_name, []))
        band[1].append((datetime_to_mjd(obs_date), float(mag), float(mag_err)))
        counts[tid] = counts.get(tid, 0) + 1
        if modified is not None and (latest.get(tid) is None or modified > latest[tid]):
            latest[tid] = modified
    for tid, entry in result.items():
        stamp = latest.get(tid)
        entry['token'] = '%d:%s' % (counts.get(tid, 0), stamp.isoformat() if stamp else 'none')
    return result


def _fit_cache_key(transient_id: int, token: str) -> str:
    return 'bazin_fits_%s_%s_%s' % (FIT_CACHE_VERSION, transient_id, token.replace(' ', '_'))


def fits_for_transients(transient_ids: Iterable[int]) -> Dict[int, Dict[int, Tuple[str, BazinFit]]]:
    """Per-band Bazin fits for many transients: ``{transient_id: {band_id: (band_name, fit)}}``.

    One photometry query for the whole batch (see :func:`detections_by_transient`);
    fits come from the Django cache when the transient's photometry token is
    unchanged and are computed (and cached) otherwise.  Transients without a
    usable fit map to an empty dict.
    """
    from django.core.cache import cache

    detections = detections_by_transient(transient_ids)
    keys = {tid: _fit_cache_key(tid, entry['token']) for tid, entry in detections.items()}
    try:
        cached = cache.get_many(list(keys.values())) if keys else {}
    except Exception:  # a cache outage must not take the observing page down
        cached = {}
    result: Dict[int, Dict[int, Tuple[str, BazinFit]]] = {}
    to_store = {}
    for tid, entry in detections.items():
        hit = cached.get(keys[tid])
        if isinstance(hit, dict):
            result[tid] = hit
            continue
        fits = fit_bands(
            {band_id: pts for band_id, (_name, pts) in entry['bands'].items()},
            {band_id: band_effective_wavelength(name) for band_id, (name, _pts) in entry['bands'].items()},
        )
        result[tid] = {
            band_id: (entry['bands'][band_id][0], fit) for band_id, fit in fits.items()
        }
        to_store[keys[tid]] = result[tid]
    if to_store:
        try:
            cache.set_many(to_store, timeout=FIT_CACHE_TIMEOUT)
        except Exception:
            pass
    return result


def extrapolated_mags(
    transient_ids: Iterable[int], at_mjd: float, band_preference: Optional[Sequence[str]] = None,
) -> Dict[int, Tuple[float, str]]:
    """``{transient_id: (mag, band_name)}`` at ``at_mjd`` for every transient with a usable fit.

    ``band_preference`` lists band names to try first; otherwise the band
    with the most recent detection wins (see :func:`pick_extrapolated_mag`).
    """
    out: Dict[int, Tuple[float, str]] = {}
    for tid, bands in fits_for_transients(transient_ids).items():
        if not bands:
            continue
        fits = {band_id: fit for band_id, (_name, fit) in bands.items()}
        preferred = []
        for name in band_preference or ():
            preferred.extend(bid for bid, (bname, _fit) in bands.items() if bname == name)
        picked = pick_extrapolated_mag(fits, at_mjd, preferred)
        if picked is not None:
            mag, band_id = picked
            out[tid] = (mag, bands[band_id][0])
    return out


def extrapolated_mag(transient, at_mjd, band_preference: Optional[Sequence[str]] = None) -> Optional[Tuple[float, str]]:
    """``(mag, band_name)`` for one transient (a ``Transient`` or its id) at ``at_mjd``, or ``None``."""
    transient_id = getattr(transient, 'pk', transient)
    return extrapolated_mags([transient_id], datetime_to_mjd(at_mjd), band_preference).get(transient_id)
