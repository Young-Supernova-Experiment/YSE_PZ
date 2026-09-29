"""Compute and store ``TransientPhotStat`` rows (#268).

The statistics follow SkyPortal's ``PhotStat`` (skyportal/models/phot_stat.py,
BSD-3-Clause; the logic is re-derived here for YSE-PZ's magnitude-based
rows, no code is copied).  Which points count is decided the way the
light-curve plot on the detail page draws them (``services.phot_points``,
the one classification both call, #368):

* **detection**: unflagged point with ``mag`` and ``mag_err`` and either no
  flux information or ``mag_err <= 0.36`` (S/N 3), the marker the plot
  draws; a noisier forced-photometry magnitude is not one;
* **upper limit**: unflagged point with ``flux`` and ``flux_err``,
  ``flux / flux_err < 3``, whose limit is ``-2.5 log10(flux + 3 flux_err) +
  zp`` (zero point 27.5 when the row has none), the plot's inverted
  triangle; a point that is also a detection counts as the detection;
* flagged points are ignored (as ``Transient.recent_mag()`` and the Bazin
  fit ignore them); points that are neither count toward
  ``num_obs_global`` only.

Rates are positive numbers in mag/day.  ``rise_rate`` uses the band of the
first detection: ``(first_mag - peak_mag) / (peak_mjd - first_mjd)`` with the
peak in that band, ``None`` when the first detection is that band's peak.
``decay_rate`` uses the band of the last detection: ``(last_mag - peak_mag)
/ (last_mjd - peak_mjd)``, ``None`` when the last detection is the peak.
Upper-limit statistics (``num_limits_global``, ``deepest_limit``,
``last_non_detection``) use only the limits taken **before the first
detection** (every limit when there is none); a non-detection during the
decline is not a constraint on the explosion.  ``time_to_non_detection`` is
``first_detected_mjd`` minus the latest such limit.  Each stored limit
carries its band.

``SCHEMA_VERSION`` is written into every row; bump it when the rules change
so existing rows are recomputed (the fingerprint includes it, so the next
signal or ``rebuild_photstats`` rewrites them, ``--stale-only`` visits only
them, and the detail page refreshes a stale row when it is opened).

Three ways to keep rows current:

* :func:`recompute` for one transient (what the signals call);
* :func:`stat_for_transient` for the detail page and the API: returns the row
  and computes it first when the backfill has not reached that transient;
* :func:`recompute_many` for a batch with one photometry query per batch
  (the ``rebuild_photstats`` command);
* :func:`deferred_updates`, a context manager the ingest paths wrap their
  work in, so N saved points cost one recompute per transient at the end.

Deletions pass ``create=False``: while a ``Transient`` cascade is running
the photometry rows disappear before the transient does, and inserting a new
stat row at that moment would violate the foreign key when the transient
row goes; updating an existing row is safe (the cascade removes it).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from django.db import transaction
from django.db.models import Count

from YSE_App.models.phot_stat_models import (
    TransientPhotStat,
    datetime_to_mjd,
    mjd_to_datetime,
)
from YSE_App.services import phot_points

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500

# 1: #341 (all limits counted, no limit bands).  2: #349 (pre-detection
# limits only, deepest_limit_band / last_non_detection_band, per-band limits).
# 3: #368 (the light-curve plot's detection / upper-limit classification).
SCHEMA_VERSION = 3

_local = threading.local()


# ------------------------------------------------------------------- pure part


@dataclass
class PhotPoint:
    """One photometry row as the statistics see it."""

    mjd: float
    band_id: Optional[int]
    band_name: str = ''
    mag: Optional[float] = None
    mag_err: Optional[float] = None
    flux: Optional[float] = None
    flux_err: Optional[float] = None
    flux_zero_point: Optional[float] = None
    flagged: bool = False


_finite = phot_points.finite


def is_detection(point: PhotPoint) -> bool:
    """The light-curve plot's detection rule, minus flagged points."""
    return (not point.flagged) and phot_points.is_detection(
        point.mag, point.mag_err, point.flux, point.flux_err
    )


def limiting_mag(point: PhotPoint) -> Optional[float]:
    """The light-curve plot's upper limit for an unflagged point, or ``None``.

    Independent of :func:`is_detection`, as on the plot; ``compute_stats``
    asks for the detection first.
    """
    if point.flagged:
        return None
    return phot_points.limiting_mag(point.flux, point.flux_err, point.flux_zero_point)


@dataclass
class PhotStatValues:
    """The computed statistics; ``None`` fields mean "not defined"."""

    num_obs_global: int = 0
    num_det_global: int = 0
    num_limits_global: int = 0
    last_obs_mjd: Optional[float] = None
    first_detected_mjd: Optional[float] = None
    first_detected_mag: Optional[float] = None
    first_detected_band_id: Optional[int] = None
    last_detected_mjd: Optional[float] = None
    last_detected_mag: Optional[float] = None
    last_detected_band_id: Optional[int] = None
    peak_mjd: Optional[float] = None
    peak_mag: Optional[float] = None
    peak_band_id: Optional[int] = None
    mean_mag: Optional[float] = None
    faintest_mag: Optional[float] = None
    deepest_limit: Optional[float] = None
    deepest_limit_mjd: Optional[float] = None
    deepest_limit_band_id: Optional[int] = None
    last_non_detection_mjd: Optional[float] = None
    last_non_detection_band_id: Optional[int] = None
    time_to_non_detection: Optional[float] = None
    rise_rate: Optional[float] = None
    decay_rate: Optional[float] = None
    per_band: Dict[str, dict] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def fingerprint(self) -> str:
        payload = {k: v for k, v in self.__dict__.items()}
        text = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha1(text.encode('utf-8')).hexdigest()


def _band_entry(per_band: Dict[str, dict], point: PhotPoint) -> dict:
    key = str(point.band_id)
    entry = per_band.get(key)
    if entry is None:
        entry = per_band[key] = {
            'name': point.band_name or '',
            'n_det': 0,
            'peak_mag': None, 'peak_mjd': None,
            'first_mag': None, 'first_mjd': None,
            'last_mag': None, 'last_mjd': None,
            'n_limits': 0,
            'deepest_limit': None, 'deepest_limit_mjd': None,
            'last_limit_mjd': None,
        }
    return entry


def compute_stats(points: Iterable[PhotPoint]) -> PhotStatValues:
    """Statistics for ``points``; pure, no database access."""
    out = PhotStatValues()
    per_band: Dict[str, dict] = {}
    detections: List[PhotPoint] = []
    limits: List[tuple] = []  # (mjd, limiting mag, point)
    for p in points:
        if p.flagged:
            continue
        mjd = _finite(p.mjd)
        if mjd is None:
            continue
        out.num_obs_global += 1
        out.last_obs_mjd = mjd if out.last_obs_mjd is None else max(out.last_obs_mjd, mjd)
        if is_detection(p):
            detections.append(p)
            continue
        lim = limiting_mag(p)
        if lim is not None:
            limits.append((mjd, lim, p))

    if detections:
        out.num_det_global = len(detections)
        first = min(detections, key=lambda d: d.mjd)
        last = max(detections, key=lambda d: d.mjd)
        peak = min(detections, key=lambda d: (float(d.mag), d.mjd))
        out.first_detected_mjd, out.first_detected_mag = float(first.mjd), float(first.mag)
        out.first_detected_band_id = first.band_id
        out.last_detected_mjd, out.last_detected_mag = float(last.mjd), float(last.mag)
        out.last_detected_band_id = last.band_id
        out.peak_mjd, out.peak_mag, out.peak_band_id = float(peak.mjd), float(peak.mag), peak.band_id
        mags = [float(d.mag) for d in detections]
        out.mean_mag = sum(mags) / len(mags)
        out.faintest_mag = max(mags)

        for d in detections:
            entry = _band_entry(per_band, d)
            entry['n_det'] += 1
            mag, mjd = float(d.mag), float(d.mjd)
            if entry['peak_mag'] is None or mag < entry['peak_mag']:
                entry['peak_mag'], entry['peak_mjd'] = mag, mjd
            if entry['first_mjd'] is None or mjd < entry['first_mjd']:
                entry['first_mag'], entry['first_mjd'] = mag, mjd
            if entry['last_mjd'] is None or mjd > entry['last_mjd']:
                entry['last_mag'], entry['last_mjd'] = mag, mjd

        first_band = per_band[str(first.band_id)]
        if first_band['peak_mjd'] > out.first_detected_mjd:
            out.rise_rate = (out.first_detected_mag - first_band['peak_mag']) / (
                first_band['peak_mjd'] - out.first_detected_mjd
            )
        last_band = per_band[str(last.band_id)]
        if last_band['peak_mjd'] < out.last_detected_mjd:
            out.decay_rate = (out.last_detected_mag - last_band['peak_mag']) / (
                out.last_detected_mjd - last_band['peak_mjd']
            )

    # Only limits before the first detection constrain the explosion; a
    # non-detection during the decline is not "the deepest limit".
    if out.first_detected_mjd is not None:
        limits = [t for t in limits if t[0] < out.first_detected_mjd]
    if limits:
        out.num_limits_global = len(limits)
        deepest = max(limits, key=lambda t: (t[1], t[0]))
        out.deepest_limit, out.deepest_limit_mjd = deepest[1], deepest[0]
        out.deepest_limit_band_id = deepest[2].band_id
        last = max(limits, key=lambda t: t[0])
        out.last_non_detection_mjd = last[0]
        out.last_non_detection_band_id = last[2].band_id
        if out.first_detected_mjd is not None:
            out.time_to_non_detection = out.first_detected_mjd - out.last_non_detection_mjd
        for mjd, lim, p in limits:
            entry = _band_entry(per_band, p)
            entry['n_limits'] += 1
            if entry['deepest_limit'] is None or lim > entry['deepest_limit']:
                entry['deepest_limit'], entry['deepest_limit_mjd'] = lim, mjd
            if entry['last_limit_mjd'] is None or mjd > entry['last_limit_mjd']:
                entry['last_limit_mjd'] = mjd

    out.per_band = per_band
    return out


# --------------------------------------------------------------- database part


def is_stale(stat: TransientPhotStat) -> bool:
    """Whether ``stat`` was written under older rules (see ``SCHEMA_VERSION``)."""
    return (stat.schema_version or 0) < SCHEMA_VERSION


def _phot_rows(transient_ids: Sequence[int]):
    """Unflagged-or-flagged photometry rows for ``transient_ids`` in one query."""
    from YSE_App.models.phot_models import TransientPhotData

    return (
        TransientPhotData.objects.filter(photometry__transient_id__in=list(transient_ids))
        .annotate(n_dq=Count('data_quality'))
        .values_list(
            'photometry__transient_id', 'band_id', 'band__name', 'obs_date',
            'mag', 'mag_err', 'flux', 'flux_err', 'flux_zero_point', 'n_dq',
        )
    )


def points_by_transient(transient_ids: Sequence[int]) -> Dict[int, List[PhotPoint]]:
    """``{transient_id: [PhotPoint, ...]}`` for ``transient_ids`` (one query)."""
    ids = list({int(t) for t in transient_ids if t is not None})
    grouped: Dict[int, List[PhotPoint]] = {tid: [] for tid in ids}
    if not ids:
        return grouped
    for tid, band_id, band_name, obs_date, mag, mag_err, flux, flux_err, zp, n_dq in _phot_rows(ids):
        grouped[tid].append(PhotPoint(
            mjd=datetime_to_mjd(obs_date), band_id=band_id, band_name=band_name or '',
            mag=mag, mag_err=mag_err, flux=flux, flux_err=flux_err, flux_zero_point=zp,
            flagged=bool(n_dq),
        ))
    return grouped


def apply_values(stat: TransientPhotStat, values: PhotStatValues) -> bool:
    """Copy ``values`` onto ``stat``; returns whether anything changed."""
    new_hash = values.fingerprint()
    if stat.pk is not None and stat.phot_hash == new_hash and not is_stale(stat):
        return False
    for name in (
        'num_obs_global', 'num_det_global', 'num_limits_global', 'last_obs_mjd',
        'first_detected_mjd', 'first_detected_mag', 'first_detected_band_id',
        'last_detected_mjd', 'last_detected_mag', 'last_detected_band_id',
        'peak_mjd', 'peak_mag', 'peak_band_id', 'mean_mag', 'faintest_mag',
        'deepest_limit', 'deepest_limit_mjd', 'deepest_limit_band_id',
        'last_non_detection_mjd', 'last_non_detection_band_id',
        'time_to_non_detection', 'rise_rate', 'decay_rate', 'schema_version',
    ):
        setattr(stat, name, getattr(values, name))
    stat.last_obs_date = mjd_to_datetime(values.last_obs_mjd)
    stat.first_detected_date = mjd_to_datetime(values.first_detected_mjd)
    stat.last_detected_date = mjd_to_datetime(values.last_detected_mjd)
    stat.peak_date = mjd_to_datetime(values.peak_mjd)
    stat.per_band = values.per_band
    stat.phot_hash = new_hash
    return True


def recompute(transient_id: int, *, create: bool = True) -> Optional[TransientPhotStat]:
    """Recompute and store the stat row for ``transient_id``.

    Returns the row (``None`` when the transient does not exist, or when
    ``create`` is false and there is no row yet).  Writes only when a value
    changed.
    """
    from YSE_App.models.transient_models import Transient

    transient_id = int(transient_id)
    stat = TransientPhotStat.objects.filter(transient_id=transient_id).first()
    if stat is None:
        if not create or not Transient.objects.filter(pk=transient_id).exists():
            return None
        stat = TransientPhotStat(transient_id=transient_id)
    values = compute_stats(points_by_transient([transient_id]).get(transient_id, []))
    had_row = stat.pk is not None
    old_num_det = stat.num_det_global or 0
    if apply_values(stat, values):
        stat.save()
        if had_row and (stat.num_det_global or 0) > old_num_det:
            _announce_new_photometry(stat, (stat.num_det_global or 0) - old_num_det)
    return stat


def _announce_new_photometry(stat, new_points: int) -> None:
    """Favorite-activity hook (#323): detections that arrived since the last recompute."""
    from YSE_App.services import favorites

    band = getattr(getattr(stat, 'last_detected_band', None), 'name', '') or ''
    favorites._safely(favorites.on_photometry, stat.transient_id, new_points,
                      latest_mag=stat.last_detected_mag, latest_band=band)


DISPLAY_RELATED = (
    'peak_band', 'first_detected_band', 'last_detected_band',
    'deepest_limit_band', 'last_non_detection_band',
)




def stat_for_transient(transient_id: int, *, compute_missing: bool = True) -> Optional[TransientPhotStat]:
    """The stat row for ``transient_id`` with its bands loaded, for a page or API view.

    A transient that has had no photometry upload since the table was added
    has no row until ``rebuild_photstats`` runs (#345).  With
    ``compute_missing`` the row is then computed and stored on the spot, one
    pass over that transient's photometry, so the detail page never waits on
    the backfill; a row written under an older ``SCHEMA_VERSION`` is
    recomputed the same way.  A failure is logged and gives ``None`` (or the
    old row): the caller renders what it has instead of failing the page.
    """
    transient_id = int(transient_id)
    queryset = TransientPhotStat.objects.filter(transient_id=transient_id).select_related(*DISPLAY_RELATED)
    stat = queryset.first()
    if not compute_missing:
        return stat
    if stat is not None:
        if not is_stale(stat):
            return stat
        # Written under older rules (an integer compare, no photometry read
        # for a current row): refresh it now, keep the old row on failure.
        try:
            recompute(transient_id)
        except Exception:
            logger.exception('photstat refresh failed for transient %s', transient_id)
            return stat
        return queryset.first()
    try:
        if compute_missing_stat(transient_id) is None:
            return None
    except Exception:
        # Also the race with a concurrent upload whose signal inserted the row
        # first: the reload below then finds it.
        logger.exception('photstat on-demand compute failed for transient %s', transient_id)
    return queryset.first()


def compute_missing_stat(transient_id: int) -> Optional[TransientPhotStat]:
    """Insert the stat row for ``transient_id``, which the caller found missing.

    Three queries: the photometry pass, the transient existence check and the
    INSERT (``recompute`` would look the row up again first).  ``None`` for
    an unknown transient.
    """
    from YSE_App.models.transient_models import Transient

    values = compute_stats(points_by_transient([transient_id]).get(transient_id, []))
    if not Transient.objects.filter(pk=transient_id).exists():
        return None
    stat = TransientPhotStat(transient_id=transient_id)
    apply_values(stat, values)
    stat.save(force_insert=True)
    return stat


@dataclass
class RecomputeResult:
    processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0


def recompute_many(transient_ids: Sequence[int], *, batch_size: int = DEFAULT_BATCH_SIZE) -> RecomputeResult:
    """Recompute ``transient_ids`` in batches: one photometry query and one
    stat query per batch, bulk inserts for new rows, one UPDATE per changed row."""
    result = RecomputeResult()
    ids = [int(t) for t in transient_ids]
    for start in range(0, len(ids), max(1, int(batch_size))):
        batch = ids[start:start + batch_size]
        existing = {s.transient_id: s for s in TransientPhotStat.objects.filter(transient_id__in=batch)}
        grouped = points_by_transient(batch)
        to_create: List[TransientPhotStat] = []
        with transaction.atomic():
            for tid in batch:
                values = compute_stats(grouped.get(tid, []))
                stat = existing.get(tid)
                result.processed += 1
                if stat is None:
                    stat = TransientPhotStat(transient_id=tid)
                    apply_values(stat, values)
                    to_create.append(stat)
                    result.created += 1
                elif apply_values(stat, values):
                    stat.save()
                    result.updated += 1
                else:
                    result.unchanged += 1
            if to_create:
                TransientPhotStat.objects.bulk_create(to_create, batch_size=batch_size)
    return result


# --------------------------------------------------------------- scheduling


def _state():
    if not hasattr(_local, 'depth'):
        _local.depth = 0
        _local.pending_transients = set()
        _local.pending_photometry = set()
    return _local


def deferred_active() -> bool:
    return _state().depth > 0


@contextlib.contextmanager
def deferred_updates():
    """Collect recompute requests and run each transient once on exit.

    Nesting is fine; the outermost exit does the work.  An exception inside
    the block still flushes what was collected (the saved rows are there).
    """
    state = _state()
    state.depth += 1
    try:
        yield
    finally:
        state.depth -= 1
        if state.depth == 0:
            flush_pending()


def _transient_ids_for_photometry(photometry_ids: Iterable[int]) -> List[int]:
    from YSE_App.models.phot_models import TransientPhotometry

    ids = [int(p) for p in photometry_ids if p is not None]
    if not ids:
        return []
    return list(
        TransientPhotometry.objects.filter(pk__in=ids)
        .values_list('transient_id', flat=True).distinct()
    )


def flush_pending() -> int:
    """Recompute every transient collected so far; returns how many."""
    state = _state()
    transient_ids = set(state.pending_transients)
    photometry_ids = set(state.pending_photometry)
    state.pending_transients.clear()
    state.pending_photometry.clear()
    if photometry_ids:
        transient_ids.update(_transient_ids_for_photometry(photometry_ids))
    for tid in sorted(t for t in transient_ids if t is not None):
        try:
            recompute(tid)
        except Exception:  # one bad transient must not lose the others
            logger.exception('photstat recompute failed for transient %s', tid)
    return len(transient_ids)


def schedule_recompute(transient_id: Optional[int] = None, *, photometry_id: Optional[int] = None,
                       create: bool = True) -> None:
    """Recompute now, or collect for the end of the enclosing :func:`deferred_updates`.

    Callers pass ``transient_id`` when they have it; signal handlers on
    ``TransientPhotData`` pass ``photometry_id`` and the transient is looked
    up (once per flush when deferred).
    """
    state = _state()
    if state.depth > 0:
        if transient_id is not None:
            state.pending_transients.add(int(transient_id))
        elif photometry_id is not None:
            state.pending_photometry.add(int(photometry_id))
        return
    if transient_id is None and photometry_id is not None:
        ids = _transient_ids_for_photometry([photometry_id])
        transient_id = ids[0] if ids else None
    if transient_id is None:
        return
    try:
        recompute(transient_id, create=create)
    except Exception:
        logger.exception('photstat recompute failed for transient %s', transient_id)
