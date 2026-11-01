"""Step through a list from the transient page (#850), for UI preview users only.

A list page adds its context to the links into the transient page:

* ``ls``: the list source, one of :data:`SOURCES` (``dash``, ``search``, ``tag``);
* ``lq``: the list page's own query string (filters, sort, page), unchanged;
* ``lt``: the dashboard tab (``dash`` only).

The transient page rebuilds that list with the list's own filter and sort, for the
viewing user, and reads only a window around the current object: the list page
the object is on plus one row either side (one COUNT and one LIMIT query), or, for
the unpaginated tag list, keyset queries on the primary key. Nothing about the
path through the list is stored: lists are stateless pools, so there is no
"seen", "skipped" or score anywhere.
"""

import copy
import logging

from django.db.models import Exists, OuterRef
from django.http import QueryDict
from django.urls import reverse
from django.utils.http import urlencode

logger = logging.getLogger(__name__)

SOURCES = ('dash', 'search', 'tag')
MAX_QUERY_LENGTH = 2000
MAX_LABEL_LENGTH = 160


def _sub_request(request, query):
    """A shallow copy of ``request`` whose GET is the list page's query string."""
    sub = copy.copy(request)
    sub.GET = QueryDict(query)
    return sub


def _clip(text, limit=MAX_LABEL_LENGTH):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _detail_url(slug, source, query, tab=''):
    params = {'ls': source, 'lq': query}
    if tab:
        params['lt'] = tab
    return '%s?%s' % (reverse('transient_detail', kwargs={'slug': slug}), urlencode(params))


def _with_page(query, page_param, page):
    params = QueryDict(query, mutable=True)
    if page <= 1:
        params.pop(page_param, None)
    else:
        params[page_param] = str(page)
    return params.urlencode()


def table_window(table, transient_pk):
    """``(position, count, prev_index_row, next_index_row)`` from a configured, paginated table.

    Rows are ``(pk, slug, absolute index)``. Reads the table's own ordered queryset,
    sliced to the current page plus one row before and after it.
    """
    page = getattr(table, 'page', None)
    if page is None:
        return None
    per_page = table.paginator.per_page
    start = (page.number - 1) * per_page
    low = max(start - 1, 0)
    rows = list(table.data.data.values_list('pk', 'slug')[low:start + per_page + 1])
    pks = [row[0] for row in rows]
    if transient_pk not in pks:
        return {'count': table.paginator.count, 'position': None, 'prev': None, 'next': None,
                'per_page': per_page}
    idx = pks.index(transient_pk)
    prev_row = (rows[idx - 1][0], rows[idx - 1][1], low + idx - 1) if idx > 0 else None
    next_row = (rows[idx + 1][0], rows[idx + 1][1], low + idx + 1) if idx + 1 < len(rows) else None
    return {'count': table.paginator.count, 'position': low + idx + 1, 'prev': prev_row,
            'next': next_row, 'per_page': per_page}


def _dash(request, transient, query, tab):
    from YSE_App import dashboard_preview
    from YSE_App.views import _DASHBOARD_STATUS_SECTIONS

    sections = {name.lower(): (title, name) for title, name in _DASHBOARD_STATUS_SECTIONS}
    if tab not in sections:
        return None
    title, statusname = sections[tab]
    sub = _sub_request(request, query)
    table = dashboard_preview.section_table(sub, statusname, dashboard_preview._status_by_name())
    window = table_window(table, transient.pk)
    if window is None:
        return None
    label = 'Dashboard · %s' % title
    text = (sub.GET.get('%s-ex' % dashboard_preview.FILTER_PREFIX) or '').strip()
    if text:
        label += ' (filter "%s")' % text
    back = '%s?%s#%s' % (reverse('dashboard'), query, tab) if query else '%s#%s' % (reverse('dashboard'), tab)
    return dict(window, label=label, back_url=back, page_param='%s-page' % tab)


def _search(request, transient, query, tab):
    from YSE_App.views import build_search_table

    sub = _sub_request(request, query)
    filterset, table, _per_page = build_search_table(sub)
    window = table_window(table, transient.pk)
    if window is None:
        return None
    try:
        chips = ['%s: %s' % (label, value) for _, label, value in filterset.active_filters()]
    except Exception:  # noqa: BLE001 - the label is a convenience
        chips = []
    label = 'Search' + (' (%s)' % '; '.join(chips) if chips else ' results')
    back = '%s?%s' % (reverse('search'), query) if query else reverse('search')
    return dict(window, label=label, back_url=back, page_param='page')


def tag_queryset(user, tag_ids, match_all=False):
    """The tag page's list (any of the tags, or all of them), as the tag page lists it."""
    from YSE_App.models import Transient
    from YSE_App.services.visibility import filter_listed_transients

    if match_all and tag_ids:
        base = Transient.objects.all()
        for tag_id in set(tag_ids):
            base = base.filter(Exists(Transient.tags.through.objects.filter(
                transient_id=OuterRef('pk'), transienttag_id=tag_id)))
    else:
        base = Transient.objects.filter(tags__in=tag_ids).distinct()
    return filter_listed_transients(user, base)


def _tag(request, transient, query, tab):
    from YSE_App.models import TransientTag

    params = QueryDict(query)
    try:
        tag_ids = sorted({int(v) for v in params.getlist('tagid')})
    except ValueError:
        return None
    if not tag_ids:
        return None
    match_all = params.get('match') == 'all'
    qs = tag_queryset(request.user, tag_ids, match_all).order_by('pk')
    # The tag list is not paginated: keyset queries on pk, so only the neighbours are read.
    count = qs.count()
    if not qs.filter(pk=transient.pk).exists():
        position, prev_row, next_row = None, None, None
    else:
        position = qs.filter(pk__lt=transient.pk).count() + 1
        prev_pk = qs.filter(pk__lt=transient.pk).order_by('-pk').values_list('pk', 'slug').first()
        next_pk = qs.filter(pk__gt=transient.pk).order_by('pk').values_list('pk', 'slug').first()
        prev_row = (prev_pk[0], prev_pk[1], position - 2) if prev_pk else None
        next_row = (next_pk[0], next_pk[1], position) if next_pk else None
    names = list(TransientTag.objects.filter(pk__in=tag_ids).order_by('name').values_list('name', flat=True))
    joiner = ' and ' if match_all else ' or '
    label = 'Tag%s %s' % ('s' if len(names) > 1 else '', joiner.join(names) or 'list')
    back = '%s?%s' % (reverse('transient_tags'), query)
    return {'count': count, 'position': position, 'prev': prev_row, 'next': next_row, 'per_page': None,
            'label': label, 'back_url': back, 'page_param': None}


_BUILDERS = {'dash': _dash, 'search': _search, 'tag': _tag}


def parse_list_params(get):
    """``(source, query, tab)`` from the transient page's query string, or None."""
    source = get.get('ls', '')
    if source not in SOURCES:
        return None
    query = get.get('lq', '')
    if len(query) > MAX_QUERY_LENGTH or any(ch in query for ch in '#\r\n'):
        return None
    tab = get.get('lt', '').lower()
    if source == 'dash' and not tab:
        return None
    return source, query.lstrip('?'), tab


def list_step_context(request, transient):
    """Context for the "4 of 23" stepper, or None when the page was not opened from a list."""
    parsed = parse_list_params(request.GET)
    if parsed is None:
        return None
    source, query, tab = parsed
    try:
        info = _BUILDERS[source](request, transient, query, tab)
    except Exception:  # noqa: BLE001 - a bad list query must never break the transient page
        logger.exception('list stepping: could not rebuild the %s list', source)
        return None
    if info is None:
        return None

    def step_url(row):
        if row is None:
            return None
        pk, slug, index = row
        step_query = query
        if info['page_param'] and info['per_page']:
            step_query = _with_page(query, info['page_param'], index // info['per_page'] + 1)
        return _detail_url(slug, source, step_query, tab)

    label = _clip(info['label'])
    return {
        'source': source,
        'label': label,
        'position': info['position'],
        'count': info['count'],
        'prev_url': step_url(info['prev']),
        'next_url': step_url(info['next']),
        'back_url': info['back_url'],
        'default_comment': 'In list: %s' % label,
    }
