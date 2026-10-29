"""The rows of each data table one viewer may see, as SQL (#723).

:mod:`YSE_App.services.explorer_scope` replaces every data table a query reads
with a derived table built here. Each derived table is
``SELECT <the table's columns> FROM <table> WHERE <key> IN (<visible keys>)``,
where ``<visible keys>`` is a Django queryset compiled for the viewer from the
application's own read rules (``services.visibility``,
``services.access_rules``, ``services.photstat_visibility``), so the database
applies exactly the per-point rules the pages use:

* photometry points: ``visible_photometry_ids_q`` (with access rules on, the
  exact points; access is per point, never per header);
* photometry headers: ``photometry_visible_q`` (listed when a point is
  visible, or by its groups when it has none);
* spectra: ``spectrum_visible_q``; host photometry and spectra: their groups;
* transients and their names and tags: ``listed_transient_q``;
* photometry statistics: the all-data row only without hidden photometry
  (``hidden_photometry_exists``), scope rows by ``_candidate_scope_q``;
* annotations: their groups.

Columns that point at a spectrum (``Transient.best_spectrum_id``,
``Host.best_spec_id``) are NULL unless that spectrum is visible.

The compiled SQL has its parameters inlined as literals (the saved-search
compiler's ``inline_sql_params``), because Explorer runs a single SQL string.
It reads only application tables, ``auth_group`` (group names) and, with
``DATA_ACCESS_GRANTS_ENABLED`` on, the access-rule tables, so the Explorer
database user needs ``SELECT`` on those (runbook).
"""

from __future__ import annotations

from typing import Dict, NamedTuple, Optional, Tuple

from django.core.exceptions import EmptyResultSet

#: SQL for "no key is visible" (an IN list that matches nothing).
_NOTHING = "SELECT NULL FROM (SELECT 1 AS `x`) AS `__yse_none` WHERE 1 = 0"


class Mask(NamedTuple):
    """A column that is NULL unless the row it points at is visible."""

    column: str
    visible: str  # ViewerRows method returning the visible pks of the target table
    target_table: str
    target_pk: str


class RowRule(NamedTuple):
    """How to restrict one table: ``key`` must be in ``ViewerRows.<visible>()``."""

    table: str
    columns: Optional[Tuple[str, ...]]  # None: SELECT * (database views)
    key: Optional[str]  # None: every row (only masks apply)
    visible: Optional[str]
    masks: Tuple[Mask, ...] = ()


# ---------------------------------------------------------------- table rules


def _columns(model) -> Tuple[str, ...]:
    return tuple(f.column for f in model._meta.concrete_fields)


def _rule(model, key, visible, masks=()) -> RowRule:
    return RowRule(model._meta.db_table, _columns(model), key, visible, tuple(masks))


def _through_rules(model, visible, *, field_names=None, other_side=False):
    """Rules for ``model``'s auto-created many-to-many tables, restricted by ``model``'s side
    (or, with ``other_side``, by the related model's side)."""
    out = []
    for field in model._meta.many_to_many:
        if field_names is not None and field.name not in field_names:
            continue
        through = field.remote_field.through
        if not through._meta.auto_created:
            continue
        key = field.m2m_reverse_name() if other_side else field.m2m_column_name()
        out.append(_rule(through, key, visible))
    return out


def _mask(column, visible, target_model) -> Mask:
    return Mask(column, visible, target_model._meta.db_table, target_model._meta.pk.column)


_RULES: Optional[Dict[str, RowRule]] = None


def row_rules() -> Dict[str, RowRule]:
    """``{lower-case table name: RowRule}`` for every table holding restricted data."""
    global _RULES
    if _RULES is not None:
        return _RULES
    from YSE_App import models as m

    rules = [
        _rule(m.Transient, 'id', 'listed_transients',
              masks=[_mask('best_spectrum_id', 'visible_transient_spectra', m.TransientSpectrum)]),
        *_through_rules(m.Transient, 'listed_transients'),
        _rule(m.AlternateTransientNames, 'transient_id', 'listed_transients'),
        *_through_rules(m.SurveyField, 'listed_transients', field_names={'targeted_transients'}, other_side=True),

        _rule(m.TransientPhotometry, 'id', 'visible_transient_photometry'),
        *_through_rules(m.TransientPhotometry, 'visible_transient_photometry'),
        _rule(m.TransientPhotData, 'id', 'visible_transient_points'),
        *_through_rules(m.TransientPhotData, 'visible_transient_points'),
        _rule(m.TransientImage, 'phot_data_id', 'visible_transient_points'),
        _rule(m.TransientDiffImage, 'phot_data_id', 'visible_transient_points'),

        _rule(m.TransientSpectrum, 'id', 'visible_transient_spectra'),
        *_through_rules(m.TransientSpectrum, 'visible_transient_spectra'),
        _rule(m.TransientSpecData, 'spectrum_id', 'visible_transient_spectra'),
        _rule(m.TransientSpectrumArrays, 'spectrum_id', 'visible_transient_spectra'),  # pixels as arrays (#891)

        _rule(m.Host, None, None, masks=[_mask('best_spec_id', 'visible_host_spectra', m.HostSpectrum)]),
        _rule(m.HostPhotometry, 'id', 'visible_host_photometry'),
        *_through_rules(m.HostPhotometry, 'visible_host_photometry'),
        _rule(m.HostPhotData, 'id', 'visible_host_points'),
        *_through_rules(m.HostPhotData, 'visible_host_points'),
        _rule(m.HostImage, 'phot_data_id', 'visible_host_points'),
        _rule(m.HostSpectrum, 'id', 'visible_host_spectra'),
        *_through_rules(m.HostSpectrum, 'visible_host_spectra'),
        _rule(m.HostSpecData, 'spectrum_id', 'visible_host_spectra'),
        _rule(m.HostSpectrumArrays, 'spectrum_id', 'visible_host_spectra'),  # pixels as arrays (#891)

        _rule(m.TransientPhotStat, 'id', 'visible_photstat'),
        _rule(m.TransientPhotStatScope, 'id', 'visible_photstat_scopes'),
        # The stored-statistics view of migration 0034 (one row per TransientPhotStat).
        RowRule('yse_transient_photstat_v', None, 'transient_id', 'visible_photstat_transients'),

        # Comments and follow-ups: the rules of the pages (staff see every row, as there).
        _rule(m.Log, 'id', 'visible_logs'),
        *_through_rules(m.Log, 'visible_logs'),
        _rule(m.TransientFollowup, 'id', 'visible_transient_followups'),
        *_through_rules(m.TransientFollowup, 'visible_transient_followups'),

        _rule(m.TransientAnnotation, 'id', 'visible_annotations'),
        *_through_rules(m.TransientAnnotation, 'visible_annotations'),
        _rule(m.TransientAnnotationValue, 'annotation_id', 'visible_annotations'),
    ]
    _RULES = {r.table.lower(): r for r in rules}
    return _RULES


def open_tables():
    """Lower-case names of reference tables every scoped viewer may read as they are."""
    from YSE_App import models as m

    models = [
        m.TransientStatus, m.FollowupStatus, m.TaskStatus, m.AntaresClassification, m.InternalSurvey,
        m.ObservationGroup, m.SEDType, m.HostMorphology, m.Phase, m.TransientClass, m.ClassicalNightType,
        m.InformationSource, m.WebAppColor, m.Unit, m.DataQuality, m.MagSystem, m.PhotometricBand,
        m.Instrument, m.InstrumentConfig, m.ConfigElement, m.Telescope, m.Observatory, m.TransientTag,
        m.HostSED, m.SurveyField, m.SurveyFieldMSB, m.SurveyObservation,
    ]
    names = {model._meta.db_table.lower() for model in models}
    for model in (m.ConfigElement, m.SurveyFieldMSB):
        for field in model._meta.many_to_many:
            if field.remote_field.through._meta.auto_created:
                names.add(field.remote_field.through._meta.db_table.lower())
    return frozenset(names)


def column_limited_tables():
    """Lower-case names of account tables every scoped viewer reads only as rewritten here:
    group names (the read rules name groups)."""
    return {'auth_group': "SELECT `id`, `name` FROM `auth_group`"}


def staff_only_tables():
    """Lower-case names of tables only staff may read, and only as rewritten here (never
    as stored): ``auth_user`` is reduced to id and names."""
    return {'auth_user': "SELECT `id`, `username`, `first_name`, `last_name` FROM `auth_user`"}


# --------------------------------------------------------------- per viewer


def _compile(queryset) -> str:
    from django.db import connections

    from YSE_App.services.search_queries import inline_sql_params

    try:
        sql, params = queryset.query.sql_with_params()
    except EmptyResultSet:
        return _NOTHING
    return inline_sql_params(sql, params, connections[queryset.db])


class ViewerRows:
    """The visible keys of every data table for one viewer, as SQL (memoised per instance)."""

    def __init__(self, user):
        self.user = user
        self._names = None
        self._sql: Dict[str, str] = {}

    @property
    def names(self):
        if self._names is None:
            from YSE_App.services.explorer_scope import viewer_group_names

            self._names = viewer_group_names(self.user)
        return self._names

    def sql(self, method: str) -> str:
        if method not in self._sql:
            self._sql[method] = _compile(getattr(self, method)())
        return self._sql[method]

    def table_sql(self, rule: RowRule) -> str:
        """``SELECT`` of ``rule.table`` limited to this viewer's rows, with the same column names."""
        from django.db import connection

        qn = connection.ops.quote_name
        base = '`__yse_b`'
        if rule.columns is None:
            select, joins = base + '.*', []
        else:
            masks = {mk.column: mk for mk in rule.masks}
            cols, joins = [], []
            for i, col in enumerate(rule.columns):
                mk = masks.get(col)
                if mk is None:
                    cols.append('%s.%s' % (base, qn(col)))
                    continue
                alias = '`__yse_m%d`' % i
                joins.append('LEFT JOIN %s %s ON %s.%s = %s.%s AND %s.%s IN (%s)' % (
                    qn(mk.target_table), alias, alias, qn(mk.target_pk), base, qn(col),
                    alias, qn(mk.target_pk), self.sql(mk.visible)))
                cols.append('%s.%s AS %s' % (alias, qn(mk.target_pk), qn(col)))
            select = ', '.join(cols)
        sql = 'SELECT %s FROM %s %s' % (select, qn(rule.table), base)
        if joins:
            sql += ' ' + ' '.join(joins)
        if rule.key is not None:
            sql += ' WHERE %s.%s IN (%s)' % (base, qn(rule.key), self.sql(rule.visible))
        return sql

    # Each method returns a ``values()`` queryset of the visible keys.

    def listed_transients(self):
        from YSE_App.models import Transient
        from YSE_App.services.visibility import listed_transient_q

        return Transient.objects.filter(listed_transient_q(self.user, names=self.names)).values('pk')

    def visible_transient_photometry(self):
        from YSE_App.models import TransientPhotometry
        from YSE_App.services.visibility import photometry_visible_q

        return TransientPhotometry.objects.filter(photometry_visible_q(self.user, names=self.names)).values('pk')

    def visible_transient_points(self):
        from YSE_App.models import TransientPhotData
        from YSE_App.services.visibility import visible_photometry_ids_q

        q = visible_photometry_ids_q(self.user)
        qs = TransientPhotData.objects.all() if q is None else TransientPhotData.objects.filter(q)
        return qs.values('pk')

    def visible_transient_spectra(self):
        from YSE_App.models import TransientSpectrum
        from YSE_App.services.visibility import spectrum_visible_q

        return TransientSpectrum.objects.filter(spectrum_visible_q(self.user, names=self.names)).values('pk')

    def _group_rule(self, model):
        from YSE_App.services.visibility import get_user_group_query

        no_group, contains_group = get_user_group_query(self.user, names=self.names)
        return model.objects.filter(no_group | contains_group)

    def visible_host_photometry(self):
        from YSE_App.models import HostPhotometry

        return self._group_rule(HostPhotometry).values('pk')

    def visible_host_points(self):
        from YSE_App.models import HostPhotData

        return HostPhotData.objects.filter(photometry_id__in=self.visible_host_photometry()).values('pk')

    def visible_host_spectra(self):
        from YSE_App.models import HostSpectrum

        return self._group_rule(HostSpectrum).values('pk')

    def _photstat(self):
        from YSE_App.models import TransientPhotStat
        from YSE_App.services.visibility import hidden_photometry_exists

        return (TransientPhotStat.objects.filter(transient_id__in=self.listed_transients())
                .exclude(hidden_photometry_exists(self.user, 'transient_id')))

    def visible_photstat(self):
        return self._photstat().values('pk')

    def visible_photstat_transients(self):
        return self._photstat().values('transient_id')

    def visible_photstat_scopes(self):
        from YSE_App.models import TransientPhotStatScope
        from YSE_App.services.photstat_visibility import _candidate_scope_q

        return (TransientPhotStatScope.objects.filter(_candidate_scope_q(self.user))
                .filter(transient_id__in=self.listed_transients()).values('pk'))

    def visible_annotations(self):
        from YSE_App.models import TransientAnnotation

        return (self._group_rule(TransientAnnotation)
                .filter(transient_id__in=self.listed_transients()).values('pk'))

    def visible_logs(self):
        from YSE_App.models import Log
        from YSE_App.services.visibility import filter_transient_comments_for_user

        return filter_transient_comments_for_user(Log.objects.all(), self.user).values('pk')

    def visible_transient_followups(self):
        from YSE_App.models import TransientFollowup
        from YSE_App.services.visibility import filter_transient_followups_for_user

        return filter_transient_followups_for_user(TransientFollowup.objects.all(), self.user).values('pk')
