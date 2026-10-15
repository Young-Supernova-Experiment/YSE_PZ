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
from YSE_App.services.notify import prune

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


class PruneNotifications(CronJobBase):
    """Retention (#320): delete old notification rows and finished job rows once a day.

    Settings: ``NOTIFICATION_RETENTION_DAYS`` (read rows, 90),
    ``NOTIFICATION_UNREAD_RETENTION_DAYS`` (365), ``JOB_RETENTION_DAYS`` (30),
    ``NOTIFICATION_PRUNE_CRON_ENABLED`` / ``NOTIFICATION_PRUNE_CRON_MINUTES``.
    The same work is available as the ``notifications.prune`` job kind and as
    ``manage.py prune_notifications``.
    """

    RUN_EVERY_MINS = int(getattr(settings, 'NOTIFICATION_PRUNE_CRON_MINUTES', 1440) or 1440)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Job_Queue.PruneNotifications'

    def do(self):
        if not getattr(settings, 'NOTIFICATION_PRUNE_CRON_ENABLED', True):
            logger.info("notification prune cron: disabled (NOTIFICATION_PRUNE_CRON_ENABLED is off)")
            return "disabled"
        result = prune()
        summary = "pruned %d read + %d unread notifications, %d finished jobs" % (
            result["notifications_read"], result["notifications_unread"], result["jobs"])
        logger.info("notification prune cron: %s", summary)
        return summary
