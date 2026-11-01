"""Tabbed main dashboard for preview users (#859).

Only users with the UI preview switch on (``YSE_App.common.ui_preview``) see
this page; everyone else gets the stacked sections of ``views.dashboard``.

* The seven status sections become tabs, each with a count. The counts are
  one ``GROUP BY status_id`` query on the indexed status column (with the
  shared filter applied), not a query per tab.
* Only the open tab's table is loaded (``dashboard/section/<status>/``); the
  others load the first time they are opened.
* One filter box (``dash-ex``) applies to every tab and to the counts.
* The table shows eight columns by default; the rest are rendered hidden and
  shown from the Columns menu (stored in the browser).
* Statuses are set in bulk from ticked rows, through the same
  ``PATCH /api/transients/<id>/`` the per-row menu uses, so permissions,
  ``modified_by`` (#886) and the change log are unchanged.
"""

import django_tables2 as tables
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.utils.html import format_html
from django_tables2 import RequestConfig

from YSE_App.models import Transient, TransientStatus
from YSE_App.services.visibility import filter_listed_transients
from YSE_App.table_utils import (
    LastDetColumn,
    TransientFilter,
    TransientTable,
    annotate_dashboard_transient_fields,
)

FILTER_PREFIX = 'dash'

# Tab keys tried, in order, when the browser has no remembered tab.
DEFAULT_TAB_PREFERENCE = ('following',)
FALLBACK_TAB = 'followuprequested'

# (column name, menu label, shown by default). ``select`` and ``name_string``
# are always shown and are not in the menu.
COLUMN_CHOICES = (
    ('favorite', 'Favorite', True),
    ('disc_date_string', 'Disc. date', True),
    ('recent_mag', 'Last mag (age)', True),
    ('peak_mag', 'Peak mag', True),
    ('best_spec_class', 'Class', True),
    ('best_redshift', 'Redshift', True),
    ('status_string', 'Status', True),
    ('ra_string', 'RA', False),
    ('dec_string', 'Dec', False),
    ('recent_magdate', 'Last obs.', False),
    ('mw_ebv', 'MW E(B-V)', False),
    ('obs_group', 'Group', False),
    ('interest_count', 'Papers', False),
    ('ps_score', 'PS score', False),
)
DEFAULT_COLUMNS = tuple(name for name, _, shown in COLUMN_CHOICES if shown)


def preview_enabled(request):
    from YSE_App.common.ui_preview import ui_preview_enabled

    return ui_preview_enabled(request)


def _age_text(days):
    if days < 1:
        return '<1d'
    return '%dd' % int(days)


class LastDetAgeColumn(LastDetColumn):
    """"Last mag": the last detection's magnitude and band plus its age in days."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('verbose_name', 'Last mag')
        super().__init__(*args, **kwargs)

    def render(self, record, table, bound_column):
        from YSE_App.models.phot_stat_models import datetime_to_mjd

        text = super().render(record, table, bound_column)
        info = self._info(record, table)
        if not info or info['det_mag'] is None or info.get('det_mjd') is None:
            return text
        days = datetime_to_mjd(timezone.now()) - info['det_mjd']
        return format_html('{} <small class="text-muted yse-dash-age" title="Days since this detection">{}</small>',
                           text, _age_text(max(days, 0.0)))


STATUS_LABEL_TEMPLATE = (
    '<span id="{{ record.id }}_status_name" class="yse-dash-status" '
    'title="{{ record.status|status_title }}">{{ record.status|status_label }}</span>'
)


class DashboardTabTable(TransientTable):
    """``TransientTable`` for the tabbed dashboard: a tick box, a plain status label, and the last mag's age."""

    select = tables.Column(accessor='pk', verbose_name='Select', orderable=False, empty_values=())
    recent_mag = LastDetAgeColumn()
    status_string = tables.TemplateColumn(STATUS_LABEL_TEMPLATE, verbose_name='Status', orderable=True,
                                          order_by='status')
    ps_score = tables.Column(accessor='point_source_probability', verbose_name='PS Score', orderable=True)

    def render_select(self, record):
        return format_html(
            '<input type="checkbox" class="yse-dash-select" value="{}" aria-label="Select {}">',
            record.pk, record.name)

    class Meta(TransientTable.Meta):
        template_name = 'YSE_App/dashboard_v2_table.html'
        fields = ('select', 'name_string') + tuple(name for name, _, _ in COLUMN_CHOICES)
        sequence = fields
        attrs = {
            'th': TransientTable.Meta.attrs['th'],
            'class': 'table table-bordered table-hover yse-dash-table',
        }


def _sections():
    from YSE_App.views import _DASHBOARD_STATUS_SECTIONS

    return _DASHBOARD_STATUS_SECTIONS


def _status_by_name():
    return {s.name: s for s in TransientStatus.objects.filter(name__in=[name for _, name in _sections()])}


def _listed(request, condition):
    return filter_listed_transients(request.user, Transient.objects.filter(condition))


def _filtered(request, queryset):
    return TransientFilter(request.GET, request=request, queryset=queryset, prefix=FILTER_PREFIX).qs


def tab_counts(request, status_by_name):
    """``{tab key: rows}`` for every tab, filtered like the tables, in one GROUP BY query."""
    names = [name for _, name in _sections()]
    present = [status_by_name[name].pk for name in names if name in status_by_name]
    condition = Q(status_id__in=present)
    if len(present) < len(names):
        # A missing status lists the transients without one, like views._dashboard_section_tuple.
        condition |= Q(status__isnull=True)
    rows = (_filtered(request, _listed(request, condition))
            .order_by().values('status_id').annotate(n=Count('pk', distinct=True)))
    by_status = {row['status_id']: row['n'] for row in rows}
    counts = {}
    for name in names:
        status = status_by_name.get(name)
        counts[name.lower()] = by_status.get(status.pk if status else None, 0)
    return counts


def default_tab(counts):
    """The tab to open when the browser remembers none: Following if it has rows, else Follow-up requested."""
    for key in DEFAULT_TAB_PREFERENCE:
        if counts.get(key):
            return key
    return FALLBACK_TAB


def section_table(request, statusname, status_by_name):
    status = status_by_name.get(statusname)
    condition = Q(status=status) if status else Q(status=None)
    queryset = annotate_dashboard_transient_fields(_listed(request, condition).order_by('-disc_date'))
    prefix = statusname.lower()
    table = DashboardTabTable(_filtered(request, queryset), prefix=prefix,
                              attrs=dict(DashboardTabTable.Meta.attrs, id='yse-dash-tbl-' + prefix))
    from YSE_App.views import dashboard_per_page

    RequestConfig(request, paginate={'per_page': dashboard_per_page(request)}).configure(table)
    return table


def dashboard(request, extra_context):
    """The tabbed dashboard page; tables come from :func:`section`."""
    status_by_name = _status_by_name()
    counts = tab_counts(request, status_by_name)
    tabs = [{'key': name.lower(), 'title': title, 'count': counts[name.lower()]} for title, name in _sections()]
    context = dict(extra_context)
    context.update({
        'tabs': tabs,
        'default_tab': default_tab(counts),
        'dash_filter': TransientFilter(request.GET, request=request, queryset=Transient.objects.none(),
                                       prefix=FILTER_PREFIX),
        'column_choices': COLUMN_CHOICES,
        'default_columns': ' '.join(DEFAULT_COLUMNS),
        'all_transient_statuses': TransientStatus.objects.order_by('name'),
    })
    return render(request, 'YSE_App/dashboard_v2.html', context)


def section(request, statusname, status_by_name):
    """One tab's table (AJAX fragment)."""
    table = section_table(request, statusname, status_by_name)
    return render(request, 'YSE_App/dashboard_v2_section.html', {'table': table})
