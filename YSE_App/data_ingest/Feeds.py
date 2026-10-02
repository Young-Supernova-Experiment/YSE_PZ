"""django_cron classes that queue the feed jobs (#280).

``FeedPoll`` enqueues one ``feeds.poll`` job per enabled ``FeedSource`` (a
source with a poll still queued or running is skipped); ``MinorPlanetScreen``
enqueues one ``feeds.screen_minor_planets`` sweep (#283). Both are no-ops until
switched on under ``[feeds]`` in settings.ini (``POLL_CRON_ENABLED``,
``MPC_SCREEN_CRON_ENABLED``); the jobs run through the background queue, so the
network calls never happen inside ``manage.py runcrons``.
"""

import logging

from django.conf import settings
from django_cron import CronJobBase, Schedule

from YSE_App.feeds.jobs import enqueue_poll, enqueue_screen, poll_active, screen_active
from YSE_App.models.feed_models import FeedSource

logger = logging.getLogger(__name__)


def enqueue_polls():
    """Enqueue a poll for every enabled source without an active poll job; returns the slugs."""
    enqueued = []
    for source in FeedSource.objects.filter(enabled=True).order_by("kind", "name"):
        if poll_active(source):
            continue
        enqueue_poll(source, created_by=source.created_by)
        enqueued.append(source.slug)
    return enqueued


class FeedPoll(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, "FEEDS_POLL_CRON_MINUTES", 15) or 15)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = "YSE_App.Feeds.FeedPoll"

    def do(self):
        if not getattr(settings, "FEEDS_POLL_CRON_ENABLED", False):
            logger.info("feed poll cron: disabled (FEEDS_POLL_CRON_ENABLED is off)")
            return "disabled"
        enqueued = enqueue_polls()
        summary = "enqueued %d feed poll(s): %s" % (len(enqueued), ", ".join(enqueued) or "none")
        logger.info("feed poll cron: %s", summary)
        return summary


class MinorPlanetScreen(CronJobBase):
    RUN_EVERY_MINS = int(getattr(settings, "FEEDS_MPC_SCREEN_CRON_MINUTES", 360) or 360)

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = "YSE_App.Feeds.MinorPlanetScreen"

    def do(self):
        if not getattr(settings, "FEEDS_MPC_SCREEN_CRON_ENABLED", False):
            logger.info("minor-planet screen cron: disabled (FEEDS_MPC_SCREEN_CRON_ENABLED is off)")
            return "disabled"
        if screen_active():
            return "a screening sweep is already queued or running"
        job = enqueue_screen()
        summary = "queued job %s" % job.pk
        logger.info("minor-planet screen cron: %s", summary)
        return summary
