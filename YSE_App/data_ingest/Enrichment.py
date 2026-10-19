"""django_cron class that queues the enrichment sweep (#439).

Registered in ``CRON_CLASSES``; a no-op until ``[enrichment] ENRICH_SWEEP_CRON_ENABLED`` (or
``YSE_ENRICH_SWEEP_CRON=1``) is set. Every ``ENRICH_SWEEP_CRON_MINUTES`` (60) it queues one
``enrich.sweep`` job, unless one is still queued or running; the job runner executes it. The
sweep runs the services named in ``ENRICH_SWEEP_SERVICES`` over their due objects
(``YSE_App.services.enrichment.sweep``).
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.data_ingest.Sharing_Jobs import _queue_once

logger = logging.getLogger(__name__)


class EnrichmentSweep(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'ENRICH_SWEEP_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.data_ingest.Enrichment.EnrichmentSweep'

    def do(self):
        if not getattr(settings, 'ENRICH_SWEEP_CRON_ENABLED', False):
            logger.info("enrichment cron: disabled (ENRICH_SWEEP_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.enrichment import SWEEP_KIND, sweep_services

        if not sweep_services():
            logger.info("enrichment cron: ENRICH_SWEEP_SERVICES is empty")
            return "no services"
        queued = _queue_once(SWEEP_KIND, {})
        summary = "queued job %s" % queued.pk if queued is not None else "a sweep job is already queued or running"
        logger.info("enrichment cron: %s", summary)
        return summary
