"""Photometry statistics a viewer may see (#607, option (a)).

``TransientPhotStat`` summarises *all* of a transient's photometry. For a
viewer who may not see some of it (a photometry set restricted to
collaboration groups the viewer is not in), every derived value (last
detection, last observation, peak, rates, limits, the Last Det. / Last Obs.
/ Peak Mag columns, their sorting and filters, the PhotStat API and the
search export) comes instead from one stored ``TransientPhotStatScope`` row:

* **staff** (and superusers) see the all-data row;
* a viewer with **no hidden photometry** on the transient sees the all-data
  row too, because it is exactly their data (this covers every transient
  without restricted photometry, and a viewer who is in every restricting
  group);
* otherwise the viewer sees the **most complete scope they may see**: the
  public scope, or the scope of one of their groups that has data on the
  transient, whichever counts the most unflagged points (then detections,
  then ``scope_key``). Rows are only ever built from data the viewer may
  see, so nothing leaks.

**Several groups.** A viewer in two groups that both restrict data on the
same transient may see the union of both scopes, which is not stored (the
combinations grow with the number of groups). They are shown the larger of
the two single-group scopes, so their values are correct for that scope but
can miss the other group's points; staff and viewers in every restricting
group get exact values. This is the documented approximation (#607).

**Before the backfill** a transient with restricted photometry has no scope
rows yet: a viewer with hidden photometry then sees no derived values for it
(the stop-gap rule (c)), never the all-data values. The detail page computes
the missing rows on the spot.

SQL reads (filters and sorting) use :func:`stat_expression`: for staff the
plain ``photstat__<field>`` LEFT JOIN, otherwise a ``CASE`` over one
``EXISTS`` (hidden photometry) and one correlated subquery on the indexed
scope table. Python reads (table cells, the detail page, the API) use
:func:`visible_stats`: at most three queries for any number of transients.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from django.db.models import Case, F, OuterRef, Q, Subquery, When

from YSE_App.services.visibility import (
    hidden_photometry_exists,
    transients_with_hidden_photometry,
    user_group_ids,
    user_sees_everything,
)

__all__ = [
    'STAT_FIELDS',
    'STAT_EXPORT_FIELDS',
    'stat_expression',
    'alias_stat_fields',
    'annotate_stat_fields',
    'apply_visible_values',
    'visible_stats',
    'visible_stat',
    'public_stat',
    'best_scope',
    'scope_rank',
]

# Columns a viewer-dependent read may ask for.
STAT_FIELDS = (
    'num_obs_global', 'num_det_global', 'num_limits_global', 'last_obs_mjd', 'last_obs_date',
    'first_detected_mjd', 'first_detected_date', 'first_detected_mag', 'first_detected_band_id',
    'last_detected_mjd', 'last_detected_date', 'last_detected_mag', 'last_detected_band_id',
    'peak_mjd', 'peak_date', 'peak_mag', 'peak_band_id', 'mean_mag', 'faintest_mag',
    'deepest_limit', 'deepest_limit_mjd', 'deepest_limit_band_id',
    'last_non_detection_mjd', 'last_non_detection_band_id', 'time_to_non_detection',
    'rise_rate', 'decay_rate',
)

# Ordering of a viewer's candidate scopes, best first (SQL and Python agree).
SCOPE_ORDER = ('-num_obs_global', '-num_det_global', 'scope_key')

DISPLAY_RELATED = (
    'peak_band', 'first_detected_band', 'last_detected_band',
    'deepest_limit_band', 'last_non_detection_band',
)


def scope_rank(row):
    """Python sort key matching ``SCOPE_ORDER`` (smaller is better)."""
    return (-(row.num_obs_global or 0), -(row.num_det_global or 0), row.scope_key or '')


def _candidate_scope_q(user) -> Q:
    """The scope rows ``user`` may see: the public scope and their groups' scopes."""
    gids = user_group_ids(user)
    q = Q(group__isnull=True)
    if gids:
        q |= Q(group_id__in=gids)
    return q


def _output_field(field: str):
    from django.db.models import IntegerField

    from YSE_App.models.phot_stat_models import TransientPhotStat

    if field.endswith('_id'):
        return IntegerField()
    return TransientPhotStat._meta.get_field(field).__class__()


def stat_expression(user, field: str, *, transient_ref: str = 'pk', stat_path: str = 'photstat__'):
    """SQL expression for the stat column ``field`` as ``user`` may see it.

    ``transient_ref`` names the transient id on the queried model (``pk`` on
    ``Transient``, ``transient_id`` on follow-ups); ``stat_path`` the path to
    its ``TransientPhotStat`` row (``photstat__`` / ``transient__photstat__``).
    """
    full = F(stat_path + field)
    if user_sees_everything(user):
        return full
    from YSE_App.models.phot_stat_models import TransientPhotStatScope

    output_field = _output_field(field)
    best = (
        TransientPhotStatScope.objects.filter(transient_id=OuterRef(transient_ref))
        .filter(_candidate_scope_q(user))
        .order_by(*SCOPE_ORDER)
        .values(field)[:1]
    )
    return Case(
        When(hidden_photometry_exists(user, transient_ref), then=Subquery(best, output_field=output_field)),
        default=full,
        output_field=output_field,
    )


def alias_name(field: str) -> str:
    return '_vs_' + field


def alias_stat_fields(queryset, user, fields: Iterable[str], *, transient_ref: str = 'pk',
                      stat_path: str = 'photstat__'):
    """``(queryset, {field: name})``: each stat ``field`` as ``user`` may see it, under ``name``.

    For staff the names are the plain ``photstat__<field>`` paths (no alias,
    the indexed LEFT JOIN as before); otherwise ``_vs_<field>`` aliases
    (``QuerySet.alias``, not selected) usable in ``filter`` and ``order_by``.
    """
    fields = list(dict.fromkeys(fields))
    if user_sees_everything(user):
        return queryset, {f: stat_path + f for f in fields}
    names = {}
    aliases = {}
    for f in fields:
        name = alias_name(f)
        names[f] = name
        if name not in queryset.query.annotations:
            aliases[name] = stat_expression(user, f, transient_ref=transient_ref, stat_path=stat_path)
    if aliases:
        queryset = queryset.alias(**aliases)
    return queryset, names


# The stat columns of the search CSV export.
STAT_EXPORT_FIELDS = ('peak_mag', 'last_detected_mag', 'last_detected_mjd', 'num_det_global')


def annotate_stat_fields(queryset, user, fields: Iterable[str], *, transient_ref: str = 'pk',
                         stat_path: str = 'photstat__'):
    """Like :func:`alias_stat_fields`, but selected (``annotate``), for ``values()`` / ``values_list()``."""
    fields = list(dict.fromkeys(fields))
    if user_sees_everything(user):
        return queryset, {f: stat_path + f for f in fields}
    names = {f: '_vss_' + f for f in fields}
    queryset = queryset.annotate(**{
        names[f]: stat_expression(user, f, transient_ref=transient_ref, stat_path=stat_path) for f in fields
    })
    return queryset, names


def best_scope(rows):
    """The best of ``rows`` (scope rows the viewer may see), or ``None``."""
    rows = list(rows)
    if not rows:
        return None
    return min(rows, key=scope_rank)


def _full_rows(transient_ids, related=DISPLAY_RELATED):
    from YSE_App.models.phot_stat_models import TransientPhotStat

    return {
        s.transient_id: s
        for s in TransientPhotStat.objects.filter(transient_id__in=list(transient_ids)).select_related(*related)
    }


def visible_stats(user, transient_ids: Iterable[int], *, full_rows: Optional[Dict[int, object]] = None,
                  related=DISPLAY_RELATED) -> Dict[int, Optional[object]]:
    """``{transient_id: stat row or None}`` with the row ``user`` may see for each transient.

    The value is a ``TransientPhotStat`` (all data) or a
    ``TransientPhotStatScope`` (same attribute names), with ``related`` bands
    loaded. Staff: one query, none when ``full_rows``
    (``{transient_id: TransientPhotStat or None}``) is given. Others: always
    three (hidden photometry, all-data rows, scope rows), whatever the data,
    so a page's query count never depends on which rows hide photometry.
    """
    ids = {int(t) for t in transient_ids if t is not None}
    if not ids:
        return {}
    out: Dict[int, Optional[object]] = dict.fromkeys(ids)
    if user_sees_everything(user):
        rows = full_rows if full_rows is not None else _full_rows(ids, related)
        missing = [tid for tid in ids if tid not in rows]
        if missing:
            rows = dict(rows)
            rows.update(_full_rows(missing, related))
        for tid in ids:
            out[tid] = rows.get(tid)
        return out
    from YSE_App.models.phot_stat_models import TransientPhotStatScope

    hidden = transients_with_hidden_photometry(user, ids)
    rows = _full_rows(ids, related)
    candidates: Dict[int, List[object]] = {}
    for row in (TransientPhotStatScope.objects.filter(transient_id__in=ids)
                .filter(_candidate_scope_q(user)).select_related(*related)):
        candidates.setdefault(row.transient_id, []).append(row)
    for tid in ids:
        out[tid] = best_scope(candidates.get(tid, ())) if tid in hidden else rows.get(tid)
    return out


_BAND_FIELDS = DISPLAY_RELATED
_SCALAR_FIELDS = tuple(f for f in STAT_FIELDS if not f.endswith('_band_id'))


def apply_visible_values(user, rows) -> None:
    """Overwrite, in memory, the values of all-data ``TransientPhotStat`` ``rows`` with what ``user`` may see.

    For the PhotStat API (same schema, same row ids): a row whose transient
    has photometry hidden from ``user`` takes the values of the best scope
    they may see, or empty values when there is none yet. Nothing is saved.
    """
    rows = [r for r in rows if r is not None]
    if not rows or user_sees_everything(user):
        return
    hidden = transients_with_hidden_photometry(user, [r.transient_id for r in rows])
    if not hidden:
        return
    scoped = visible_stats(user, hidden)
    for row in rows:
        if row.transient_id not in hidden:
            continue
        src = scoped.get(row.transient_id)
        for name in _SCALAR_FIELDS:
            default = 0 if name.startswith('num_') else None
            setattr(row, name, getattr(src, name) if src is not None else default)
        for name in _BAND_FIELDS:
            setattr(row, name, getattr(src, name) if src is not None else None)
        row.per_band_json = src.per_band_json if src is not None else None


def visible_stat(user, transient_id: int, *, compute_missing: bool = True):
    """The stat row ``user`` may see for one transient (the detail page, summaries, payloads).

    With ``compute_missing`` a transient without its all-data row or (for a
    viewer with hidden photometry) without scope rows gets them computed on
    the spot, as ``photstat.stat_for_transient`` does; a failure is logged
    and gives ``None``.
    """
    import logging

    from YSE_App.services import photstat

    transient_id = int(transient_id)
    if user_sees_everything(user) or not transients_with_hidden_photometry(user, [transient_id]):
        return photstat.stat_for_transient(transient_id, compute_missing=compute_missing)
    row = visible_stats(user, [transient_id]).get(transient_id)
    if row is None and compute_missing:
        try:
            photstat.sync_scopes([transient_id])
        except Exception:
            logging.getLogger(__name__).exception('photstat scope compute failed for transient %s', transient_id)
            return None
        row = visible_stats(user, [transient_id]).get(transient_id)
    return row


def public_stat(transient_id: int, *, compute_missing: bool = True):
    """The stat row of the photometry *without* collaboration groups (what an anonymous viewer could see).

    For collaboration-wide outputs written once for everyone, such as the AI
    summary (``services.summaries``).
    """
    from django.contrib.auth.models import AnonymousUser

    return visible_stat(AnonymousUser(), transient_id, compute_missing=compute_missing)
