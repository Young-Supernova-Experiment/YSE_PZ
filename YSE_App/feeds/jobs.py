"""Job-queue handlers for the feeds (#280).

``feeds.poll`` runs one ``FeedSource`` through its provider (enqueued by the
``FeedPoll`` cron, the ``feeds`` management command, the /feeds/ page and the
API); ``feeds.screen_minor_planets`` screens one transient, or a sweep of the
recently created ones, against known minor planets (#283).
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from YSE_App.feeds import registry as feed_registry
from YSE_App.feeds.base import FeedError
from YSE_App.jobs import JobFailed, enqueue, job
from YSE_App.models.feed_models import FeedSource
from YSE_App.models.transient_models import Transient

logger = logging.getLogger(__name__)

POLL_KIND = "feeds.poll"
SCREEN_KIND = "feeds.screen_minor_planets"


def run_source(source: FeedSource, *, user=None, dry_run: bool = False) -> dict:
    """Poll ``source`` with its provider and record the outcome on the row."""
    provider = feed_registry.provider_for(source)
    if provider is None:
        source.record_poll("failed: no provider for kind %r" % source.kind, "no provider for kind %r" % source.kind)
        raise FeedError("no feed provider registered for kind %r" % source.kind)
    if not provider.available():
        reason = provider.unavailable_reason()
        source.record_poll("failed: %s" % reason, reason)
        raise FeedError("%s: %s" % (source.slug, reason))
    result = provider.run(source, user=user, dry_run=dry_run)
    if result.get("error"):
        raise FeedError(result["error"])  # the source already recorded it; the job is marked failed
    return result


@job(POLL_KIND, max_attempts=2, backoff_seconds=300)
def poll_job(payload, job=None):
    payload = payload or {}
    source = FeedSource.objects.filter(pk=payload.get("source_id")).select_related("credential", "created_by").first()
    if source is None:
        raise JobFailed("feed source %r not found" % payload.get("source_id"))
    if not source.enabled and not payload.get("force"):
        return {"source": source.slug, "skipped": "disabled"}
    user = User.objects.filter(pk=payload.get("user_id")).first() if payload.get("user_id") else None
    try:
        return run_source(source, user=user, dry_run=bool(payload.get("dry_run")))
    except FeedError as exc:
        raise JobFailed(str(exc))


def enqueue_poll(source: FeedSource, *, created_by=None, dry_run: bool = False, force: bool = False):
    return enqueue(POLL_KIND, {"source_id": source.pk, "dry_run": bool(dry_run), "force": bool(force),
                               "user_id": getattr(created_by, "pk", None)}, created_by=created_by)


def poll_active(source: FeedSource) -> bool:
    from YSE_App.models.job_models import Job

    for row in Job.objects.filter(kind=POLL_KIND, status__in=Job.ACTIVE_STATUSES).only("payload"):
        if (row.payload or {}).get("source_id") == source.pk:
            return True
    return False


# --- minor-planet screening (#283) --------------------------------------------------

def screen_since_days() -> float:
    return float(getattr(settings, "FEEDS_MPC_SCREEN_SINCE_DAYS", 3) or 3)


def screen_max_per_run() -> int:
    return int(getattr(settings, "FEEDS_MPC_SCREEN_MAX_PER_RUN", 50) or 50)


def unscreened_transients(since_days: Optional[float] = None, limit: Optional[int] = None):
    """Transients created in the window without a ``minor_planet`` annotation, oldest first."""
    from YSE_App.feeds.scout import ORIGIN
    from YSE_App.models.annotation_models import TransientAnnotation

    since = timezone.now() - datetime.timedelta(days=float(since_days if since_days is not None else screen_since_days()))
    screened = TransientAnnotation.objects.filter(origin=ORIGIN).values_list("transient_id", flat=True)
    qs = Transient.objects.filter(created_date__gte=since).exclude(pk__in=screened).order_by("created_date")
    return list(qs[: int(limit if limit is not None else screen_max_per_run())])


@job(SCREEN_KIND, max_attempts=3, backoff_seconds=600)
def screen_job(payload, job=None):
    from YSE_App.feeds.scout import screen_transient

    payload = payload or {}
    user = User.objects.filter(pk=payload.get("user_id")).first() if payload.get("user_id") else None
    if payload.get("transient_id"):
        transient = Transient.objects.filter(pk=payload["transient_id"]).first()
        if transient is None:
            raise JobFailed("transient %r not found" % payload["transient_id"])
        targets = [transient]
    else:
        targets = unscreened_transients(payload.get("since_days"), payload.get("limit"))
    result = {"screened": 0, "matches": 0, "errors": 0, "names": []}
    failures = []
    for transient in targets:
        try:
            document = screen_transient(transient, user=user, radius_arcsec=payload.get("radius_arcsec"))
        except FeedError as exc:
            result["errors"] += 1
            failures.append("%s: %s" % (transient.name, exc))
            logger.warning("minor-planet screening of %s failed: %s", transient.name, exc)
            continue
        result["screened"] += 1
        if document.get("possible_mpc"):
            result["matches"] += 1
            result["names"].append("%s=%s" % (transient.name, document["possible_mpc"]))
    result["failures"] = failures[:20]
    if failures and not result["screened"]:
        raise RuntimeError("; ".join(failures[:3]))  # everything failed: retry with backoff
    return result


def enqueue_screen(transient: Optional[Transient] = None, *, created_by=None, since_days=None, limit=None):
    payload = {"user_id": getattr(created_by, "pk", None)}
    if transient is not None:
        payload["transient_id"] = transient.pk
    else:
        payload.update({"since_days": since_days, "limit": limit})
    return enqueue(SCREEN_KIND, payload, created_by=created_by, transient=transient)


def screen_active() -> bool:
    from YSE_App.models.job_models import Job

    return Job.objects.filter(kind=SCREEN_KIND, status__in=Job.ACTIVE_STATUSES, transient__isnull=True).exists()


__all__ = ["POLL_KIND", "SCREEN_KIND", "enqueue_poll", "enqueue_screen", "poll_active", "run_source", "screen_active",
           "unscreened_transients"]
