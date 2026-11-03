"""Combined candidate list page (#849), for UI preview users only (404 otherwise).

``GET combined-list/``: the union of the viewer's chosen sources (Selections, dashboard
queries, tags, recent follow-up requests, bright TNS objects), each object once, with a
"Why it's here" column. The sources are in the query string (see
``services.combined_list``); nothing is stored on the server. Links into the transient page
carry the list context (#850, source ``comb``), so J/K step through this list and the
follow-up form's default comment names the object's sources (#955).
"""

from __future__ import annotations

import django_tables2 as tables
from django.contrib.auth.decorators import login_required
from django.http import Http404, QueryDict
from django.shortcuts import render
from django.utils.html import format_html
from django.views.decorators.http import require_GET
from django_tables2 import RequestConfig

from YSE_App.common.ui_preview import ui_preview_enabled
from YSE_App.services import combined_list as svc
from YSE_App.table_utils import (
    SearchTransientTable,
    _page_visible_stats,
    _scoped_viewer,
    _table_viewer,
)

#: Parameters of the page itself (the table's paging and sorting).
PAGE_KEYS = ('page', 'per_page', 'sort')
PER_PAGE_CHOICES = (25, 50, 100, 200)


def _stat_for(record, table):
    viewer = _scoped_viewer(_table_viewer(table))
    if viewer is not None:
        return _page_visible_stats(record, table, 'pk', viewer).get(record.pk)
    try:
        return record.photstat
    except Exception:  # noqa: BLE001 - no stat row yet
        return None


def trend_text(stat):
    """``(symbol, text)`` for the light-curve trend of a stat row, or ``None``.

    Rising: the newest detection is the brightest. Fading: the decay rate since the peak.
    """
    if stat is None or getattr(stat, 'last_detected_mag', None) is None:
        return None
    if (getattr(stat, 'num_det_global', 0) or 0) <= 1:
        return ('·', 'one detection')
    peak_mjd, last_mjd = getattr(stat, 'peak_mjd', None), getattr(stat, 'last_detected_mjd', None)
    if peak_mjd is not None and last_mjd is not None and abs(last_mjd - peak_mjd) < 0.01:
        return ('↑', 'rising (newest detection is the brightest)')
    decay = getattr(stat, 'decay_rate', None)
    if decay is not None:
        return ('↓', 'fading %.2f mag/day since peak' % decay)
    return None


class CombinedListTable(SearchTransientTable):
    """The search table's cells, with "Why it's here", trend and host. ``reasons`` is set per page."""

    reasons = tables.Column(accessor='pk', verbose_name="Why it's here", orderable=False, empty_values=())
    trend = tables.Column(accessor='pk', verbose_name='Trend', orderable=False, empty_values=())
    host_name = tables.Column(accessor='host', verbose_name='Host', orderable=True, order_by='host__name',
                              empty_values=(None, ''))

    def render_reasons(self, record):
        labels = (getattr(self, 'reason_map', None) or {}).get(record.pk) or []
        if not labels:
            return '-'
        return format_html('<span class="yse-comb-why">{}</span>', svc.reasons_text(labels))

    def render_trend(self, record):
        trend = trend_text(_stat_for(record, self))
        if trend is None:
            return '-'
        return format_html('<span class="yse-comb-trend" title="{}">{} <small>{}</small></span>',
                           trend[1], trend[0], trend[1].split(' (')[0])

    def render_host_name(self, value):
        name = getattr(value, 'name', None)
        return name or 'unnamed host'

    class Meta(SearchTransientTable.Meta):
        empty_text = 'None of the chosen sources has objects you can see. Choose more sources above.'
        fields = ('select', 'name_string', 'reasons', 'recent_mag', 'trend', 'recent_magdate', 'best_spec_class',
                  'best_redshift', 'host_name', 'ra_string', 'dec_string', 'disc_date_string', 'favorite')
        sequence = fields
        exclude = ('separation', 'gal_b', 'annotation_value', 'mw_ebv', 'obs_group', 'peak_mag',
                   'status_string', 'interest_count')
        attrs = dict(SearchTransientTable.Meta.attrs, id='combined_list_tbl')


def combined_table(request):
    """``(table, sources, choice)`` for ``request.GET``: paginated, sorted, reasons for the page.

    Shared by the page and the transient page's list stepping, so both see the same rows in
    the same order.
    """
    user = request.user
    choice = svc.parse_choice(request.GET, user)
    sources = svc.build_sources(user, choice)
    queryset = svc.combined_queryset(user, sources)
    try:
        per_page = int(request.GET.get('per_page', PER_PAGE_CHOICES[0]))
    except (TypeError, ValueError):
        per_page = PER_PAGE_CHOICES[0]
    if per_page not in PER_PAGE_CHOICES:
        per_page = PER_PAGE_CHOICES[0]
    table = CombinedListTable(queryset, prefix='')
    RequestConfig(request, paginate={'per_page': per_page}).configure(table)
    page = getattr(table, 'page', None)
    ids = [row.record.pk for row in page.object_list] if page is not None else []
    table.reason_map = svc.reasons_for(sources, ids)
    table.sources = sources
    return table, sources, choice


@login_required
@require_GET
def combined_list(request):
    if not ui_preview_enabled(request):
        raise Http404('The combined candidate list is part of the new layout preview.')
    from YSE_App.models import TransientTag, UserQuery
    from YSE_App.services import selections as sel_svc
    from YSE_App.services.observable_tonight import MAX_OBJECTS

    table, sources, choice = combined_table(request)
    count = table.paginator.count
    obs_blocked = ''
    if count > MAX_OBJECTS:
        obs_blocked = ('this list has %d objects; it works on at most %d. Choose fewer sources first.'
                       % (count, MAX_OBJECTS))
    list_query = QueryDict(mutable=True)
    for key in svc.CHOICE_KEYS + PAGE_KEYS:
        if key in request.GET:
            list_query.setlist(key, request.GET.getlist(key))
    user = request.user
    panels = []
    for panel in UserQuery.objects.filter(user=user).select_related('query').order_by('pk'):
        title = panel.query.title if panel.query_id else (panel.python_query or '').replace('_', ' ')
        if title:
            panels.append((panel.pk, title))
    return render(request, 'YSE_App/combined_list.html', {
        'table': table,
        'sources': sources,
        'choice': choice,
        'result_count': count,
        'obs_blocked': obs_blocked,
        'list_query': list_query.urlencode(),
        'choice_query': svc.choice_query(choice),
        'selections': list(sel_svc.visible_selections(user).order_by('name', 'pk')),
        'panels': panels,
        'tags': list(TransientTag.objects.order_by('name')),
        'max_nights': svc.MAX_NIGHTS,
        'tns_mag': svc.TNS_MAG_LIMIT,
        'tns_days': svc.TNS_DAYS,
    })
