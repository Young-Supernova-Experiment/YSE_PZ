"""Helpers for the saved queries pinned to a user's personal dashboard.

A ``UserQuery`` row attaches either an explorer ``Query`` (``query``) or a
registered Python query (``python_query``) to one user.  The model has no
uniqueness constraint, so the same query can end up attached many times
(issue #203).  These helpers define what "the same query" means and are shared
by the add/remove form views and the ``dedupe_dashboard_queries`` command.
"""

from __future__ import annotations

from collections import defaultdict

from YSE_App.models import UserQuery


def _identity(user_query):
    """(user_id, query_id, python_query) tuple that identifies a saved query."""
    return (
        user_query.user_id,
        user_query.query_id,
        user_query.python_query or "",
    )


def matching_user_queries(user, query=None, python_query=None):
    """All ``UserQuery`` rows attaching the same query to ``user``.

    An explorer query is matched on ``query``; a Python query on
    ``python_query``.  When both are empty the result is empty.
    """
    rows = UserQuery.objects.filter(user=user)
    if query is not None:
        return rows.filter(query=query)
    if python_query:
        return rows.filter(query__isnull=True, python_query=python_query)
    return rows.none()


def duplicates_of(user_query):
    """Every row (including ``user_query`` itself) attaching the same query."""
    return matching_user_queries(
        user_query.user, query=user_query.query, python_query=user_query.python_query
    )


def find_duplicate_groups(queryset=None):
    """Group rows by (user, query, python_query) and return groups with > 1 row.

    Returns a list of lists of ``UserQuery`` ordered by id; the first element of
    each group is the row to keep.
    """
    if queryset is None:
        queryset = UserQuery.objects.all()
    groups = defaultdict(list)
    for row in queryset.order_by("id"):
        groups[_identity(row)].append(row)
    return [rows for rows in groups.values() if len(rows) > 1]


def dedupe_user_queries(queryset=None, *, dry_run=False):
    """Delete all but the oldest row in each duplicate group.

    Returns ``(groups, deleted_ids)`` where ``groups`` is the output of
    :func:`find_duplicate_groups` and ``deleted_ids`` are the ids that were (or,
    with ``dry_run``, would be) deleted.
    """
    groups = find_duplicate_groups(queryset)
    doomed = [row.id for rows in groups for row in rows[1:]]
    if doomed and not dry_run:
        UserQuery.objects.filter(id__in=doomed).delete()
    return groups, doomed


# --------------------------------------------------------------------------- #
# Which saved SQL may run on the dashboard (issue #258)
# --------------------------------------------------------------------------- #

import re as _re

from django.conf import settings as _settings

# Leading whitespace and SQL comments: ``-- ...`` and ``# ...`` line comments,
# ``/* ... */`` block comments, in any combination.
_LEADING_NOISE_RE = _re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|#[^\n]*(?:\n|$)|/\*.*?\*/)+", _re.S)
_READ_STATEMENT_RE = _re.compile(r"^(?:select|with)\b", _re.I)


def strip_leading_sql_comments(sql):
    """``sql`` without its leading whitespace and comments."""
    text = sql or ""
    match = _LEADING_NOISE_RE.match(text)
    return text[match.end():] if match else text


def dashboard_sql_rejection_reason(sql):
    """
    Why a saved Explorer query cannot back a dashboard section, or None when
    it can. The dashboard runs the text as-is and reads ``name`` from the
    first column, so it must be a single read statement (``SELECT``, or a
    ``WITH`` common-table expression that ends in one) over
    ``YSE_App_transient`` that mentions ``name``. Leading whitespace and
    comments are ignored (Explorer keeps whatever the author typed); a
    statement that is not a SELECT/WITH is refused.
    """
    body = strip_leading_sql_comments(sql)
    lowered = body.lower()
    if not _READ_STATEMENT_RE.match(body):
        return "the query must start with SELECT (or WITH ... SELECT)"
    if "yse_app_transient" not in lowered:
        return "the query must read from YSE_App_transient"
    if "name" not in lowered:
        return "the query must return the transient name column"
    return None


def dashboard_sql_is_supported(sql):
    return dashboard_sql_rejection_reason(sql) is None


def explorer_query_cache_seconds():
    """TTL for cached saved-query results ([site_settings] EXPLORER_QUERY_CACHE_SECONDS)."""
    try:
        return int(getattr(_settings, "EXPLORER_QUERY_CACHE_SECONDS", 3600) or 3600)
    except (TypeError, ValueError):
        return 3600
