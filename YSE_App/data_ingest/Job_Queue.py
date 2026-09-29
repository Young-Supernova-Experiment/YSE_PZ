"""
django_cron job: run one pass of the background job queue (issue #263).

Registered in ``CRON_CLASSES`` so the existing ``manage.py runcrons`` crontab
entry drains the queue without new infrastructure. ``JOB_RUNNER_CRON_ENABLED``
(default True) turns it off when a ``run_jobs --loop`` worker under systemd
handles the queue instead; ``JOB_RUNNER_CRON_MINUTES`` is the interval and
``JOB_RUNNER_CRON_BUDGET_SECONDS`` caps one pass so a long job cannot hold the
``runcrons`` run for the other crons. See docs/background-jobs.md.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.jobs import queue_counts, run_pass

logger = logging.getLogger(__name__)


class RunQueuedJobs(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'JOB_RUNNER_CRON_MINUTES', 1) or 1)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Job_Queue.RunQueuedJobs'

    def do(self):
        if not getattr(settings, 'JOB_RUNNER_CRON_ENABLED', True):
            logger.info("job queue cron: disabled (JOB_RUNNER_CRON_ENABLED is off)")
            return "disabled"
        budget = float(getattr(settings, 'JOB_RUNNER_CRON_BUDGET_SECONDS', 50) or 0) or None
        result = run_pass(budget_seconds=budget, worker_id="runcrons")
        counts = queue_counts()
        summary = "%s; queue now: %d queued (%d due), %d running, %d failed" % (
            result.summary(), counts["queued"], counts["due"], counts["running"], counts["failed"])
        logger.info("job queue cron: %s", summary)
        return summary
