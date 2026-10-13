"""django_cron job: monthly SciX researcher metrics (#629).

Off by default: ``[research] RESEARCHER_METRICS_ENABLED: True`` in settings.ini
(or env ``YSE_RESEARCHER_METRICS=1``). RESEARCHER_METRICS_CRON_MINUTES sets the
interval (default 43200, 30 days). See docs/researcher-profile.md.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.services import researcher_metrics

logger = logging.getLogger(__name__)


class RefreshResearcherMetrics(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'RESEARCHER_METRICS_CRON_MINUTES', 43200) or 43200)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Researcher_Metrics.RefreshResearcherMetrics'

    def do(self):
        if not researcher_metrics.enabled():
            logger.info("researcher metrics: disabled (RESEARCHER_METRICS_ENABLED is off)")
            return "disabled"
        summary = researcher_metrics.refresh_all()
        logger.info("researcher metrics: %s", summary)
        return str(summary)
