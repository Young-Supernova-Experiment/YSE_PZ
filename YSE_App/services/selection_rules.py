"""Rule-tree Selections (#843): fields, validation, the compiler and "View as SQL".

A rule tree is plain JSON::

    {"op": "and", "rules": [
        {"field": "peak_mag", "operator": "brighter", "value": "19"},
        {"op": "or", "rules": [
            {"field": "status", "operator": "any", "value": ["New", "Watch"]},
            {"field": "has_spectrum", "operator": "is", "value": "true"}]}]}

Every field of the builder is a :class:`RuleField` from :data:`RULE_FIELDS`: a human-readable
label over one or more filters of the transient search (``TransientSearchFilterSet``), never a
database column. A rule compiles to exactly what the search page does with those parameters:
the FilterSet validates the value with its own form field and builds the clause, for the
viewing user (so per-point visibility, #607, applies to the photometry and spectrum fields).
Groups combine the rules with ``Q`` objects (``AND`` / ``OR``; "is none of" is ``NOT``). No
SQL is ever built from user input: values only reach the database as bound parameters.

:func:`compile_rules` starts from :func:`YSE_App.services.selections.base_queryset`, the search
page's rows for the viewer, and ``selection_queryset`` applies the listing rule again on top.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Tuple

from django.db.models import Q
from django.http import QueryDict

from YSE_App.models import Selection, Transient
from YSE_App.services.selections import (
    SelectionError,
    _ViewerRequest,
    base_queryset,
    register_compiler,
)

__all__ = [
    'MAX_DEPTH',
    'MAX_RULES',
    'RULE_FIELDS',
    'RULE_FIELD_GROUPS',
    'RuleField',
    'clean_rules',
    'compile_rules',
    'describe_rules',
    'empty_tree',
    'field_choices',
    'parse_rules',
    'rules_queryset',
    'rules_sql',
]

#: Nesting limit (the root group is depth 1) and the most rules in one tree.
MAX_DEPTH = 4
MAX_RULES = 40
MAX_VALUE_LENGTH = 200
MAX_CHOICES = 100

GROUP_OPS = ('and', 'or')
GROUP_OP_LABELS = {'and': 'all', 'or': 'any'}


@dataclass(frozen=True)
class Operator:
    key: str
    label: str
    #: search parameter -> fixed value; ``None`` means "the rule's value".
    params: Tuple[Tuple[str, Optional[str]], ...]
    negate: bool = False


@dataclass(frozen=True)
class RuleField:
    key: str
    label: str
    group: str
    kind: str  # number, mag, date, days, text, bool, choice
    operators: Tuple[Operator, ...]
    #: the search filter whose form field lists the options (kind ``choice``)
    choices_from: str = ''
    unit: str = ''
    help: str = ''

    def operator(self, key):
        for op in self.operators:
            if op.key == key:
                return op
        return None

    @property
    def filters(self):
        return {name for op in self.operators for name, _ in op.params}


def _op(key, label, param, negate=False, **fixed):
    params = ((param, None),) + tuple(sorted(fixed.items()))
    return Operator(key, label, params, negate)


def _range(key, label, group, low, high, unit='', help=''):
    return RuleField(key, label, group, 'number', (_op('at_least', 'at least', low), _op('at_most', 'at most', high)),
                     unit=unit, help=help)


def _mag(key, label, group, low, high, help=''):
    # Smaller magnitudes are brighter: "brighter than 19" is mag <= 19 (the search's "at most").
    return RuleField(key, label, group, 'mag', (_op('brighter', 'brighter than', high),
                                                _op('fainter', 'fainter than', low)), unit='mag', help=help)


def _dates(key, label, group, after, before):
    return RuleField(key, label, group, 'date', (_op('after', 'on or after', after), _op('before', 'before', before)))


def _within(key, label, group, param):
    return RuleField(key, label, group, 'days', (_op('within', 'within the last', param),), unit='days')


def _bool(key, label, group, param):
    return RuleField(key, label, group, 'bool', (_op('is', 'is', param),))


def _choice(key, label, group, param, negatable=True, all_param=''):
    ops = [_op('any', 'is any of', param)]
    if all_param:
        ops = [_op('any', 'has any of', param), _op('all', 'has all of', all_param)]
    if negatable:
        ops.append(_op('none', 'has none of' if all_param else 'is none of', param, negate=True))
    return RuleField(key, label, group, 'choice', tuple(ops), choices_from=param)


def _text(key, label, group, param, op_label):
    return RuleField(key, label, group, 'text', (_op('is', op_label, param),))


def _spec_phase_fields():
    from YSE_App.filters.transient_search import SPEC_PHASE_REFERENCE_CHOICES

    fields = [RuleField('spec_phase', 'Spectrum phase (days)', 'Spectra', 'number',
                        (_op('at_least', 'at least', 'spec_phase_min'), _op('at_most', 'at most', 'spec_phase_max')),
                        unit='days', help='From the Bazin peak: rest frame; observer frame without a redshift.')]
    for ref, label in SPEC_PHASE_REFERENCE_CHOICES:
        fields.append(RuleField(
            'spec_phase_%s' % ref, 'Spectrum phase, %s (days)' % label, 'Spectra', 'number',
            (_op('at_least', 'at least', 'spec_phase_min', spec_phase_ref=ref),
             _op('at_most', 'at most', 'spec_phase_max', spec_phase_ref=ref)), unit='days'))
    return fields


def _build_fields():
    fields = [
        # Names
        _text('name_contains', 'Name', 'Names', 'name_contains', 'contains'),
        _text('alias', 'Name or alias', 'Names', 'alias', 'contains'),
        _text('name', 'Name (exact)', 'Names', 'name', 'is'),
        _bool('has_tns_name', 'Has a TNS name', 'Names', 'has_tns_name'),
        # Position
        _range('ra', 'RA (deg)', 'Position', 'ra_gte', 'ra_lte', unit='deg'),
        _range('dec', 'Dec (deg)', 'Position', 'dec_gte', 'dec_lte', unit='deg'),
        _range('gal_b_abs', 'Galactic latitude |b| (deg)', 'Position', 'gal_b_abs_min', 'gal_b_abs_max', unit='deg'),
        # Time
        _dates('disc_date', 'Discovery date', 'Time', 'disc_date_after', 'disc_date_before'),
        _within('days_since_disc', 'Discovered', 'Time', 'days_since_disc_max'),
        _dates('created', 'Added to YSE-PZ', 'Time', 'created_after', 'created_before'),
        _dates('modified', 'Last changed', 'Time', 'modified_after', 'modified_before'),
        # Photometry
        _mag('peak_mag', 'Peak magnitude', 'Photometry', 'peak_mag_min', 'peak_mag_max'),
        _mag('latest_mag', 'Latest magnitude', 'Photometry', 'latest_mag_min', 'latest_mag_max'),
        _range('num_det', 'Number of detections', 'Photometry', 'num_det_min', 'num_det_max'),
        _dates('first_det', 'First detection', 'Photometry', 'first_det_after', 'first_det_before'),
        _dates('last_det', 'Last detection', 'Photometry', 'last_det_after', 'last_det_before'),
        _within('days_since_last_det', 'Last detected', 'Photometry', 'days_since_last_det_max'),
        _range('rise_rate', 'Rise rate (mag/day)', 'Photometry', 'rise_rate_min', 'rise_rate_max', unit='mag/day'),
        _range('decay_rate', 'Decay rate (mag/day)', 'Photometry', 'decay_rate_min', 'decay_rate_max', unit='mag/day'),
        _mag('deepest_limit', 'Deepest upper limit', 'Photometry', 'deepest_limit_min', 'deepest_limit_max'),
        # Classification
        _choice('classification', 'Class (spectroscopic or photometric)', 'Classification', 'classification'),
        _choice('spec_class', 'Spectroscopic class', 'Classification', 'spec_class'),
        _choice('photo_class', 'Photometric class', 'Classification', 'photo_class'),
        _bool('has_redshift', 'Has a redshift', 'Classification', 'has_redshift'),
        _range('redshift', 'Redshift', 'Classification', 'redshift_min', 'redshift_max'),
        # Relations
        _choice('status', 'Status', 'Relations', 'status'),
        _choice('obs_group', 'Observation group', 'Relations', 'obs_group'),
        _choice('internal_survey', 'Internal survey', 'Relations', 'internal_survey'),
        _choice('tags', 'Tags', 'Relations', 'tags', all_param='tags_all'),
        _bool('has_spectrum', 'Has a spectrum', 'Relations', 'has_spectrum'),
        _bool('has_followup', 'Has follow-up', 'Relations', 'has_followup'),
        _choice('followup_status', 'Follow-up status', 'Relations', 'followup_status', negatable=False),
        _bool('has_comment', 'Has a comment', 'Relations', 'has_comment'),
        _bool('has_host', 'Has a host galaxy', 'Relations', 'has_host'),
        _range('host_redshift', 'Host redshift', 'Relations', 'host_redshift_min', 'host_redshift_max'),
        _bool('favorites', 'In my favorites', 'Relations', 'favorites'),
        _bool('has_interest', 'Has an open paper interest', 'Relations', 'has_interest'),
        _bool('has_hst', 'Has HST data', 'Relations', 'has_hst'),
        _bool('has_jwst', 'Has JWST data', 'Relations', 'has_jwst'),
        _bool('has_chandra', 'Has Chandra data', 'Relations', 'has_chandra'),
    ]
    return tuple(fields + _spec_phase_fields())


RULE_FIELDS: Tuple[RuleField, ...] = _build_fields()
_BY_KEY = {f.key: f for f in RULE_FIELDS}
#: ``[(group title, [RuleField])]`` in the order of the search page's groups.
RULE_FIELD_GROUPS: List[Tuple[str, List[RuleField]]] = []
for _f in RULE_FIELDS:
    if not RULE_FIELD_GROUPS or RULE_FIELD_GROUPS[-1][0] != _f.group:
        RULE_FIELD_GROUPS.append((_f.group, []))
    RULE_FIELD_GROUPS[-1][1].append(_f)
del _f

BOOL_CHOICES = (('true', 'Yes'), ('false', 'No'))


def get_field(key):
    return _BY_KEY.get(key)


def field_choices(rule_field):
    """``[(value, label)]`` a ``choice`` or ``bool`` field offers (from the search filter's own form field)."""
    if rule_field.kind == 'bool':
        return list(BOOL_CHOICES)
    if rule_field.kind != 'choice':
        return []
    from YSE_App.filters.transient_search import TransientSearchFilterSet

    form_field = TransientSearchFilterSet().form.fields[rule_field.choices_from]
    return [(str(value), str(label)) for value, label in form_field.choices if str(value) != '']


def empty_tree():
    return {'op': 'and', 'rules': []}


# --------------------------------------------------------------------------- validation

def _is_group(node):
    return isinstance(node, dict) and 'rules' in node


def _clean_value(rule_field, value):
    if rule_field.kind == 'choice':
        values = value if isinstance(value, (list, tuple)) else [value]
        values = [str(v).strip() for v in values if v is not None and str(v).strip()]
        if not values:
            raise SelectionError('%s: choose at least one value.' % rule_field.label)
        if len(values) > MAX_CHOICES:
            raise SelectionError('%s: choose at most %d values.' % (rule_field.label, MAX_CHOICES))
        if any(len(v) > MAX_VALUE_LENGTH for v in values):
            raise SelectionError('%s: a value is too long.' % rule_field.label)
        return sorted(set(values), key=values.index)
    if isinstance(value, (list, tuple, dict)):
        raise SelectionError('%s: give one value.' % rule_field.label)
    if isinstance(value, bool):
        value = 'true' if value else 'false'
    text = '' if value is None else str(value).strip()
    if not text:
        raise SelectionError('%s: give a value.' % rule_field.label)
    if len(text) > MAX_VALUE_LENGTH:
        raise SelectionError('%s: the value is too long.' % rule_field.label)
    if rule_field.kind == 'bool' and text not in dict(BOOL_CHOICES):
        raise SelectionError('%s: choose Yes or No.' % rule_field.label)
    return text


def _rule_params(rule_field, operator, value):
    """The search parameters of one rule, as a ``QueryDict``."""
    data = QueryDict(mutable=True)
    for name, fixed in operator.params:
        if fixed is not None:
            data[name] = fixed
        elif isinstance(value, list):
            data.setlist(name, value)
        else:
            data[name] = value
    return data


def _filterset(rule_field, data, user, queryset):
    from YSE_App.filters.transient_search import TransientSearchFilterSet

    fs = TransientSearchFilterSet(data, queryset=queryset, request=_ViewerRequest(user=user, GET=data))
    if not fs.is_valid():
        messages = []
        for errors in fs.errors.values():
            messages.extend(str(e) for e in errors)
        raise SelectionError('%s: %s' % (rule_field.label, ' '.join(messages) or 'the value is not valid.'))
    return fs


def _clean_rule(node):
    rule_field = get_field(str(node.get('field') or ''))
    if rule_field is None:
        raise SelectionError('Choose a field for every rule.' if not node.get('field')
                             else 'Unknown field "%s".' % str(node.get('field'))[:60])
    operator = rule_field.operator(str(node.get('operator') or ''))
    if operator is None:
        raise SelectionError('%s: choose a comparison.' % rule_field.label)
    value = _clean_value(rule_field, node.get('value'))
    # The search filter's own form field checks the type (numbers, dates, known names, ...).
    _filterset(rule_field, _rule_params(rule_field, operator, value), None, Transient.objects.none())
    return {'field': rule_field.key, 'operator': operator.key, 'value': value}


def clean_rules(tree):
    """``tree`` checked and normalised (raises :class:`SelectionError` with a user-facing message).

    Empty nested groups are dropped; the root may be empty (it then selects every listed transient).
    """
    if isinstance(tree, str):
        tree = parse_rules(tree)
    if not _is_group(tree):
        raise SelectionError('The rules must be a group of rules.')
    count = [0]

    def walk(node, depth):
        if depth > MAX_DEPTH:
            raise SelectionError('Groups can be nested at most %d deep.' % MAX_DEPTH)
        op = str(node.get('op') or 'and').lower()
        if op not in GROUP_OPS:
            raise SelectionError('A group must match all (AND) or any (OR) of its rules.')
        children = node.get('rules')
        if not isinstance(children, list):
            raise SelectionError('The rules must be a group of rules.')
        out = []
        for child in children:
            if _is_group(child):
                cleaned = walk(child, depth + 1)
                if cleaned['rules']:
                    out.append(cleaned)
            elif isinstance(child, dict):
                count[0] += 1
                if count[0] > MAX_RULES:
                    raise SelectionError('Use at most %d rules.' % MAX_RULES)
                out.append(_clean_rule(child))
            else:
                raise SelectionError('The rules must be a group of rules.')
        return {'op': op, 'rules': out}

    return walk(tree, 1)


def parse_rules(text):
    """The rule tree posted by the builder (JSON text)."""
    if isinstance(text, (dict, list)):
        return text
    try:
        tree = json.loads(text or '')
    except (TypeError, ValueError):
        raise SelectionError('The rules could not be read.')
    return tree


# --------------------------------------------------------------------------- compiler

def _rule_q(node, user):
    rule_field = get_field(node['field'])
    operator = rule_field.operator(node['operator'])
    fs = _filterset(rule_field, _rule_params(rule_field, operator, node['value']), user, Transient.objects.all())
    q = Q(pk__in=fs.qs.order_by().values('pk'))
    return ~q if operator.negate else q


def _group_q(node, user):
    q = Q()
    for child in node['rules']:
        child_q = _group_q(child, user) if _is_group(child) else _rule_q(child, user)
        if node['op'] == 'or':
            q = child_q if not q else (q | child_q)
        else:
            q &= child_q
    return q


def compile_rules(tree, user, queryset=None):
    """The transients ``tree`` selects among ``queryset`` (default: the search page's rows for ``user``).

    Each rule runs the transient search's filter for ``user``; the groups combine them.
    """
    tree = clean_rules(tree)
    if queryset is None:
        queryset = base_queryset(user)
    q = _group_q(tree, user)
    return queryset.filter(q) if q else queryset


def compile_selection(selection, user):
    if selection.rules is None:
        raise SelectionError('This Selection has no rules.')
    return compile_rules(selection.rules, user)


register_compiler(Selection.KIND_RULES, compile_selection)


# --------------------------------------------------------------------------- describe / SQL

def _value_text(rule_field, value):
    if rule_field.kind == 'bool':
        return dict(BOOL_CHOICES).get(value, value)
    if isinstance(value, list):
        return ', '.join(value)
    if rule_field.unit and rule_field.kind in ('days',):
        return '%s %s' % (value, rule_field.unit)
    return str(value)


def describe_rules(tree, top=True):
    """The tree as one readable line: ``Peak magnitude brighter than 19 AND (Status is any of New OR ...)``."""
    try:
        tree = clean_rules(tree) if top else tree
    except SelectionError as exc:
        return str(exc)
    parts = []
    for child in tree['rules']:
        if _is_group(child):
            inner = describe_rules(child, top=False)
            parts.append('(%s)' % inner if len(child['rules']) > 1 else inner)
        else:
            rule_field = get_field(child['field'])
            operator = rule_field.operator(child['operator'])
            parts.append('%s %s %s' % (rule_field.label, operator.label, _value_text(rule_field, child['value'])))
    if not parts:
        return 'all transients'
    return (' %s ' % tree['op'].upper()).join(parts)


def _sql_literal(value):
    """A readable SQL literal for display only (the database receives the value as a parameter)."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        value = value.isoformat(sep=' ') if isinstance(value, datetime.datetime) else value.isoformat()
    return "'%s'" % str(value).replace("'", "''")


def rules_queryset(tree, user):
    """The transients ``tree`` selects that ``user`` may see listed (plain rows, for counting and SQL)."""
    from YSE_App.services.visibility import filter_listed_transients

    return filter_listed_transients(user, compile_rules(tree, user, queryset=Transient.objects.all()))


def rules_sql(tree, user):
    """``(sql, params, display)`` of the query that selects ``tree``'s transients for ``user``.

    ``sql`` has placeholders and ``params`` the values Django binds to them; ``display`` is
    the same query with the values shown in place, formatted for reading. It is the selecting
    query (listing rule included); the results table adds its display columns to it.
    """
    qs = rules_queryset(tree, user).order_by('-disc_date', '-pk').values('pk', 'name')
    sql, params = qs.query.sql_with_params()
    try:
        display = sql % tuple(_sql_literal(p) for p in params)
    except (TypeError, ValueError):
        display = sql
    try:
        import sqlparse

        display = sqlparse.format(display, reindent=True, keyword_case='upper')
    except Exception:  # formatting is cosmetic
        pass
    return sql, list(params), display
