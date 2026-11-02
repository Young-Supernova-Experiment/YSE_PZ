"""Facets with live counts for Transient Search (#842), UI preview only.

The search page's filter groups get a sidebar of facets: status, spectroscopic class,
tags, survey (observation group), internal survey, peak magnitude, best redshift and
discovery date. Each value shows how many of the rows the search lists *now* it would
keep, and is a link that adds (or removes) the matching search parameter. The
parameters are the search's own (``status``, ``spec_class``, ``tags``, ``obs_group``,
``internal_survey``, ``peak_mag_min`` / ``peak_mag_max``, ``redshift_min`` /
``redshift_max``, ``has_redshift``, ``days_since_disc_max``), so a URL, "Save as SQL
query" and "Save as Selection" (#841) carry the facet state with no extra code.

Cost (see ``docs/transient-search.md``):

* every count runs over the search's own filtered queryset, which is already limited to
  the transients the viewer may see listed (``filter_listed_transients``, #607), so a
  hidden object is never counted;
* one ``GROUP BY`` per value facet (status, class, survey, internal survey, and one on
  the tag link table), plus one aggregate statement for all the range facets together:
  six statements whatever the number of rows. The magnitude buckets read the stored
  ``TransientPhotStat`` value the viewer may see (``alias_stat_fields``), never the raw
  photometry;
* the counts are cached for ``SEARCH_FACET_CACHE_SECONDS`` (default 60) per viewer and
  normalised filter string.

A bucket's count is exactly what clicking it returns: the buckets use the filters' own
inclusive bounds, so a value on a bucket edge (a peak of exactly 19.0) is counted in both
neighbouring buckets.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import QueryDict

__all__ = [
    'FACETS',
    'RANGE_FACETS',
    'facet_cache_key',
    'facet_counts',
    'build_facets',
]

CACHE_VERSION = 1
DEFAULT_CACHE_SECONDS = 60
#: Facet values shown before the rest go under "more".
TOP_VALUES = 8

# Parameters that never change which rows match (left out of the cache key).
_NOT_CRITERIA = ('ordering',)


@dataclass(frozen=True)
class ValueFacet:
    """A facet over one name-valued multiple-choice filter, counted with one ``GROUP BY``."""

    key: str
    title: str
    param: str
    group_by: str  # values() path of the name on Transient, or '' for tags
    label: Callable[[str], str] = str


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    params: Tuple[Tuple[str, str], ...]  # the parameters the bucket sets
    q: Callable  # (names) -> Q over the facet queryset


@dataclass(frozen=True)
class RangeFacet:
    key: str
    title: str
    params: Tuple[str, ...]  # every parameter a bucket of this facet may set
    buckets: Tuple[Bucket, ...] = field(default_factory=tuple)


def _status_label(name):
    from YSE_App.common.status_display import status_label

    return status_label(name)


FACETS = (
    ValueFacet('status', 'Status', 'status', 'status__name', _status_label),
    ValueFacet('spec_class', 'Class (spectroscopic)', 'spec_class', 'best_spec_class__name'),
    ValueFacet('tags', 'Tags', 'tags', ''),
    ValueFacet('obs_group', 'Survey / telescope group', 'obs_group', 'obs_group__name'),
    ValueFacet('internal_survey', 'Internal survey', 'internal_survey', 'internal_survey__name'),
)


def _fmt(value):
    return ('%g' % value)


def _range_bucket(prefix, lo, hi, field_name, label_fmt, min_param, max_param):
    params = []
    if lo is not None:
        params.append((min_param, _fmt(lo)))
    if hi is not None:
        params.append((max_param, _fmt(hi)))

    def q(names, lo=lo, hi=hi):
        path = names[field_name]
        cond = Q(**{path + '__isnull': False})
        if lo is not None:
            cond &= Q(**{path + '__gte': lo})
        if hi is not None:
            cond &= Q(**{path + '__lte': hi})
        return cond

    if lo is None:
        label = label_fmt[0] % _fmt(hi)
    elif hi is None:
        label = label_fmt[2] % _fmt(lo)
    else:
        label = label_fmt[1] % (_fmt(lo), _fmt(hi))
    return Bucket('%s_%s_%s' % (prefix, _fmt(lo) if lo is not None else 'lo', _fmt(hi) if hi is not None else 'hi'),
                  label, tuple(params), q)


def _ranges(prefix, edges, field_name, label_fmt, min_param, max_param):
    bounds = [None] + list(edges) + [None]
    return tuple(_range_bucket(prefix, bounds[i], bounds[i + 1], field_name, label_fmt, min_param, max_param)
                 for i in range(len(bounds) - 1))


def _no_redshift_q(names):
    from YSE_App.services.redshift_sql import has_best_redshift_q

    return ~has_best_redshift_q()


def _disc_bucket(days, label):
    def q(names, days=days):
        from YSE_App.filters.transient_search import UtcNowMinusSeconds

        return Q(disc_date__gte=UtcNowMinusSeconds(days * 86400.0))
    return Bucket('disc_%d' % days, label, (('days_since_disc_max', str(days)),), q)


RANGE_FACETS = (
    RangeFacet(
        'peak_mag', 'Peak magnitude', ('peak_mag_min', 'peak_mag_max'),
        _ranges('mag', (17, 18, 19, 20, 21), 'peak_mag', ('brighter than %s', '%s to %s', 'fainter than %s'),
                'peak_mag_min', 'peak_mag_max'),
    ),
    RangeFacet(
        'redshift', 'Redshift', ('redshift_min', 'redshift_max', 'has_redshift'),
        _ranges('z', (0.01, 0.03, 0.06, 0.1, 0.2), 'best_redshift', ('below %s', '%s to %s', 'above %s'),
                'redshift_min', 'redshift_max')
        + (Bucket('z_none', 'no redshift', (('has_redshift', 'false'),), _no_redshift_q),),
    ),
    RangeFacet(
        'discovered', 'Discovered', ('days_since_disc_max',),
        (_disc_bucket(1, 'in the last day'), _disc_bucket(3, 'in the last 3 days'),
         _disc_bucket(7, 'in the last week'), _disc_bucket(30, 'in the last 30 days'),
         _disc_bucket(365, 'in the last year')),
    ),
)


# --------------------------------------------------------------------------- counts

def _cache_seconds():
    try:
        return int(getattr(settings, 'SEARCH_FACET_CACHE_SECONDS', DEFAULT_CACHE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_SECONDS


def criteria_string(params):
    """The criteria of ``params`` as a canonical query string (page state and ordering dropped)."""
    from YSE_App.services.selections import normalize_search_params

    data = QueryDict(normalize_search_params(params), mutable=True)
    for key in _NOT_CRITERIA:
        data.pop(key, None)
    return data.urlencode()


def facet_cache_key(user, params):
    """Cache key per viewer and filter string: counts depend on what the viewer may see."""
    digest = hashlib.sha256(criteria_string(params).encode('utf-8')).hexdigest()
    who = getattr(user, 'pk', None) or 'anon'
    return 'search_facets:v%d:%s:%s' % (CACHE_VERSION, who, digest)


def _value_counts(qs, facet):
    """``[(name, count)]`` for one value facet: one GROUP BY over the filtered rows."""
    if facet.group_by:
        rows = (qs.order_by().values(facet.group_by)
                .annotate(n=Count('pk')).order_by())
        return [(r[facet.group_by], r['n']) for r in rows if r[facet.group_by] not in (None, '')]
    from YSE_App.models import Transient

    through = Transient.tags.through
    rows = (through.objects.filter(transient_id__in=qs.order_by().values('pk'))
            .values('transienttag__name').annotate(n=Count('transient_id')).order_by())
    return [(r['transienttag__name'], r['n']) for r in rows]


def _range_counts(qs, user):
    """``{bucket key: count}`` for every range facet, in one aggregate statement."""
    from YSE_App.filters.transient_search import annotate_best_redshift, visible_stat_lookups

    qs = qs.order_by()
    qs, names = visible_stat_lookups(qs, user, ['peak_mag'])
    qs = annotate_best_redshift(qs)
    names = dict(names, best_redshift='best_redshift')
    aggregates = {}
    for facet in RANGE_FACETS:
        for bucket in facet.buckets:
            aggregates[bucket.key] = Count('pk', filter=bucket.q(names))
    return {k: int(v or 0) for k, v in qs.aggregate(**aggregates).items()}


def facet_counts(filterset, user, params, *, use_cache=True):
    """Counts for every facet over ``filterset.qs`` (``None`` when the filters are invalid).

    Returns ``{'values': {facet key: [(name, count)]}, 'ranges': {bucket key: count}}``.
    """
    if not filterset.is_valid():
        return None
    key = facet_cache_key(user, params)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached
    qs = filterset.qs
    result = {
        'values': {facet.key: _value_counts(qs, facet) for facet in FACETS},
        'ranges': _range_counts(qs, user),
    }
    timeout = _cache_seconds()
    if use_cache and timeout > 0:
        cache.set(key, result, timeout=timeout)
    return result


# --------------------------------------------------------------------------- display

def _url(params):
    data = params.copy()
    data.pop('page', None)
    encoded = data.urlencode()
    return '?' + encoded if encoded else '?'


def _toggle_value(params, param, value):
    data = params.copy()
    current = [v for v in data.getlist(param) if v != '']
    if value in current:
        current.remove(value)
        active = True
    else:
        current.append(value)
        active = False
    if current:
        data.setlist(param, current)
    else:
        data.pop(param, None)
    return _url(data), active


def _same_number(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _bucket_active(params, facet, bucket):
    wanted = dict(bucket.params)
    for name in facet.params:
        have = params.get(name, '')
        if name in wanted:
            if name == 'has_redshift':
                if have.lower() != wanted[name]:
                    return False
            elif not _same_number(have, wanted[name]):
                return False
        elif have not in ('', None):
            return False
    return True


def _toggle_bucket(params, facet, bucket, active):
    data = params.copy()
    for name in facet.params:
        data.pop(name, None)
    if not active:
        for name, value in bucket.params:
            data[name] = value
    return _url(data)


def build_facets(counts, params, *, top=TOP_VALUES):
    """Template data: ``[{key, title, values: [{label, count, url, active}], more: [...], active}]``."""
    if counts is None:
        return []
    out = []
    for facet in FACETS:
        selected = [v for v in params.getlist(facet.param) if v != '']
        rows = dict(counts['values'].get(facet.key, []))
        for name in selected:  # a chosen value stays visible (and removable) at 0
            rows.setdefault(name, 0)
        values = []
        for name, n in sorted(rows.items(), key=lambda item: (-item[1], str(item[0]).lower())):
            url, active = _toggle_value(params, facet.param, name)
            values.append({'label': facet.label(name), 'value': name, 'count': n, 'url': url, 'active': active})
        values.sort(key=lambda v: not v['active'])  # stable: chosen first, then by count
        out.append({'key': facet.key, 'title': facet.title, 'values': values[:top], 'more': values[top:],
                    'active': bool(selected)})
    for facet in RANGE_FACETS:
        values = []
        any_active = False
        for bucket in facet.buckets:
            active = _bucket_active(params, facet, bucket)
            any_active = any_active or active
            n = counts['ranges'].get(bucket.key, 0)
            if not n and not active:
                continue
            values.append({'label': bucket.label, 'value': bucket.key, 'count': n,
                           'url': _toggle_bucket(params, facet, bucket, active), 'active': active})
        out.append({'key': facet.key, 'title': facet.title, 'values': values, 'more': [], 'active': any_active})
    return out


def facet_toggle_params(params, facet_key, value) -> Optional[QueryDict]:
    """The parameters after clicking ``value`` of facet ``facet_key`` (used by the tests)."""
    for facet in FACETS:
        if facet.key == facet_key:
            url, _ = _toggle_value(params, facet.param, value)
            return QueryDict(url.lstrip('?'))
    for facet in RANGE_FACETS:
        if facet.key == facet_key:
            for bucket in facet.buckets:
                if bucket.key == value:
                    active = _bucket_active(params, facet, bucket)
                    return QueryDict(_toggle_bucket(params, facet, bucket, active).lstrip('?'))
    return None


def all_facet_values(counts) -> List[Tuple[str, str, int]]:
    """``[(facet key, value, count)]`` for every counted value (the tests walk them)."""
    out = []
    for facet in FACETS:
        out.extend((facet.key, name, n) for name, n in counts['values'].get(facet.key, []))
    for facet in RANGE_FACETS:
        out.extend((facet.key, b.key, counts['ranges'].get(b.key, 0)) for b in facet.buckets)
    return out
