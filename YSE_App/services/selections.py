"""Selections (#841): who sees and edits them, and the transients they select.

A :class:`~YSE_App.models.Selection` stores criteria only. This module is the one place that
turns them into rows, always for the *viewing* user:

* :func:`selection_queryset` (also ``selection.queryset(user)``) returns the matching
  ``Transient`` rows that ``user`` may see listed, with the search page's annotations and
  default order (``-disc_date, -pk``). Search-backed Selections use the same base queryset and
  ``TransientSearchFilterSet`` as ``/search/``, so they return the same rows in the same order.
  SQL-backed Selections run their Explorer query through the dashboards' runner, rewritten to
  the viewer's rows (#723) and refused where the dashboards refuse it; the owner's rights never
  apply. Every result then passes ``filter_listed_transients`` (#607), whatever the compiler.
* :func:`visible_selections`, :func:`can_view`, :func:`can_edit`, :func:`can_delete`: the
  sharing rules. ``me``: the owner only. ``team``: the owner and the members, owners and deputies
  of ``selection.team``, who may also edit it (Ryan, 2026-10-25). ``all``: every signed-in user
  may view it; the owner (and, when a team is set, its members) may edit it. Only the owner deletes.
* :func:`create_selection`, :func:`update_selection`, :func:`selection_from_user_query`.
* :func:`register_compiler`: how a new criteria kind (the rule tree of #843) plugs in.

A Selection never grants access to anything: it changes which rows are asked for, never what
the viewer may see.
"""

from __future__ import annotations

import types

from django.contrib.auth.models import Group
from django.db.models import Q, QuerySet
from django.http import QueryDict
from django.utils.text import slugify

from YSE_App.models import Selection, Transient

__all__ = [
    'SelectionError',
    'SelectionAccessError',
    'SEARCH_STATE_KEYS',
    'can_delete',
    'can_edit',
    'can_view',
    'create_selection',
    'editable_selections',
    'normalize_search_params',
    'register_compiler',
    'selection_from_user_query',
    'selection_queryset',
    'team_choices',
    'unique_slug',
    'update_selection',
    'user_team_ids',
    'visible_selections',
]

NAME_MAX_LENGTH = 200
SLUG_BASE_MAX_LENGTH = 70
#: Slugs that would collide with the pages under ``selections/``.
RESERVED_SLUGS = frozenset({'save'})

#: Search page parameters that are page state, not criteria (paging, sorting, messages).
SEARCH_STATE_KEYS = ('page', 'per_page', 'sort', 'saved_query', 'saved_user_query', 'save_error',
                     'bulk_result', 'selection_saved', 'selection_error')


class SelectionError(ValueError):
    """A Selection could not be saved or compiled; ``str(exc)`` is user-facing."""


class SelectionAccessError(SelectionError):
    """The user may not view (or change) this Selection."""


# --------------------------------------------------------------------------- teams and sharing

def _authenticated(user):
    return user is not None and getattr(user, 'is_authenticated', False)


def user_team_ids(user):
    """Ids of the groups ``user`` is a member, owner or deputy of."""
    if not _authenticated(user):
        return set()
    return set(Group.objects.filter(
        Q(user=user) | Q(group_profile__owners=user) | Q(group_profile__deputies=user)
    ).values_list('pk', flat=True).distinct())


def team_choices(user):
    """Teams ``user`` may share a Selection with: their non-system groups, by name."""
    if not _authenticated(user):
        return []
    from YSE_App.services.groups import system_group_ids

    system = system_group_ids(user)
    return [g for g in Group.objects.filter(pk__in=user_team_ids(user)).order_by('name') if g.pk not in system]


def visible_selections(user):
    """Selections ``user`` may view."""
    if not _authenticated(user):
        return Selection.objects.none()
    return Selection.objects.filter(
        Q(owner=user)
        | Q(visibility=Selection.VISIBILITY_ALL)
        | Q(visibility=Selection.VISIBILITY_TEAM, team_id__in=user_team_ids(user))
    ).select_related('owner', 'team', 'query')


def editable_selections(user):
    """Selections ``user`` may edit (rename, re-share, change criteria)."""
    if not _authenticated(user):
        return Selection.objects.none()
    return Selection.objects.filter(
        Q(owner=user)
        | Q(visibility__in=(Selection.VISIBILITY_TEAM, Selection.VISIBILITY_ALL), team_id__in=user_team_ids(user))
    )


def can_view(user, selection, team_ids=None):
    if not _authenticated(user):
        return False
    if selection.owner_id == user.pk or selection.visibility == Selection.VISIBILITY_ALL:
        return True
    if selection.visibility == Selection.VISIBILITY_TEAM and selection.team_id:
        return selection.team_id in (user_team_ids(user) if team_ids is None else team_ids)
    return False


def can_edit(user, selection, team_ids=None):
    if not _authenticated(user):
        return False
    if selection.owner_id == user.pk:
        return True
    if selection.visibility in (Selection.VISIBILITY_TEAM, Selection.VISIBILITY_ALL) and selection.team_id:
        return selection.team_id in (user_team_ids(user) if team_ids is None else team_ids)
    return False


def can_delete(user, selection):
    return _authenticated(user) and selection.owner_id == user.pk


# --------------------------------------------------------------------------- validation helpers

def unique_slug(name, exclude_pk=None):
    """A free slug for ``name`` ("young-ia", then "young-ia-2", ...)."""
    base = slugify(name or '')[:SLUG_BASE_MAX_LENGTH].strip('-') or 'selection'
    taken = Selection.objects.filter(slug__startswith=base)
    if exclude_pk is not None:
        taken = taken.exclude(pk=exclude_pk)
    taken = set(taken.values_list('slug', flat=True)) | RESERVED_SLUGS
    slug, n = base, 2
    while slug in taken:
        slug = '%s-%d' % (base, n)
        n += 1
    return slug


def _clean_name(name):
    name = ' '.join((name or '').split())
    if not name:
        raise SelectionError('Give the Selection a name.')
    if len(name) > NAME_MAX_LENGTH:
        raise SelectionError('The name must be at most %d characters.' % NAME_MAX_LENGTH)
    return name


def _clean_sharing(user, visibility, team):
    """``(visibility, team)`` checked for ``user``; a team is kept only where it is used."""
    if visibility not in dict(Selection.VISIBILITY_CHOICES):
        raise SelectionError('Choose who can see the Selection.')
    if isinstance(team, (int, str)) and str(team).strip():
        team = Group.objects.filter(pk=int(team)).first() if str(team).strip().isdigit() else None
        if team is None:
            raise SelectionError('That team does not exist.')
    elif not isinstance(team, Group):
        team = None
    if team is not None and team.pk not in {g.pk for g in team_choices(user)}:
        raise SelectionError('You can only share with a team you belong to.')
    if visibility == Selection.VISIBILITY_TEAM and team is None:
        raise SelectionError('Choose the team to share with.')
    if visibility == Selection.VISIBILITY_ME:
        team = None
    return visibility, team


def normalize_search_params(params):
    """The search criteria of ``params`` (a query string, ``QueryDict`` or dict) as a query string.

    Paging, table sorting and page messages are dropped; empty values too. The search's own
    ``ordering`` filter stays (it is part of what was asked for).
    """
    if isinstance(params, str):
        data = QueryDict(params.lstrip('?'), mutable=True)
    elif hasattr(params, 'getlist'):
        data = params.copy()
    else:
        data = QueryDict(mutable=True)
        for key, value in dict(params or {}).items():
            if isinstance(value, (list, tuple)):
                data.setlist(key, [str(v) for v in value])
            elif value is not None:
                data[key] = str(value)
    out = QueryDict(mutable=True)
    for key in sorted(data.keys()):
        if key in SEARCH_STATE_KEYS:
            continue
        values = [v for v in data.getlist(key) if str(v).strip() != '']
        if values:
            out.setlist(key, values)
    return out.urlencode()


class _ViewerRequest(types.SimpleNamespace):
    """The two attributes ``TransientSearchFilterSet`` reads from a request (``user``, ``GET``)."""


def _search_filterset(params, user, queryset):
    from YSE_App.filters.transient_search import TransientSearchFilterSet, quick_search_params

    data = quick_search_params(QueryDict(params, mutable=True))
    filterset = TransientSearchFilterSet(data, queryset=queryset, request=_ViewerRequest(user=user, GET=data))
    if not filterset.is_valid():
        messages = []
        for field, errors in filterset.errors.items():
            messages.extend(str(e) for e in errors)
        raise SelectionError('The search filters are not valid: ' + '; '.join(messages))
    return filterset


def _python_query_names():
    from YSE_App.queries.yse_python_queries import python_query_reg

    return set(python_query_reg.all.keys())


# --------------------------------------------------------------------------- create / update

def create_selection(owner, name, *, kind=Selection.KIND_SEARCH, visibility=Selection.VISIBILITY_ME, team=None,
                     search_params='', rules=None, query=None, python_query='', user_query=None, description=''):
    """Save a new Selection for ``owner`` and return it. Raises :class:`SelectionError`.

    ``kind='search'`` needs ``search_params`` (validated by the search FilterSet);
    ``kind='sql'`` an Explorer ``Query`` (anyone may point at a saved query, as on the
    dashboards; it always runs for the viewer, so nothing is gained by it);
    ``kind='function'`` a registered Python query name; ``kind='rules'`` a JSON-able rule tree.
    """
    if not _authenticated(owner):
        raise SelectionError('Log in to save a Selection.')
    name = _clean_name(name)
    visibility, team = _clean_sharing(owner, visibility, team)
    fields = {'search_params': '', 'rules': None, 'query': None, 'python_query': ''}
    if kind == Selection.KIND_SEARCH:
        fields['search_params'] = normalize_search_params(search_params)
        _search_filterset(fields['search_params'], owner, Transient.objects.none())
    elif kind == Selection.KIND_SQL:
        from explorer.models import Query

        if not isinstance(query, Query):
            raise SelectionError('Choose a saved SQL query.')
        fields['query'] = query
    elif kind == Selection.KIND_FUNCTION:
        if python_query not in _python_query_names():
            raise SelectionError('Unknown built-in list "%s".' % python_query)
        fields['python_query'] = python_query
    elif kind == Selection.KIND_RULES:
        if not isinstance(rules, (dict, list)):
            raise SelectionError('A rule-based Selection needs a rule tree.')
        fields['rules'] = rules
    else:
        raise SelectionError('Unknown kind of Selection.')
    if user_query is not None and user_query.user_id != owner.pk:
        raise SelectionError('You can only link your own dashboard panels.')
    return Selection.objects.create(
        name=name, slug=unique_slug(name), description=(description or '').strip(), owner=owner,
        visibility=visibility, team=team, kind=kind, user_query=user_query, modified_by=owner, **fields)


def selection_from_user_query(owner, user_query, name=None, *, visibility=Selection.VISIBILITY_ME, team=None):
    """A Selection over one of ``owner``'s dashboard panels (Explorer- or function-backed).

    The ``UserQuery`` row and its panel are not changed; the Selection only points at it.
    """
    if user_query.query_id:
        return create_selection(owner, name or user_query.query.title, kind=Selection.KIND_SQL,
                                visibility=visibility, team=team, query=user_query.query, user_query=user_query)
    if user_query.python_query:
        return create_selection(owner, name or str(user_query.python_query).replace('_', ' '),
                                kind=Selection.KIND_FUNCTION, visibility=visibility, team=team,
                                python_query=user_query.python_query, user_query=user_query)
    raise SelectionError('That dashboard panel has no query.')


_UNSET = object()


def update_selection(user, selection, *, name=_UNSET, visibility=_UNSET, team=_UNSET, description=_UNSET,
                     search_params=_UNSET):
    """Change ``selection`` as ``user`` (who must be allowed to edit it). The slug never changes."""
    if not can_edit(user, selection):
        raise SelectionAccessError('You cannot edit this Selection.')
    if name is not _UNSET:
        selection.name = _clean_name(name)
    if visibility is not _UNSET or team is not _UNSET:
        new_visibility = selection.visibility if visibility is _UNSET else visibility
        if new_visibility == Selection.VISIBILITY_ME and selection.owner_id != user.pk:
            raise SelectionAccessError('Only the owner can make a Selection private.')
        if team is _UNSET:
            # Keep the current team (a team member re-sharing it need not pick it again).
            if new_visibility not in dict(Selection.VISIBILITY_CHOICES):
                raise SelectionError('Choose who can see the Selection.')
            if new_visibility == Selection.VISIBILITY_TEAM and selection.team_id is None:
                raise SelectionError('Choose the team to share with.')
            selection.visibility = new_visibility
            if new_visibility == Selection.VISIBILITY_ME:
                selection.team = None
        else:
            selection.visibility, selection.team = _clean_sharing(user, new_visibility, team)
    if description is not _UNSET:
        selection.description = (description or '').strip()
    if search_params is not _UNSET:
        if selection.kind != Selection.KIND_SEARCH:
            raise SelectionError('Only search Selections have search filters.')
        params = normalize_search_params(search_params)
        _search_filterset(params, user, Transient.objects.none())
        selection.search_params = params
    selection.modified_by = user
    selection.save()
    return selection


# --------------------------------------------------------------------------- compiler

def base_queryset(user):
    """The search page's base rows for ``user``: listed transients, annotated, ``-disc_date, -pk``."""
    from YSE_App.views import search_results_queryset

    return search_results_queryset(user)


def _compile_search(selection, user):
    return _search_filterset(selection.search_params, user, base_queryset(user)).qs


def _compile_sql(selection, user):
    query = selection.query
    if query is None:
        raise SelectionError('The saved SQL query of this Selection was deleted.')
    from YSE_App import views
    from YSE_App.services.dashboard_queries import dashboard_sql_rejection_reason
    from YSE_App.services.page_query_scope import PageQueryRefused

    # The dashboards' rules, for the viewer (never the owner): refused where they refuse it,
    # otherwise rewritten to the viewer's visible rows (#715, #723).
    reason = dashboard_sql_rejection_reason(query.sql, user)
    if reason is not None:
        raise SelectionError('This SQL query cannot be used here: %s.' % reason)
    try:
        names = views.run_explorer_query_cached(query, user=user)
    except PageQueryRefused as exc:
        raise SelectionError('This SQL query cannot be used here: %s.' % exc)
    return base_queryset(user).filter(name__in=set(names or ()))


def _compile_function(selection, user):
    from YSE_App.queries.yse_python_queries import run_python_query

    if selection.python_query not in _python_query_names():
        raise SelectionError('The built-in list "%s" no longer exists.' % selection.python_query)
    rows = run_python_query(selection.python_query, user)
    if isinstance(rows, QuerySet):
        return base_queryset(user).filter(pk__in=rows.values('pk'))
    return base_queryset(user).filter(pk__in=[t.pk for t in rows])


_COMPILERS = {
    Selection.KIND_SEARCH: _compile_search,
    Selection.KIND_SQL: _compile_sql,
    Selection.KIND_FUNCTION: _compile_function,
}


def register_compiler(kind, func):
    """Register ``func(selection, user) -> Transient QuerySet`` for ``kind`` (e.g. 'rules', #843).

    Start from :func:`base_queryset`; :func:`selection_queryset` applies the listing rule again
    on top, so a compiler cannot widen what the viewer sees.
    """
    _COMPILERS[kind] = func


def selection_queryset(selection, user):
    """The transients ``selection`` selects, as ``user`` may see them listed.

    Raises :class:`SelectionAccessError` when ``user`` may not view the Selection and
    :class:`SelectionError` when its criteria cannot be compiled (for example a rule
    tree before #843, a deleted Explorer query, or SQL the dashboards refuse for ``user``).
    """
    from YSE_App.services.visibility import filter_listed_transients

    if not can_view(user, selection):
        raise SelectionAccessError('You cannot see this Selection.')
    compiler = _COMPILERS.get(selection.kind)
    if compiler is None:
        raise SelectionError('Selections of kind "%s" cannot be listed yet.' % selection.get_kind_display())
    return filter_listed_transients(user, compiler(selection, user))


def criteria_summary(selection, user=None):
    """``[(label, value)]`` describing the criteria, for pages and the API."""
    if selection.kind == Selection.KIND_SEARCH:
        try:
            filterset = _search_filterset(selection.search_params, user, Transient.objects.none())
        except SelectionError as exc:
            return [('Search', str(exc))]
        chips = [(label, value) for _name, label, value in filterset.active_filters()]
        return chips or [('Search', 'all transients')]
    if selection.kind == Selection.KIND_SQL:
        return [('SQL query', selection.query.title if selection.query_id else '(deleted)')]
    if selection.kind == Selection.KIND_FUNCTION:
        return [('Built-in list', selection.python_query.replace('_', ' '))]
    return [('Rules', 'rule tree (not listable yet)')]
