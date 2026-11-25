"""django_cron class that queues the scheduled SALT / Bazin refit pass (#846).

Registered in ``CRON_CLASSES``. Every ``FIT_REFIT_CRON_MINUTES`` (1440, daily) it enqueues one
``fits.refit_pass`` job when ``FIT_REFIT_ENABLED`` is on (default off), unless one is already
queued or running. The queue runner (``RunQueuedJobs`` or ``run_jobs --loop``) executes the
pass, which starts the refit runs on the same queue (``services/fit_tables.py``), so no fit
runs inside ``manage.py runcrons`` itself.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

logger = logging.getLogger(__name__)


class FitRefit(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'FIT_REFIT_CRON_MINUTES', 1440) or 1440)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Fit_Refit.FitRefit'

    def do(self):
        from YSE_App.services.fit_tables import REFIT_KIND, refit_enabled
        from YSE_App.services.photstat_jobs import queue_job

        if not refit_enabled():
            summary = '%s disabled' % REFIT_KIND
        else:
            queued = queue_job(REFIT_KIND)
            summary = '%s %s' % (REFIT_KIND, 'queued job %s' % queued.pk if queued else 'already queued or running')
        logger.info('fit refit cron: %s', summary)
        return summary
