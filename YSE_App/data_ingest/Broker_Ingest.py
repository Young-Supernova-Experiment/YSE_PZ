"""
django_cron job: enqueue one ``brokers.ingest`` job per broker that has an
enabled ``BrokerFilter`` (issue #278, polling half; Kafka consumers are a
separate piece of work).

Off by default: ``BROKER_INGEST_CRON_ENABLED`` (``[brokers] INGEST_CRON_ENABLED``
in settings.ini) turns it on; ``BROKER_INGEST_CRON_MINUTES`` (60) is the
interval. The job itself runs through the background queue (#263), so this
cron returns at once and never holds the ``runcrons`` run. A broker with an
ingest job still queued or running is skipped.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.brokers import registry
from YSE_App.brokers.jobs import INGEST_KIND, enqueue_ingest
from YSE_App.models.candidate_models import BrokerFilter
from YSE_App.models.job_models import Job

logger = logging.getLogger(__name__)


def brokers_with_enabled_filters():
    slugs = set(BrokerFilter.objects.filter(enabled=True).values_list("broker", flat=True).distinct())
    return [s for s in registry.enabled_slugs() if s in slugs]


def ingest_job_active(broker):
    for job in Job.objects.filter(kind=INGEST_KIND, status__in=Job.ACTIVE_STATUSES).only("payload"):
        if (job.payload or {}).get("broker") == broker:
            return True
    return False


def enqueue_polls():
    """Enqueue a poll for every broker with enabled filters; returns the slugs enqueued."""
    enqueued = []
    for slug in brokers_with_enabled_filters():
        if ingest_job_active(slug):
            continue
        enqueue_ingest(slug)
        enqueued.append(slug)
    return enqueued


class BrokerPoll(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, "BROKER_INGEST_CRON_MINUTES", 60) or 60)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = "YSE_App.Broker_Ingest.BrokerPoll"

    def do(self):
        if not getattr(settings, "BROKER_INGEST_CRON_ENABLED", False):
            logger.info("broker poll cron: disabled (BROKER_INGEST_CRON_ENABLED is off)")
            return "disabled"
        enqueued = enqueue_polls()
        summary = "enqueued %d broker poll(s): %s" % (len(enqueued), ", ".join(enqueued) or "none")
        logger.info("broker poll cron: %s", summary)
        return summary
