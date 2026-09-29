"""Bazin light-curve fits for extrapolated magnitudes (#225).

The Bazin function (Bazin et al. 2009)::

    flux(t) = A * exp(-(t - t0) / tau_fall) / (1 + exp(-(t - t0) / tau_rise)) + B

is fitted per band, in flux space (zero point ``FLUX_ZERO_POINT``, the
convention of ``lightcurveplot_flux`` and the SALT3 block), weighted by the
flux error, with ``scipy.optimize.curve_fit``.  The fitted curve is then
evaluated at a chosen MJD and converted back to a magnitude.  Two surfaces
use it: the "Show Bazin Fit" overlay on the transient detail page and the
"Bazin Mag @ Night" column of the classical observing-night table.

Only detections are fitted: rows with a magnitude and a magnitude error,
no upper limits, no rows carrying a ``data_quality`` flag (the same
exclusion as ``_recent_phot_subqueries`` / ``Transient.recent_mag()``), and
the light-curve plot's ``mag_err <= MAX_MAG_ERR`` cut when flux and flux
error are present.  A band needs ``MIN_DETECTIONS`` such points.

The fit is bounded so a page never hangs on a pathological light curve:
``tau_rise`` in ``TAU_RISE_BOUNDS``, ``tau_fall`` in ``TAU_FALL_BOUNDS``,
``t0`` within the data span padded by ``T0_PAD_DAYS``, ``A > 0``, and
``MAX_NFEV`` function evaluations.  Anything the fitter raises, a
non-finite parameter, or a non-positive amplitude yields ``None`` instead
of a fit.  Extrapolated fluxes that are not positive have no magnitude and
also yield ``None``.

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
MAX_NFEV = 2000
# Plot grid: this many days before the first detection and after the later
# of today / the last detection.
GRID_LEAD_DAYS = 5.0
EXTRAPOLATION_DAYS = 15.0
GRID_STEP_DAYS = 0.5

# Fit results are cached per (transient, photometry token); the token changes
# whenever a detection is added or edited, so the timeout only bounds growth.
FIT_CACHE_TIMEOUT = 7 * 24 * 3600
FIT_CACHE_VERSION = 'v1'

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


def _fit_bounds(mjd: np.ndarray, flux: np.ndarray):
    lower = [0.0, float(mjd.min()) - T0_PAD_DAYS, TAU_FALL_BOUNDS[0], TAU_RISE_BOUNDS[0], -np.inf]
    upper = [np.inf, float(mjd.max()) + T0_PAD_DAYS, TAU_FALL_BOUNDS[1], TAU_RISE_BOUNDS[1], np.inf]
    return lower, upper


def fit_bazin(mjd: Sequence[float], mag: Sequence[float], magerr: Sequence[float]) -> Optional[BazinFit]:
    """Fit one band's detections; ``None`` when there are too few points or the fit is unusable.

    ``p0 = [max flux, MJD at max flux, tau_fall=30, tau_rise=10, B=0]`` as in
    the reference script.  Magnitude errors are floored at ``MAG_ERR_FLOOR``.
    """
    mjd = np.asarray(mjd, dtype=float)
    mag = np.asarray(mag, dtype=float)
    magerr = np.asarray(magerr, dtype=float)
    keep = np.isfinite(mjd) & np.isfinite(mag) & np.isfinite(magerr)
    mjd, mag, magerr = mjd[keep], mag[keep], magerr[keep]
    if len(mjd) < MIN_DETECTIONS:
        return None
    order = np.argsort(mjd)
    mjd, mag, magerr = mjd[order], mag[order], magerr[order]
    if mjd.max() - mjd.min() <= 0:
        return None

    flux = mag_to_flux(mag)
    flux_err = flux_err_from_mag_err(flux, np.maximum(magerr, MAG_ERR_FLOOR))
    p0 = [float(flux.max()), float(mjd[int(np.argmax(flux))]), 30.0, 10.0, 0.0]
    lower, upper = _fit_bounds(mjd, flux)
    # Bounded taus keep p0 legal; p0's t0 sits inside the padded span by construction.
    p0[2] = min(max(p0[2], lower[2]), upper[2])
    p0[3] = min(max(p0[3], lower[3]), upper[3])

    try:
        from scipy.optimize import curve_fit

        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            popt, pcov = curve_fit(
                bazin_flux, mjd, flux, p0=p0, sigma=flux_err, absolute_sigma=True,
                bounds=(lower, upper), method='trf', max_nfev=MAX_NFEV,
            )
    except Exception as exc:  # RuntimeError (no convergence), ValueError, LinAlgError, ...
        logger.debug("Bazin fit failed: %s: %s", type(exc).__name__, exc)
        return None

    popt = np.asarray(popt, dtype=float)
    if not np.all(np.isfinite(popt)):
        return None
    A, t0, tau_fall, tau_rise, B = (float(v) for v in popt)
    if A <= 0 or tau_fall <= 0 or tau_rise <= 0:
        return None
    model = bazin_flux(mjd, *popt)
    if not np.all(np.isfinite(model)):
        return None
    chi2 = float(np.sum(((flux - model) / flux_err) ** 2))
    pcov = np.asarray(pcov, dtype=float)
    return BazinFit(
        params=(A, t0, tau_fall, tau_rise, B),
        cov=tuple(tuple(float(v) for v in row) for row in pcov),
        chi2=chi2,
        dof=int(len(mjd) - 5),
        n_points=int(len(mjd)),
        mjd_min=float(mjd.min()),
        mjd_max=float(mjd.max()),
    )


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


def fit_bands(points_by_band: Dict[object, Iterable[Tuple[float, float, float]]]) -> Dict[object, BazinFit]:
    """Fit every band in ``{key: [(mjd, mag, magerr), ...]}``; bands without a usable fit are left out."""
    fits = {}
    for key, points in points_by_band.items():
        points = list(points)
        if len(points) < MIN_DETECTIONS:
            continue
        arr = np.asarray(points, dtype=float).reshape(-1, 3)
        fit = fit_bazin(arr[:, 0], arr[:, 1], arr[:, 2])
        if fit is not None:
            fits[key] = fit
    return fits


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
        fits = fit_bands({band_id: pts for band_id, (_name, pts) in entry['bands'].items()})
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
