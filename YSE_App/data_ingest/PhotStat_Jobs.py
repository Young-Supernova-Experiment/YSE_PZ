"""django_cron class that queues the photometry-statistics upkeep jobs (#269, #271).

Registered in ``CRON_CLASSES``. Every ``PHOTSTAT_REFRESH_CRON_MINUTES`` (60)
it enqueues one ``photstat.refresh_stale`` job (missing or outdated
``TransientPhotStat`` rows, at most ``PHOTSTAT_REFRESH_LIMIT`` per pass) when
``PHOTSTAT_REFRESH_CRON_ENABLED`` is on (default on), and one
``photstat.bazin_extrapolate`` job (Bazin magnitude for transients with an
open follow-up) when ``PHOTSTAT_BAZIN_CRON_ENABLED`` is on (default off).
The queue runner (``RunQueuedJobs`` or ``run_jobs --loop``) executes them,
so no recompute or fit runs inside ``manage.py runcrons`` itself.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

logger = logging.getLogger(__name__)


class PhotStatRefresh(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'PHOTSTAT_REFRESH_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.PhotStat_Jobs.PhotStatRefresh'

    def do(self):
        from YSE_App.services.photstat_jobs import BAZIN_KIND, REFRESH_STALE_KIND, queue_job

        parts = []
        for flag, default, kind in (
            ('PHOTSTAT_REFRESH_CRON_ENABLED', True, REFRESH_STALE_KIND),
            ('PHOTSTAT_BAZIN_CRON_ENABLED', False, BAZIN_KIND),
        ):
            if not getattr(settings, flag, default):
                parts.append('%s disabled' % kind)
                continue
            queued = queue_job(kind)
            parts.append('%s %s' % (kind, 'queued job %s' % queued.pk if queued else 'already queued or running'))
        summary = '; '.join(parts)
        logger.info('photstat cron: %s', summary)
        return summary
