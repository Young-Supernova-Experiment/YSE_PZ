"""Turn a transient search into a saved Explorer query (#287, umbrella #284).

The search page and ``/api/transients/`` filter with
``YSE_App.filters.transient_search.TransientSearchFilterSet``. Power users keep
their long-lived selections as django-sql-explorer ``Query`` rows, attached to
their personal dashboard through ``UserQuery``. This module bridges the two:

* :func:`compile_search_sql` compiles the FilterSet's queryset for the
  ``explorer`` database connection and inlines the parameters as SQL literals,
  giving one ``SELECT YSE_App_transient.name FROM ... WHERE ... ORDER BY ...``
  statement that returns exactly the ids the search returns. The dashboard
  reads the transient name from the first column and runs the text as-is
  (``views.run_explorer_query_cached``), so the statement is checked against
  ``dashboard_sql_is_supported`` before it is offered.
* :func:`save_search_query` creates the ``Query`` (title from the user,
  description with the human-readable filters and the shareable search URL)
  and, on request, the ``UserQuery`` that puts it on the user's dashboard.

Relative-time filters (``days_since_disc_max``, ``days_since_last_det_max``)
compile to ``UTC_TIMESTAMP()`` / ``UNIX_TIMESTAMP()`` expressions, so a saved
"last detected within 5 days" keeps moving; every other value is frozen as
typed. Filters that depend on the requesting user (``has_spectrum`` for a
non-staff user, ``visible_to_group``) bake that user's groups into the SQL.
"""

import datetime
import decimal
import re

from django.core.exceptions import EmptyResultSet
from django.db import connections, transaction
from django.utils import timezone

from YSE_App.filters.transient_search import TransientSearchFilterSet, quick_search_params
from YSE_App.models import Transient, UserQuery
from YSE_App.services.dashboard_queries import (
    dashboard_sql_rejection_reason,
    matching_user_queries,
)

__all__ = [
    'SearchSaveError',
    'compile_search_sql',
    'compiled_search',
    'default_search_title',
    'save_search_query',
    'search_description',
    'sql_literal',
    'inline_sql_params',
]

EXPLORER_ALIAS = 'explorer'
TITLE_MAX_LENGTH = 255
_PLACEHOLDER_RE = re.compile(r'%(s|%)')


class SearchSaveError(ValueError):
    """A search could not be compiled or saved; ``str(exc)`` is user-facing."""


def sql_literal(value, connection):
    """``value`` as an SQL literal for ``connection``'s dialect.

    Numbers and booleans are written bare, ``None`` is ``NULL``, dates and
    datetimes go through the backend's own adapters (UTC naive text for a
    ``DateTimeField``) and strings are single-quoted. MySQL treats a backslash
    inside a quoted literal as an escape, sqlite does not, so the backslash is
    doubled only where the server would eat it.
    """
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float, decimal.Decimal)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, datetime.datetime):
        value = connection.ops.adapt_datetimefield_value(value)
    elif isinstance(value, datetime.date):
        value = connection.ops.adapt_datefield_value(value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode('utf-8', 'replace')
    text = str(value)
    if connection.vendor == 'mysql':
        text = text.replace('\\', '\\\\')
    text = text.replace("'", "''")
    if '\x00' in text:
        raise SearchSaveError('The search contains a value that cannot be written as SQL.')
    return "'%s'" % text


def inline_sql_params(sql, params, connection):
    """``sql`` with every ``%s`` replaced by the matching literal (``%%`` -> ``%``)."""
    params = list(params)
    out = []
    pos = 0
    index = 0
    for match in _PLACEHOLDER_RE.finditer(sql):
        out.append(sql[pos:match.start()])
        if match.group(1) == '%':
            out.append('%')
        else:
            if index >= len(params):
                raise SearchSaveError('Internal error: SQL placeholders and parameters do not match.')
            out.append(sql_literal(params[index], connection))
            index += 1
        pos = match.end()
    out.append(sql[pos:])
    if index != len(params):
        raise SearchSaveError('Internal error: SQL placeholders and parameters do not match.')
    return ''.join(out)


def compiled_search(params, request=None, using=EXPLORER_ALIAS):
    """``(filterset, sql)`` for ``params`` (a ``QueryDict`` or dict of lists/strings).

    Raises :class:`SearchSaveError` when the parameters do not validate or the
    resulting statement is one the dashboard would refuse.
    """
    from django.http import QueryDict

    if not hasattr(params, 'getlist'):
        data = QueryDict(mutable=True)
        for key, value in dict(params).items():
            if isinstance(value, (list, tuple)):
                data.setlist(key, [str(v) for v in value])
            elif value is not None:
                data[key] = str(value)
        params = data
    params = quick_search_params(params)
    filterset = TransientSearchFilterSet(params, queryset=Transient.objects.all(), request=request)
    if not filterset.is_valid():
        messages = []
        for field, errors in filterset.errors.items():
            label = field if field == '__all__' else (filterset.filters.get(field).label if field in filterset.filters else field)
            prefix = '' if field == '__all__' else '%s: ' % label
            messages.extend(prefix + str(e) for e in errors)
        raise SearchSaveError('The search is not valid: ' + '; '.join(messages))
    queryset = filterset.qs.values('name')
    connection = connections[using]
    try:
        sql, sql_params = queryset.query.get_compiler(using=using).as_sql()
    except EmptyResultSet:
        # The ORM already knows the search matches nothing (for example
        # ``visible_to_group`` for a group the user is not in); there is no
        # point in saving a query that can never return a row.
        raise SearchSaveError('This search can never match a transient, so there is nothing to save.')
    sql = inline_sql_params(sql, sql_params, connection)
    reason = dashboard_sql_rejection_reason(sql)
    if reason is not None:
        raise SearchSaveError('The dashboard cannot run this search as SQL: %s.' % reason)
    return filterset, sql


def compile_search_sql(params, request=None, using=EXPLORER_ALIAS):
    """The SQL text alone (see :func:`compiled_search`)."""
    return compiled_search(params, request=request, using=using)[1]


def default_search_title(filterset, max_length=80):
    """A title suggestion built from the active filters ("Search: status New; tags young")."""
    parts = ['%s %s' % (label, value) for _, label, value in filterset.active_filters()
             if _ not in ('ordering', 'per_page')]
    title = 'Search: ' + ('; '.join(parts) if parts else 'all transients')
    if len(title) > max_length:
        title = title[:max_length - 3].rstrip() + '...'
    return title


def search_description(filterset, search_url=None, user=None):
    """Description text for the Explorer query: the filters and the shareable URL."""
    lines = ['Saved from the transient search page on %s UTC%s.' % (
        timezone.now().strftime('%Y-%m-%d %H:%M'), (' by %s' % user.username) if user is not None else '')]
    chips = [(label, value) for name, label, value in filterset.active_filters()]
    if chips:
        lines.append('Filters: ' + '; '.join('%s = %s' % (label, value) for label, value in chips))
    else:
        lines.append('Filters: none (every transient).')
    if search_url:
        lines.append('Search page: %s' % search_url)
    lines.append('The SQL was generated by YSE_App.services.search_queries; edit the search and save again '
                 'rather than editing the SQL by hand.')
    return '\n'.join(lines)


def save_search_query(user, params, title, *, add_to_dashboard=False, request=None, search_url=None):
    """Create the Explorer ``Query`` (and the ``UserQuery`` link) for a search.

    Returns ``{'query', 'user_query', 'sql', 'filterset', 'created'}``;
    ``user_query`` is ``None`` unless ``add_to_dashboard``. Raises
    :class:`SearchSaveError` for a missing or duplicate title and for
    searches that do not compile (see :func:`compiled_search`).
    """
    from explorer.models import Query

    if user is None or not user.is_authenticated:
        raise SearchSaveError('Log in to save a search.')
    title = (title or '').strip()
    if not title:
        raise SearchSaveError('Give the saved query a title.')
    if len(title) > TITLE_MAX_LENGTH:
        raise SearchSaveError('The title must be at most %d characters.' % TITLE_MAX_LENGTH)
    if Query.objects.filter(title=title).exists():
        # transient_summary and download_bulk_photometry look queries up by
        # title, so a second query with the same title would be unreachable.
        raise SearchSaveError('A saved query called "%s" already exists; choose another title.' % title)
    filterset, sql = compiled_search(params, request=request)
    with transaction.atomic():
        query = Query.objects.create(
            title=title,
            sql=sql,
            description=search_description(filterset, search_url=search_url, user=user),
            snapshot=False,
            created_by_user=user,
        )
        user_query = None
        if add_to_dashboard:
            user_query = matching_user_queries(user, query=query).order_by('id').first()
            if user_query is None:
                user_query = UserQuery.objects.create(
                    user=user, query=query, created_by=user, modified_by=user,
                )
    return {'query': query, 'user_query': user_query, 'sql': sql, 'filterset': filterset, 'created': True}
