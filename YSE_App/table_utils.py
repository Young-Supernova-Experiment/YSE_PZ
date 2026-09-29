from django.shortcuts import render
from .models import *
from . import view_utils
import django_tables2 as tables
from django_tables2 import RequestConfig
from django.db.models import F, Q
from django.db.models.functions import Length, Substr
from django.db.models import Count, OuterRef, Prefetch, Subquery, Value, Max, Min
from django.db.models.functions import Greatest, Coalesce
from django_tables2 import A
from django.db import models
from django.db.models.query import QuerySet
from .data import PhotometryService
import time
import django_filters
from astropy.coordinates import get_moon, SkyCoord
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.dates import DateFormatter
from matplotlib import rcParams
from django.db.models.expressions import RawSQL
from .common.magnitude_format import format_magnitude
# Circular: table_utils is imported from yse_pa during views import, before
# follow-up request helpers are safe to load. Import in render methods.
rcParams['figure.figsize'] = (7,7)


def stable_order_by(queryset, field, is_descending):
    """Append pk tie-breaker so pagination stays stable (Fixes #35)."""
    if is_descending:
        return queryset.order_by(f'-{field}', '-pk')
    return queryset.order_by(field, 'pk')


def _recent_phot_subqueries(transient_ref):
    """(recent_mag, recent_magdate) scalar subqueries for the transient at ``transient_ref``.

    Matches PhotometryService / Transient.recent_mag(): flagged bad data excluded.
    """
    recent_phot = TransientPhotData.objects.filter(
        photometry__transient=OuterRef(transient_ref),
    ).exclude(data_quality__isnull=False)
    recent_mag_sq = (
        recent_phot.filter(mag__isnull=False)
        .order_by('-obs_date')
        .values('mag')[:1]
    )
    recent_magdate_sq = recent_phot.order_by('-obs_date').values('obs_date')[:1]
    return Subquery(recent_mag_sq), Subquery(recent_magdate_sq)


def annotate_followup_recent_mag(qs):
    """recent_mag for each TransientFollowup row, one subquery instead of a query per row."""
    if 'recent_mag' in qs.query.annotations:
        return qs
    recent_mag, _ = _recent_phot_subqueries('transient_id')
    return qs.annotate(recent_mag=recent_mag)


class FollowupRecentMagMixin:
    """Follow-up tables: recent_mag from the annotation, formatted like Transient.recent_mag()."""

    def __init__(self, data, *args, **kwargs):
        if isinstance(data, QuerySet):
            data = annotate_followup_recent_mag(data)
        super().__init__(data, *args, **kwargs)

    def render_recent_mag(self, value):
        return '%.2f' % value

    def order_recent_mag(self, queryset, is_descending):
        queryset = annotate_followup_recent_mag(queryset)
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)


_REQUESTED_FOLLOWUP_STATUSES = ('Requested', 'InProcess')
_SUCCESSFUL_FOLLOWUP_STATUSES = ('Successful',)
_RESOURCE_SELECT_RELATED = (
    'status',
    'too_resource__telescope',
    'classical_resource__telescope',
    'queued_resource__telescope',
)


def prefetch_followup_resources(qs):
    """
    Prefetch what the Req. Followup / Followed By / Followup Comments columns read.

    Without it each YSE table row ran six TransientFollowup queries (three
    resource types x two status groups) plus one per follow-up for comments.
    """
    followups = TransientFollowup.objects.select_related(*_RESOURCE_SELECT_RELATED).order_by('-id')
    return qs.prefetch_related(
        Prefetch('transientfollowup_set', queryset=followups, to_attr='resource_followups'),
        Prefetch(
            'resource_followups__requests',
            queryset=TransientFollowupRequest.objects.select_related('requestor').order_by(
                'requested_at', 'id'
            ),
        ),
    )


def _resource_followups(record):
    followups = getattr(record, 'resource_followups', None)
    if followups is None:  # queryset was not prefetched: fall back to one query per row
        followups = list(
            TransientFollowup.objects.filter(transient__id=record.pk)
            .select_related(*_RESOURCE_SELECT_RELATED)
            .prefetch_related('requests__requestor')
        )
    return followups


def followup_resource_names(record, status_names):
    """Sorted, de-duplicated telescope names of follow-ups in ``status_names``."""
    names = []
    for followup in _resource_followups(record):
        if followup.status.name not in status_names:
            continue
        for resource in (followup.too_resource, followup.classical_resource, followup.queued_resource):
            if resource is not None:
                names.append(resource.telescope.name)
    return ', '.join(np.unique(names))


def followup_comments_text(record):
    from YSE_App.services.followup_requests import format_comments

    comments = []
    for followup in _resource_followups(record):
        text = format_comments(followup)
        if text:
            comments.append(text)
    return '; '.join(comments)


class TargetVisibilityMixin:
    """
    Rise/Set/Moon Angle columns for one observer and one night.

    The moon position is computed once per table (it was recomputed per row)
    and rise/set times are solved for every target on the current page in one
    vectorised astroplan call the first time a row asks, then served from a
    dict keyed by (ra, dec) string. Values are formatted exactly as before.
    """

    horizon = 18 * u.deg

    def _set_observer(self, telescope, obs_date):
        location = EarthLocation.from_geodetic(
            telescope.longitude * u.deg, telescope.latitude * u.deg, telescope.elevation * u.m)
        self.tel = Observer(location=location, timezone="UTC")
        self.tme = Time(str(obs_date).split()[0])
        self._moon = None
        self._rise_set = {}

    @property
    def moon(self):
        if self._moon is None:
            self._moon = get_moon(self.tme)
        return self._moon

    @staticmethod
    def _time_str(t):
        # astroplan masks targets that never cross the horizon (NaN before v0.7)
        value = t.value
        if np.ma.is_masked(value) or value != value:
            return None
        return t.isot.split('T')[-1].split('.')[0]

    def _page_coords(self, bound_column, first):
        """(ra, dec) strings for the rows about to render, ``first`` included."""
        keys = [first]
        rows = self.page.object_list if getattr(self, 'page', None) is not None else self.rows
        try:
            for row in rows:
                coord = bound_column.accessor.resolve(row.record)
                key = (coord[0], coord[1])
                if key not in keys and key not in self._rise_set:
                    keys.append(key)
        except Exception:
            pass
        return keys

    def _solve_rise_set(self, keys):
        sc = SkyCoord(['%s %s' % k for k in keys], unit=(u.hourangle, u.deg))
        rise = self.tel.target_rise_time(self.tme, sc, horizon=self.horizon, which="previous")
        sett = self.tel.target_set_time(self.tme, sc, horizon=self.horizon, which="previous")
        rise, sett = rise.reshape(-1), sett.reshape(-1)
        for i, key in enumerate(keys):
            self._rise_set[key] = (self._time_str(rise[i]), self._time_str(sett[i]))

    def _rise_set_for(self, value, bound_column):
        key = (value[0], value[1])
        if key not in self._rise_set:
            keys = self._page_coords(bound_column, key)
            try:
                self._solve_rise_set(keys)
            except Exception:
                self._rise_set = {}
                self._solve_rise_set([key])
        return self._rise_set[key]

    def render_rise_time(self, value, bound_column):
        return self._rise_set_for(value, bound_column)[0]

    def render_set_time(self, value, bound_column):
        return self._rise_set_for(value, bound_column)[1]

    def render_moon_angle(self, value):
        sc = SkyCoord('%s %s' % (value[0], value[1]), unit=(u.hourangle, u.deg))
        return '%.1f' % sc.separation(self.moon).deg


class MagnitudeColumn(tables.Column):
    """Dashboard magnitude column with two decimal places."""

    def render(self, value):
        return format_magnitude(value)


class LastObsDateColumn(tables.Column):
    """Dashboard 'Last Obs. Date' column rendered as MM/DD/YYYY.

    ``annotate_dashboard_transient_fields`` supplies a raw datetime via a
    Subquery, whereas ``Transient.recent_magdate()`` (used on un-annotated
    querysets and on production before the annotation) returns a string that
    is already ``strftime('%m/%d/%Y')``-formatted. Format the datetime here so
    both paths render the same way.
    """

    DATE_FORMAT = '%m/%d/%Y'

    def render(self, value):
        if hasattr(value, 'strftime'):
            return value.strftime(self.DATE_FORMAT)
        return value


class TransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')

    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)


    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }

class FieldTransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    ztf_field = tables.Column(accessor='nearest_ztf_field',
                              verbose_name='ZTF Field',orderable=False)
    ztf_sep = tables.Column(accessor='nearest_ztf_field_sep',
                            verbose_name='ZTF Sep.',orderable=False)
    get_yse_pointings = tables.TemplateColumn(
        "<a href=\"{% url 'yse_pointings' record.nearest_ztf_field record.name %}\" target='_blank'>Get Pointings</a>",
        orderable=False)


    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)


    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }

class AdjustFieldTransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    yse_field = tables.Column(accessor='nearest_yse_field',
                              verbose_name='YSE Field',orderable=False)
    yse_sep = tables.Column(accessor='nearest_yse_field_sep',
                            verbose_name='YSE Sep.',orderable=False)
    get_yse_pointings = tables.TemplateColumn(
        "<a href=\"{% url 'adjust_yse_pointings' record.nearest_yse_field record.name %}\" target='_blank'>Get Pointings</a>",
        orderable=False,verbose_name='Get YSE Pointings')


    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)


    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }


class YSETransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    requested_followup_resources = tables.Column(accessor='pk',verbose_name='Req. Followup')
    successful_followup_resources = tables.Column(accessor='pk',verbose_name='Followed By')
    followup_comments = tables.Column(accessor='pk',verbose_name='Followup Comments')
    context_class = tables.Column(accessor='context_class',
                                  verbose_name='QUB Class.',orderable=True)


    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name_yse" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)

    def render_requested_followup_resources(self, value, record):
        return followup_resource_names(record, _REQUESTED_FOLLOWUP_STATUSES)

    def render_successful_followup_resources(self, value, record):
        return followup_resource_names(record, _SUCCESSFUL_FOLLOWUP_STATUSES)

    def render_followup_comments(self, value, record):
        return followup_comments_text(record)


    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }

class YSEFullTransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    context_class = tables.Column(accessor='context_class',
                                  verbose_name='QUB Class.',orderable=True)


    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name_yse" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)

    def render_requested_followup_resources(self, value, record):
        return followup_resource_names(record, _REQUESTED_FOLLOWUP_STATUSES)

    def render_successful_followup_resources(self, value, record):
        return followup_resource_names(record, _SUCCESSFUL_FOLLOWUP_STATUSES)


    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','context_class','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }

class YSERisingTransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    context_class = tables.Column(accessor='context_class',
                                  verbose_name='QUB Class.',orderable=True)
    dm_g = tables.TemplateColumn('{{ record.dm_g|floatformat:3 }}',
                                 verbose_name='Delta g',orderable=True,order_by='dm_g')
    dm_r = tables.TemplateColumn('{{ record.dm_r|floatformat:3 }}',
                                 verbose_name='Delta r',orderable=True,order_by='dm_r')
    dm_i = tables.TemplateColumn('{{ record.dm_i|floatformat:3 }}',
                                 verbose_name='Delta i',orderable=True,order_by='dm_i')


    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name_yse" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)

    def render_requested_followup_resources(self, value, record):
        return followup_resource_names(record, _REQUESTED_FOLLOWUP_STATUSES)

    def render_successful_followup_resources(self, value, record):
        return followup_resource_names(record, _SUCCESSFUL_FOLLOWUP_STATUSES)


    def render_dt(self,value):

        return '%.2f'%value/24/60/60

    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','context_class','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }


class NewTransientTable(tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = MagnitudeColumn(accessor='recent_mag',
                               verbose_name='Last Mag',orderable=True)
    recent_magdate = LastObsDateColumn(accessor='recent_magdate',
                               verbose_name='Last Obs. Date',orderable=True)
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    ps_score = tables.Column(accessor='point_source_probability',
                             verbose_name='PS Score',orderable=True)

    #mw_ebv = tables.Column(accessor='mw_ebv',
    #						   verbose_name='MW E(B-V)',orderable=True)
    mw_ebv = tables.TemplateColumn("""{% if record.mw_ebv %}
{% if record.mw_ebv >= 0.2 %}
&nbsp;<b class="text-red">{{ record.mw_ebv }}</b>
{% else %}
{{ record.mw_ebv }}
{% endif %}
{% else %}
-
{% endif %}""",
                                   verbose_name='MW E(B-V)',orderable=True,order_by='mw_ebv')


    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Status',orderable=True,order_by='status')


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)

    def order_recent_mag(self, queryset, is_descending):

        raw_query = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

        queryset = queryset.annotate(recent_mag=RawSQL(raw_query,()))
        return (stable_order_by(queryset, 'recent_mag', is_descending), True)

    def order_recent_magdate(self, queryset, is_descending):

        all_phot = TransientPhotometry.objects.values('transient').filter(transient__in = queryset)
        phot_ids = all_phot.values('id')

        phot_data_query = Q(transientphotometry__id__in=phot_ids)
        queryset = queryset.annotate(
            recent_magdate=Max('transientphotometry__transientphotdata__obs_date',filter=phot_data_query), #,filter=phot_data_query
        )
        return (stable_order_by(queryset, 'recent_magdate', is_descending), True)


    class Meta:
        model = Transient
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','ps_score','status_string')

        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            'class': 'table table-bordered table-hover',
            'id': 'k2_transient_tbl',
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            "order": [[ 3, "desc" ]],
        }


class FollowupTable(FollowupRecentMagMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.transient.slug %}\">{{ record.transient.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='transient__name')
    ra_string = tables.Column(accessor='transient.CoordString.0',
                              verbose_name='RA',orderable=True,order_by='transient.ra')
    dec_string = tables.Column(accessor='transient.CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='transient.dec')
    recent_mag = tables.Column(accessor='recent_mag',
                               verbose_name='Recent Mag',orderable=True)


    observation_window = tables.Column(accessor='observation_window',
                              verbose_name='Observation Window',orderable=True,order_by='valid_start')

    action = tables.TemplateColumn("<a target=\"_blank\" href=\"{% url 'admin:YSE_App_transientfollowup_change' record.id %}\">Edit</a>",
                                   verbose_name='Action',orderable=False)

    status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:5px;" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_followup_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Followup Status',orderable=True,order_by='status')



    #disc_mag = tables.Column(accessor='disc_mag',
    #						 verbose_name='Disc. Mag',orderable=True)

    def __init__(self,*args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_columns['transient.status'].verbose_name = 'Transient Status'
        #self.base_columns['status'].verbose_name = 'Followup Status'

    class Meta:
        model = TransientFollowup
        fields = ('name_string','ra_string','dec_string','recent_mag','transient.status','observation_window','action')
        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            'class': 'table table-bordered table-hover',
            "order": [[ 2, "desc" ]],
        }

class ObsNightFollowupTable(FollowupRecentMagMixin, TargetVisibilityMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.transient.slug %}\">{{ record.transient.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='transient__name')
    ra_string = tables.Column(accessor='transient.CoordString.0',
                              verbose_name='RA',orderable=True,order_by='transient.ra')
    dec_string = tables.Column(accessor='transient.CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='transient.dec')
    recent_mag = tables.Column(accessor='recent_mag',
                               verbose_name='Recent Mag',orderable=True)


    #observation_window = tables.Column(accessor='observation_window',
    #						  verbose_name='Observation Window',orderable=True,order_by='valid_start')

    rise_time = tables.Column(verbose_name='Rise Time (UT)',orderable=False,accessor='transient.CoordString')
    set_time = tables.Column(verbose_name='Set Time (UT)',orderable=False,accessor='transient.CoordString')
    moon_angle = tables.Column(verbose_name='Moon Angle',orderable=False,accessor='transient.CoordString')
    requestors = tables.Column(verbose_name='Requestors',orderable=False,accessor='id')
    priority = tables.Column(verbose_name='Priority',orderable=True,accessor='priority')
    comment = tables.Column(verbose_name='Comments',orderable=False,accessor='id')

    transient_status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.transient.id }}_status_name" class="dropbtn">{{ record.transient.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.transient.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Transient Status',orderable=True,order_by='status')


    followup_status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_followup_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="followupStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Followup Status',orderable=True,order_by='status')



    #disc_mag = tables.Column(accessor='disc_mag',
    #						 verbose_name='Disc. Mag',orderable=True)

    def __init__(self,*args, classical_obs_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_observer(classical_obs_date.resource.telescope, classical_obs_date.obs_date)

    def render_airmass(self, value):
        from astroplan.plots import plot_airmass

    def render_requestors(self, value, record):
        from YSE_App.services.followup_requests import format_requestors

        return format_requestors(record)

    def render_comment(self, value, record):
        from YSE_App.services.followup_requests import format_comments

        return format_comments(record)

    class Meta:
        model = TransientFollowup
        fields = ('name_string','ra_string','dec_string','recent_mag',
                  'rise_time','set_time','moon_angle','transient_status_string',
                  'requestors','priority','comment')
        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            'class': 'table table-bordered table-hover',
            "order": [[ 2, "desc" ]],
        }

class ToOFollowupTable(FollowupRecentMagMixin, TargetVisibilityMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.transient.slug %}\">{{ record.transient.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='transient.CoordString.0',
                              verbose_name='RA',orderable=True,order_by='transient.ra')
    dec_string = tables.Column(accessor='transient.CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='transient.dec')
    recent_mag = tables.Column(accessor='recent_mag',
                               verbose_name='Recent Mag',orderable=True)


    #observation_window = tables.Column(accessor='observation_window',
    #						  verbose_name='Observation Window',orderable=True,order_by='valid_start')

    rise_time = tables.Column(verbose_name='Rise Time (UT)',orderable=False,accessor='transient.CoordString')
    set_time = tables.Column(verbose_name='Set Time (UT)',orderable=False,accessor='transient.CoordString')
    moon_angle = tables.Column(verbose_name='Moon Angle',orderable=False,accessor='transient.CoordString')
    requestors = tables.Column(verbose_name='Requestors',orderable=False,accessor='id')
    priority = tables.Column(verbose_name='Priority',orderable=True,accessor='priority')
    comment = tables.Column(verbose_name='Comments',orderable=False,accessor='id')

    transient_status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.transient.id }}_status_name" class="dropbtn">{{ record.transient.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_transient_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.transient.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Transient Status',orderable=True,order_by='status')


    followup_status_string = tables.TemplateColumn("""<div class="btn-group">
<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
                                            <span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
                                        </button>
                                        <ul class="dropdown-menu">
                                            {% for status in all_followup_statuses %}
                                                    <li><a data-status_id="{{ status.id }}" data-status_name="{{ status.name }}" transient_id="{{ record.id }}" class="followupStatusChange" href="#">{{ status.name }}</a></li>
                                            {% endfor %}
                                        </ul>
</div>""",
                                          verbose_name='Followup Status',orderable=True,order_by='status')

    
    def __init__(self,*args, too_resource=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_observer(too_resource.telescope, datetime.datetime.now())

    def render_airmass(self, value):
        from astroplan.plots import plot_airmass

    def render_requestors(self, value, record):
        from YSE_App.services.followup_requests import format_requestors

        return format_requestors(record)

    def render_comment(self, value, record):
        from YSE_App.services.followup_requests import format_comments

        return format_comments(record)

    class Meta:
        model = TransientFollowup
        fields = ('name_string','ra_string','dec_string','recent_mag',
                  'rise_time','set_time','moon_angle','transient_status_string',
                  'requestors','priority','comment')
        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            'class': 'table table-bordered table-hover',
            "order": [[ 2, "desc" ]],
        }



class YSEObsNightTable(TargetVisibilityMixin, tables.Table):

    field_id = tables.Column(accessor="survey_field.field_id",verbose_name="Field ID",order_by="survey_field.field_id")
    ra_string = tables.Column(accessor='survey_field.CoordString.0',
                              verbose_name='RA',orderable=True,order_by='survey_field.ra_dec')
    dec_string = tables.Column(accessor='survey_field.CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='survey_field.dec_cen')
    band = tables.Column(accessor='photometric_band.name',
                         verbose_name='band',orderable=True)

    rise_time = tables.Column(verbose_name='Rise Time (UT)',orderable=False,accessor='survey_field.CoordString')
    set_time = tables.Column(verbose_name='Set Time (UT)',orderable=False,accessor='survey_field.CoordString')
    moon_angle = tables.Column(verbose_name='Moon Angle',orderable=False,accessor='survey_field.CoordString')
    selection = tables.CheckBoxColumn(accessor="pk",attrs = { "th__input":
                                                              {"onclick": "toggle(this)"}})
    status_str = tables.TemplateColumn("<span id='{{record.id}}_status'>{{record.status.name}}</span>",verbose_name="status")
    #status_string = tables.TemplateColumn("""<div class="btn-group">
#<button style="margin-bottom:-5px;margin-top:-10px;padding:1px 5px" type="button" class="btn btn-secondary btn-sm dropdown-toggle" data-toggle="dropdown">
    #										<span id="{{ record.id }}_status_name" class="dropbtn">{{ record.status }}</span>
    #									</button>
    #									<ul class="dropdown-menu">
    #										{% for status in all_followup_statuses %}
    #												<li><a data-status_id="{{ status.id }}" transient_id="{{ record.id }}" class="transientStatusChange" href="#">{{ status.name }}</a></li>
    #										{% endfor %}
    #									</ul>
#</div>""",
    #									  verbose_name='Followup Status',orderable=True,order_by='status')


    def __init__(self,*args, obs_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_observer(Telescope.objects.get(name='Pan-STARRS1'), obs_date)

    def render_airmass(self, value):
        from astroplan.plots import plot_airmass

    class Meta:
        model = SurveyObservation
        fields = ('field_id','ra_string','dec_string','rise_time','set_time','moon_angle')#,'transient.status')
        template_name='YSE_App/django-tables2/bootstrap.html'
        attrs = {
            'th' : {
                '_ordering': {
                    'orderable': 'sortable', # Instead of `orderable`
                    'ascending': 'ascend',	 # Instead of `asc`
                    'descending': 'descend'	 # Instead of `desc`
                }
            },
            "columnDefs": [
                {"type":"title-numeric","targets":1},
                {"type":"title-numeric","targets":2},
            ],
            'class': 'table table-bordered table-hover',
            "order": [[ 2, "desc" ]],
        }

def annotate_dashboard_transient_fields(qs):
    """
    Prefetch FKs and annotate recent photometry for dashboard tables.

    Avoids N+1 queries from Transient.recent_mag() / recent_magdate() during render.
    """
    recent_mag, recent_magdate = _recent_phot_subqueries('pk')
    return qs.select_related('status', 'host', 'obs_group').annotate(
        recent_mag=recent_mag,
        recent_magdate=recent_magdate,
    )


def annotate_search_fields(qs):
    """
    Aliases for the search box: plain FK lookups instead of Min() aggregates.

    The old annotate_with_disc_mag() wrapped every alias in Min(), which forced a
    GROUP BY over the whole transient table (and a HAVING for each LIKE). disc_mag
    is the only value that needs a subquery; the rest are single-valued joins.
    """
    disc_mag_sq = (
        TransientPhotData.objects.filter(
            photometry__transient=OuterRef('pk'),
            discovery_point=1,
            mag__isnull=False,
        )
        .order_by('mag')
        .values('mag')[:1]
    )
    return qs.annotate(
        disc_mag=Subquery(disc_mag_sq),
        obs_group_name=F('obs_group__name'),
        host_redshift=F('host__redshift'),
        spec_class=F('best_spec_class__name'),
        status_name=F('status__name'),
    )


# Backwards-compatible name (callers outside this module).
annotate_with_disc_mag = annotate_search_fields


def filter_tokens_any_field(qs, search_fields, value):
    """
    Every whitespace token must icontains-match at least one of search_fields.

    Replaces itertools.permutations(search_fields, n_tokens), which produced
    fields!/(fields-n)! AND-groups OR'd together (720 groups for 3 tokens over 10
    fields). This is one WHERE with n_tokens * len(search_fields) LIKE clauses, so
    the SQL shape and query count do not depend on the number of tokens. Any
    match the old expansion found is also a match here (it is a superset).
    """
    tokens = value.split()
    if not tokens:
        return qs
    q_total = Q()
    for token in tokens:
        q_token = Q()
        for field in search_fields:
            q_token |= Q(**{field + '__icontains': token})
        q_total &= q_token
    return qs.filter(q_total)

class TransientFilter(django_filters.FilterSet):

    #name_string = django_filters.CharFilter(name='name',lookup_expr='icontains',
    #										label='Name')

    ex = django_filters.CharFilter(method='filter_ex',label='Search')
    search_fields = ['name','ra','dec','disc_date','disc_mag','obs_group_name',
                     'spec_class','redshift','host_redshift',
                     'status_name']

    class Meta:
        model = Transient
        fields = ['ex',]

    def filter_ex(self, qs, name, value):
        if value:
            qs = annotate_search_fields(qs)
            qs = filter_tokens_any_field(qs, self.search_fields, value)
        return qs

class RisingTransientFilter(django_filters.FilterSet):

    #name_string = django_filters.CharFilter(name='name',lookup_expr='icontains',
    #										label='Name')

    #name = django_filters.CharFilter(name='name',lookup_expr='icontains',method='filter_name')
    recent_mag_lt = django_filters.NumberFilter(field_name='recent_mag',label='Max Recent Mag',lookup_expr='lt')
    days_since_disc = django_filters.NumberFilter(field_name='days_since_disc',label='Max Days Since Disc',lookup_expr='lt')
    ra_min = django_filters.NumberFilter(field_name='ra',label='Min. RA (deg)',lookup_expr='gt')
    ra_max = django_filters.NumberFilter(field_name='ra',label='Max. RA (deg)',lookup_expr='lt')
    dec_min = django_filters.NumberFilter(field_name='dec',label='Min. Dec (deg)',lookup_expr='gt')
    dec_max = django_filters.NumberFilter(field_name='dec',label='Max. Dec (deg)',lookup_expr='lt')
    ebv_max = django_filters.NumberFilter(field_name='mw_ebv',label='Max. MW E(B-V)',lookup_expr='lt')

    #recent_mag__gt = django_filters.NumberFilter(name='recent_mag', lookup_expr='recent_mag__gt')
    #recent_mag__lt = django_filters.NumberFilter(name='recent_mag', lookup_expr='recent_mag__lt')
    ex = django_filters.CharFilter(method='filter_ex',label='Search')
    search_fields = ['name','ra','dec','disc_date','disc_mag','obs_group_name',
                     'spec_class','redshift','host_redshift',
                     'status_name','recent_mag']

    class Meta:
        model = Transient
        fields = ['ex','recent_mag_lt','days_since_disc','ra_min','ra_max','dec_min','dec_max','ebv_max']

    def filter_ex(self, qs, name, value):
        if value:
            qs = annotate_search_fields(qs)
            qs = filter_tokens_any_field(qs, self.search_fields, value)
        return qs

class FollowupFilter(django_filters.FilterSet):

    ex = django_filters.CharFilter(method='filter_ex',label='Search')
    search_fields = ['transient__name','transient__status__name','status__name','valid_start','valid_stop']

    class Meta:
        model = TransientFollowup
        fields = ['ex',]

    def filter_ex(self, qs, name, value):
        if value:
            qs = filter_tokens_any_field(qs, self.search_fields, value)
        return qs

class ObsNightFollowupFilter(django_filters.FilterSet):

    ex = django_filters.CharFilter(method='filter_ex',label='Search')
    search_fields = ['transient__name','transient__status__name','status__name','valid_start','valid_stop']

    class Meta:
        model = TransientFollowup
        fields = ['ex',]

    def filter_ex(self, qs, name, value):
        if value:
            qs = filter_tokens_any_field(qs, self.search_fields, value)
        return qs



def dashboard_tables(request):

    k2_transients = Transient.objects.all()

    table = TransientTable(k2_transients)
    RequestConfig(request, paginate={'per_page': 10}).configure(table)

    context = {'k2_transients': table}

    return render(request, 'YSE_App/dashboard_table.html', context)
