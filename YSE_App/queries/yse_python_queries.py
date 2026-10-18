from YSE_App.models import *
from django.db.models import DateTimeField, ExpressionWrapper, F, Q
from django.db.models.functions import Greatest
from django.db.models.expressions import RawSQL
from datetime import timedelta
import inspect
import astropy.coordinates as cd
import astropy.units as u
import numpy as np
from YSE_App.common.utilities import *
from itertools import chain
from django.db import connection,connections
from django.db.models import FloatField
from YSE_App.queries.raw_sql import LAST_MAG_BY_BAND_SQL

def makeRegistrar():
    registry = {}
    def registrar(func):
        registry[func.__name__] = func
        return func	 # normally a decorator returns a wrapped function,
                     # but here we return func unmodified, after registering it
    registrar.all = registry
    return registrar
python_query_reg = makeRegistrar()


def get_python_query(name):
    """The registered python query called ``name`` (#505); KeyError when there is none."""
    func = python_query_reg.all.get(name) if isinstance(name, str) else None
    if func is None:
        raise KeyError(name)
    return func


def run_python_query(name, user=None):
    """Run the saved Python query ``name`` for ``user`` (#687).

    Queries that take a ``user`` (the rising-transient family) select rows
    using only the photometry that viewer may see, so a row cannot appear or
    vanish because of data they cannot read. Staff and ``user=None`` keep the
    all-data selection. Queries without a ``user`` parameter read no
    photometry (position and tag selections) and are called as before. The
    caller still applies ``filter_listed_transients`` to the rows.
    """
    func = get_python_query(name)
    if user is not None and 'user' in inspect.signature(func).parameters:
        return func(user=user)
    return func()

def annotate_recent_mag(qs, user=None):
    """``recent_mag`` for the saved-query and planning tables: the stored last detection (#270).

    ``TransientPhotStat.last_detected_mag`` (a LEFT JOIN, NULL without a stat
    row or detection) instead of a correlated photometry subquery per row, so
    the "Max Recent Mag" filter and the "Last Det." cell agree: the latest
    unflagged detection by the light-curve plot's rule. Explorer users read
    the same columns from ``yse_transient_photstat_v`` (docs/photstat.md).

    With ``user`` the value is the one that user may see (#607, #692): the
    stored per-scope stat for a non-staff viewer with hidden photometry, so
    filters on it cannot probe restricted points. Staff, and callers that
    pass no user, keep the all-data column.
    """
    if user is None:
        return qs.annotate(recent_mag=F('photstat__last_detected_mag'))
    from YSE_App.services.photstat_visibility import stat_expression

    return qs.annotate(recent_mag=stat_expression(user, 'last_detected_mag'))


def _visible_point_sql(user):
    """``(sql, params)`` of an ``EXISTS`` body keeping only photometry ``user`` may see, or ``None``.

    Correlated on the raw query's ``p`` (``TransientPhotometry``) alias and
    built from the central rule, so group rules and any later read-time
    grants (#648) apply. Staff (and ``user=None``) get ``None``: unchanged SQL.
    """
    from YSE_App.services.visibility import photometry_visible_q, user_sees_everything

    if user is None or user_sees_everything(user):
        return None
    qs = TransientPhotometry.objects.filter(photometry_visible_q(user)).extra(
        where=['`YSE_App_transientphotometry`.`id` = p.id']).values('pk')
    return qs.query.sql_with_params()


def _last_mag_sql(column, filters, offset, visible=None):
    sql = LAST_MAG_BY_BAND_SQL % (column, filters, offset)
    if visible is None:
        return sql, ()
    vis_sql, vis_params = visible
    return sql.replace('ORDER BY', 'AND EXISTS (%s)\n   ORDER BY' % vis_sql, 1), tuple(vis_params)


def annotate_rising_transient_qs(qs, user=None):
    """Rise-rate annotations; for a non-staff ``user`` only from photometry they may see (#692)."""
    visible = _visible_point_sql(user)

    g_filters = "('g-ZTF', 'g', 'V', 'gp', 'g-Sloan')"
    r_filters = "('r-ZTF', 'r', 'rp', 'r-Sloan')"
    i_filters = "('i-ZTF', 'i')"

    # g/V rise
    qs = qs.annotate(recent_g_mag=RawSQL(*_last_mag_sql('pd.mag',g_filters,0,visible))).\
            annotate(recent_g_mag_err=RawSQL(*_last_mag_sql('pd.mag_err',g_filters,0,visible))).\
            annotate(recent_g_obs=RawSQL(*_last_mag_sql('UNIX_TIMESTAMP(pd.obs_date)',g_filters,0,visible))).\
            annotate(second_most_recent_g_mag=RawSQL(*_last_mag_sql('pd.mag',g_filters,1,visible))).\
            annotate(second_most_recent_g_obs=RawSQL(*_last_mag_sql('UNIX_TIMESTAMP(pd.obs_date)',g_filters,1,visible))).\
            annotate(second_most_recent_g_ulim=RawSQL(*_last_mag_sql('-2.5*LOG10(pd.flux+3*pd.flux_err)+27.5 as lim',g_filters,1,visible))).\
            annotate(second_most_recent_g_flux=RawSQL(*_last_mag_sql('pd.flux',g_filters,1,visible))).\
            annotate(second_most_recent_g_flux_err=RawSQL(*_last_mag_sql('pd.flux_err',g_filters,1,visible)))

    qs = qs.annotate(dm_g=-1.0*(F('recent_g_mag') - F('second_most_recent_g_mag')))
    qs = qs.annotate(dt_g=F('recent_g_obs') - F('second_most_recent_g_obs'))
    qs = qs.annotate(rise_rate_g=ExpressionWrapper(F('dm_g')/(Greatest(F('dt_g'),4.*60.*60.)/24./60./60.),output_field=FloatField()))
    qs = qs.annotate(dm_lim_g=-1.0*(F('recent_g_mag') - F('second_most_recent_g_ulim')))
    qs = qs.annotate(rise_rate_g_lim=ExpressionWrapper(F('dm_lim_g')/(F('dt_g')/24./60./60.),output_field=FloatField()))
    qs = qs.annotate(rise_rate_g_sig=F('dm_g')/F('recent_g_mag_err'))
    qs = qs.annotate(rise_rate_g_lim_sig=F('dm_lim_g')/F('recent_g_mag_err'))

    # r rise
    qs = qs.\
            annotate(recent_r_mag=RawSQL(*_last_mag_sql('pd.mag',r_filters,0,visible))).\
            annotate(recent_r_mag_err=RawSQL(*_last_mag_sql('pd.mag_err',r_filters,0,visible))).\
            annotate(recent_r_obs=RawSQL(*_last_mag_sql('UNIX_TIMESTAMP(pd.obs_date)',r_filters,0,visible))).\
            annotate(second_most_recent_r_mag=RawSQL(*_last_mag_sql('pd.mag',r_filters,1,visible))).\
            annotate(second_most_recent_r_obs=RawSQL(*_last_mag_sql('UNIX_TIMESTAMP(pd.obs_date)',r_filters,1,visible))).\
            annotate(second_most_recent_r_ulim=RawSQL(*_last_mag_sql('-2.5*LOG10(pd.flux+3*pd.flux_err)+27.5 as lim',r_filters,1,visible))).\
            annotate(second_most_recent_r_flux=RawSQL(*_last_mag_sql('pd.flux',r_filters,1,visible))).\
            annotate(second_most_recent_r_flux_err=RawSQL(*_last_mag_sql('pd.flux_err',r_filters,1,visible)))

    qs = qs.annotate(dm_r=-1.0*(F('recent_r_mag') - F('second_most_recent_r_mag')))
    qs = qs.annotate(dt_r=F('recent_r_obs') - F('second_most_recent_r_obs'))
    qs = qs.annotate(rise_rate_r=ExpressionWrapper(F('dm_r')/(F('dt_r')/24./60./60.),output_field=FloatField()))
    qs = qs.annotate(dm_lim_r=-1.0*(F('recent_r_mag') - F('second_most_recent_r_ulim')))
    qs = qs.annotate(rise_rate_r_lim=ExpressionWrapper(F('dm_lim_r')/(F('dt_r')/24./60./60.),output_field=FloatField()))
    qs = qs.annotate(rise_rate_r_sig=F('dm_r')/F('recent_r_mag_err'))
    qs = qs.annotate(rise_rate_r_lim_sig=F('dm_lim_r')/F('recent_r_mag_err'))

    # i rise
    qs = qs.\
        annotate(recent_i_mag=RawSQL(*_last_mag_sql('pd.mag',i_filters,0,visible))).\
        annotate(recent_i_mag_err=RawSQL(*_last_mag_sql('pd.mag_err',i_filters,0,visible))).\
        annotate(recent_i_obs=RawSQL(*_last_mag_sql('UNIX_TIMESTAMP(pd.obs_date)',i_filters,0,visible))).\
        annotate(second_most_recent_i_mag=RawSQL(*_last_mag_sql('pd.mag',i_filters,1,visible))).\
        annotate(second_most_recent_i_obs=RawSQL(*_last_mag_sql('UNIX_TIMESTAMP(pd.obs_date)',i_filters,1,visible))).\
        annotate(second_most_recent_i_ulim=RawSQL(*_last_mag_sql('-2.5*LOG10(pd.flux+3*pd.flux_err)+27.5 as lim',i_filters,1,visible))).\
        annotate(second_most_recent_i_flux=RawSQL(*_last_mag_sql('pd.flux',i_filters,1,visible))).\
        annotate(second_most_recent_i_flux_err=RawSQL(*_last_mag_sql('pd.flux_err',i_filters,1,visible)))

    qs = qs.annotate(dm_i=-1.0*(F('recent_i_mag') - F('second_most_recent_i_mag')))
    qs = qs.annotate(dt_i=F('recent_i_obs') - F('second_most_recent_i_obs'))
    qs = qs.annotate(rise_rate_i=ExpressionWrapper(F('dm_i')/(F('dt_i')/24./60./60.),output_field=FloatField()))
    qs = qs.annotate(dm_lim_i=-1.0*(F('recent_i_mag') - F('second_most_recent_i_ulim')))
    qs = qs.annotate(rise_rate_i_lim=ExpressionWrapper(F('dm_lim_i')/(F('dt_i')/24./60./60.),output_field=FloatField()))
    qs = qs.annotate(rise_rate_i_sig=F('dm_i')/F('recent_i_mag_err'))
    qs = qs.annotate(rise_rate_i_lim_sig=F('dm_lim_i')/F('recent_i_mag_err'))

    return qs

@python_query_reg
def fastrising_transient_queryset(ndays=7, user=None):

    # g/V rise
    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(ndays))
    qs = annotate_rising_transient_qs(qs, user)

    qs_final = qs.filter((Q(rise_rate_g__gte=0.5) & (Q(rise_rate_g_sig__gte=1)))|
                         (Q(rise_rate_g_lim__gte=0.5) & (Q(rise_rate_g_lim_sig__gte=1)))|
                         (Q(rise_rate_r__gte=0.5) & (Q(rise_rate_r_sig__gte=1)))|
                         (Q(rise_rate_r_lim__gte=0.5) & (Q(rise_rate_r_lim_sig__gte=1)))|
                         (Q(rise_rate_i__gte=0.5) & (Q(rise_rate_i_sig__gte=1)))|
                         (Q(rise_rate_i_lim__gte=0.5) & (Q(rise_rate_i_lim_sig__gte=1))))

    return qs_final

@python_query_reg
def rising_transient_queryset(ndays=14, user=None):

    # g/V rise
    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(ndays))
    qs = annotate_rising_transient_qs(qs, user)
    qs_final = qs.filter((Q(rise_rate_g__gte=0) & (Q(rise_rate_g_sig__gte=1)))|
                         (Q(rise_rate_g_lim__gte=0) & (Q(rise_rate_g_lim_sig__gte=1)))|
                         (Q(rise_rate_r__gte=0) & (Q(rise_rate_r_sig__gte=1)))|
                         (Q(rise_rate_r_lim__gte=0) & (Q(rise_rate_r_lim_sig__gte=1)))|
                         (Q(rise_rate_i__gte=0) & (Q(rise_rate_i_sig__gte=1)))|
                         (Q(rise_rate_i_lim__gte=0) & (Q(rise_rate_i_lim_sig__gte=1))))

    return qs_final

@python_query_reg
def tns_yse_rising_transient_queryset(ndays=14, user=None):

    # g/V rise
    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(ndays)).filter(tags__name__in='YSE').filter(name__startswith='20')
    qs = annotate_rising_transient_qs(qs, user)
    qs_final = qs.filter((Q(rise_rate_g__gte=0.0) & (Q(rise_rate_g_sig__gte=1.0)))|
                         (Q(rise_rate_g_lim__gte=0.0) & (Q(rise_rate_g_lim_sig__gte=1.0)))|
                         (Q(rise_rate_r__gte=0.0) & (Q(rise_rate_r_sig__gte=1.0)))|
                         (Q(rise_rate_r_lim__gte=0.0) & (Q(rise_rate_r_lim_sig__gte=1.0)))|
                         (Q(rise_rate_i__gte=0.0) & (Q(rise_rate_i_sig__gte=1.0)))|
                         (Q(rise_rate_i_lim__gte=0.0) & (Q(rise_rate_i_lim_sig__gte=1.0))))

    return qs_final


@python_query_reg
def recent_rising_transient_queryset(ndays=5, user=None):

    # g/V rise
    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(ndays))
    qs = annotate_rising_transient_qs(qs, user)
    qs_final = qs.filter((Q(rise_rate_g__gte=0) & (Q(rise_rate_g_sig__gte=1)))|
                         (Q(rise_rate_g_lim__gte=0) & (Q(rise_rate_g_lim_sig__gte=1)))|
                         (Q(rise_rate_r__gte=0) & (Q(rise_rate_r_sig__gte=1)))|
                         (Q(rise_rate_r_lim__gte=0) & (Q(rise_rate_r_lim_sig__gte=1)))|
                         (Q(rise_rate_i__gte=0) & (Q(rise_rate_i_sig__gte=1)))|
                         (Q(rise_rate_i_lim__gte=0) & (Q(rise_rate_i_lim_sig__gte=1))))

    return qs_final

@python_query_reg
def sne_in_last_nights_fields():

    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(90)).filter(tags__name='YSE')
    survey_obs = SurveyObservation.objects.filter(obs_mjd__gte=date_to_mjd(datetime.datetime.utcnow()-datetime.timedelta(1)))
    qs_list = []
    done_list = []
    for s in survey_obs:
        if s.survey_field.field_id in done_list: continue
        done_list += [s.survey_field.field_id]
        d = s.survey_field.dec_cen*np.pi/180
        width_corr = 3.3/np.abs(np.cos(d))
        # Define the tile offsets:
        ra_offset = cd.Angle(width_corr/2., unit=u.deg)
        dec_offset = cd.Angle(3.3/2., unit=u.deg)
        qs_list += [(Q(ra__gt = s.survey_field.ra_cen-ra_offset.degree) &
                     Q(ra__lt = s.survey_field.ra_cen+ra_offset.degree) &
                     Q(dec__gt = s.survey_field.dec_cen-dec_offset.degree) &
                     Q(dec__lt = s.survey_field.dec_cen+dec_offset.degree))]

    if len(qs_list):
        query_full = qs_list[0]
        for q in qs_list[1:]:
            query_full = np.bitwise_or(query_full,q)
        qs_final = qs.filter(query_full)
    else:
        qs_final = Transient.objects.none()

    return qs_final

@python_query_reg
def sne_15deg_from_yse(qs_start=None):

    survey_obs = SurveyObservation.objects.filter(Q(obs_mjd__gte=date_to_mjd(datetime.datetime.utcnow()-datetime.timedelta(14))) |
                                                  Q(mjd_requested__gte=date_to_mjd(datetime.datetime.utcnow()-datetime.timedelta(14))))
    done_list = []
    qs = Transient.objects.none()
    for s in survey_obs:
        if s.survey_field.field_id in done_list: continue
        done_list += [s.survey_field.field_id]
        d = s.survey_field.dec_cen*np.pi/180
        width_corr = 15/np.abs(np.cos(d))
        #radius = ((ra-s.survey_field.ra_cen)**2. + (dec-s.survey_field.dec_cen)**2.)**(1/2.)

        ra_offset = cd.Angle(width_corr, unit=u.deg)
        dec_offset = cd.Angle(15, unit=u.deg)

        query= """SELECT t.name, ACOS(SIN(t.dec*3.14159/180)*SIN(%s*3.14159/180)+COS(t.dec*3.14159/180)*COS(%s*3.14159/180)*COS((t.ra-%s)*3.14159/180))*180/3.14159 as sep
FROM YSE_App_transient t
WHERE t.ra > %s AND t.ra < %s AND t.dec > %s AND t.dec < %s
HAVING sep < 15
""" % (
    s.survey_field.dec_cen, s.survey_field.dec_cen, s.survey_field.ra_cen,
    s.survey_field.ra_cen-ra_offset.degree,s.survey_field.ra_cen+ra_offset.degree,
    s.survey_field.dec_cen-dec_offset.degree,s.survey_field.dec_cen+dec_offset.degree)
        cursor = connections['explorer'].cursor()
        cursor.execute(query, ())

        #if qs is None:
        #	qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(90)).filter(~Q(tags__name='YSE')).filter(~Q(status__name='Ignore')).filter(name__in=(x[0] for x in cursor))
        #else:
        if qs_start is None:
            qs_single = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(90)).filter(~Q(tags__name='YSE')).filter(~Q(status__name='Ignore')).filter(name__in=(x[0] for x in cursor))
        else:
            qs_single = qs_start.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(90)).filter(~Q(tags__name='YSE')).filter(~Q(status__name='Ignore')).filter(name__in=(x[0] for x in cursor))
        qs = qs | qs_single
        cursor.close()

    return qs

def sne_in_yse_field(field_id):

    qs = None
    survey_field = SurveyField.objects.filter(field_id=field_id)[0]

    d = survey_field.dec_cen*np.pi/180
    width_corr = 1.65/np.abs(np.cos(d))
    #radius = ((ra-s.survey_field.ra_cen)**2. + (dec-s.survey_field.dec_cen)**2.)**(1/2.)

    ra_offset = cd.Angle(width_corr, unit=u.deg)
    dec_offset = cd.Angle(15, unit=u.deg)

    query= """SELECT t.name, ACOS(SIN(t.dec*3.14159/180)*SIN(%s*3.14159/180)+COS(t.dec*3.14159/180)*COS(%s*3.14159/180)*COS((t.ra-%s)*3.14159/180))*180/3.14159 as sep
FROM YSE_App_transient t
WHERE t.ra > %s AND t.ra < %s AND t.dec > %s AND t.dec < %s
HAVING sep < 3.3
""" % (
    survey_field.dec_cen, survey_field.dec_cen, survey_field.ra_cen,
    survey_field.ra_cen-ra_offset.degree,survey_field.ra_cen+ra_offset.degree,
    survey_field.dec_cen-dec_offset.degree,survey_field.dec_cen+dec_offset.degree)
    cursor = connections['explorer'].cursor()
    cursor.execute(query, ())

    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(90)).filter(~Q(status__name='Ignore')).filter(name__in=(x[0] for x in cursor))

    return qs

def sne_in_yse_field_with_ignore(field_id):

    qs = None
    survey_field = SurveyField.objects.filter(field_id=field_id)[0]

    d = survey_field.dec_cen*np.pi/180
    width_corr = 1.65/np.abs(np.cos(d))
    #radius = ((ra-s.survey_field.ra_cen)**2. + (dec-s.survey_field.dec_cen)**2.)**(1/2.)

    ra_offset = cd.Angle(width_corr, unit=u.deg)
    dec_offset = cd.Angle(1.65, unit=u.deg)

    query= """SELECT t.name, ACOS(SIN(t.dec*3.14159/180)*SIN(%s*3.14159/180)+COS(t.dec*3.14159/180)*COS(%s*3.14159/180)*COS((t.ra-%s)*3.14159/180))*180/3.14159 as sep
FROM YSE_App_transient t
WHERE t.ra > %s AND t.ra < %s AND t.dec > %s AND t.dec < %s
HAVING sep < 3.3
""" % (
    survey_field.dec_cen, survey_field.dec_cen, survey_field.ra_cen,
    survey_field.ra_cen-ra_offset.degree,survey_field.ra_cen+ra_offset.degree,
    survey_field.dec_cen-dec_offset.degree,survey_field.dec_cen+dec_offset.degree)
    cursor = connections['explorer'].cursor()
    cursor.execute(query, ())

    qs = Transient.objects.filter(created_date__gte=datetime.datetime.utcnow()-datetime.timedelta(90)).filter(name__in=(x[0] for x in cursor))

    return qs
