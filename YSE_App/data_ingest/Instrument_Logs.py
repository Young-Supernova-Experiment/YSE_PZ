"""django_cron classes for instrument logs and weather (#309: #310, #311).

Both are registered in ``CRON_CLASSES`` and are no-ops until switched on under
``[observatory]`` in ``settings.ini``: ``INSTRUMENT_LOG_PULL_CRON_ENABLED``
queues one ``instrument_logs.pull`` job (the last ``INSTRUMENT_LOG_PULL_HOURS``
for every instrument with a facility allocation that publishes logs) every
``INSTRUMENT_LOG_PULL_CRON_MINUTES``; ``WEATHER_REFRESH_CRON_ENABLED`` queues one
``weather.refresh`` job every ``WEATHER_REFRESH_CRON_MINUTES``. The queue runner
(``RunQueuedJobs`` or a ``run_jobs --loop`` worker) executes them, so no HTTP
call runs inside ``manage.py runcrons`` itself. A job of the same kind that is
still queued or running is not duplicated.
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


class InstrumentLogPull(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'INSTRUMENT_LOG_PULL_CRON_MINUTES', 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Instrument_Logs.InstrumentLogPull'

    def do(self):
        if not getattr(settings, 'INSTRUMENT_LOG_PULL_CRON_ENABLED', False):
            logger.info("instrument log pull cron: disabled (INSTRUMENT_LOG_PULL_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.instrument_logs import PULL_JOB_KIND

        hours = int(getattr(settings, 'INSTRUMENT_LOG_PULL_HOURS', 24) or 24)
        job = _queue_once(PULL_JOB_KIND, {"hours": hours})
        summary = "queued job %s" % job.pk if job is not None else "a pull job is already queued or running"
        logger.info("instrument log pull cron: %s", summary)
        return summary


class WeatherRefresh(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'WEATHER_REFRESH_CRON_MINUTES', 10) or 10)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Instrument_Logs.WeatherRefresh'

    def do(self):
        if not getattr(settings, 'WEATHER_REFRESH_CRON_ENABLED', False):
            logger.info("weather refresh cron: disabled (WEATHER_REFRESH_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.weather import REFRESH_JOB_KIND

        job = _queue_once(REFRESH_JOB_KIND, {})
        summary = "queued job %s" % job.pk if job is not None else "a refresh job is already queued or running"
        logger.info("weather refresh cron: %s", summary)
        return summary
