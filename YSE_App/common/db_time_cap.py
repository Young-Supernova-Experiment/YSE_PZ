"""
Statement execution time cap for the ``explorer`` database connection.

Saved Explorer SQL is user-written and runs on a dashboard cache miss inside a
web request (``run_explorer_query_cached``). MySQL 5.7+ honours a per-session
``max_execution_time`` (milliseconds, SELECT only) and MariaDB
``max_statement_time`` (seconds). The cap is applied once, when Django opens a
connection for the ``explorer`` alias (``connection_created`` signal, wired in
``YSE_App.signals``), so it costs one statement per new connection rather than
two round trips per query, and cannot leak into or out of other aliases. Other
backends (sqlite in tests) are a no-op, and setting
``EXPLORER_QUERY_MAX_EXECUTION_MS`` to 0 disables it.
"""

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from django.conf import settings
from django.db import DatabaseError

logger = logging.getLogger(__name__)

EXPLORER_ALIAS = 'explorer'

# MySQL ER_QUERY_TIMEOUT / MariaDB ER_STATEMENT_TIMEOUT
_TIMEOUT_ERROR_CODES = {3024, 1969}


class QueryTimeout(DatabaseError):
    """The statement exceeded the configured execution time cap."""


# Process-wide override of the configured cap (None = use settings). Set by
# ``explorer_cap_override`` for the cache warmer, which runs saved queries
# from a cron process and must not be cut off by the interactive cap.
_CAP_OVERRIDE_MS: Optional[int] = None


def explorer_cap_ms() -> int:
    if _CAP_OVERRIDE_MS is not None:
        return _CAP_OVERRIDE_MS
    try:
        return int(getattr(settings, 'EXPLORER_QUERY_MAX_EXECUTION_MS', 0) or 0)
    except (TypeError, ValueError):
        return 0


def _close_explorer_connection() -> None:
    from django.db import connections

    if EXPLORER_ALIAS in connections:
        connections[EXPLORER_ALIAS].close()


@contextmanager
def explorer_cap_override(max_ms: int) -> Iterator[None]:
    """
    Run the block with the explorer statement cap set to ``max_ms`` (0 = no
    cap). The cap is applied when a connection is opened, so the current
    explorer connection is closed on entry and again on exit; the next
    statement in each phase opens a connection with the right cap.
    """
    global _CAP_OVERRIDE_MS
    previous = _CAP_OVERRIDE_MS
    _close_explorer_connection()
    _CAP_OVERRIDE_MS = int(max_ms or 0)
    try:
        yield
    finally:
        _CAP_OVERRIDE_MS = previous
        _close_explorer_connection()


def is_timeout_error(exc: BaseException) -> bool:
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int) and args[0] in _TIMEOUT_ERROR_CODES:
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_timeout_error(cause)
    return False


def apply_statement_time_cap(connection, max_ms: Optional[int]) -> bool:
    """
    ``SET SESSION`` the statement time cap on the Django connection wrapper
    ``connection``. Returns True when a cap was set. No-op (False) when
    ``max_ms`` is falsy or the backend is not MySQL/MariaDB; a failing SET
    (old server, missing privilege) is logged, never raised.
    """
    if not max_ms or getattr(connection, 'vendor', None) != 'mysql':
        return False
    try:
        if getattr(connection, 'mysql_is_mariadb', False):
            sql, args = 'SET SESSION max_statement_time = %s', [max_ms / 1000.0]
        else:
            sql, args = 'SET SESSION max_execution_time = %s', [int(max_ms)]
        with connection.cursor() as cursor:
            cursor.execute(sql, args)
    except DatabaseError as exc:
        logger.warning(
            "could not set the statement time cap on connection %r: %s",
            getattr(connection, 'alias', '?'), exc,
        )
        return False
    return True


def cap_explorer_connection(sender, connection, **kwargs):
    """``connection_created`` receiver: cap every new ``explorer`` connection."""
    if getattr(connection, 'alias', None) != EXPLORER_ALIAS:
        return
    apply_statement_time_cap(connection, explorer_cap_ms())


@contextmanager
def translate_query_timeout(max_ms: Optional[int]) -> Iterator[None]:
    """
    Re-raise a MySQL/MariaDB statement-timeout ``DatabaseError`` from the
    block as :class:`QueryTimeout` with a message that names the cap. Every
    other exception passes through unchanged.
    """
    try:
        yield
    except DatabaseError as exc:
        if is_timeout_error(exc):
            raise QueryTimeout(
                'query exceeded the %d ms execution time cap' % int(max_ms or 0)
            ) from exc
        raise
