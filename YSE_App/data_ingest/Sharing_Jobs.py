"""django_cron classes that queue the sharing jobs (#326, #327).

Both are registered in ``CRON_CLASSES`` and are no-ops until switched on in
``settings.ini`` (``[sharing]``): ``TNS_RETRIEVAL_CRON_ENABLED`` queues one
``sharing.tns_retrieval`` job every ``TNS_RETRIEVAL_CRON_MINUTES`` (60);
``AUTOPUBLISH_CRON_ENABLED`` queues one ``sharing.autopublish_sweep`` job every
``AUTOPUBLISH_CRON_MINUTES`` (60). The queue runner (``RunQueuedJobs`` or a
``run_jobs --loop`` worker) executes them, so the TNS calls never run inside
``manage.py runcrons`` itself. A job of the same kind that is still queued or
running is not duplicated. The ``TNS_updates`` / ``TNS_recent`` crons keep
importing new TNS objects; these only match and report existing transients.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.jobs import enqueue
from YSE_App.models.job_models import Job

logger = logging.getLogger(__name__)


def _queue_once(kind, payload, created_by=None):
    if Job.objects.filter(kind=kind, status__in=Job.ACTIVE_STATUSES).exists():
        return None
    return enqueue(kind, payload, created_by=created_by)


class TNSRetrieval(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'SHARING_TNS_RETRIEVAL_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Sharing_Jobs.TNSRetrieval'

    def do(self):
        if not getattr(settings, 'SHARING_TNS_RETRIEVAL_CRON_ENABLED', False):
            logger.info("tns retrieval cron: disabled (SHARING_TNS_RETRIEVAL_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.sharing.retrieval import RETRIEVAL_KIND

        job = _queue_once(RETRIEVAL_KIND, {})
        summary = "queued job %s" % job.pk if job is not None else "a retrieval job is already queued or running"
        logger.info("tns retrieval cron: %s", summary)
        return summary


class AutoPublishSweep(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'SHARING_AUTOPUBLISH_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Sharing_Jobs.AutoPublishSweep'

    def do(self):
        if not getattr(settings, 'SHARING_AUTOPUBLISH_CRON_ENABLED', False):
            logger.info("auto-publish cron: disabled (SHARING_AUTOPUBLISH_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.sharing.autopublish import SWEEP_KIND

        job = _queue_once(SWEEP_KIND, {})
        summary = "queued job %s" % job.pk if job is not None else "a sweep job is already queued or running"
        logger.info("auto-publish cron: %s", summary)
        return summary
