"""
Warm the saved-query result cache for every Explorer query pinned to any
user's personal dashboard.

``run_explorer_query_cached`` caches the list of transient names a saved
query returns (``EXPLORER_QUERY_CACHE_SECONDS``, default one hour). Without a
warmer the first visitor after every expiry, and after every Apache restart
when the cache is per-process ``LocMemCache``, pays the full run of each query
inside a web request (#233). The warmer runs the same function from a cron
process with the interactive statement cap disabled, so the browser only ever
reads a warm entry. With ``REDIS_URL`` set the warmed result is shared by
every web process.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from django.conf import settings

from YSE_App.common.db_time_cap import explorer_cap_override
from YSE_App.services.dashboard_queries import (
    dashboard_sql_rejection_reason,
    explorer_query_cache_seconds,
)

logger = logging.getLogger(__name__)


@dataclass
class WarmResult:
    query_id: int
    title: str
    elapsed_ms: float
    n_names: Optional[int] = None
    error: Optional[str] = None
    skipped: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None


def warming_enabled() -> bool:
    return bool(getattr(settings, "DASHBOARD_CACHE_WARM_ENABLED", False))


def dashboard_saved_queries():
    """Distinct Explorer ``Query`` rows attached to at least one dashboard."""
    from explorer.models import Query

    return Query.objects.filter(userquery__isnull=False).distinct().order_by("id")


def warm_dashboard_saved_queries(*, timeout: Optional[int] = None, uncapped: bool = True) -> List[WarmResult]:
    """
    Re-run every dashboard saved query and store its names under the cache key
    the views read, refreshing entries that are still warm so the TTL restarts
    from now. Each query is isolated: an error is recorded and the loop goes on.
    """
    from YSE_App.views import run_explorer_query_cached

    if timeout is None:
        timeout = explorer_query_cache_seconds()
    results: List[WarmResult] = []

    def _run():
        for query in dashboard_saved_queries():
            reason = dashboard_sql_rejection_reason(query.sql)
            if reason is not None:
                results.append(WarmResult(query.id, query.title, 0.0, skipped=reason))
                continue
            start = time.perf_counter()
            try:
                names = run_explorer_query_cached(query, timeout=timeout, refresh=True)
            except Exception as exc:  # one bad query must not stop the others
                elapsed = (time.perf_counter() - start) * 1000.0
                logger.warning("dashboard warm: query %s (%s) failed after %.0f ms: %s",
                               query.id, query.title, elapsed, exc)
                results.append(WarmResult(query.id, query.title, elapsed, error=str(exc)[:500]))
                continue
            elapsed = (time.perf_counter() - start) * 1000.0
            logger.info("dashboard warm: query %s (%s): %d names in %.0f ms",
                        query.id, query.title, len(names), elapsed)
            results.append(WarmResult(query.id, query.title, elapsed, n_names=len(names)))

    if uncapped:
        with explorer_cap_override(0):
            _run()
    else:
        _run()
    return results
