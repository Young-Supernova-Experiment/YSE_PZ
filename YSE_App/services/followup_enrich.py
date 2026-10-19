"""Automatic TNS and ZTF refresh when a classical-night follow-up is requested (#114).

A new ``TransientFollowup`` on a classical night enqueues one ``followup.enrich`` job after the
transaction commits (``schedule_enrichment``, called from the same post_save as the #69 notices).
The job, which the job runner executes, then for the object:

* TNS: ``services.tns_resync.resync_object`` (#438), unless the ``tns`` service ran for it in the
  last ``FOLLOWUP_ENRICH_WINDOW_HOURS`` (24);
* ZTF: ``services.ztf_forced.submit_forced_phot`` (the page button's function: allocation first, then
  the legacy script), unless ZTF was asked about it in that window.

On by default (Ryan, 2026-10-18); ``FOLLOWUP_AUTO_ENRICH = False`` turns it off. ToO and queued
follow-ups are untouched. TNS rate limits and transport errors, and a failed ZTF submission, retry
through the job queue; nothing here ever runs in the request.

Not done: a Prost host match. It exists only inside the TNS upload cron, not as a function that can be
called for one object, so objects without a host still get none.
"""

from __future__ import annotations

import datetime
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from YSE_App.jobs import JobRetry, enqueue, job

logger = logging.getLogger(__name__)

KIND = "followup.enrich"


def enabled() -> bool:
    return bool(getattr(settings, "FOLLOWUP_AUTO_ENRICH", True))


def window_hours() -> float:
    try:
        return max(0.0, float(getattr(settings, "FOLLOWUP_ENRICH_WINDOW_HOURS", 24)))
    except (TypeError, ValueError):
        return 24.0


def is_classical_night(followup) -> bool:
    return getattr(followup, "classical_resource_id", None) is not None


def schedule_enrichment(followup):
    """Enqueue ``followup.enrich`` once the surrounding transaction commits; returns True if scheduled."""
    if not enabled() or not is_classical_night(followup):
        return False
    payload = {"followup_id": followup.pk, "transient_id": followup.transient_id}
    created_by = followup.created_by if getattr(followup, "created_by_id", None) else None
    transient_id = followup.transient_id

    def _enqueue():
        try:
            enqueue(KIND, payload, created_by=created_by, transient=_transient(transient_id))
        except Exception:  # noqa: BLE001 - the follow-up itself is saved; enrichment is best effort
            logger.exception("could not enqueue %s for follow-up %s", KIND, payload["followup_id"])

    transaction.on_commit(_enqueue)
    return True


def schedule_enrichment_safely(followup):
    try:
        return schedule_enrichment(followup)
    except Exception:  # noqa: BLE001 - must never break saving the follow-up
        logger.exception("follow-up enrichment for TransientFollowup %s failed to schedule", getattr(followup, "pk", "?"))
        return False


def _transient(pk):
    from YSE_App.models import Transient

    return Transient.objects.filter(pk=pk).first()


def _tns_synced_within(transient, hours: float) -> bool:
    from YSE_App.models.external_service_models import ExternalServiceRun
    from YSE_App.services import tns_resync

    last = (ExternalServiceRun.objects
            .filter(service__slug=tns_resync.SERVICE_SLUG, transient=transient,
                    status=ExternalServiceRun.STATUS_SUCCEEDED)
            .order_by("-finished_at", "-pk").values_list("finished_at", flat=True).first())
    return last is not None and last > timezone.now() - datetime.timedelta(hours=hours)


def _ztf_user(followup) -> User:
    """The requester (their ZTF allocation access applies), else the service account."""
    user = getattr(followup, "created_by", None)
    if user is not None and user.pk and user.is_active:
        return user
    from YSE_App.services import tns_resync

    return tns_resync.service_user()


def enrich(transient, followup=None, *, hours=None) -> dict:
    """Run the TNS and ZTF steps for one object; returns what each step did. Raises JobRetry to retry."""
    from YSE_App.sharing import tns
    from YSE_App.services import tns_resync, ztf_forced

    hours = window_hours() if hours is None else hours
    report = {"transient": transient.name}
    retry = None

    # TNS
    if _tns_synced_within(transient, hours):
        report["tns"] = "skipped: synced in the last %g h" % hours
    else:
        try:
            result = tns_resync.resync_object(transient, dry_run=False, import_missing=False)
            report["tns"] = result.summary()
        except tns.TNSRateLimited as exc:
            report["tns"] = "rate limited"
            retry = JobRetry(str(exc), delay=exc.retry_after)
        except tns.TNSTransportError as exc:
            report["tns"] = "transport error"
            retry = JobRetry(str(exc))
        except tns_resync.ResyncError as exc:
            report["tns"] = "not run: %s" % exc  # configuration, not worth retrying
            logger.warning("follow-up TNS refresh for %s not run: %s", transient.name, exc)

    # ZTF
    if ztf_forced.queried_within(transient, hours):
        report["ztf"] = "skipped: queried in the last %g h" % hours
    else:
        try:
            outcome = ztf_forced.submit_forced_phot(transient, _ztf_user(followup))
        except Exception as exc:  # noqa: BLE001 - e.g. no service account
            report["ztf"] = "not run: %s" % type(exc).__name__
            logger.exception("follow-up ZTF request for %s failed", transient.name)
            retry = retry or JobRetry("ZTF request error: %s" % type(exc).__name__)
        else:
            report["ztf"] = "%s (%s): %s" % (outcome.kind, outcome.via, outcome.message)
            if outcome.kind == "failed":
                retry = retry or JobRetry(outcome.message)
    if retry is not None:
        # a retry redoes only what is still due: finished steps are inside the window by then
        logger.info("follow-up enrichment for %s will retry: %s", transient.name, report)
        raise retry
    return report


@job(KIND, max_attempts=3, backoff_seconds=900)
def enrich_followup(payload=None, job=None):
    from YSE_App.models import Transient, TransientFollowup

    payload = payload or {}
    if not enabled():
        return {"skipped": "FOLLOWUP_AUTO_ENRICH is off"}
    transient = Transient.objects.filter(pk=payload.get("transient_id")).first()
    if transient is None:
        return {"skipped": "transient not found"}
    followup = TransientFollowup.objects.filter(pk=payload.get("followup_id")).select_related("created_by").first()
    return enrich(transient, followup)
