"""django_cron class that queues the nightly AI-summary batch (#296).

Registered in ``CRON_CLASSES``; a no-op until ``SUMMARY_BATCH_CRON_ENABLED`` is
set under ``[llm]`` in ``settings.ini``. Every ``SUMMARY_BATCH_CRON_MINUTES``
(1440) it enqueues one ``summaries.refresh_stale`` job, which queues a
collaboration-wide summariser run for each transient with a new public comment,
spectrum or follow-up in the last ``SUMMARY_BATCH_HOURS``. The queue runner
(``RunQueuedJobs`` or ``run_jobs --loop``) executes them, so no provider call
ever runs inside ``manage.py runcrons`` itself.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

logger = logging.getLogger(__name__)


class SummaryRefresh(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'SUMMARY_BATCH_CRON_MINUTES', 1440) or 1440)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Summary_Jobs.SummaryRefresh'

    def do(self):
        if not getattr(settings, 'SUMMARY_BATCH_CRON_ENABLED', False):
            logger.info("summary refresh cron: disabled (SUMMARY_BATCH_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.summaries import queue_refresh

        job = queue_refresh()
        summary = "queued job %s" % job.pk if job is not None else "a refresh job is already queued or running"
        logger.info("summary refresh cron: %s", summary)
        return summary
