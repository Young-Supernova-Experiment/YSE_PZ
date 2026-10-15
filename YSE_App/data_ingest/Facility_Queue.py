"""django_cron class for the facility queue (#300): poll open facility requests.

Registered in ``CRON_CLASSES``; a no-op until ``FACILITY_POLL_CRON_ENABLED`` is
switched on under ``[site_settings]`` in ``settings.ini``. Every
``FACILITY_POLL_CRON_MINUTES`` (default 10) it queues one ``facility.poll`` job,
which the queue runner executes (LCO / SOAR request groups, ZTF and ATLAS
forced-photometry tasks and GENERIC allocations with a ``status_url``); a poll
job still queued or running is not duplicated. See docs/facility-apis.md.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.jobs import enqueue
from YSE_App.models.job_models import Job

logger = logging.getLogger(__name__)


class FacilityPoll(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, 'FACILITY_POLL_CRON_MINUTES', 10) or 10)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'YSE_App.Facility_Queue.FacilityPoll'

    def do(self):
        if not getattr(settings, 'FACILITY_POLL_CRON_ENABLED', False):
            logger.info("facility poll cron: disabled (FACILITY_POLL_CRON_ENABLED is off)")
            return "disabled"
        from YSE_App.services.facility_requests import POLL_JOB_KIND

        if Job.objects.filter(kind=POLL_JOB_KIND, status__in=Job.ACTIVE_STATUSES).exists():
            summary = "a poll job is already queued or running"
        else:
            job = enqueue(POLL_JOB_KIND, {"limit": int(getattr(settings, 'FACILITY_POLL_LIMIT', 200) or 200)})
            summary = "queued job %s" % job.pk
        logger.info("facility poll cron: %s", summary)
        return summary
