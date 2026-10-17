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

from YSE_App.models.phot_stat_models import (
    EXTRAPOLATED_FIELDS,
    TransientPhotStat,
    datetime_to_mjd,
    mjd_to_datetime,
)
from YSE_App.common.quality_mask import is_legacy_flagged
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
    # The ``TransientPhotometry`` set the point belongs to (its groups decide
    # which visibility scopes count it, #607).
    photometry_id: Optional[int] = None
    # The ``TransientPhotData`` id (per-point access rules, #648).
    point_id: Optional[int] = None


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


def _phot_rows(transient_ids: Sequence[int], point_filter=None):
    """Unflagged-or-flagged photometry rows for ``transient_ids`` in one query (``point_filter``: a ``Q``)."""
    from YSE_App.models.phot_models import TransientPhotData

    rows = TransientPhotData.objects.filter(photometry__transient_id__in=list(transient_ids))
    if point_filter is not None:
        rows = rows.filter(point_filter)
    return rows.values_list(
        'photometry__transient_id', 'band_id', 'band__name', 'obs_date',
        'mag', 'mag_err', 'flux', 'flux_err', 'flux_zero_point', 'quality_mask',
        'photometry_id', 'pk',
    )


def points_by_transient(transient_ids: Sequence[int], point_filter=None) -> Dict[int, List[PhotPoint]]:
    """``{transient_id: [PhotPoint, ...]}`` for ``transient_ids`` (one query).

    ``point_filter`` (a ``Q`` on ``TransientPhotData``) keeps only some points,
    for example what one viewer may see (``photstat_visibility``).
    """
    ids = list({int(t) for t in transient_ids if t is not None})
    grouped: Dict[int, List[PhotPoint]] = {tid: [] for tid in ids}
    if not ids:
        return grouped
    for (tid, band_id, band_name, obs_date, mag, mag_err, flux, flux_err, zp, mask, phot_id,
         point_id) in _phot_rows(ids, point_filter):
        grouped[tid].append(PhotPoint(
            mjd=datetime_to_mjd(obs_date), band_id=band_id, band_name=band_name or '',
            mag=mag, mag_err=mag_err, flux=flux, flux_err=flux_err, flux_zero_point=zp,
            flagged=is_legacy_flagged(mask, 'TransientPhotData'), photometry_id=phot_id,
            point_id=point_id,
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


def _recompute_update_fields() -> List[str]:
    """Columns a recompute writes on an existing row: everything but the
    Bazin columns (``EXTRAPOLATED_FIELDS``, written by the Bazin job), so a
    recompute never overwrites a newer extrapolation with the value it read."""
    return [
        f.name for f in TransientPhotStat._meta.concrete_fields
        if not f.primary_key and f.name not in EXTRAPOLATED_FIELDS and f.name != 'transient'
    ]


def _save(stat: TransientPhotStat) -> None:
    if stat.pk is None:
        stat.save()
    else:
        stat.save(update_fields=_recompute_update_fields())


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
    points = points_by_transient([transient_id]).get(transient_id, [])
    values = compute_stats(points)
    had_row = stat.pk is not None
    old_num_det = stat.num_det_global or 0
    if apply_values(stat, values):
        _save(stat)
        if had_row and (stat.num_det_global or 0) > old_num_det:
            _announce_new_photometry(stat, (stat.num_det_global or 0) - old_num_det)
    # A group change moves points between scopes without changing the
    # all-data row, so the scopes are synced even when ``stat`` was unchanged.
    # ``create=False`` (a delete, maybe inside a Transient cascade): update or
    # delete scope rows, never insert one.
    sync_scopes([transient_id], {transient_id: points}, create=create)
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

    points = points_by_transient([transient_id]).get(transient_id, [])
    values = compute_stats(points)
    if not Transient.objects.filter(pk=transient_id).exists():
        return None
    stat = TransientPhotStat(transient_id=transient_id)
    apply_values(stat, values)
    stat.save(force_insert=True)
    sync_scopes([transient_id], {transient_id: points})
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
                    _save(stat)
                    result.updated += 1
                else:
                    result.unchanged += 1
            if to_create:
                TransientPhotStat.objects.bulk_create(to_create, batch_size=batch_size)
            sync_scopes(batch, grouped)
    return result


# ------------------------------------------------------- visibility scopes (#607)
#
# A transient with at least one photometry set that has collaboration groups
# gets ``TransientPhotStatScope`` rows: the **public** scope (the points of
# sets without groups) and one scope per restricting group (the public points
# plus the points of every set shared with that group). A transient without
# restricted photometry has none: its all-data row is what everyone may see.
# ``services.photstat_visibility`` picks the row a viewer is shown.


@dataclass
class ScopeResult:
    transients: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


def photometry_groups(transient_ids: Sequence[int]) -> Dict[int, Dict[int, frozenset]]:
    """``{transient_id: {photometry_id: frozenset(group ids)}}`` for the sets that have groups (one query)."""
    from YSE_App.models.phot_models import TransientPhotometry

    ids = list({int(t) for t in transient_ids if t is not None})
    out: Dict[int, Dict[int, set]] = {}
    if not ids:
        return {}
    rows = TransientPhotometry.groups.through.objects.filter(
        transientphotometry__transient_id__in=ids,
    ).values_list('transientphotometry__transient_id', 'transientphotometry_id', 'group_id')
    for tid, phot_id, group_id in rows:
        out.setdefault(tid, {}).setdefault(phot_id, set()).add(group_id)
    return {tid: {pid: frozenset(g) for pid, g in sets.items()} for tid, sets in out.items()}


def scope_point_sets(points: Sequence[PhotPoint], groups_by_photometry: Dict[int, frozenset],
                     point_rules: Optional[Dict[int, Dict[int, str]]] = None) -> Dict[Optional[int], List[PhotPoint]]:
    """``{None: public points, group_id: public + that group's points}``; ``{}`` without restricted sets.

    Pure. ``groups_by_photometry`` maps each restricted photometry set of the
    transient to its groups; a set that is not in it is public. A restricted
    set without points still creates the scopes (the viewer's "has hidden
    photometry" check looks at sets, not points).

    ``point_rules`` (#648, only with ``DATA_ACCESS_GRANTS_ENABLED``) maps a
    point id to ``{group_id: "allow" | "deny"}``: a group's scope then holds
    the points the group may see, its set's groups plus its allows minus its
    denies. Public points are in every scope (they can never be restricted).
    """
    if not groups_by_photometry:
        return {}
    point_rules = point_rules or {}
    group_ids = {g for groups in groups_by_photometry.values() for g in groups}
    group_ids |= {g for rules in point_rules.values() for g, effect in rules.items() if effect == 'allow'}
    out: Dict[Optional[int], List[PhotPoint]] = {None: []}
    for gid in sorted(group_ids):
        out[gid] = []
    for p in points:
        groups = groups_by_photometry.get(p.photometry_id)
        if not groups:
            for bucket in out.values():
                bucket.append(p)
            continue
        rules = point_rules.get(p.point_id) if p.point_id is not None else None
        if rules:
            groups = (set(groups) - {g for g, e in rules.items() if e == 'deny'}) | {
                g for g, e in rules.items() if e == 'allow'}
        for gid in groups:
            out[gid].append(p)
    return out


def sync_scopes(transient_ids: Sequence[int], grouped_points: Optional[Dict[int, List[PhotPoint]]] = None,
                *, dry_run: bool = False, create: bool = True) -> ScopeResult:
    """Write the scope rows of ``transient_ids`` (insert, update or delete; unchanged rows are not written).

    Three queries for the batch (photometry when ``grouped_points`` is not
    given, the set groups, the existing scope rows) plus the writes.
    ``dry_run`` counts what would change and writes nothing; ``create=False``
    never inserts a row (see ``recompute``).
    """
    from YSE_App.models.phot_stat_models import TransientPhotStatScope, scope_key_for_group

    result = ScopeResult()
    ids = sorted({int(t) for t in transient_ids if t is not None})
    if not ids:
        return result
    groups = photometry_groups(ids)
    existing = {
        (row.transient_id, row.scope_key): row
        for row in TransientPhotStatScope.objects.filter(transient_id__in=ids)
    }
    if not groups and not existing:
        result.transients = len(ids)
        return result
    if grouped_points is None:
        grouped_points = points_by_transient([tid for tid in ids if tid in groups])
    from YSE_App.services import access_rules

    point_rules = access_rules.point_group_rules([t for t in ids if t in groups]) if access_rules.enabled() else {}
    to_create: List[TransientPhotStatScope] = []
    to_update: List[TransientPhotStatScope] = []
    for tid in ids:
        result.transients += 1
        for gid, pts in scope_point_sets(grouped_points.get(tid, []), groups.get(tid, {}),
                                         point_rules.get(tid)).items():
            key = scope_key_for_group(gid)
            row = existing.pop((tid, key), None)
            values = compute_stats(pts)
            if row is None:
                if not create:
                    continue
                row = TransientPhotStatScope(transient_id=tid, group_id=gid, scope_key=key)
                apply_values(row, values)
                to_create.append(row)
                result.created += 1
            elif apply_values(row, values):
                to_update.append(row)
                result.updated += 1
            else:
                result.unchanged += 1
    stale_pks = [row.pk for row in existing.values()]
    result.deleted = len(stale_pks)
    if dry_run:
        return result
    with transaction.atomic():
        for row in to_update:
            row.save()
        if to_create:
            # ignore_conflicts: a concurrent recompute of the same transient may
            # have inserted the row first; its values are as current as ours.
            TransientPhotStatScope.objects.bulk_create(to_create, batch_size=DEFAULT_BATCH_SIZE,
                                                       ignore_conflicts=True)
        if stale_pks:
            TransientPhotStatScope.objects.filter(pk__in=stale_pks).delete()
    return result


def transients_needing_scopes(after_pk: int = 0, limit: Optional[int] = None) -> List[int]:
    """Transients with restricted photometry or scope rows, by pk (the backfill's selection)."""
    from YSE_App.models.phot_models import TransientPhotometry
    from YSE_App.models.phot_stat_models import TransientPhotStatScope

    restricted = set(
        TransientPhotometry.groups.through.objects.filter(transientphotometry__transient_id__gt=after_pk)
        .values_list('transientphotometry__transient_id', flat=True).distinct()
    )
    restricted |= set(
        TransientPhotStatScope.objects.filter(transient_id__gt=after_pk)
        .values_list('transient_id', flat=True).distinct()
    )
    ids = sorted(restricted)
    return ids[:int(limit)] if limit else ids


def stale_transient_ids(limit: Optional[int] = None) -> List[int]:
    """Transients whose stat row is missing or was written under older rules.

    The same selection as ``rebuild_photstats --stale-only``, oldest pk first;
    ``limit`` caps one pass of the periodic refresh (#269).
    """
    from YSE_App.models.transient_models import Transient

    current = TransientPhotStat.objects.filter(schema_version__gte=SCHEMA_VERSION)
    qs = Transient.objects.exclude(pk__in=current.values('transient_id')).order_by('pk')
    if limit:
        qs = qs[:int(limit)]
    return list(qs.values_list('pk', flat=True))


def refresh_stale(limit: Optional[int] = None, *, batch_size: int = DEFAULT_BATCH_SIZE) -> RecomputeResult:
    """Recompute up to ``limit`` missing or outdated rows (the periodic job, #269)."""
    return recompute_many(stale_transient_ids(limit), batch_size=batch_size)


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


# ---------------------------------------------------- access rules (#648)


def invalidate_scopes(pairs) -> int:
    """Delete the scope rows of ``(transient_id, group_id)`` pairs whose group's access changed; queue a rebuild.

    Until the rebuild (the ``photstat.sync_scopes`` job) runs, viewers who
    relied on those rows see the next best scope or no derived values, never
    stale ones. Returns the number of rows deleted.
    """
    from YSE_App.models.phot_stat_models import TransientPhotStatScope

    by_group: Dict[int, set] = {}
    for tid, gid in pairs:
        if tid is not None and gid is not None:
            by_group.setdefault(int(gid), set()).add(int(tid))
    if not by_group:
        return 0
    deleted = 0
    transients = set()
    for gid, tids in sorted(by_group.items()):
        tids = sorted(tids)
        transients.update(tids)
        for i in range(0, len(tids), DEFAULT_BATCH_SIZE):
            n, _ = TransientPhotStatScope.objects.filter(
                group_id=gid, transient_id__in=tids[i:i + DEFAULT_BATCH_SIZE]).delete()
            deleted += n
    from YSE_App.services import photstat_jobs

    photstat_jobs.queue_scope_sync(sorted(transients))
    return deleted
