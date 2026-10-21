"""
django_cron job: keep the personal-dashboard saved-query cache warm.

Off by default; ``[site_settings] DASHBOARD_CACHE_WARM_ENABLED: True`` in
settings.ini turns it on, ``DASHBOARD_CACHE_WARM_MINUTES`` (default 60) sets
the interval. Runs from ``manage.py runcrons`` with the explorer statement cap
disabled, so a slow saved query finishes here instead of timing out in a web
request. See docs/dashboard-performance.md.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.services.dashboard_cache_warmer import (
    warm_dashboard_saved_queries,
    warming_enabled,
)

logger = logging.getLogger(__name__)


class WarmDashboardQueries(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'DASHBOARD_CACHE_WARM_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Dashboard_Cache_Warm.WarmDashboardQueries'

    def do(self):
        if not warming_enabled():
            logger.info("dashboard cache warm: disabled (DASHBOARD_CACHE_WARM_ENABLED is off)")
            return "disabled"
        results = warm_dashboard_saved_queries()
        ok = sum(1 for r in results if r.ok)
        failed = [r for r in results if r.error]
        skipped = [r for r in results if r.skipped]
        summary = "warmed %d/%d saved queries (%d failed, %d skipped)" % (
            ok, len(results), len(failed), len(skipped))
        logger.info("dashboard cache warm: %s", summary)
        for r in failed:
            logger.warning("dashboard cache warm: %s (%s): %s", r.query_id, r.title, r.error)
        return summary
