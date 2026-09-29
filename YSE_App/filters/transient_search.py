"""Transient search filters shared by ``/search/`` and ``/api/transients/`` (#284, #285, #286).

One ``TransientSearchFilterSet`` answers the questions a follow-up team asks
of the transient table, the way SkyPortal's source search does (its filter
list was the reference; the SQL here is YSE-PZ's own):

* **names**: exact name, name contains, name-or-alias contains
  (``AlternateTransientNames``), has a TNS-style name;
* **position**: cone search (RA/Dec in degrees or sexagesimal plus a radius
  in arcsec), RA/Dec ranges, absolute galactic latitude range;
* **time**: discovery, created and modified windows, days since discovery;
* **photometry**: peak / latest magnitude, detection count, first / last
  detection windows, days since the last detection, rise / decay rate and
  deepest upper limit, all read from the stored ``TransientPhotStat`` row
  (#268) so no filter touches the raw photometry table;
* **classification**: spectroscopic, photometric, either, or neither of a
  set of classes; has a redshift; redshift range on the transient's own or
  the host's redshift;
* **relations**: status, observation group, internal survey, tags (any /
  all), has spectrum, has follow-up (optionally in a status), has comment,
  has host, host redshift range, visible to one collaboration group;
* **annotations** (#319): has an origin, has a key, a key's value equals /
  at least / at most (on the indexed ``TransientAnnotationValue`` side table;
  ``legacy.<column>`` reads the ``Transient`` column itself), and an optional
  results column showing one ``origin.key``;
* **ordering** on every plain column, every stat column, the cone
  separation and the galactic latitude.

Every filter is one SQL clause on the transient table: FK columns join, the
stat row is a LEFT JOIN, relations are ``EXISTS`` subqueries (no ``DISTINCT``),
the cone is a bounding box on the indexed ``ra`` / ``dec`` columns followed by
an exact great-circle separation, and the galactic latitude is a closed-form
expression of ``ra`` and ``dec``. So one query lists the matches whatever the
combination of filters, and the same class serves the DRF viewset (where the
legacy parameter names ``created_date_gte``, ``status_in``, ``tag_in``,
``peak_mag_lte`` ... keep working) and the search page.
"""

import math
import re

import django_filters
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import CharField, DateTimeField, Exists, F, FloatField, Func, OuterRef, Q, Subquery, Value
from django.db.models.functions import (
    Abs,
    ACos,
    ASin,
    Coalesce,
    Cos,
    Degrees,
    Greatest,
    Least,
    Radians,
    Sin,
)
from django.utils import timezone
from django_filters import widgets as filter_widgets

from YSE_App.common.utilities import getRADecBox
from YSE_App.models import (
    AlternateTransientNames,
    FollowupStatus,
    InternalSurvey,
    Log,
    ObservationGroup,
    Transient,
    TransientAnnotation,
    TransientAnnotationValue,
    TransientClass,
    TransientFollowup,
    TransientPhotometry,
    TransientSpectrum,
    TransientStatus,
    TransientTag,
)
from YSE_App.models.phot_stat_models import datetime_to_mjd

__all__ = [
    'TransientSearchFilterSet',
    'FIELD_GROUPS',
    'DEFAULT_RADIUS_ARCSEC',
    'annotate_gal_b',
    'annotate_best_redshift',
    'annotate_separation',
    'annotate_annotation_column',
    'parse_annotation_column',
    'UtcNowMinusSeconds',
    'NowMjd',
    'cone_q',
    'parse_coordinate_pair',
    'parse_quick_search',
    'quick_search_params',
]

# North galactic pole, J2000 (Reid & Brunthaler 2004 values used by astropy).
NGP_RA_DEG = 192.85948
NGP_DEC_DEG = 27.12825

DEFAULT_RADIUS_ARCSEC = 5.0

# Names issued by the Transient Name Server: 20YYabc (one to four letters).
TNS_NAME_REGEX = r'^20[0-9]{2}[a-z]{1,4}$'


def _float(value, output_field=None):
    return Value(float(value), output_field=output_field or FloatField())


def _clamped(expr):
    """``expr`` clamped to [-1, 1] so rounding never takes ACOS/ASIN out of range."""
    return Least(_float(1.0), Greatest(_float(-1.0), expr))


def galactic_latitude_expression():
    """Galactic latitude ``b`` (degrees) of the row's ``ra`` / ``dec``.

    ``sin b = sin(dec) sin(dec_NGP) + cos(dec) cos(dec_NGP) cos(ra - ra_NGP)``,
    a closed form the database evaluates per row; the stored ``gal_b`` column
    of #286 can replace this without changing callers.
    """
    dec = Radians(F('dec'))
    ra = Radians(F('ra'))
    sin_b = (
        Sin(dec) * _float(math.sin(math.radians(NGP_DEC_DEG)))
        + Cos(dec) * _float(math.cos(math.radians(NGP_DEC_DEG))) * Cos(ra - _float(math.radians(NGP_RA_DEG)))
    )
    return Degrees(ASin(_clamped(sin_b)))


def separation_expression(ra_deg, dec_deg):
    """Great-circle separation (degrees) of the row from ``(ra_deg, dec_deg)``.

    The same spherical-cosine form as ``data_utils.box_search``, clamped so a
    row exactly at the centre cannot produce ``ACOS(1.0000001)``.
    """
    dec = Radians(F('dec'))
    ra = Radians(F('ra'))
    dec0 = math.radians(float(dec_deg))
    ra0 = math.radians(float(ra_deg))
    cos_sep = (
        Sin(dec) * _float(math.sin(dec0))
        + Cos(dec) * _float(math.cos(dec0)) * Cos(ra - _float(ra0))
    )
    return Degrees(ACos(_clamped(cos_sep)))


def annotate_gal_b(qs):
    if 'gal_b' in qs.query.annotations:
        return qs
    return qs.annotate(gal_b=galactic_latitude_expression())


def annotate_best_redshift(qs):
    """The transient's redshift, else the host's (what the tables show)."""
    if 'best_redshift' in qs.query.annotations:
        return qs
    return qs.annotate(best_redshift=Coalesce('redshift', 'host__redshift'))


def annotate_separation(qs, ra_deg, dec_deg):
    return qs.annotate(separation=separation_expression(ra_deg, dec_deg))


def parse_annotation_column(text):
    """``(origin or None, key)`` from ``origin.key`` or ``key``; ``None`` for blank."""
    text = (text or '').strip()
    if not text:
        return None
    origin, sep, key = text.partition('.')
    if not sep:
        return None, origin
    if not key:
        return None
    return origin, key


def _annotation_value_rows(origin, key):
    rows = TransientAnnotationValue.objects.filter(transient_id=OuterRef('pk'), key=key)
    if origin:
        rows = rows.filter(origin=origin)
    return rows.order_by('origin')


def annotate_annotation_column(qs, origin, key):
    """``annotation_value`` (text) and ``annotation_value_num`` of one ``origin.key`` per row, as subqueries."""
    if 'annotation_value' in qs.query.annotations:
        return qs
    from YSE_App.services.annotations import LEGACY_COLUMNS, LEGACY_NUMERIC_KEYS

    if origin == 'legacy' and key in LEGACY_COLUMNS:
        column = LEGACY_COLUMNS[key]
        if key in LEGACY_NUMERIC_KEYS:
            return qs.annotate(annotation_value=F(column), annotation_value_num=F(column))
        return qs.annotate(annotation_value=F(column), annotation_value_num=Value(None, output_field=FloatField()))
    rows = _annotation_value_rows(origin, key)
    return qs.annotate(
        annotation_value=Subquery(rows.values('value_text')[:1], output_field=CharField()),
        annotation_value_num=Subquery(rows.values('value_num')[:1], output_field=FloatField()),
    )


def _box_q(ramin, ramax, decmin, decmax):
    """Bounding-box ``Q`` on the indexed ``ra`` / ``dec`` columns, RA wrap-safe."""
    q = Q(dec__gte=decmin, dec__lte=decmax)
    if decmax >= 90.0 or decmin <= -90.0 or ramax - ramin >= 360.0:
        return q  # the cone contains a pole: RA is unconstrained
    if ramin < 0.0:
        return q & (Q(ra__gte=ramin + 360.0) | Q(ra__lte=ramax))
    if ramax > 360.0:
        return q & (Q(ra__gte=ramin) | Q(ra__lte=ramax - 360.0))
    return q & Q(ra__gte=ramin, ra__lte=ramax)


def cone_q(ra_deg, dec_deg, radius_arcsec):
    """Bounding-box prefilter for a cone (the exact cut is on ``separation``)."""
    radius_deg = float(radius_arcsec) / 3600.0
    ramin, ramax, decmin, decmax = getRADecBox(float(ra_deg), float(dec_deg), size=2.0 * radius_deg)
    return _box_q(ramin, ramax, decmin, decmax)


def parse_coordinate_pair(ra_text, dec_text):
    """``(ra_deg, dec_deg)`` from decimal degrees or sexagesimal strings.

    ``"10.5", "-20.25"`` are degrees; anything else (``"10:30:00"``,
    ``"10h30m00s"``, ``"10 30 00"``) is hours for RA and degrees for Dec, as
    the header search box has always accepted.
    """
    ra_text = str(ra_text).strip()
    dec_text = str(dec_text).strip()
    try:
        ra, dec = float(ra_text), float(dec_text)
    except ValueError:
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        try:
            sc = SkyCoord(ra_text, dec_text, unit=(u.hourangle, u.deg))
        except Exception as exc:  # astropy raises several types
            raise ValueError('Could not read "%s %s" as RA and Dec.' % (ra_text, dec_text)) from exc
        ra, dec = float(sc.ra.deg), float(sc.dec.deg)
    if not -90.0 <= dec <= 90.0:
        raise ValueError('Dec %.4f is outside -90..90.' % dec)
    return ra % 360.0, dec


_QUICK_SPLIT = re.compile(r'[,\s]+')


def parse_quick_search(text):
    """Interpret the header search box.

    Returns ``('cone', ra_deg, dec_deg, radius_arcsec)`` when ``text`` reads as
    coordinates (``ra dec``, ``ra, dec``, ``ra dec radius_arcsec``, or six
    sexagesimal fields with an optional radius) and ``('name', text)``
    otherwise. Coordinates that fail to parse fall back to a name search, so
    a typo never produces an error page.
    """
    text = (text or '').strip()
    if not text:
        return ('name', '')
    tokens = [t for t in _QUICK_SPLIT.split(text) if t]
    ra_text = dec_text = None
    radius = DEFAULT_RADIUS_ARCSEC
    if len(tokens) in (2, 3):
        ra_text, dec_text = tokens[0], tokens[1]
        if len(tokens) == 3:
            radius = tokens[2]
    elif len(tokens) in (6, 7):
        ra_text, dec_text = ' '.join(tokens[0:3]), ' '.join(tokens[3:6])
        if len(tokens) == 7:
            radius = tokens[6]
    if ra_text is None:
        return ('name', text)
    try:
        ra, dec = parse_coordinate_pair(ra_text, dec_text)
        radius = float(radius)
    except ValueError:
        return ('name', text)
    if radius <= 0:
        radius = DEFAULT_RADIUS_ARCSEC
    return ('cone', ra, dec, radius)


def quick_search_params(params):
    """A mutable copy of ``params`` (a ``QueryDict``) with ``q`` expanded.

    ``q=2026abc`` becomes ``name_contains=2026abc``; ``q=10.5 -20.25`` becomes
    ``ra=10.5&dec=-20.25&radius_arcsec=5``. Explicit filter parameters win
    over ``q``. The copy is what the FilterSet and its form see, so the form
    shows the values the quick search stood for and the URL stays shareable.
    """
    data = params.copy()
    q = data.get('q')
    if q is None:
        return data
    parsed = parse_quick_search(q)
    if parsed[0] == 'cone':
        _, ra, dec, radius = parsed
        data.setdefault('ra', '%.6f' % ra)
        data.setdefault('dec', '%.6f' % dec)
        data.setdefault('radius_arcsec', ('%g' % radius))
    elif parsed[1]:
        data.setdefault('name_contains', parsed[1])
    return data


def _rows_visible_to(qs, user):
    """Restrict grouped rows (photometry / spectra) to what ``user`` may see."""
    if user is None or user.is_staff or user.is_superuser:
        return qs
    names = list(user.groups.values_list('name', flat=True)) if user.is_authenticated else []
    return qs.filter(Q(groups__isnull=True) | Q(groups__name__in=names))


class UtcNowMinusSeconds(Func):
    """``now - seconds`` evaluated by the database, so saved SQL stays relative.

    The relative-time filters (``days_since_disc_max``, ``days_since_last_det_max``)
    used to compare against ``timezone.now()`` from Python; a search saved as an
    Explorer query (``services/search_queries.py``) would then freeze that
    instant. ``UTC_TIMESTAMP()`` on MySQL and ``datetime('now')`` on sqlite are
    both UTC, which is how ``DateTimeField`` values are stored.
    """

    output_field = DateTimeField()

    def __init__(self, seconds):
        self.seconds = int(round(float(seconds)))
        super().__init__()

    def as_sql(self, compiler, connection, **extra_context):
        return '(UTC_TIMESTAMP() - INTERVAL %d SECOND)' % self.seconds, []

    def as_mysql(self, compiler, connection, **extra_context):
        return self.as_sql(compiler, connection, **extra_context)

    def as_sqlite(self, compiler, connection, **extra_context):
        return "datetime('now', '-%d seconds')" % self.seconds, []


class NowMjd(Func):
    """The current MJD, evaluated by the database (see ``UtcNowMinusSeconds``)."""

    output_field = FloatField()

    def __init__(self):
        super().__init__()

    def as_sql(self, compiler, connection, **extra_context):
        # UNIX_TIMESTAMP() with no argument is epoch seconds, independent of the
        # session time zone; 40587 is the MJD of 1970-01-01.
        return '(UNIX_TIMESTAMP() / 86400.0 + 40587.0)', []

    def as_mysql(self, compiler, connection, **extra_context):
        return self.as_sql(compiler, connection, **extra_context)

    def as_sqlite(self, compiler, connection, **extra_context):
        return "(julianday('now') - 2400000.5)", []


def _now_mjd():
    return datetime_to_mjd(timezone.now())


class TransientSearchForm(forms.Form):
    """Form behind the FilterSet: validates the cone centre as a pair."""

    cone_center = None

    def clean(self):
        cleaned = super().clean()
        ra, dec = cleaned.get('ra'), cleaned.get('dec')
        if bool(ra) != bool(dec):
            raise ValidationError('Give both RA and Dec for a cone search.')
        if ra and dec:
            try:
                self.cone_center = parse_coordinate_pair(ra, dec)
            except ValueError as exc:
                raise ValidationError(str(exc))
        has_value = any(cleaned.get(n) not in (None, '') for n in
                        ('annotation_value_eq', 'annotation_value_min', 'annotation_value_max'))
        if has_value and not cleaned.get('annotation_key'):
            raise ValidationError('Give an annotation key to filter on its value.')
        column = cleaned.get('annotation_column')
        if column and parse_annotation_column(column) is None:
            raise ValidationError('Annotation column must be "origin.key" or "key".')
        return cleaned


class StableOrderingFilter(django_filters.OrderingFilter):
    """``ordering`` with a primary-key tie-breaker so pages never overlap."""

    def filter(self, qs, value):
        if not value or not any(v for v in value):
            return qs
        ordering = [self.get_ordering_value(v) for v in value if v]
        tie = '-pk' if ordering and ordering[0].startswith('-') else 'pk'
        return qs.order_by(*ordering, tie)


class AnyBooleanWidget(filter_widgets.BooleanWidget):
    """``BooleanWidget`` whose empty choice reads "Any" instead of "Unknown"."""

    def __init__(self, attrs=None):
        choices = (('', 'Any'), ('true', 'Yes'), ('false', 'No'))
        super(filter_widgets.BooleanWidget, self).__init__(attrs, choices)


class _NameChoiceField(forms.ModelMultipleChoiceField):
    """Options labelled by ``name`` (``TransientTag.__str__`` says ``Name: x``)."""

    def label_from_instance(self, obj):
        return obj.name


class NameMultipleChoiceFilter(django_filters.ModelMultipleChoiceFilter):
    field_class = _NameChoiceField


def _bool(method, label):
    return django_filters.BooleanFilter(method=method, label=label, widget=AnyBooleanWidget())


def _num(field_name, lookup, label):
    return django_filters.NumberFilter(field_name=field_name, lookup_expr=lookup, label=label)


def _when(field_name, lookup, label):
    # forms.DateTimeField (Django >= 3.1) reads both ``2026-09-01`` and ISO
    # 8601 ``2026-09-01T12:00:00Z``; naive values are taken in the current
    # time zone (UTC on the servers).
    return django_filters.DateTimeFilter(
        field_name=field_name, lookup_expr=lookup, label=label,
        widget=forms.DateTimeInput(attrs={'placeholder': 'YYYY-MM-DD[THH:MM]'}),
    )


def _by_pk(field_name):
    """``method`` for a ModelMultipleChoiceFilter: ``<fk>__in`` on the chosen rows' pks."""
    def _filter(qs, name, value):
        if not value:
            return qs
        return qs.filter(**{field_name + '__in': [obj.pk for obj in value]})
    return _filter


def _names(model, label, **kwargs):
    """Multi-select of ``model`` rows addressed by ``name`` (``?status=New&status=Watch``)."""
    return NameMultipleChoiceFilter(
        queryset=model.objects.order_by('name'), to_field_name='name', label=label,
        distinct=False, **kwargs,
    )


# Field groups the search page renders (the legacy API-only names stay out).
FIELD_GROUPS = (
    ('Names', ('name_contains', 'alias', 'name', 'has_tns_name')),
    ('Position', ('ra', 'dec', 'radius_arcsec', 'ra_gte', 'ra_lte', 'dec_gte', 'dec_lte',
                  'gal_b_abs_min', 'gal_b_abs_max')),
    ('Time', ('disc_date_after', 'disc_date_before', 'days_since_disc_max',
              'created_after', 'created_before', 'modified_after', 'modified_before')),
    ('Photometry', ('peak_mag_min', 'peak_mag_max', 'latest_mag_min', 'latest_mag_max',
                    'num_det_min', 'num_det_max', 'first_det_after', 'first_det_before',
                    'last_det_after', 'last_det_before', 'days_since_last_det_max',
                    'rise_rate_min', 'rise_rate_max', 'decay_rate_min', 'decay_rate_max',
                    'deepest_limit_min', 'deepest_limit_max')),
    ('Classification', ('classification', 'spec_class', 'photo_class', 'exclude_class',
                        'has_redshift', 'redshift_min', 'redshift_max')),
    ('Relations', ('status', 'obs_group', 'internal_survey', 'tags', 'tags_all',
                   'has_spectrum', 'has_followup', 'followup_status', 'has_comment',
                   'has_host', 'host_redshift_min', 'host_redshift_max', 'visible_to_group')),
    ('Annotations', ('annotation_origin', 'annotation_key', 'annotation_value_eq', 'annotation_value_min',
                     'annotation_value_max', 'annotation_column')),
)

ORDERING_FIELDS = (
    ('name', 'name'), ('ra', 'ra'), ('dec', 'dec'), ('disc_date', 'disc_date'),
    ('created_date', 'created_date'), ('modified_date', 'modified_date'),
    ('redshift', 'redshift'), ('best_redshift', 'best_redshift'), ('mw_ebv', 'mw_ebv'),
    ('photstat__peak_mag', 'peak_mag'), ('photstat__peak_mjd', 'peak_mjd'),
    ('photstat__last_detected_mag', 'last_det_mag'),
    ('photstat__last_detected_mjd', 'last_det_mjd'),
    ('photstat__first_detected_mjd', 'first_det_mjd'),
    ('photstat__num_det_global', 'num_det'),
    ('photstat__rise_rate', 'rise_rate'), ('photstat__decay_rate', 'decay_rate'),
    ('photstat__deepest_limit', 'deepest_limit'),
    ('separation', 'separation'), ('gal_b', 'gal_b'),
    ('annotation_value', 'annotation_value'),
)


class TransientSearchFilterSet(django_filters.FilterSet):
    """See the module docstring. Pass ``request=`` so group filters know the user."""

    # --- names -----------------------------------------------------------
    name = django_filters.CharFilter(field_name='name', lookup_expr='exact', label='Name (exact)')
    name_contains = django_filters.CharFilter(field_name='name', lookup_expr='icontains', label='Name contains')
    alias = django_filters.CharFilter(method='filter_alias', label='Name or alias contains')
    has_tns_name = _bool('filter_has_tns_name', 'Has TNS name')

    # --- position --------------------------------------------------------
    ra = django_filters.CharFilter(method='noop', label='RA (deg or h:m:s)')
    dec = django_filters.CharFilter(method='noop', label='Dec (deg or d:m:s)')
    radius_arcsec = django_filters.NumberFilter(method='noop', label='Radius (arcsec)', min_value=0)
    ra_gte = _num('ra', 'gte', 'RA at least (deg)')
    ra_lte = _num('ra', 'lte', 'RA at most (deg)')
    dec_gte = _num('dec', 'gte', 'Dec at least (deg)')
    dec_lte = _num('dec', 'lte', 'Dec at most (deg)')
    gal_b_abs_min = django_filters.NumberFilter(method='filter_gal_b_abs_min', label='|b| at least (deg)', min_value=0)
    gal_b_abs_max = django_filters.NumberFilter(method='filter_gal_b_abs_max', label='|b| at most (deg)', min_value=0)

    # --- time ------------------------------------------------------------
    disc_date_after = _when('disc_date', 'gte', 'Discovered after')
    disc_date_before = _when('disc_date', 'lte', 'Discovered before')
    days_since_disc_max = django_filters.NumberFilter(method='filter_days_since_disc_max', label='Discovered within (days)', min_value=0)
    created_after = _when('created_date', 'gte', 'Created after')
    created_before = _when('created_date', 'lte', 'Created before')
    modified_after = _when('modified_date', 'gte', 'Modified after')
    modified_before = _when('modified_date', 'lte', 'Modified before')
    # legacy API names
    created_date_gte = django_filters.DateTimeFilter(field_name='created_date', lookup_expr='gte')
    modified_date_gte = django_filters.DateTimeFilter(field_name='modified_date', lookup_expr='gte')

    # --- photometry (TransientPhotStat, #268) ------------------------------
    peak_mag_min = _num('photstat__peak_mag', 'gte', 'Peak mag at least')
    peak_mag_max = _num('photstat__peak_mag', 'lte', 'Peak mag at most')
    latest_mag_min = _num('photstat__last_detected_mag', 'gte', 'Latest mag at least')
    latest_mag_max = _num('photstat__last_detected_mag', 'lte', 'Latest mag at most')
    num_det_min = _num('photstat__num_det_global', 'gte', 'Detections at least')
    num_det_max = _num('photstat__num_det_global', 'lte', 'Detections at most')
    first_det_after = _when('photstat__first_detected_date', 'gte', 'First detected after')
    first_det_before = _when('photstat__first_detected_date', 'lte', 'First detected before')
    last_det_after = _when('photstat__last_detected_date', 'gte', 'Last detected after')
    last_det_before = _when('photstat__last_detected_date', 'lte', 'Last detected before')
    days_since_last_det_max = django_filters.NumberFilter(method='filter_days_since_last_det_max', label='Last detected within (days)', min_value=0)
    rise_rate_min = _num('photstat__rise_rate', 'gte', 'Rise rate at least (mag/day)')
    rise_rate_max = _num('photstat__rise_rate', 'lte', 'Rise rate at most (mag/day)')
    decay_rate_min = _num('photstat__decay_rate', 'gte', 'Decay rate at least (mag/day)')
    decay_rate_max = _num('photstat__decay_rate', 'lte', 'Decay rate at most (mag/day)')
    deepest_limit_min = _num('photstat__deepest_limit', 'gte', 'Deepest limit at least')
    deepest_limit_max = _num('photstat__deepest_limit', 'lte', 'Deepest limit at most')
    # legacy API names (PR #341)
    peak_mag_lte = _num('photstat__peak_mag', 'lte', 'Peak mag at most')
    peak_mag_gte = _num('photstat__peak_mag', 'gte', 'Peak mag at least')
    last_det_mag_lte = _num('photstat__last_detected_mag', 'lte', 'Latest mag at most')
    last_det_mag_gte = _num('photstat__last_detected_mag', 'gte', 'Latest mag at least')
    last_det_mjd_gte = _num('photstat__last_detected_mjd', 'gte', 'Last detected MJD at least')
    last_det_mjd_lte = _num('photstat__last_detected_mjd', 'lte', 'Last detected MJD at most')
    first_det_mjd_gte = _num('photstat__first_detected_mjd', 'gte', 'First detected MJD at least')
    first_det_mjd_lte = _num('photstat__first_detected_mjd', 'lte', 'First detected MJD at most')
    num_det_gte = _num('photstat__num_det_global', 'gte', 'Detections at least')
    rise_rate_gte = _num('photstat__rise_rate', 'gte', 'Rise rate at least')
    decay_rate_gte = _num('photstat__decay_rate', 'gte', 'Decay rate at least')

    # --- classification -------------------------------------------------
    classification = _names(TransientClass, 'Spec. or phot. class', method='filter_classification')
    spec_class = _names(TransientClass, 'Spec. class', method=_by_pk('best_spec_class'))
    photo_class = _names(TransientClass, 'Phot. class', method=_by_pk('photo_class'))
    exclude_class = _names(TransientClass, 'Exclude class', method='filter_exclude_class')
    has_redshift = _bool('filter_has_redshift', 'Has redshift')
    redshift_min = django_filters.NumberFilter(method='filter_redshift_min', label='Redshift at least')
    redshift_max = django_filters.NumberFilter(method='filter_redshift_max', label='Redshift at most')

    # --- relations -------------------------------------------------------
    status = _names(TransientStatus, 'Status', method=_by_pk('status'))
    status_in = django_filters.BaseInFilter(field_name='status__name')  # legacy
    obs_group = _names(ObservationGroup, 'Observation group', method=_by_pk('obs_group'))
    internal_survey = _names(InternalSurvey, 'Internal survey', method=_by_pk('internal_survey'))
    tags = _names(TransientTag, 'Any of these tags', method='filter_tags_any')
    tags_all = _names(TransientTag, 'All of these tags', method='filter_tags_all')
    tag_in = django_filters.BaseInFilter(method='filter_tag_in')  # legacy
    has_spectrum = _bool('filter_has_spectrum', 'Has spectrum')
    has_followup = _bool('filter_has_followup', 'Has follow-up')
    followup_status = _names(FollowupStatus, 'Follow-up status', method='filter_followup_status')
    has_comment = _bool('filter_has_comment', 'Has comment')
    has_host = _bool('filter_has_host', 'Has host')
    host_redshift_min = _num('host__redshift', 'gte', 'Host redshift at least')
    host_redshift_max = _num('host__redshift', 'lte', 'Host redshift at most')
    visible_to_group = django_filters.CharFilter(method='filter_visible_to_group', label='Data visible to group')

    # --- annotations (#319) ------------------------------------------------
    annotation_origin = django_filters.CharFilter(method='noop', label='Annotation origin',
                                                  widget=forms.TextInput(attrs={'placeholder': 'gaia_dr3, wise, quasar, legacy'}))
    annotation_key = django_filters.CharFilter(method='noop', label='Annotation key',
                                               widget=forms.TextInput(attrs={'placeholder': 'parallax_over_error'}))
    annotation_value_eq = django_filters.CharFilter(method='noop', label='Annotation value equals')
    annotation_value_min = django_filters.NumberFilter(method='noop', label='Annotation value at least')
    annotation_value_max = django_filters.NumberFilter(method='noop', label='Annotation value at most')
    annotation_column = django_filters.CharFilter(method='noop', label='Show annotation column',
                                                  widget=forms.TextInput(attrs={'placeholder': 'origin.key'}))

    ordering = StableOrderingFilter(fields=ORDERING_FIELDS, label='Order by')

    class Meta:
        model = Transient
        fields = ()
        form = TransientSearchForm

    # ------------------------------------------------------------------
    def noop(self, qs, name, value):
        return qs

    @property
    def cone(self):
        """``(ra_deg, dec_deg, radius_arcsec)`` when a cone search is active, else ``None``."""
        form = self.form
        if not form.is_valid() or form.cone_center is None:
            return None
        radius = form.cleaned_data.get('radius_arcsec')
        radius = float(radius) if radius else DEFAULT_RADIUS_ARCSEC
        return form.cone_center[0], form.cone_center[1], radius

    def filter_queryset(self, queryset):
        cone = self.cone
        cleaned = self.form.cleaned_data
        ordering = [o for o in (cleaned.get('ordering') or []) if o]
        wanted = {o.lstrip('-') for o in ordering}
        if cone is not None:
            ra, dec, radius = cone
            queryset = annotate_separation(queryset.filter(cone_q(ra, dec, radius)), ra, dec)
            queryset = queryset.filter(separation__lte=radius / 3600.0)
            if not ordering:
                ordering = ['separation']
        elif 'separation' in wanted:
            ordering = [o for o in ordering if o.lstrip('-') != 'separation']
        if 'gal_b' in wanted:
            queryset = annotate_gal_b(queryset)
        if 'best_redshift' in wanted:
            queryset = annotate_best_redshift(queryset)
        queryset = self._filter_annotations(queryset, cleaned)
        column = self.annotation_column_spec
        if column is not None:
            queryset = annotate_annotation_column(queryset, column[0], column[1])
            if 'annotation_value' in wanted:
                # numbers first (nulls sort as the database likes), then the text form
                expanded = []
                for o in ordering:
                    if o.lstrip('-') == 'annotation_value':
                        prefix = '-' if o.startswith('-') else ''
                        expanded += [prefix + 'annotation_value_num', prefix + 'annotation_value']
                    else:
                        expanded.append(o)
                ordering = expanded
        elif 'annotation_value' in wanted:
            ordering = [o for o in ordering if o.lstrip('-') != 'annotation_value']
        if 'ordering' in cleaned:
            cleaned['ordering'] = ordering
        return super().filter_queryset(queryset)

    # --- annotations (#319) -----------------------------------------------
    @property
    def annotation_column_spec(self):
        """``(origin or None, key)`` of the results column: ``annotation_column``, else the filtered origin/key."""
        form = self.form
        if not form.is_valid():
            return None
        parsed = parse_annotation_column(form.cleaned_data.get('annotation_column'))
        if parsed is not None:
            return parsed
        key = (form.cleaned_data.get('annotation_key') or '').strip()
        if key:
            return ((form.cleaned_data.get('annotation_origin') or '').strip() or None, key)
        return None

    @property
    def annotation_column_label(self):
        column = self.annotation_column_spec
        if column is None:
            return ''
        return '%s.%s' % (column[0], column[1]) if column[0] else column[1]

    def _filter_annotations(self, qs, cleaned):
        """One ``EXISTS`` on the value side table (or a plain column filter for ``legacy``)."""
        origin = (cleaned.get('annotation_origin') or '').strip()
        key = (cleaned.get('annotation_key') or '').strip()
        eq = cleaned.get('annotation_value_eq')
        eq = str(eq).strip() if eq not in (None, '') else ''
        vmin = cleaned.get('annotation_value_min')
        vmax = cleaned.get('annotation_value_max')
        if not origin and not key:
            return qs
        if origin == 'legacy':
            return self._filter_legacy(qs, key, eq, vmin, vmax)
        if not key:
            return qs.filter(Exists(TransientAnnotation.objects.filter(transient_id=OuterRef('pk'), origin=origin)))
        rows = TransientAnnotationValue.objects.filter(transient_id=OuterRef('pk'), key=key)
        if origin:
            rows = rows.filter(origin=origin)
        if eq:
            try:
                rows = rows.filter(Q(value_text=eq) | Q(value_num=float(eq)))
            except ValueError:
                rows = rows.filter(value_text=eq)
        if vmin is not None:
            rows = rows.filter(value_num__gte=vmin)
        if vmax is not None:
            rows = rows.filter(value_num__lte=vmax)
        return qs.filter(Exists(rows))

    def _filter_legacy(self, qs, key, eq, vmin, vmax):
        from YSE_App.services.annotations import LEGACY_COLUMNS, LEGACY_NUMERIC_KEYS

        if not key:
            return qs
        column = LEGACY_COLUMNS.get(key)
        if column is None:
            return qs.none()
        if column == 'antares_classification':
            column = 'antares_classification__name'
        q = Q(**{column + '__isnull': False})
        if eq:
            if key in ('has_hst', 'has_spitzer', 'has_chandra'):
                q &= Q(**{column: eq.lower() in ('1', 'true', 'yes')})
            elif key in LEGACY_NUMERIC_KEYS:
                try:
                    q &= Q(**{column: float(eq)})
                except ValueError:
                    return qs.none()
            else:
                q &= Q(**{column: eq})
        if vmin is not None:
            q &= Q(**{column + '__gte': vmin})
        if vmax is not None:
            q &= Q(**{column + '__lte': vmax})
        return qs.filter(q)

    # --- names ---------------------------------------------------------
    def filter_alias(self, qs, name, value):
        aliases = AlternateTransientNames.objects.filter(transient_id=OuterRef('pk'), name__icontains=value)
        return qs.filter(Q(name__icontains=value) | Exists(aliases))

    def filter_has_tns_name(self, qs, name, value):
        if value is None:
            return qs
        if value:
            return qs.filter(name__iregex=TNS_NAME_REGEX)
        return qs.exclude(name__iregex=TNS_NAME_REGEX)

    # --- position ------------------------------------------------------
    def filter_gal_b_abs_min(self, qs, name, value):
        return annotate_gal_b(qs).annotate(_abs_gal_b=Abs('gal_b')).filter(_abs_gal_b__gte=value)

    def filter_gal_b_abs_max(self, qs, name, value):
        return annotate_gal_b(qs).annotate(_abs_gal_b=Abs('gal_b')).filter(_abs_gal_b__lte=value)

    # --- time ----------------------------------------------------------
    def filter_days_since_disc_max(self, qs, name, value):
        return qs.filter(disc_date__gte=UtcNowMinusSeconds(float(value) * 86400.0))

    def filter_days_since_last_det_max(self, qs, name, value):
        return qs.filter(photstat__last_detected_mjd__gte=NowMjd() - _float(value))

    # --- classification ------------------------------------------------
    def filter_classification(self, qs, name, value):
        if not value:
            return qs
        ids = [c.pk for c in value]
        return qs.filter(Q(best_spec_class__in=ids) | Q(photo_class__in=ids))

    def filter_exclude_class(self, qs, name, value):
        if not value:
            return qs
        ids = [c.pk for c in value]
        return qs.exclude(Q(best_spec_class__in=ids) | Q(photo_class__in=ids))

    def filter_has_redshift(self, qs, name, value):
        if value is None:
            return qs
        has = Q(redshift__isnull=False) | Q(host__redshift__isnull=False)
        return qs.filter(has) if value else qs.exclude(has)

    def filter_redshift_min(self, qs, name, value):
        return annotate_best_redshift(qs).filter(best_redshift__gte=value)

    def filter_redshift_max(self, qs, name, value):
        return annotate_best_redshift(qs).filter(best_redshift__lte=value)

    # --- relations -----------------------------------------------------
    def _tag_rows(self, names):
        through = Transient.tags.through
        return through.objects.filter(transient_id=OuterRef('pk'), transienttag__name__in=list(names))

    def filter_tags_any(self, qs, name, value):
        if not value:
            return qs
        return qs.filter(Exists(self._tag_rows(t.name for t in value)))

    def filter_tags_all(self, qs, name, value):
        for tag in value or ():
            qs = qs.filter(Exists(self._tag_rows([tag.name])))
        return qs

    def filter_tag_in(self, qs, name, value):
        names = [v for v in (value or []) if v]
        if not names:
            return qs
        return qs.filter(Exists(self._tag_rows(names)))

    def _user(self):
        request = getattr(self, 'request', None)
        return getattr(request, 'user', None) if request is not None else None

    def filter_has_spectrum(self, qs, name, value):
        if value is None:
            return qs
        spectra = _rows_visible_to(TransientSpectrum.objects.filter(transient_id=OuterRef('pk')), self._user())
        return qs.filter(Exists(spectra)) if value else qs.exclude(Exists(spectra))

    def filter_has_followup(self, qs, name, value):
        if value is None:
            return qs
        followups = TransientFollowup.objects.filter(transient_id=OuterRef('pk'))
        return qs.filter(Exists(followups)) if value else qs.exclude(Exists(followups))

    def filter_followup_status(self, qs, name, value):
        if not value:
            return qs
        followups = TransientFollowup.objects.filter(
            transient_id=OuterRef('pk'), status__in=[s.pk for s in value])
        return qs.filter(Exists(followups))

    def filter_has_comment(self, qs, name, value):
        if value is None:
            return qs
        comments = Log.objects.filter(transient_id=OuterRef('pk'), transient_followup__isnull=True)
        return qs.filter(Exists(comments)) if value else qs.exclude(Exists(comments))

    def filter_has_host(self, qs, name, value):
        if value is None:
            return qs
        return qs.filter(host__isnull=not value)

    def filter_visible_to_group(self, qs, name, value):
        """Transients with photometry or spectra shared with collaboration group ``value``.

        A user may only ask about groups they belong to (staff: any group);
        anything else matches nothing rather than leaking group membership.
        """
        user = self._user()
        if user is None or not user.is_authenticated:
            return qs.none()
        if not (user.is_staff or user.is_superuser):
            if not user.groups.filter(name=value).exists():
                return qs.none()
        phot = TransientPhotometry.objects.filter(transient_id=OuterRef('pk'), groups__name=value)
        spec = TransientSpectrum.objects.filter(transient_id=OuterRef('pk'), groups__name=value)
        return qs.filter(Exists(phot) | Exists(spec))

    # --- presentation helpers (search page) ------------------------------
    def active_filters(self):
        """``[(param, label, display value)]`` for every bound, non-empty filter."""
        out = []
        if not self.is_bound:
            return out
        form = self.form
        for name, filt in self.filters.items():
            raw = self.data.getlist(name) if hasattr(self.data, 'getlist') else [self.data.get(name)]
            raw = [r for r in raw if r not in (None, '')]
            if not raw:
                continue
            label = filt.label or form.fields[name].label or name.replace('_', ' ')
            out.append((name, label, ', '.join(str(r) for r in raw)))
        return out

    def group_counts(self):
        """Active filters per ``FIELD_GROUPS`` entry (for the group headers)."""
        active = {name for name, _, _ in self.active_filters()}
        return {title: sum(1 for f in fields if f in active) for title, fields in FIELD_GROUPS}
