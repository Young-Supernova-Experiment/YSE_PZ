"""django_cron class that queues the regular NED/DESI host-redshift sweep (#436, #437).

Registered in ``CRON_CLASSES``; a no-op until ``[redshifts] REDSHIFT_CATALOG_CRON_ENABLED``
(or ``YSE_REDSHIFT_CATALOG_CRON=1``) is set. Every ``REDSHIFT_CATALOG_CRON_MINUTES`` (60)
it queues one ``redshifts.sweep`` job, unless one is still queued or running; the job
runner (``RunQueuedJobs`` or ``run_jobs``) executes it, so no catalogue request runs
inside ``runcrons`` itself. The sweep is ``YSE_App.services.redshift_catalogs.sweep``.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.data_ingest.Sharing_Jobs import _queue_once

logger = logging.getLogger(__name__)


class RedshiftCatalogSweep(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'REDSHIFT_CATALOG_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.Redshift_Catalogs.RedshiftCatalogSweep'

    def do(self):
        if not getattr(settings, 'REDSHIFT_CATALOG_CRON_ENABLED', False):
            logger.info("redshift catalogue cron: disabled (REDSHIFT_CATALOG_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.redshift_catalogs import SWEEP_KIND

        queued = _queue_once(SWEEP_KIND, {})
        summary = "queued job %s" % queued.pk if queued is not None else "a sweep job is already queued or running"
        logger.info("redshift catalogue cron: %s", summary)
        return summary
