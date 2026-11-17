"""Combined candidate list (#849): one list from several sources, for UI preview users.

The viewer picks the sources; the list is their union, each object once, with the names of
every source that brought it in ("why it's here"). Sources:

* their chosen **Selections** (#841) and **personal dashboard queries** (``UserQuery``);
* chosen **tags**;
* objects with a **follow-up request in the last few nights** (``nights``, default 3);
* **TNS objects brighter than 16 mag** (last detection, as the viewer may see it) detected in the
  last 30 days, named only when no other source brought them in.

The list is stateless: it stores nothing (no "skipped", "reviewed" or "watched" state, no
score) and hides nothing a source selects. The chosen sources travel in the query string
(so a URL reproduces the list, and list stepping (#850) can rebuild it); the page keeps the
last choice in the browser's localStorage. Every source starts from the rows the viewer may
see listed (``filter_listed_transients``), so the union never widens access.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.db.models import Exists, OuterRef, Q
from django.http import QueryDict
from django.utils import timezone

__all__ = [
    'DEFAULT_NIGHTS',
    'MAX_NIGHTS',
    'TNS_DAYS',
    'TNS_MAG_LIMIT',
    'Choice',
    'Source',
    'build_sources',
    'choice_query',
    'combined_queryset',
    'parse_choice',
    'reasons_for',
    'reasons_text',
    'recent_requests_queryset',
    'tns_bright_queryset',
]

DEFAULT_NIGHTS = 3
MAX_NIGHTS = 30
TNS_MAG_LIMIT = 16.0
TNS_DAYS = 30
#: Query-string keys of the source choice (the rest is the table's paging and sorting).
CHOICE_KEYS = ('src', 'sel', 'uq', 'tag', 'nights', 'tns')
MAX_CHOSEN = 50


@dataclass
class Choice:
    """Which sources the viewer chose. ``explicit`` is False when the defaults apply."""

    selections: List[str] = field(default_factory=list)
    user_queries: List[int] = field(default_factory=list)
    tags: List[int] = field(default_factory=list)
    nights: int = DEFAULT_NIGHTS
    tns: bool = True
    explicit: bool = False


@dataclass
class Source:
    """One chosen source: ``key`` (stable), ``label`` ("why it's here"), its rows or an error."""

    key: str
    label: str
    queryset: object = None
    error: str = ''
    kind: str = ''


def _ints(values):
    out = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in out:
            out.append(number)
    return out[:MAX_CHOSEN]


def _nights(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_NIGHTS, number))


def default_choice(user):
    """The defaults: the viewer's own Selections and dashboard queries, 3 nights of requests, TNS."""
    from YSE_App.models import Selection, UserQuery

    slugs = list(Selection.objects.filter(owner=user)
                 .order_by('name', 'pk').values_list('slug', flat=True)[:MAX_CHOSEN])
    uqs = list(UserQuery.objects.filter(user=user)
               .filter(Q(query__isnull=False) | (Q(python_query__isnull=False) & ~Q(python_query='')))
               .order_by('pk').values_list('pk', flat=True)[:MAX_CHOSEN])
    return Choice(selections=slugs, user_queries=uqs, tags=[], nights=DEFAULT_NIGHTS, tns=True, explicit=False)


def parse_choice(get, user):
    """The :class:`Choice` in a query string (``src=1`` marks an explicit choice), else the defaults."""
    if get.get('src') != '1':
        return default_choice(user)
    slugs = []
    for slug in get.getlist('sel'):
        slug = (slug or '').strip()
        if slug and slug not in slugs:
            slugs.append(slug)
    return Choice(
        selections=slugs[:MAX_CHOSEN],
        user_queries=_ints(get.getlist('uq')),
        tags=_ints(get.getlist('tag')),
        nights=_nights(get.get('nights'), 0),
        tns=get.get('tns') == '1',
        explicit=True,
    )


def choice_query(choice):
    """The query string of ``choice`` (always explicit, so it survives a change of defaults)."""
    params = QueryDict(mutable=True)
    params['src'] = '1'
    params.setlist('sel', list(choice.selections))
    params.setlist('uq', [str(v) for v in choice.user_queries])
    params.setlist('tag', [str(v) for v in choice.tags])
    params['nights'] = str(choice.nights)
    if choice.tns:
        params['tns'] = '1'
    return params.urlencode()


def _base(user):
    from YSE_App.views import search_results_queryset

    return search_results_queryset(user)


def recent_requests_queryset(user, nights, now=None):
    """Transients with a follow-up the viewer may see, requested in the last ``nights`` nights.

    A follow-up counts when it was created, or got another request (``TransientFollowupRequest``),
    within ``nights`` x 24 hours of ``now``.
    """
    from YSE_App.models import TransientFollowup, TransientFollowupRequest
    from YSE_App.services.visibility import filter_transient_followups_for_user

    now = now or timezone.now()
    since = now - datetime.timedelta(days=nights)
    recent_request = TransientFollowupRequest.objects.filter(followup_id=OuterRef('pk'), requested_at__gte=since)
    followups = TransientFollowup.objects.filter(Q(created_date__gte=since) | Q(Exists(recent_request)))
    visible = filter_transient_followups_for_user(followups, user)
    return _base(user).filter(pk__in=visible.values('transient_id'))


def tns_bright_queryset(user, now=None):
    """TNS-named transients whose last detection (as the viewer may see it) is brighter than
    :data:`TNS_MAG_LIMIT` and within the last :data:`TNS_DAYS` days."""
    from YSE_App.services.photstat_visibility import alias_stat_fields
    from YSE_App.services.visibility import tns_named_q

    now = now or timezone.now()
    since = now - datetime.timedelta(days=TNS_DAYS)
    qs, names = alias_stat_fields(_base(user).filter(tns_named_q()), user,
                                  ['last_detected_mag', 'last_detected_date'])
    return qs.filter(**{names['last_detected_mag'] + '__lt': TNS_MAG_LIMIT,
                        names['last_detected_date'] + '__gte': since})


def _user_query_source(user, user_query):
    """A dashboard panel's rows for its owner, by the Selection compiler's dashboard rules."""
    from YSE_App.models import Selection
    from YSE_App.services import selections as svc

    if user_query.query_id:
        title = user_query.query.title
        probe = Selection(owner=user, kind=Selection.KIND_SQL, query=user_query.query, name=title)
    elif user_query.python_query:
        title = str(user_query.python_query).replace('_', ' ')
        probe = Selection(owner=user, kind=Selection.KIND_FUNCTION, python_query=user_query.python_query,
                          name=title)
    else:
        return Source('uq:%d' % user_query.pk, 'Dashboard query #%d' % user_query.pk,
                      error='This dashboard panel has no query.', kind='uq')
    source = Source('uq:%d' % user_query.pk, 'Dashboard · %s' % title, kind='uq')
    try:
        source.queryset = svc.selection_queryset(probe, user)
    except svc.SelectionError as exc:
        source.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a failing panel must not break the other sources
        source.error = 'This query failed: %s' % exc.__class__.__name__
    return source


def build_sources(user, choice, now=None):
    """The chosen :class:`Source` list, in display order; sources the viewer may not use are dropped."""
    from YSE_App.list_context import tag_queryset
    from YSE_App.models import TransientTag, UserQuery
    from YSE_App.services import selections as svc

    sources = []
    if choice.selections:
        by_slug = {s.slug: s for s in svc.visible_selections(user).filter(slug__in=choice.selections)}
        for slug in choice.selections:
            selection = by_slug.get(slug)
            if selection is None:
                continue
            source = Source('sel:%s' % slug, 'Selection · %s' % selection.name, kind='sel')
            try:
                source.queryset = svc.selection_queryset(selection, user)
            except svc.SelectionError as exc:
                source.error = str(exc)
            sources.append(source)
    if choice.user_queries:
        panels = {q.pk: q for q in UserQuery.objects.filter(user=user, pk__in=choice.user_queries)
                  .select_related('query')}
        for pk in choice.user_queries:
            if pk in panels:
                sources.append(_user_query_source(user, panels[pk]))
    if choice.tags:
        tags = {t.pk: t for t in TransientTag.objects.filter(pk__in=choice.tags)}
        for pk in choice.tags:
            if pk in tags:
                sources.append(Source('tag:%d' % pk, 'Tag · %s' % tags[pk].name,
                                      queryset=tag_queryset(user, [pk]), kind='tag'))
    if choice.nights > 0:
        label = 'Requested in the last %d night%s' % (choice.nights, '' if choice.nights == 1 else 's')
        sources.append(Source('req', label, queryset=recent_requests_queryset(user, choice.nights, now=now),
                              kind='req'))
    if choice.tns:
        sources.append(Source('tns', 'TNS · brighter than %g mag in the last %d days' % (TNS_MAG_LIMIT, TNS_DAYS),
                              queryset=tns_bright_queryset(user, now=now), kind='tns'))
    return sources


def _usable(sources):
    return [s for s in sources if s.queryset is not None and not s.error]


def combined_queryset(user, sources):
    """The union of ``sources`` as one ``Transient`` queryset (each object once), search-page order."""
    usable = _usable(sources)
    base = _base(user)
    if not usable:
        return base.none()
    union = Q()
    for source in usable:
        union |= Q(pk__in=source.queryset.values('pk'))
    return base.filter(union)


def reasons_for(sources, transient_ids) -> Dict[int, List[str]]:
    """``{transient_id: [source label, ...]}`` for ``transient_ids`` (one query per source).

    The TNS source is named only for objects no other source brought in.
    """
    ids = {int(i) for i in transient_ids if i is not None}
    out: Dict[int, List[str]] = {i: [] for i in ids}
    if not ids:
        return out
    tns = None
    for source in _usable(sources):
        if source.kind == 'tns':
            tns = source
            continue
        for pk in source.queryset.filter(pk__in=ids).values_list('pk', flat=True):
            if source.label not in out[pk]:
                out[pk].append(source.label)
    if tns is not None:
        rest = [i for i, labels in out.items() if not labels]
        if rest:
            for pk in tns.queryset.filter(pk__in=rest).values_list('pk', flat=True):
                out[pk].append(tns.label)
    return out


def reasons_text(labels: Optional[List[str]]) -> str:
    """The "why it's here" text: the source labels joined with "; "."""
    return '; '.join(labels or [])
