"""Selection pages (#841), for UI preview users only.

* ``POST selections/save/``: "Save as Selection" from the transient search page.
* ``GET selections/``: "My Selections": the ones the user owns, their teams' and all-YSE ones,
  with rename, sharing and delete where allowed.
* ``GET selections/<slug>/``: a Selection's results, as the search table, with list stepping
  into the transient page (#850, source ``sel``) and "Observable tonight" (#854).
* ``POST selections/<slug>/edit/`` and ``POST selections/<slug>/delete/``.
* The rule builder (#843): ``POST selections/rule-builder/check/`` (count, summary and "View as
  SQL" for a rule tree, as JSON) and ``POST selections/rule-builder/save/`` ("Save as Selection").
  It renders in the search page's facet sidebar and on a rule Selection's edit panel.

Users without the preview get 404 on every page here; the REST endpoint
(``/api/selections/``) is not a page and is not gated.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseRedirect, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_GET, require_POST
from django_tables2 import RequestConfig

from YSE_App.common.ui_preview import ui_preview_enabled
from YSE_App.download_views import download_menu_context
from YSE_App.models import Selection
from YSE_App.services import selections as svc

#: Parameters of the results page itself (the table's paging and sorting).
RESULT_PAGE_KEYS = ('page', 'per_page', 'sort')


def _require_preview(request):
    if not ui_preview_enabled(request):
        raise Http404('Selections are part of the new layout preview.')


def _visible_or_404(request, slug):
    selection = get_object_or_404(Selection.objects.select_related('owner', 'team', 'query'), slug=slug)
    if not svc.can_view(request.user, selection):
        raise Http404('No such Selection.')
    return selection


def _owner_label(user):
    from YSE_App.services.groups import display_name

    return display_name(user)


def selection_table(request, selection):
    """``(table, count)`` for ``selection``'s results, paginated and sorted from ``request.GET``.

    Search Selections build exactly the search page's table (``build_search_table``) with the
    stored filters, so the rows, columns and order match ``/search/``. Other kinds list the
    compiled rows in the search table. Raises ``SelectionError`` when they cannot be listed.
    Shared by the results page and the transient page's list
    stepping (``list_context``), so both see the same rows in the same order.
    """
    from YSE_App.views import SEARCH_PER_PAGE_CHOICES, build_search_table
    from YSE_App.table_utils import SearchTransientTable

    page_state = {key: request.GET.getlist(key) for key in RESULT_PAGE_KEYS if key in request.GET}
    if selection.kind == Selection.KIND_SEARCH:
        if not svc.can_view(request.user, selection):
            raise svc.SelectionAccessError('You cannot see this Selection.')
        params = QueryDict(selection.search_params, mutable=True)
        for key, values in page_state.items():
            params.setlist(key, values)
        from YSE_App.filters.transient_search import quick_search_params

        params = quick_search_params(params)
        filterset, table, _per_page = build_search_table(request, params)
        if not filterset.is_valid():
            raise svc.SelectionError('The search filters of this Selection are not valid any more.')
        return table, table.paginator.count if hasattr(table, 'paginator') else None
    queryset = svc.selection_queryset(selection, request.user)
    try:
        per_page = int(request.GET.get('per_page', SEARCH_PER_PAGE_CHOICES[0]))
    except (TypeError, ValueError):
        per_page = SEARCH_PER_PAGE_CHOICES[0]
    if per_page not in SEARCH_PER_PAGE_CHOICES:
        per_page = SEARCH_PER_PAGE_CHOICES[0]
    table = SearchTransientTable(queryset, prefix='', exclude=('separation', 'gal_b', 'annotation_value'))
    RequestConfig(request, paginate={'per_page': per_page}).configure(table)
    return table, table.paginator.count


@login_required
@require_POST
def selection_save(request):
    """POST from the search page: the current search becomes a Selection.

    Fields: ``name``, ``visibility`` (me/team/all), ``team`` (group id), ``params`` (the
    search page's query string). Goes to the new Selection's page, or back to the search
    with ``selection_error``.
    """
    _require_preview(request)
    params = svc.normalize_search_params(request.POST.get('params', ''))
    try:
        selection = svc.create_selection(
            request.user, request.POST.get('name', ''), kind=Selection.KIND_SEARCH,
            visibility=request.POST.get('visibility', Selection.VISIBILITY_ME),
            team=request.POST.get('team', ''), search_params=params,
            description=request.POST.get('description', ''))
    except svc.SelectionError as exc:
        back = QueryDict(params, mutable=True)
        back['selection_error'] = str(exc)
        return HttpResponseRedirect('%s?%s' % (reverse('search'), back.urlencode()))
    messages.success(request, 'Saved the Selection "%s".' % selection.name)
    return HttpResponseRedirect(reverse('selection_detail', kwargs={'slug': selection.slug}))


@login_required
@require_GET
def selection_list(request):
    """My Selections: owned, team and all-YSE Selections, with the actions each user may take."""
    _require_preview(request)
    user = request.user
    team_ids = svc.user_team_ids(user)
    rows = {'mine': [], 'team': [], 'all': []}
    for selection in svc.visible_selections(user).order_by('name', 'pk'):
        row = {
            'selection': selection,
            'owner': _owner_label(selection.owner),
            'can_edit': svc.can_edit(user, selection, team_ids),
            'can_delete': svc.can_delete(user, selection),
            'criteria': svc.criteria_summary(selection, user),
        }
        if selection.owner_id == user.pk:
            rows['mine'].append(row)
        elif selection.visibility == Selection.VISIBILITY_TEAM:
            rows['team'].append(row)
        else:
            rows['all'].append(row)
    return render(request, 'YSE_App/selections/list.html', {
        'sections': [
            ('mine', 'Mine', rows['mine']),
            ('team', 'My teams', rows['team']),
            ('all', 'Shared with all YSE', rows['all']),
        ],
        'visibility_choices': Selection.VISIBILITY_CHOICES,
        'teams': svc.team_choices(user),
    })


@login_required
@require_GET
def selection_detail(request, slug):
    """A Selection's results (the search table), with list stepping and "Observable tonight"."""
    _require_preview(request)
    selection = _visible_or_404(request, slug)
    error = None
    table, count = None, None
    try:
        table, count = selection_table(request, selection)
    except svc.SelectionError as exc:
        error = str(exc)
    obs_blocked = ''
    if error is None:
        from YSE_App.services.observable_tonight import MAX_OBJECTS

        if count is not None and count > MAX_OBJECTS:
            obs_blocked = ('this Selection has %d matches; it works on at most %d. Narrow it first.'
                           % (count, MAX_OBJECTS))
    search_url = None
    if selection.kind == Selection.KIND_SEARCH:
        search_url = '%s?%s' % (reverse('search'), selection.search_params) if selection.search_params \
            else reverse('search')
    list_query = QueryDict(mutable=True)
    for key in RESULT_PAGE_KEYS:
        if key in request.GET:
            list_query.setlist(key, request.GET.getlist(key))
    return render(request, 'YSE_App/selections/detail.html', {
        'selection': selection,
        'owner': _owner_label(selection.owner),
        'criteria': svc.criteria_summary(selection, request.user),
        'table': table,
        'result_count': count,
        'error': error,
        'can_edit': svc.can_edit(request.user, selection),
        'can_delete': svc.can_delete(request.user, selection),
        'search_url': search_url,
        'obs_blocked': obs_blocked,
        'list_query': list_query.urlencode(),
        'api_url': reverse('selection-detail', kwargs={'slug': selection.slug}),
        'visibility_choices': Selection.VISIBILITY_CHOICES,
        'teams': svc.team_choices(request.user),
        # Download -> Spectra / Photometry (#848); this page is preview-only already.
        'download_menu': download_menu_context(
            reverse('selection_download_spectra', kwargs={'slug': selection.slug}),
            reverse('selection_download_photometry', kwargs={'slug': selection.slug})),
        'rule_builder': (rule_builder_context(request, selection.rules, mode='edit', selection=selection)
                         if selection.kind == Selection.KIND_RULES and svc.can_edit(request.user, selection) else None),
    })


def _back(request, selection=None):
    target = request.POST.get('next', '')
    if target == 'detail' and selection is not None:
        return HttpResponseRedirect(reverse('selection_detail', kwargs={'slug': selection.slug}))
    return HttpResponseRedirect(reverse('selection_list'))


@login_required
@require_POST
def selection_edit(request, slug):
    """Rename and/or re-share a Selection (owner, or a member of its team)."""
    _require_preview(request)
    selection = _visible_or_404(request, slug)
    changes = {}
    if 'name' in request.POST:
        changes['name'] = request.POST.get('name', '')
    if 'visibility' in request.POST:
        changes['visibility'] = request.POST.get('visibility', '')
    if request.POST.get('team', '').strip():  # empty: keep the current team
        changes['team'] = request.POST.get('team', '')
    if 'description' in request.POST:
        changes['description'] = request.POST.get('description', '')
    if 'rules' in request.POST:  # the rule builder (#843)
        changes['rules'] = request.POST.get('rules', '')
    try:
        svc.update_selection(request.user, selection, **changes)
    except svc.SelectionAccessError as exc:
        messages.error(request, str(exc))
    except svc.SelectionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Saved "%s".' % selection.name)
    return _back(request, selection)


@login_required
@require_POST
def selection_delete(request, slug):
    """Delete a Selection (owner only). Its criteria go; no transient or query is touched."""
    _require_preview(request)
    selection = _visible_or_404(request, slug)
    if not svc.can_delete(request.user, selection):
        messages.error(request, 'Only the owner can delete this Selection.')
        return _back(request, selection)
    name = selection.name
    selection.delete()
    messages.success(request, 'Deleted the Selection "%s".' % name)
    return HttpResponseRedirect(reverse('selection_list'))


# --------------------------------------------------------------------------- rule builder (#843)

def _saved_values(node, key):
    """Values the rules of ``node`` (a cleaned tree) give field ``key``."""
    out = set()
    for child in node.get('rules', []):
        if 'rules' in child:
            out |= _saved_values(child, key)
        elif child.get('field') == key:
            value = child.get('value')
            out.update(value if isinstance(value, list) else [value])
    return out


def _field_descriptor(rule_field, choices_cache, user=None, tree=None):
    from YSE_App.services import selection_rules as rules_svc

    if rule_field.key not in choices_cache:
        keep = _saved_values(tree, rule_field.key) if tree else ()
        choices_cache[rule_field.key] = rules_svc.field_choices(rule_field, user=user, keep=keep)
    return {
        'key': rule_field.key, 'label': rule_field.label, 'kind': rule_field.kind, 'unit': rule_field.unit,
        'help': rule_field.help, 'operators': [(op.key, op.label) for op in rule_field.operators],
        'choices': choices_cache[rule_field.key],
    }


def _view_tree(node, descriptors, depth=1):
    """The template's view of a (cleaned) rule tree: groups with depth, rules with their field descriptor."""
    from YSE_App.services.selection_rules import MAX_DEPTH

    children = []
    for child in node.get('rules', []):
        if 'rules' in child:
            children.append(_view_tree(child, descriptors, depth + 1))
        else:
            value = child.get('value')
            children.append({
                'is_group': False, 'field': child['field'], 'operator': child['operator'],
                'value': value if isinstance(value, list) else str(value),
                'values': value if isinstance(value, list) else [str(value)],
                'rf': descriptors.get(child['field']),
            })
    return {'is_group': True, 'op': node.get('op', 'and'), 'children': children, 'depth': depth,
            'can_nest': depth < MAX_DEPTH}


def rule_builder_context(request, tree=None, *, mode='save', selection=None, search_params=''):
    """Context of ``selections/rule_builder.html``.

    ``mode='save'`` (search sidebar): "Save as Selection" with a name and sharing.
    ``mode='edit'`` (a rule Selection's edit panel): "Save rules" on ``selection``.
    A saved tree that no longer validates opens empty, with the reason shown.
    """
    from YSE_App.services import selection_rules as rules_svc

    error = ''
    try:
        cleaned = rules_svc.clean_rules(tree) if tree else rules_svc.empty_tree()
    except svc.SelectionError as exc:
        cleaned, error = rules_svc.empty_tree(), str(exc)
    choices_cache = {}
    groups = [(title, [_field_descriptor(f, choices_cache, request.user, cleaned) for f in fields])
              for title, fields in rules_svc.RULE_FIELD_GROUPS]
    descriptors = {d['key']: d for _title, fields in groups for d in fields}
    if mode == 'edit':
        action = reverse('selection_edit', kwargs={'slug': selection.slug})
    else:
        action = reverse('rule_builder_save')
    return {
        'mode': mode,
        'tree': _view_tree(cleaned, descriptors),
        'new_group': {'is_group': True, 'op': 'and', 'children': [], 'depth': 2,
                      'can_nest': 2 < rules_svc.MAX_DEPTH},
        'field_groups': groups,
        'fields': [d for _title, fields in groups for d in fields],
        'max_depth': rules_svc.MAX_DEPTH,
        'max_rules': rules_svc.MAX_RULES,
        'action': action,
        'check_url': reverse('rule_builder_check'),
        'selection': selection,
        'search_params': search_params,
        'teams': svc.team_choices(request.user) if mode == 'save' else [],
        'error': error,
    }


@login_required
@require_POST
def rule_builder_check(request):
    """A rule tree's summary, match count and the SQL Django runs for it, as JSON (read-only).

    Fields: ``rules`` (JSON). ``sql=1`` adds the SQL; ``count=1`` the number of matches for
    the requesting user. Errors come back as ``{"ok": false, "error": ...}``.
    """
    _require_preview(request)
    from YSE_App.services import selection_rules as rules_svc

    try:
        tree = rules_svc.clean_rules(request.POST.get('rules', ''))
        payload = {'ok': True, 'summary': rules_svc.describe_rules(tree), 'rules': tree}
        if request.POST.get('sql') == '1':
            _sql, params, display = rules_svc.rules_sql(tree, request.user)
            payload['sql'] = display
            payload['param_count'] = len(params)
        if request.POST.get('count') == '1':
            payload['count'] = rules_svc.rules_queryset(tree, request.user).count()
    except svc.SelectionError as exc:
        payload = {'ok': False, 'error': str(exc)}
    response = JsonResponse(payload)
    add_never_cache_headers(response)
    return response


@login_required
@require_POST
def rule_builder_save(request):
    """"Save as Selection" from the rule builder: a ``rules`` Selection, then its page.

    Fields: ``name``, ``visibility``, ``team``, ``description``, ``rules`` (JSON) and ``params``
    (the search page's query string, to go back to it with ``selection_error`` on failure).
    """
    _require_preview(request)
    try:
        selection = svc.create_selection(
            request.user, request.POST.get('name', ''), kind=Selection.KIND_RULES,
            visibility=request.POST.get('visibility', Selection.VISIBILITY_ME),
            team=request.POST.get('team', ''), rules=request.POST.get('rules', ''),
            description=request.POST.get('description', ''))
    except svc.SelectionError as exc:
        back = QueryDict(svc.normalize_search_params(request.POST.get('params', '')), mutable=True)
        back['selection_error'] = str(exc)
        return HttpResponseRedirect('%s?%s' % (reverse('search'), back.urlencode()))
    messages.success(request, 'Saved the Selection "%s".' % selection.name)
    return HttpResponseRedirect(reverse('selection_detail', kwargs={'slug': selection.slug}))
