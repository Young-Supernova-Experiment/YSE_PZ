"""django_cron class that queues the Selection alert pass (#844).

Registered in ``CRON_CLASSES``. Every ``SELECTION_ALERTS_CRON_MINUTES`` (15) it enqueues one
``selection_alerts.run`` job when ``SELECTION_ALERTS_ENABLED`` is on (default off), unless one is
already queued or running. The queue runner (``RunQueuedJobs`` or ``run_jobs --loop``) executes
the pass (``services/selection_alerts.py``), so no Selection is evaluated inside
``manage.py runcrons`` itself.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

logger = logging.getLogger(__name__)


class SelectionAlerts(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'SELECTION_ALERTS_CRON_MINUTES', 15) or 15)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Selection_Alerts.SelectionAlerts'

    def do(self):
        from YSE_App.services.photstat_jobs import queue_job
        from YSE_App.services.selection_alerts import RUN_KIND, alerts_enabled

        if not alerts_enabled():
            summary = '%s disabled' % RUN_KIND
        else:
            queued = queue_job(RUN_KIND)
            summary = '%s %s' % (RUN_KIND, 'queued job %s' % queued.pk if queued else 'already queued or running')
        logger.info('selection alerts cron: %s', summary)
        return summary
