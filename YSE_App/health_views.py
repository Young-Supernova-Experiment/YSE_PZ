"""Database-backed health check for monitors and the server watchdog (#768).

``GET /healthz/`` answers ``200 ok`` when Django can run ``SELECT 1`` on the
default database and ``503 db error`` when it cannot. It is public (no login),
sets no cookies, and an anonymous request makes exactly one query, so it stays
cheap enough to poll every minute. ``scripts/ops/yse-watchdog.sh`` (#769) polls
it; see docs/ziggy-runbook.md.
"""

import logging

from django.db import DEFAULT_DB_ALIAS, Error as DjangoDBError, connections
from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from YSE_App.login_required import login_exempt

logger = logging.getLogger(__name__)


def _response(body: str, status: int) -> HttpResponse:
    """Build a plain-text, uncacheable health response.

    Parameters:
        body: Response text ("ok" or "db error").
        status: HTTP status code (200 or 503).

    Returns:
        An ``HttpResponse`` with ``Content-Type: text/plain``.

    Use cases:
        Shared by both branches of ``healthz`` so they format identically.

    Limitations:
        ``never_cache`` on the view adds the no-store headers; this helper
        only sets the body, status and content type.
    """
    return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")


@login_exempt
@never_cache
@require_safe
def healthz(request: HttpRequest) -> HttpResponse:
    """Report whether Django can query the default database.

    Parameters:
        request: The incoming GET or HEAD request. Nothing in it is read.

    Returns:
        200 with body ``ok`` if ``SELECT 1`` succeeds on the default
        connection; 503 with body ``db error`` if connecting or querying
        raises a database error (``django.db.Error``: operational,
        interface or programming errors). Other methods get 405.

    Use cases:
        Polled once a minute by the server watchdog (scripts/ops/yse-watchdog.sh)
        and by external uptime monitors. Together with a static file that
        Apache serves without Django, it tells "MySQL down" apart from
        "Django stuck" and "Apache down".

    Limitations:
        Proves only that one trivial query works. It does not detect slow
        queries on particular tables, the ``explorer`` connection, caches or
        background jobs. If MySQL hangs rather than refusing, this request
        hangs too until a client read timeout fires (``[database]
        DB_READ_TIMEOUT``, #770) or Apache gives up. When every mod_wsgi
        thread is busy the request never reaches this view at all; the
        watchdog treats that timeout as a failure.
    """
    try:
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
    except DjangoDBError as exc:
        logger.warning("healthz: database check failed: %s", exc.__class__.__name__)
        return _response("db error", 503)
    if not row or row[0] != 1:
        logger.warning("healthz: SELECT 1 returned an unexpected row")
        return _response("db error", 503)
    return _response("ok", 200)
