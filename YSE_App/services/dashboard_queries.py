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
