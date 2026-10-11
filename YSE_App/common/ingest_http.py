"""
Shared ``requests`` session for the ingest crons (#473).

The ingest modules used bare ``requests.get``/``requests.post`` with no
timeout, so one remote host that stopped answering stalled a whole
``runcrons`` pass. :func:`ingest_session` returns a ``requests.Session`` that

- applies a default ``(connect, read)`` timeout to every request that does not
  pass one (an explicit ``timeout=`` still wins);
- retries connection errors, and 502/503/504 answers to GET/HEAD, a bounded
  number of times with exponential backoff (urllib3 ``Retry``). A POST is only
  retried when the connection failed before anything was sent; a read error
  or a bad status on a POST is never retried, so an upload is never sent twice.

After the last retry the final response is returned as before (no new
exception for a bad status), so callers' ``status_code`` checks still work.

Settings (``[site_settings]`` in settings.ini, see YSE_PZ/settings.py):
``INGEST_HTTP_CONNECT_TIMEOUT_SECONDS`` (15), ``INGEST_HTTP_READ_TIMEOUT_SECONDS``
(300), ``INGEST_HTTP_UPLOAD_TIMEOUT_SECONDS`` (900; :func:`upload_timeout`, for
the crons' own POSTs to ``/add_transient/``, which can run long) and
``INGEST_HTTP_RETRIES`` (3).
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_READ_TIMEOUT = 300.0
DEFAULT_UPLOAD_TIMEOUT = 900.0
DEFAULT_RETRIES = 3
RETRY_STATUSES = (502, 503, 504)


def _setting(name, default):
    try:
        from django.conf import settings

        value = getattr(settings, name, default)
    except Exception:  # settings not configured (a script run outside Django)
        return default
    return default if value is None else value


def default_timeout():
    """``(connect, read)`` seconds applied when a request passes no timeout."""
    return (
        float(_setting('INGEST_HTTP_CONNECT_TIMEOUT_SECONDS', DEFAULT_CONNECT_TIMEOUT)),
        float(_setting('INGEST_HTTP_READ_TIMEOUT_SECONDS', DEFAULT_READ_TIMEOUT)),
    )


def upload_timeout():
    """``(connect, read)`` for the crons' POSTs to this site's ``/add_transient/``."""
    return (
        float(_setting('INGEST_HTTP_CONNECT_TIMEOUT_SECONDS', DEFAULT_CONNECT_TIMEOUT)),
        float(_setting('INGEST_HTTP_UPLOAD_TIMEOUT_SECONDS', DEFAULT_UPLOAD_TIMEOUT)),
    )


def retry_policy(retries=None):
    retries = int(_setting('INGEST_HTTP_RETRIES', DEFAULT_RETRIES) if retries is None else retries)
    kwargs = dict(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.0,
        status_forcelist=RETRY_STATUSES,
        raise_on_status=False,
    )
    methods = frozenset(['GET', 'HEAD'])
    try:
        return Retry(allowed_methods=methods, **kwargs)
    except TypeError:  # urllib3 < 1.26 (the docker web image pins 1.22)
        return Retry(method_whitelist=methods, **kwargs)


class TimeoutSession(requests.Session):
    """A ``requests.Session`` with a default timeout."""

    def __init__(self, timeout=None):
        super().__init__()
        self.default_timeout = timeout if timeout is not None else default_timeout()

    def request(self, method, url, *args, **kwargs):
        if kwargs.get('timeout') is None:
            kwargs['timeout'] = self.default_timeout
        return super().request(method, url, *args, **kwargs)


def ingest_session(timeout=None, retries=None):
    """A new session with the default timeout and retry policy mounted for http(s)."""
    session = TimeoutSession(timeout=timeout)
    adapter = HTTPAdapter(max_retries=retry_policy(retries))
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


class _LazySession:
    """Module-level ``get``/``post`` facade: one session, created on first use."""

    def __init__(self):
        self._session = None

    def _get(self):
        if self._session is None:
            self._session = ingest_session()
        return self._session

    def get(self, url, *args, **kwargs):
        return self._get().get(url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self._get().post(url, *args, **kwargs)


#: ``ingest_http.http.get(...)`` / ``.post(...)``: drop-in for ``requests.get``/``post``.
http = _LazySession()
