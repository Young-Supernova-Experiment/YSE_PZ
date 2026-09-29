"""
Per-statement execution time cap for a Django database connection.

MySQL 5.7+ honours ``max_execution_time`` (milliseconds, SELECT only) and
MariaDB ``max_statement_time`` (seconds). Other backends (sqlite in tests) get
a no-op, so callers can wrap any query unconditionally.
"""

from contextlib import contextmanager
from typing import Iterator, Optional

from django.db import DatabaseError, connections

# MySQL ER_QUERY_TIMEOUT / MariaDB ER_STATEMENT_TIMEOUT
_TIMEOUT_ERROR_CODES = {3024, 1969}


class QueryTimeout(DatabaseError):
    """The statement exceeded the configured execution time cap."""


def is_timeout_error(exc: BaseException) -> bool:
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int) and args[0] in _TIMEOUT_ERROR_CODES:
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_timeout_error(cause)
    return False


@contextmanager
def statement_time_cap(alias: str, max_ms: Optional[int]) -> Iterator[None]:
    """
    Cap every statement run on ``connections[alias]`` inside the block at
    ``max_ms`` milliseconds. A capped statement raises :class:`QueryTimeout`
    (a ``DatabaseError``) instead of holding the worker for its full run.

    No-op when ``max_ms`` is falsy or the backend is not MySQL/MariaDB, and
    when the SET SESSION itself fails (old server, missing privilege).
    """
    conn = connections[alias]
    if not max_ms or conn.vendor != 'mysql':
        yield
        return

    is_mariadb = bool(getattr(conn, 'mysql_is_mariadb', False))
    if is_mariadb:
        set_sql, set_args = 'SET SESSION max_statement_time = %s', [max_ms / 1000.0]
        reset_sql = 'SET SESSION max_statement_time = 0'
    else:
        set_sql, set_args = 'SET SESSION max_execution_time = %s', [int(max_ms)]
        reset_sql = 'SET SESSION max_execution_time = 0'

    try:
        with conn.cursor() as cursor:
            cursor.execute(set_sql, set_args)
    except DatabaseError:
        yield
        return

    try:
        yield
    except DatabaseError as exc:
        if is_timeout_error(exc):
            raise QueryTimeout(
                'query exceeded the %d ms execution time cap' % int(max_ms)
            ) from exc
        raise
    finally:
        try:
            with conn.cursor() as cursor:
                cursor.execute(reset_sql)
        except DatabaseError:
            pass
