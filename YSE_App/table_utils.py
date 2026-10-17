from django.shortcuts import render
from django.utils.html import format_html
from .models import *
from . import view_utils
import django_tables2 as tables
from django_tables2 import RequestConfig
from django.db.models import F, Q
from YSE_App.common.quality_mask import legacy_good
from django.db.models.functions import Length, Substr
from django.db.models import Count, OuterRef, Prefetch, Subquery, Value, Max, Min
from django.db.models.functions import Greatest, Coalesce
from django_tables2 import A
from django.db import models
from django.db.models.query import QuerySet
from .data import PhotometryService
from .models.transient_models import batch_nearest_survey_fields
import time
import django_filters
from astropy.coordinates import SkyCoord
from YSE_App.services.night_astro import (
    cached_rise_set,
    moon_position,
    observer_for,
    rise_set_cache_key,
    store_rise_set,
)
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


# Sort keys for the "Last Det." and "Last Obs." columns (#454, per #270):
# indexed TransientPhotStat columns instead of a correlated subquery (or a
# Max() over every photometry row) evaluated for the whole bucket. Since
# #557 the cells (LastDetColumn / LastObsColumn) show the same stat columns.
#  * last_obs_date is the latest unflagged point of any kind.
#  * last_detected_mag is the latest unflagged *detection* (the light-curve
#    plot's rule).
#  * A transient without a stat row sorts as NULL, like one without photometry
#    (MySQL: first ascending, last descending).
RECENT_PHOT_ORDER_FIELDS = {
    'recent_mag': 'photstat__last_detected_mag',
    'last_mag': 'photstat__last_detected_mag',
    'recent_magdate': 'photstat__last_obs_date',
    'last_obs_date': 'photstat__last_obs_date',
}


def _table_viewer(table):
    """The user a table is rendered for (``RequestConfig`` sets ``table.request``), or ``None``.

    Every view configures its tables with ``RequestConfig``; a table built
    without one (scripts, some tests) shows the all-data statistics.
    """
    request = getattr(table, 'request', None)
    return getattr(request, 'user', None) if request is not None else None


def _scoped_viewer(user):
    """``user`` when the stat values must be scoped for them (#607), else ``None``."""
    from YSE_App.services.visibility import user_sees_everything

    if user is None or user_sees_everything(user):
        return None
    return user


def order_by_stat(queryset, field, is_descending, user=None, *, transient_ref='pk', stat_path='photstat__'):
    """``queryset`` sorted on the stat column ``field`` as ``user`` may see it (pk tie-break, #607)."""
    viewer = _scoped_viewer(user)
    if viewer is None:
        return stable_order_by(queryset, stat_path + field, is_descending)
    from YSE_App.services.photstat_visibility import alias_stat_fields

    queryset, names = alias_stat_fields(queryset, viewer, [field], transient_ref=transient_ref, stat_path=stat_path)
    return stable_order_by(queryset, names[field], is_descending)


def order_by_recent_phot(queryset, key, is_descending, user=None):
    """``queryset`` sorted on the stored photometry statistics for ``key`` (pk tie-break).

    With ``user``, on the values that user may see (#607).
    """
    field = RECENT_PHOT_ORDER_FIELDS[key][len('photstat__'):]
    return order_by_stat(queryset, field, is_descending, user)


class RecentPhotOrderMixin:
    """order_recent_mag / order_recent_magdate for Transient tables (see RECENT_PHOT_ORDER_FIELDS)."""

    def order_recent_mag(self, queryset, is_descending):
        return (order_by_recent_phot(queryset, 'recent_mag', is_descending, _table_viewer(self)), True)

    def order_recent_magdate(self, queryset, is_descending):
        return (order_by_recent_phot(queryset, 'recent_magdate', is_descending, _table_viewer(self)), True)


# "Last Det." and "Last Obs." cells (#557). Both read TransientPhotStat, the
# same columns RECENT_PHOT_ORDER_FIELDS sorts on, so display and sort agree:
#  * Last Det.: last_detected_mag and its band (the light-curve plot's
#    detection rule, services.phot_points).
#  * Last Obs.: last_obs_date, the newest unflagged point of any kind. When
#    that point is not a detection and the plot draws it as an upper limit,
#    the cell adds the limit and its band, e.g. "10/09/2026 (>21.30 r)".
#    The stat row does not store the newest point's limit (its limit fields
#    are pre-detection only), so the limit is read from that point: one
#    extra query per table page, only when a row on the page needs it.
# A transient without a stat row shows the empty default and sorts as NULL.
_LAST_PHOT_MJD_TOLERANCE = 1.0 / 86400.0  # last_obs_date round-trips through an MJD


def _stat_row(transient_id, stat):
    band = stat.last_detected_band if stat.last_detected_band_id is not None else None
    return {
        'transient_id': transient_id,
        'lastphot_det_mag': stat.last_detected_mag,
        'lastphot_det_band': band.name if band is not None else None,
        'lastphot_obs_date': stat.last_obs_date,
        'lastphot_obs_mjd': stat.last_obs_mjd,
        'lastphot_det_mjd': stat.last_detected_mjd,
    }


def _cached_stat_objects(records, transient_attr):
    """``{transient_id: TransientPhotStat}`` from the page's Transient records, or None to query them.

    ``annotate_dashboard_transient_fields`` select_related()s the stat row and
    its band, so dashboard pages need no stat query.
    """
    if transient_attr != 'pk':
        return None
    rows = {}
    for record in records:
        descriptor = getattr(type(record), 'photstat', None)
        related = getattr(descriptor, 'related', None)
        if related is None or not related.is_cached(record):
            return None
        stat = related.get_cached_value(record)
        if stat is not None and stat.last_detected_band_id is not None and \
                not type(stat).last_detected_band.field.is_cached(stat):
            return None
        rows[record.pk] = stat  # None: the transient has no stat row
    return rows


def _stat_rows_from_records(records, transient_attr):
    """Stat rows (see :func:`_stat_row`) from the page's Transient records, or None to query them."""
    cached = _cached_stat_objects(records, transient_attr)
    if cached is None:
        return None
    return [_stat_row(tid, stat) for tid, stat in cached.items() if stat is not None]


def _page_records(record, table):
    records = [record]
    rows = table.page.object_list if getattr(table, 'page', None) is not None else table.rows
    try:
        records += [row.record for row in rows]
    except Exception:
        pass
    return records


def _page_visible_stats(record, table, transient_attr, viewer):
    """``{transient_id: stat row the viewer may see}`` for the table's page, computed once per table (#607)."""
    from YSE_App.services.photstat_visibility import visible_stats

    tid = getattr(record, transient_attr, None)
    cache = table.__dict__.setdefault('_visible_stat_cache', {})
    if tid not in cache:
        records = _page_records(record, table)
        ids = {getattr(r, transient_attr, None) for r in records}
        ids.discard(None)
        ids -= set(cache)
        full = _cached_stat_objects(records, transient_attr)
        cache.update(dict.fromkeys(ids))
        cache.update(visible_stats(viewer, ids, full_rows=full, related=('last_detected_band',)))
    return cache


def last_phot_summary(transient_ids, stat_rows=None, user=None):
    """``{transient_id: dict}`` with the Last Det. / Last Obs. values (see above).

    Keys of each dict: ``det_mag``, ``det_band``, ``obs_date``, ``limit``,
    ``limit_band``. ``stat_rows`` (see :func:`_stat_row`) saves the stat query. At most two queries for any
    number of transients. With ``user`` the newest point's limit is read only
    from photometry that user may see (#607); ``stat_rows`` must then hold the
    rows the user may see.
    """
    import datetime

    from YSE_App.models.phot_stat_models import TransientPhotStat, datetime_to_mjd
    from YSE_App.services import phot_points

    ids = {i for i in transient_ids if i is not None}
    out = {}
    if not ids:
        return out
    if stat_rows is None:
        stat_rows = [
            _stat_row(stat.transient_id, stat)
            for stat in TransientPhotStat.objects.filter(transient_id__in=ids)
            .select_related('last_detected_band')
        ]
    newest_not_det = {}
    for row in stat_rows:
        tid = row['transient_id']
        if tid not in ids or row['lastphot_obs_date'] is None:
            continue
        out[tid] = {
            'det_mag': row['lastphot_det_mag'],
            'det_band': row['lastphot_det_band'],
            'obs_date': row['lastphot_obs_date'],
            'limit': None,
            'limit_band': None,
        }
        obs_mjd, det_mjd = row['lastphot_obs_mjd'], row['lastphot_det_mjd']
        if obs_mjd is not None and (det_mjd is None or obs_mjd - det_mjd > _LAST_PHOT_MJD_TOLERANCE):
            newest_not_det[tid] = (obs_mjd, row['lastphot_obs_date'])
    if not newest_not_det:
        return out
    window = Q()
    for tid, (_, obs_date) in newest_not_det.items():
        window |= Q(photometry__transient_id=tid,
                    obs_date__gte=obs_date - datetime.timedelta(days=1))
    points = TransientPhotData.objects.filter(window).filter(legacy_good('TransientPhotData'))
    if user is not None:
        from YSE_App.services.visibility import visible_photometry_ids_q

        visible = visible_photometry_ids_q(user)
        if visible is not None:
            points = points.filter(visible)
    points = points.values_list('photometry__transient_id', 'obs_date', 'band__name',
                                'mag', 'mag_err', 'flux', 'flux_err', 'flux_zero_point')
    for tid, obs_date, band, mag, mag_err, flux, flux_err, zp in points:
        obs_mjd = newest_not_det[tid][0]
        if abs(datetime_to_mjd(obs_date) - obs_mjd) > _LAST_PHOT_MJD_TOLERANCE:
            continue
        if phot_points.is_detection(mag, mag_err, flux, flux_err):
            continue
        lim = phot_points.limiting_mag(flux, flux_err, zp)
        entry = out[tid]
        if lim is not None and (entry['limit'] is None or lim > entry['limit']):
            entry['limit'], entry['limit_band'] = lim, band
    return out


class _LastPhotColumn(tables.Column):
    """Base for the Last Det. / Last Obs. columns: values for the whole page at once.

    ``transient_attr`` names the record attribute holding the transient id
    (``pk`` on Transient rows, ``transient_id`` on follow-up rows). The cells
    ignore the queryset's ``recent_mag`` / ``recent_magdate`` annotations; the
    table's ``order_recent_mag`` / ``order_recent_magdate`` sort on the same
    stat columns.
    """

    stat_field = None

    def __init__(self, *args, transient_attr='pk', **kwargs):
        kwargs.setdefault('accessor', transient_attr)
        kwargs.setdefault('orderable', True)
        # Explicit, so a queryset ordered on pk is never read as "sorted by this column".
        prefix = '' if transient_attr == 'pk' else 'transient__'
        kwargs.setdefault('order_by', prefix + self.stat_field)
        kwargs['empty_values'] = ()
        super().__init__(*args, **kwargs)
        self.transient_attr = transient_attr

    def _info(self, record, table):
        tid = getattr(record, self.transient_attr, None)
        cache = table.__dict__.setdefault('_last_phot_cache', {})
        if tid not in cache:
            viewer = _scoped_viewer(_table_viewer(table))
            if viewer is not None:
                # Only the photometry this viewer may see (#607).
                stats = _page_visible_stats(record, table, self.transient_attr, viewer)
                ids = {i for i in stats if i not in cache}
                stat_rows = [_stat_row(i, stats[i]) for i in ids if stats[i] is not None]
                cache.update(dict.fromkeys(ids))
                cache.update(last_phot_summary(ids, stat_rows, user=viewer))
                return cache.get(tid)
            records = _page_records(record, table)
            ids = {getattr(r, self.transient_attr, None) for r in records}
            ids.discard(None)
            stat_rows = _stat_rows_from_records(records, self.transient_attr)
            cache.update(dict.fromkeys(ids))
            cache.update(last_phot_summary(ids, stat_rows))
        return cache.get(tid)


class LastDetColumn(_LastPhotColumn):
    """"Last Det.": last detection magnitude and band, e.g. ``18.71 r``."""

    stat_field = RECENT_PHOT_ORDER_FIELDS['recent_mag']

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('verbose_name', 'Last Det.')
        super().__init__(*args, **kwargs)

    def render(self, record, table, bound_column):
        from .common.filter_display import display_filter_label

        info = self._info(record, table)
        if not info or info['det_mag'] is None:
            return bound_column.default
        return '%s %s' % (format_magnitude(info['det_mag']), display_filter_label(info['det_band']))


class LastObsColumn(_LastPhotColumn):
    """"Last Obs.": date of the newest point, plus its limit and band when it is a non-detection."""

    DATE_FORMAT = '%m/%d/%Y'
    stat_field = RECENT_PHOT_ORDER_FIELDS['recent_magdate']

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('verbose_name', 'Last Obs.')
        super().__init__(*args, **kwargs)

    def render(self, record, table, bound_column):
        from .common.filter_display import display_filter_label

        info = self._info(record, table)
        if not info or info['obs_date'] is None:
            return bound_column.default
        text = info['obs_date'].strftime(self.DATE_FORMAT)
        if info['limit'] is not None:
            text += ' (>%s %s)' % (format_magnitude(info['limit']), display_filter_label(info['limit_band']))
        return text


class FollowupRecentMagMixin:
    """Follow-up tables: the "Last Det." column sorts on the transient's stat row (#557)."""

    def order_recent_mag(self, queryset, is_descending):
        field = RECENT_PHOT_ORDER_FIELDS['recent_mag'][len('photstat__'):]
        return (order_by_stat(queryset, field, is_descending, _table_viewer(self),
                              transient_ref='transient_id', stat_path='transient__photstat__'), True)


class BazinMagMixin:
    """
    "Bazin Mag" column for follow-up tables: the magnitude the per-band Bazin
    fit extrapolates to the table's epoch (#225).

    The fits for every transient on the current page are computed the first
    time a row asks (one photometry query for the page, fits cached per
    transient in the Django cache by ``YSE_App.services.bazin``), so the query
    count does not grow with rows.  The cell shows the magnitude in the band
    with the most recent detection that has a usable fit, e.g. ``18.71 r``,
    and is blank when no band has one.  The column cannot be a SQL
    annotation, so it is not orderable.
    """

    def _set_bazin_epoch(self, at_mjd):
        self._bazin_mjd = float(at_mjd)
        self._bazin_mags = {}
        self._bazin_seen = set()

    def _page_transient_ids(self, first):
        ids = {first}
        rows = self.page.object_list if getattr(self, 'page', None) is not None else self.rows
        try:
            for row in rows:
                ids.add(row.record.transient_id)
        except Exception:
            pass
        return ids

    def _bazin_mag_for(self, transient_id):
        from YSE_App.services.bazin import extrapolated_mags

        if transient_id not in self._bazin_seen:
            ids = self._page_transient_ids(transient_id)
            # Fitted to the photometry the viewer may see (#607).
            self._bazin_mags.update(extrapolated_mags(ids, self._bazin_mjd, user=_table_viewer(self)))
            self._bazin_seen |= ids
        return self._bazin_mags.get(transient_id)

    def render_bazin_mag(self, record):
        from .common.filter_display import display_filter_label

        picked = self._bazin_mag_for(record.transient_id)
        if picked is None:
            return ''
        mag, band_name = picked
        return '%s %s' % (format_magnitude(mag), display_filter_label(band_name))


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
    and rise/set times and moon separations are solved for every target on the
    current page in one vectorised astroplan call the first time a row asks,
    then served from a dict keyed by (ra, dec) string. That dict is also kept in the Django cache
    per (telescope, night), so a repeat load of the same night (or the same
    targets on another page) does no astroplan work at all. Values are
    formatted exactly as before.
    """

    horizon = 18 * u.deg

    def _set_observer(self, telescope, obs_date):
        self.tel = observer_for(telescope)
        date_str = str(obs_date).split()[0]
        self.tme = Time(date_str)
        self._moon = None
        self._rise_set_key = rise_set_cache_key(telescope, date_str, self.horizon.to_value(u.deg))
        self._rise_set = cached_rise_set(self._rise_set_key)

    @property
    def moon(self):
        if self._moon is None:
            self._moon = moon_position(self.tme)
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
        moon_angle = np.atleast_1d(sc.separation(self.moon).deg)
        for i, key in enumerate(keys):
            self._rise_set[key] = (
                self._time_str(rise[i]), self._time_str(sett[i]), '%.1f' % moon_angle[i]
            )
        store_rise_set(self._rise_set_key, self._rise_set)

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

    def render_moon_angle(self, value, bound_column):
        return self._rise_set_for(value, bound_column)[2]


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


class PeakMagColumn(MagnitudeColumn):
    """Brightest stored detection (``TransientPhotStat.peak_mag``, #268).

    The value comes from the ``peak_mag`` annotation ``annotate_peak_mag``
    adds (a LEFT JOIN on the one-row-per-transient stat table, no extra
    query); a transient without a stat row shows the empty default. A viewer
    who may not see all the photometry gets the peak of the photometry they
    may see (#607), read with the page's other stat values.
    """

    def __init__(self, *args, **kwargs):
        kwargs['empty_values'] = ()
        super().__init__(*args, **kwargs)

    def render(self, value, record=None, table=None, bound_column=None):
        viewer = _scoped_viewer(_table_viewer(table)) if table is not None else None
        if viewer is not None and record is not None:
            stat = _page_visible_stats(record, table, 'pk', viewer).get(record.pk)
            value = getattr(stat, 'peak_mag', None)
        if value is None or value == '':
            return bound_column.default if bound_column is not None else '-'
        return format_magnitude(value)


def annotate_peak_mag(qs):
    """``peak_mag`` from the stored photometry statistics, once per queryset."""
    if 'peak_mag' in qs.query.annotations:
        return qs
    return qs.annotate(peak_mag=F('photstat__peak_mag'))


# Star toggle (#323). The state is painted after load by base.html from
# /my/favorites/ids.json (one fetch per page), so table renders stay
# query-free; a queryset annotated with ``is_favorite`` renders it directly.
FAVORITE_STAR_TEMPLATE = (
    '<a href="#" class="yse-fav-star" data-transient-id="{{ record.id }}" '
    'data-state="{% if record.is_favorite is True %}on{% elif record.is_favorite is False %}off{% else %}unknown{% endif %}" '
    'title="Favorite" aria-label="Toggle favorite for {{ record.name }}">'
    '<i class="fa {% if record.is_favorite %}fa-star{% else %}fa-star-o{% endif %}" aria-hidden="true"></i></a>'
)

# Open paper interests on the row (#290); ``annotate_open_interest_count`` fills it in the same query.
INTEREST_COUNT_TEMPLATE = (
    '{% if record.open_interest_count %}<span class="badge badge-info yse-interest-count" '
    'title="{{ record.open_interest_count }} open paper interest{{ record.open_interest_count|pluralize }} '
    '(see Working on this on the summary tab)">{{ record.open_interest_count }}</span>'
    '{% else %}<span class="text-muted">-</span>{% endif %}'
)


def annotate_open_interest_count(qs):
    """``open_interest_count`` per row from a correlated subquery (no extra query, #290)."""
    if 'open_interest_count' in qs.query.annotations:
        return qs
    from django.db.models import IntegerField, OuterRef, Subquery
    from YSE_App.models.interest_models import TransientInterest

    counts = (TransientInterest.objects.filter(transient=OuterRef('pk'), status__in=TransientInterest.OPEN_STATUSES)
              .order_by().values('transient').annotate(n=Count('id')).values('n')[:1])
    return qs.annotate(open_interest_count=Subquery(counts, output_field=IntegerField()))


class TransientTable(RecentPhotOrderMixin, tables.Table):

    # The first line of the AI/human summary (#295) as the link's tooltip.
    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\"{% if record.summary %} title=\"{{ record.summary_first_line }}\" class=\"yse-has-summary\"{% endif %}>{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    favorite = tables.TemplateColumn(FAVORITE_STAR_TEMPLATE, verbose_name='Fav.', orderable=False,
                                     attrs={'td': {'class': 'yse-fav-cell'}, 'th': {'class': 'yse-fav-cell'}})
    interest_count = tables.TemplateColumn(INTEREST_COUNT_TEMPLATE, verbose_name='Papers', orderable=True,
                                           order_by='open_interest_count')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
    peak_mag = PeakMagColumn(accessor='peak_mag',
                             verbose_name='Peak Mag',orderable=True)
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


    def __init__(self, data, *args, **kwargs):
        if isinstance(data, QuerySet):
            data = annotate_open_interest_count(annotate_peak_mag(data))
        super().__init__(data, *args, **kwargs)

        self.base_columns['best_spec_class'].verbose_name = 'Spec. Class'

    def order_interest_count(self, queryset, is_descending):
        queryset = annotate_open_interest_count(queryset)
        return (stable_order_by(queryset, 'open_interest_count', is_descending), True)

    def order_peak_mag(self, queryset, is_descending):
        if _scoped_viewer(_table_viewer(self)) is not None:
            return (order_by_stat(queryset, 'peak_mag', is_descending, _table_viewer(self)), True)
        queryset = annotate_peak_mag(queryset)
        return (stable_order_by(queryset, 'peak_mag', is_descending), True)


    def order_best_spec_class(self, queryset, is_descending):
        return (stable_order_by(queryset, 'best_spec_class', is_descending), True)

    def order_best_redshift(self, queryset, is_descending):

        queryset = queryset.annotate(
            best_redshift=Coalesce('redshift', 'host__redshift'),
        )
        return (stable_order_by(queryset, 'best_redshift', is_descending), True)




    class Meta:
        model = Transient
        # The star and paper-interest columns (#323, #290) come last so the
        # title-numeric / default-order column indexes below stay valid.
        fields = ('name_string','ra_string','dec_string','disc_date_string','recent_mag','recent_magdate','peak_mag','mw_ebv',
                  'obs_group','best_spec_class','best_redshift','status_string','interest_count','favorite')

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

class SearchTransientTable(TransientTable):
    """``TransientTable`` plus the cone-search separation for ``/search/`` (#284).

    ``separation`` is the ``annotate_separation`` value in degrees; the
    column shows arcseconds and is excluded (``exclude=('separation',)``)
    when no cone search is active. ``gal_b`` is the stored galactic latitude
    (#286), shown when a ``|b|`` filter or ``gal_b`` ordering is active. ``best_spec_class`` is selected with the
    row (``select_related``) by the search view, so the table renders a page
    without per-row queries.
    """

    # #284: tick box for the bulk actions (POSTed as ``transient_id``).
    select = tables.Column(accessor='pk', verbose_name='', orderable=False, empty_values=())

    def render_select(self, value):
        return format_html(
            '<input type="checkbox" class="search-select-row" name="transient_id" value="{}" form="search-bulk-form"'
            ' aria-label="Select transient">', value)

    separation = tables.Column(accessor='separation', verbose_name='Sep. (arcsec)', orderable=True)

    def render_separation(self, value):
        return '%.1f' % (float(value) * 3600.0)

    # #319: one annotation ``origin.key`` per row (``annotate_annotation_column``); excluded unless asked for.
    annotation_value = tables.Column(accessor='annotation_value', verbose_name='Annotation', orderable=True,
                                     order_by=('annotation_value_num', 'annotation_value'), default='-')

    def order_separation(self, queryset, is_descending):
        return (stable_order_by(queryset, 'separation', is_descending), True)

    # #286: the stored galactic latitude; excluded unless a |b| filter or gal_b ordering is active.
    gal_b = tables.Column(accessor='gal_b', verbose_name='Gal. b (deg)', orderable=True, default='-')

    def render_gal_b(self, value):
        return '%+.2f' % float(value)

    def order_gal_b(self, queryset, is_descending):
        return (stable_order_by(queryset, 'gal_b', is_descending), True)

    class Meta(TransientTable.Meta):
        fields = ('select', 'name_string', 'separation', 'ra_string', 'dec_string', 'gal_b', 'disc_date_string', 'recent_mag',
                  'recent_magdate', 'peak_mag', 'mw_ebv', 'obs_group', 'best_spec_class', 'best_redshift',
                  'annotation_value', 'status_string', 'interest_count', 'favorite')
        sequence = fields
        attrs = dict(TransientTable.Meta.attrs, id='search_transient_tbl')


class FavoriteTransientTable(TransientTable):
    """``/my/favorites/`` and the dashboard section (#323): the shared columns plus when it was starred."""

    favorited_at = tables.DateTimeColumn(accessor='favorited_at', verbose_name='Starred', format='Y-m-d H:i',
                                         orderable=True)

    class Meta(TransientTable.Meta):
        fields = ('favorite', 'name_string', 'ra_string', 'dec_string', 'disc_date_string', 'recent_mag',
                  'recent_magdate', 'peak_mag', 'obs_group', 'best_spec_class', 'best_redshift', 'status_string',
                  'interest_count', 'favorited_at')
        sequence = fields
        exclude = ('mw_ebv',)
        attrs = dict(TransientTable.Meta.attrs, id='favorite_transient_tbl')
        attrs['columnDefs'] = [{"type": "title-numeric", "targets": 2}, {"type": "title-numeric", "targets": 3}]
        attrs['order'] = [[13, "desc"]]


class NearestFieldPageMixin:
    """
    Fill the nearest-survey-field values for the current page in one query.

    The columns read Transient.nearest_*_field / *_sep, which used to run four SurveyField
    queries and a SkyCoord per row. After pagination, load the candidate fields once and
    compute the separations vectorised (batch_nearest_survey_fields), caching the result on
    each record. Unpaginated use falls back to the per-record path (still correct).
    """
    nearest_field_kind = 'ztf'

    def paginate(self, *args, **kwargs):
        super().paginate(*args, **kwargs)
        records = [row.record for row in self.page.object_list]
        results = batch_nearest_survey_fields(records, self.nearest_field_kind)
        for record, result in zip(records, results):
            cache = record.__dict__.setdefault('_nearest_field_cache', {})
            cache[self.nearest_field_kind] = result


class FieldTransientTable(NearestFieldPageMixin, RecentPhotOrderMixin, tables.Table):
    nearest_field_kind = 'ztf'

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    ztf_field = tables.Column(accessor='nearest_ztf_field',
                              verbose_name='ZTF Field',orderable=False)
    ztf_sep = tables.Column(accessor='nearest_ztf_field_sep',
                            verbose_name='ZTF Sep.',orderable=False)
    get_yse_pointings = tables.TemplateColumn(
        "{% if record.nearest_ztf_field %}<a href=\"{% url 'yse_pointings' record.nearest_ztf_field record.name %}\" target='_blank'>Get Pointings</a>{% endif %}",
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

class AdjustFieldTransientTable(NearestFieldPageMixin, RecentPhotOrderMixin, tables.Table):
    nearest_field_kind = 'yse'

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
    best_redshift = tables.Column(accessor='z_or_hostz',
                                  verbose_name='Redshift',orderable=True,order_by='host__redshift')
    yse_field = tables.Column(accessor='nearest_yse_field',
                              verbose_name='YSE Field',orderable=False)
    yse_sep = tables.Column(accessor='nearest_yse_field_sep',
                            verbose_name='YSE Sep.',orderable=False)
    get_yse_pointings = tables.TemplateColumn(
        "{% if record.nearest_yse_field %}<a href=\"{% url 'adjust_yse_pointings' record.nearest_yse_field record.name %}\" target='_blank'>Get Pointings</a>{% endif %}",
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


class YSETransientTable(RecentPhotOrderMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
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

class YSEFullTransientTable(RecentPhotOrderMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
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

class YSERisingTransientTable(RecentPhotOrderMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
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


class NewTransientTable(RecentPhotOrderMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.slug %}\">{{ record.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='CoordString.0',
                              verbose_name='RA',orderable=True,order_by='ra')
    dec_string = tables.Column(accessor='CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='dec')
    disc_date_string = tables.Column(accessor='disc_date_string',
                                     verbose_name='Disc. Date',orderable=True,order_by='disc_date')
    recent_mag = LastDetColumn()
    recent_magdate = LastObsColumn()
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
    recent_mag = LastDetColumn(transient_attr='transient_id')


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

class ObsNightFollowupTable(FollowupRecentMagMixin, BazinMagMixin, TargetVisibilityMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.transient.slug %}\">{{ record.transient.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='transient__name')
    ra_string = tables.Column(accessor='transient.CoordString.0',
                              verbose_name='RA',orderable=True,order_by='transient.ra')
    dec_string = tables.Column(accessor='transient.CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='transient.dec')
    recent_mag = LastDetColumn(transient_attr='transient_id')
    # Bazin extrapolation to local midnight of the observing night (#225)
    bazin_mag = tables.Column(accessor='transient_id', verbose_name='Bazin Mag @ Night',
                              orderable=False, empty_values=(),
                              attrs={'th': {'title': 'Per-band Bazin fit extrapolated to local midnight of the night; band shown after the magnitude'}})


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
        from YSE_App.services.bazin import local_midnight_mjd

        super().__init__(*args, **kwargs)
        telescope = classical_obs_date.resource.telescope
        self._set_observer(telescope, classical_obs_date.obs_date)
        observatory = telescope.observatory if telescope.observatory_id else None
        self._set_bazin_epoch(local_midnight_mjd(
            classical_obs_date.obs_date, observatory.utc_offset if observatory else 0))

    def render_requestors(self, value, record):
        from YSE_App.services.followup_requests import format_requestors

        return format_requestors(record)

    def render_comment(self, value, record):
        from YSE_App.services.followup_requests import format_comments

        return format_comments(record)

    class Meta:
        model = TransientFollowup
        fields = ('name_string','ra_string','dec_string','recent_mag','bazin_mag',
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

class ToOFollowupTable(FollowupRecentMagMixin, BazinMagMixin, TargetVisibilityMixin, tables.Table):

    name_string = tables.TemplateColumn("<a href=\"{% url 'transient_detail' record.transient.slug %}\">{{ record.transient.name }}</a>",
                                        verbose_name='Name',orderable=True,order_by='name')
    ra_string = tables.Column(accessor='transient.CoordString.0',
                              verbose_name='RA',orderable=True,order_by='transient.ra')
    dec_string = tables.Column(accessor='transient.CoordString.1',
                               verbose_name='DEC',orderable=True,order_by='transient.dec')
    recent_mag = LastDetColumn(transient_attr='transient_id')
    # Bazin extrapolation to now: a ToO is triggered at request time (#225)
    bazin_mag = tables.Column(accessor='transient_id', verbose_name='Bazin Mag Now',
                              orderable=False, empty_values=(),
                              attrs={'th': {'title': 'Per-band Bazin fit extrapolated to now; band shown after the magnitude'}})


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
        from YSE_App.services.bazin import datetime_to_mjd

        super().__init__(*args, **kwargs)
        now = datetime.datetime.now()
        self._set_observer(too_resource.telescope, now)
        self._set_bazin_epoch(datetime_to_mjd(datetime.datetime.now(datetime.timezone.utc)))

    def render_requestors(self, value, record):
        from YSE_App.services.followup_requests import format_requestors

        return format_requestors(record)

    def render_comment(self, value, record):
        from YSE_App.services.followup_requests import format_comments

        return format_comments(record)

    class Meta:
        model = TransientFollowup
        fields = ('name_string','ra_string','dec_string','recent_mag','bazin_mag',
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


    def __init__(self,*args, obs_date=None, telescope=None, **kwargs):
        super().__init__(*args, **kwargs)
        if telescope is None:  # the view already has it; look it up only when called bare
            telescope = Telescope.objects.get(name='Pan-STARRS1')
        self._set_observer(telescope, obs_date)

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

class PageFirstQuerySet(QuerySet):
    """
    Annotated table queryset whose page slice selects the page's pks first.

    ``qs[a:b]`` on the dashboard queryset used to be one statement carrying the
    select_related joins and the two recent-photometry subqueries. MySQL 8 then
    hash-joins the whole status bucket into a temporary table before it can
    ORDER BY ... LIMIT, and evaluates both dependent subqueries for every
    candidate row (loops = bucket size: 170 ms for a 2.7k-row bucket, growing
    with the bucket). Slicing here runs a bare ``SELECT pk ... ORDER BY ... LIMIT``
    (no joins, no subqueries: sorted on the base table, or on an index once P8
    lands) and then loads the annotated, joined rows for those pks only, so the
    subqueries run once per page row. Two cheap statements instead of one that
    scales with the bucket. ``count()`` leaves the annotations out (#453);
    everything else (values, further filtering) is a plain QuerySet. See #256.
    """

    @classmethod
    def wrap(cls, qs):
        """A clone of ``qs`` of this class, keeping its prefetch lookups and other state."""
        if isinstance(qs, cls):
            return qs
        clone = qs._chain()
        clone.__class__ = cls
        return clone

    def count(self):
        """COUNT over the filtered pks, without the SELECT-list annotations (#453).

        Django 3.2's ``count()`` on an annotated queryset runs
        ``SELECT COUNT(*) FROM (SELECT ..., <every annotation> ...) subquery``,
        so each correlated subquery (recent_mag, recent_magdate,
        open_interest_count) is in the derived table for every row of the
        bucket. ``values('pk')`` masks the annotations out of the SELECT list;
        a filter on an annotation keeps its expression in WHERE, and ordering
        is dropped either way. Aggregate (GROUP BY), DISTINCT, sliced and
        combined querysets keep Django's own count.
        """
        if (
            self._result_cache is None
            and self._fields is None
            and self.query.annotations
            and self.query.group_by is None
            and not self.query.distinct
            and not self.query.is_sliced
            and not self.query.combinator
        ):
            return self.values('pk').order_by().count()
        return super().count()

    def __getitem__(self, k):
        if (
            isinstance(k, slice)
            and self._fields is None  # not a values()/values_list() queryset
            and not self.query.is_sliced
            and self.query.annotations
            and (k.start or 0) >= 0
            and k.stop is not None
            and k.step is None
        ):
            ids = list(self.values_list('pk', flat=True)[k])
            return self.filter(pk__in=ids)
        return super().__getitem__(k)


def annotate_dashboard_transient_fields(qs):
    """
    Prefetch the FKs the dashboard tables render, including the stat row.

    The Last Det. / Last Obs. cells and sorts read ``TransientPhotStat``
    (``photstat__last_detected_mag`` / ``photstat__last_obs_date``, #557), so
    the former ``recent_mag`` / ``recent_magdate`` photometry subqueries are
    gone (#270). Returns a PageFirstQuerySet so django-tables2's page slice
    stays cheap when a caller adds annotations (see PageFirstQuerySet).
    """
    return PageFirstQuerySet.wrap(
        # select_related is left out of the bare pk and count queries.
        qs.select_related('status', 'host', 'obs_group',
                          'photstat', 'photstat__last_detected_band')
    )


def _filter_viewer(filterset):
    """The user a filter set runs for: ``request.user`` of the ``request=`` it was built with.

    Without a request the filter runs for no one (public data only), never
    for staff, so a missing request cannot widen a filter (#692).
    """
    from django.contrib.auth.models import AnonymousUser

    request = getattr(filterset, 'request', None)
    user = getattr(request, 'user', None)
    return AnonymousUser() if user is None else user


def annotate_search_fields(qs, user=None, with_recent_mag=False):
    """
    Aliases for the search box: plain FK lookups instead of Min() aggregates.

    The old annotate_with_disc_mag() wrapped every alias in Min(), which forced a
    GROUP BY over the whole transient table (and a HAVING for each LIKE). disc_mag
    is the only value that needs a subquery; the rest are single-valued joins.

    The searchable magnitudes are what ``user`` may see (#607, #692): disc_mag
    reads only visible photometry for a non-staff viewer, and
    ``with_recent_mag`` adds ``search_recent_mag`` from the viewer's stat scope.
    ``user=None`` means no viewer: only public (ungrouped) photometry.
    """
    from django.contrib.auth.models import AnonymousUser

    from YSE_App.services.visibility import visible_photometry_ids_q

    user = AnonymousUser() if user is None else user
    points = TransientPhotData.objects.filter(
        photometry__transient=OuterRef('pk'),
        discovery_point=1,
        mag__isnull=False,
    )
    visible = visible_photometry_ids_q(user)
    if visible is not None:
        points = points.filter(visible)
    disc_mag_sq = points.order_by('mag').values('mag')[:1]
    extra = {}
    if with_recent_mag:
        from YSE_App.services.photstat_visibility import stat_expression

        extra['search_recent_mag'] = stat_expression(user, 'last_detected_mag')
    return qs.annotate(
        disc_mag=Subquery(disc_mag_sq),
        obs_group_name=F('obs_group__name'),
        host_redshift=F('host__redshift'),
        spec_class=F('best_spec_class__name'),
        status_name=F('status__name'),
        **extra,
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
            qs = annotate_search_fields(qs, _filter_viewer(self))
            qs = filter_tokens_any_field(qs, self.search_fields, value)
        return qs

class RisingTransientFilter(django_filters.FilterSet):

    #name_string = django_filters.CharFilter(name='name',lookup_expr='icontains',
    #										label='Name')

    #name = django_filters.CharFilter(name='name',lookup_expr='icontains',method='filter_name')
    recent_mag_lt = django_filters.NumberFilter(method='filter_recent_mag_lt',label='Max Recent Mag')
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
                     'status_name','search_recent_mag']

    class Meta:
        model = Transient
        fields = ['ex','recent_mag_lt','days_since_disc','ra_min','ra_max','dec_min','dec_max','ebv_max']

    def filter_recent_mag_lt(self, qs, name, value):
        """Brighter than ``value`` by the last detection the viewer may see (#692)."""
        if value is None:
            return qs
        from YSE_App.services.photstat_visibility import stat_expression

        return qs.annotate(_filter_recent_mag=stat_expression(_filter_viewer(self), 'last_detected_mag')
                           ).filter(_filter_recent_mag__lt=value)

    def filter_ex(self, qs, name, value):
        if value:
            qs = annotate_search_fields(qs, _filter_viewer(self), with_recent_mag=True)
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
