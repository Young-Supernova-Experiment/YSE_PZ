"""``sharing.tns_retrieval``: match YSE-PZ transients to TNS objects (#327).

The ``TNS_updates`` / ``TNS_recent`` crons in ``YSE_App.data_ingest.TNS_uploads``
keep *importing new TNS objects* into YSE-PZ. This job does the other half:
for transients we already have that carry only an internal name (no
``20xxabc`` designation), it cone-searches TNS and records the designation the
way an accepted discovery report does (rename, keep the old name as an
alternate; or add an alternate name when the service says not to rename).
It also confirms the object name of accepted submissions whose transient
still lacks it. Nothing is merged: a designation that already belongs to
another transient is logged on the transient as a conflict and left alone.

Rate limits: one request per candidate, ``SHARING_TNS_RETRIEVAL_MAX_PER_RUN``
candidates per run, ``SHARING_TNS_REQUEST_INTERVAL_SECONDS`` between requests,
and a 429 stops the run and retries the job after TNS's reset time.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from YSE_App.jobs import JobRetry, job
from YSE_App.models.sharing_models import SharingService, SharingSubmission
from YSE_App.models.transient_models import Transient
from YSE_App.sharing import tns

log = logging.getLogger(__name__)

RETRIEVAL_KIND = "sharing.tns_retrieval"
TNS_REGEX = r"^20[0-9]{2}[a-zA-Z]{1,5}$"
DEFAULT_STATUSES = ("New", "Watch", "Following", "FollowupRequested", "Interesting")


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def retrieval_service(slug: str = "") -> Optional[SharingService]:
    """The TNS service to search with: ``slug``, else ``SHARING_TNS_RETRIEVAL_SERVICE``, else any enabled one."""
    slug = slug or _setting("SHARING_TNS_RETRIEVAL_SERVICE", "")
    qs = SharingService.objects.filter(kind=SharingService.KIND_TNS, enabled=True).select_related("credential")
    if slug:
        service = qs.filter(slug=slug).first()
        if service is not None and service.has_credential:
            return service
    for service in qs.order_by("testing", "slug"):
        if service.has_credential:
            return service
    return None


def candidates(since_days: float, statuses, limit: int) -> List[Transient]:
    """Transients without a TNS designation, newest first."""
    since = timezone.now() - timezone.timedelta(days=float(since_days))
    qs = (
        Transient.objects.filter(status__name__in=list(statuses))
        .filter(Q(disc_date__gte=since) | Q(disc_date__isnull=True, created_date__gte=since))
        .exclude(name__regex=TNS_REGEX)
        .exclude(alternatetransientnames__name__regex=TNS_REGEX)
        .select_related("status", "obs_group", "modified_by")
        .order_by("-created_date")
    )
    return list(qs[: int(limit)])


def match_transient(client: tns.TNSClient, transient: Transient, service: SharingService, radius_arcsec: float) -> dict:
    """Cone-search TNS for ``transient`` and record the first designation found."""
    hits = client.search(transient.ra, transient.dec, radius_arcsec)
    hits = [h for h in hits if (h.get("objname") or "").strip()]
    if not hits:
        return {"transient": transient.name, "matched": False}
    hit = hits[0]
    objname = str(hit["objname"]).strip()
    result = tns.record_tns_name(transient, objname, service=service, source="TNS retrieval")
    return {"transient": transient.name, "matched": True, "tns_name": objname,
            "prefix": hit.get("prefix", ""), "result": result}


def confirm_submissions(service: SharingService, limit: int = 100) -> int:
    """Accepted submissions whose transient still lacks the designation get it recorded."""
    fixed = 0
    qs = SharingSubmission.objects.filter(
        service=service, status=SharingSubmission.STATUS_ACCEPTED, kind=SharingSubmission.KIND_DISCOVERY,
    ).exclude(tns_name="").select_related("transient", "created_by").order_by("-finished_at")[:limit]
    for submission in qs:
        transient = submission.transient
        if tns.tns_name_for(transient):
            continue
        result = tns.record_tns_name(transient, submission.tns_name, service=service, user=submission.created_by,
                                     source="TNS")
        if result in ("renamed", "alternate"):
            fixed += 1
    return fixed


def run_retrieval(*, service: Optional[SharingService] = None, since_days=None, statuses=None,
                  radius_arcsec=None, limit=None, sleep_seconds=None) -> dict:
    """One retrieval pass; raises :class:`tns.TNSRateLimited` when TNS throttles us."""
    service = service or retrieval_service()
    if service is None:
        return {"skipped": "no enabled TNS sharing service with a credential"}
    since_days = float(since_days if since_days is not None else _setting("SHARING_TNS_RETRIEVAL_SINCE_DAYS", 30))
    statuses = statuses or _setting("SHARING_TNS_RETRIEVAL_STATUSES", DEFAULT_STATUSES)
    radius = float(radius_arcsec if radius_arcsec is not None else _setting("SHARING_TNS_RETRIEVAL_RADIUS_ARCSEC", 3.0))
    limit = int(limit if limit is not None else _setting("SHARING_TNS_RETRIEVAL_MAX_PER_RUN", 50))
    pause = float(sleep_seconds if sleep_seconds is not None else _setting("SHARING_TNS_REQUEST_INTERVAL_SECONDS", 1.0))
    client = tns.TNSClient.for_service(service)
    summary = {"service": service.slug, "checked": 0, "matched": 0, "renamed": 0, "alternate": 0,
               "conflicts": 0, "confirmed": confirm_submissions(service), "matches": []}
    for index, transient in enumerate(candidates(since_days, statuses, limit)):
        if index and pause:
            time.sleep(pause)
        result = match_transient(client, transient, service, radius)
        summary["checked"] += 1
        if result["matched"]:
            summary["matched"] += 1
            summary["matches"].append({k: result[k] for k in ("transient", "tns_name", "result")})
            key = {"renamed": "renamed", "alternate": "alternate", "conflict": "conflicts"}.get(result["result"])
            if key:
                summary[key] += 1
    return summary


@job(RETRIEVAL_KIND, max_attempts=3, backoff_seconds=600)
def tns_retrieval(payload, job=None):
    payload = payload or {}
    service = retrieval_service(payload.get("service", "")) if payload.get("service") else None
    try:
        return run_retrieval(
            service=service, since_days=payload.get("since_days"), statuses=payload.get("statuses"),
            radius_arcsec=payload.get("radius_arcsec"), limit=payload.get("limit"),
        )
    except tns.TNSRateLimited as exc:
        raise JobRetry(str(exc), delay=exc.retry_after)
    except tns.TNSTransportError as exc:
        raise JobRetry(str(exc))
