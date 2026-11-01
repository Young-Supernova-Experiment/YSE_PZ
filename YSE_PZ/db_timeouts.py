"""Client-side MySQL timeouts for the ``default`` and ``explorer`` connections (#770).

Without them a stalled MySQL, or a statement that runs for minutes, holds the
web thread that issued it for as long as it takes. On a mod_wsgi daemon with a
fixed number of threads a few such requests make every DB-backed page fail.

Settings (``[database]`` in settings.ini; the ``YSE_``-prefixed environment
variable wins), all whole seconds, unset or 0 meaning "no timeout", which is
today's behaviour:

* ``DB_CONNECT_TIMEOUT`` (``YSE_DB_CONNECT_TIMEOUT``): opening a connection;
* ``DB_READ_TIMEOUT`` (``YSE_DB_READ_TIMEOUT``): waiting for the server's reply;
* ``DB_WRITE_TIMEOUT`` (``YSE_DB_WRITE_TIMEOUT``): sending to the server;
* ``EXPLORER_DB_READ_TIMEOUT`` (``YSE_EXPLORER_DB_READ_TIMEOUT``): read timeout
  for the ``explorer`` alias only. When set (0 included) it replaces
  ``DB_READ_TIMEOUT`` for that alias; when unset, ``DB_READ_TIMEOUT`` applies.

This module is imported by ``YSE_PZ/settings.py`` before Django is set up, so
it must not import Django or ``YSE_App``.
"""

import os
from typing import Callable, Dict, Optional

# (option key for mysqlclient, settings.ini key, environment variable)
_COMMON = (
    ('connect_timeout', 'DB_CONNECT_TIMEOUT', 'YSE_DB_CONNECT_TIMEOUT'),
    ('write_timeout', 'DB_WRITE_TIMEOUT', 'YSE_DB_WRITE_TIMEOUT'),
)
_READ = ('read_timeout', 'DB_READ_TIMEOUT', 'YSE_DB_READ_TIMEOUT')
_EXPLORER_READ = ('read_timeout', 'EXPLORER_DB_READ_TIMEOUT', 'YSE_EXPLORER_DB_READ_TIMEOUT')

IniGetter = Callable[[str], Optional[str]]


def _seconds(ini_get: IniGetter, environ: Dict[str, str], ini_key: str, env_key: str) -> Optional[int]:
    """Read one timeout in whole seconds.

    Parameters:
        ini_get: Returns the raw ``[database]`` value for a key, or None if absent.
        environ: Environment mapping; a non-empty ``env_key`` wins over the ini value.
        ini_key: Key in ``[database]`` of settings.ini.
        env_key: Environment variable name.

    Returns:
        The timeout in seconds, or None when unset, empty or 0 (no timeout).

    Use cases:
        Parsing each of the four timeout settings the same way.

    Limitations:
        Only non-negative whole numbers are accepted; anything else raises
        ``ValueError`` naming the key, so a typo fails at startup instead of
        silently leaving the timeout off.
    """
    raw = (environ.get(env_key) or '').strip()
    source = env_key
    if not raw:
        raw = (ini_get(ini_key) or '').strip()
        source = '[database] ' + ini_key
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError('%s must be a whole number of seconds, got %r' % (source, raw))
    value = int(raw)
    return value or None


def timeout_options(ini_get: IniGetter, environ: Optional[Dict[str, str]] = None, explorer: bool = False) -> Dict[str, int]:
    """Build the mysqlclient timeout ``OPTIONS`` for one database alias.

    Parameters:
        ini_get: Returns the raw ``[database]`` value for a key, or None if absent
            (in settings.py: ``lambda k: config.get('database', k, fallback=None)``).
        environ: Environment mapping; defaults to ``os.environ``.
        explorer: True for the ``explorer`` alias: a configured
            ``EXPLORER_DB_READ_TIMEOUT`` (0 included) replaces ``DB_READ_TIMEOUT``.

    Returns:
        A dict with any of ``connect_timeout``, ``read_timeout`` and
        ``write_timeout`` that are configured; empty when none are, so the
        connection behaves exactly as before.

    Use cases:
        Merged into ``DATABASES[alias]['OPTIONS']`` in settings.py.

    Limitations:
        These are client-side socket timeouts. libmysqlclient retries a timed-out
        read, so a statement fails after up to three times ``read_timeout``, and
        MySQL keeps running the statement on the server until it next tries to
        send to the closed connection. Pair a read timeout with a server-side
        statement cap (``EXPLORER_QUERY_MAX_EXECUTION_MS`` for Explorer) where
        long statements are expected. A read timeout shorter than the slowest
        legitimate query (saved dashboard queries, migrations, ``rebuild_*``
        commands) makes that query fail with "Lost connection to MySQL server";
        run such commands with ``YSE_DB_READ_TIMEOUT=0`` in the environment.
    """
    env = os.environ if environ is None else environ
    options: Dict[str, int] = {}
    for opt, ini_key, env_key in _COMMON:
        value = _seconds(ini_get, env, ini_key, env_key)
        if value is not None:
            options[opt] = value
    read_key = _READ
    if explorer and ((env.get(_EXPLORER_READ[2]) or '').strip() or (ini_get(_EXPLORER_READ[1]) or '').strip()):
        read_key = _EXPLORER_READ
    read = _seconds(ini_get, env, read_key[1], read_key[2])
    if read is not None:
        options['read_timeout'] = read
    return options
