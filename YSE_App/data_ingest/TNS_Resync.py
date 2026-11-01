"""django_cron class that queues the regular TNS re-sync (#438).

Registered in ``CRON_CLASSES``; a no-op until ``[sharing] TNS_RESYNC_CRON_ENABLED`` (or
``YSE_TNS_RESYNC_CRON=1``) is set. Every ``TNS_RESYNC_CRON_MINUTES`` (60) it queues one
``tns.resync_sweep`` job, unless one is still queued or running; the job runner
(``RunQueuedJobs`` or ``run_jobs``) executes it, so no TNS request runs inside
``runcrons`` itself. The sweep is ``YSE_App.services.tns_resync.sweep``.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.data_ingest.Sharing_Jobs import _queue_once

logger = logging.getLogger(__name__)


class TNSResync(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'TNS_RESYNC_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.TNS_Resync.TNSResync'

    def do(self):
        if not getattr(settings, 'TNS_RESYNC_CRON_ENABLED', False):
            logger.info("tns re-sync cron: disabled (TNS_RESYNC_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.tns_resync import SWEEP_KIND

        queued = _queue_once(SWEEP_KIND, {})
        summary = "queued job %s" % queued.pk if queued is not None else "a re-sync job is already queued or running"
        logger.info("tns re-sync cron: %s", summary)
        return summary
