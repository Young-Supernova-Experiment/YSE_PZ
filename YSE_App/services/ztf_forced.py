"""ZTF forced-photometry requests, shared by the transient page button and the follow-up enrichment (#114).

:func:`submit_forced_phot` is what the ``ztf_forced_phot`` view runs: a ``ztf`` allocation open to the
user first (``facility_requests.submit_request``), then the legacy mail-based script with its 12 h
throttle. Every accepted submission is also recorded as a run of the ``ztf_forced`` external service,
and :func:`last_queried` answers "when was the ZTF server last asked about this object" from those runs
plus the older records (the forced-phot comment, and requests on a ``ztf`` allocation), so the two
callers share one freshness check.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

import pytz
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from YSE_App.models import Log
from YSE_App.models.allocation_models import FacilityRequest
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun

logger = logging.getLogger(__name__)

SERVICE_SLUG = "ztf_forced"
ZTF_FORCED_PHOT_LOG_PREFIX = "ZTF Forced Phot"
LEGACY_THROTTLE_HOURS = 12


@dataclass
class Outcome:
    """``kind``: submitted | refused (already open or throttled) | failed (nothing was submitted)."""

    kind: str
    message: str
    http_status: int = 200
    via: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "submitted"


def ztf_service(user: User) -> ExternalService:
    service = ExternalService.objects.filter(slug=SERVICE_SLUG).first()
    if service is None:
        service, _ = ExternalService.objects.get_or_create(
            slug=SERVICE_SLUG,
            defaults={
                "name": "ZTF forced photometry", "kind": ExternalService.KIND_FACILITY,
                "description": "One run per ZTF forced-photometry submission (page button or automatic "
                               "follow-up enrichment, #114); used to avoid asking ZTF again too soon.",
                "created_by": user, "modified_by": user,
            },
        )
    return service


def record_query(transient, user: User, *, via: str, message: str = "") -> ExternalServiceRun:
    now = timezone.now()
    return ExternalServiceRun.objects.create(
        service=ztf_service(user), transient=transient, target_ref=transient.name,
        request_payload={"via": via}, status=ExternalServiceRun.STATUS_SUCCEEDED,
        started_at=now, finished_at=now, result={"message": message[:500]},
        attempts=1, created_by=user, modified_by=user)


def last_queried(transient) -> Optional[datetime.datetime]:
    """Latest time ZTF was asked for this object's forced photometry, from any of the records."""
    times = []
    run = (ExternalServiceRun.objects.filter(service__slug=SERVICE_SLUG, transient=transient,
                                             status=ExternalServiceRun.STATUS_SUCCEEDED)
           .order_by("-finished_at", "-pk").first())
    if run is not None and run.finished_at:
        times.append(run.finished_at)
    log = (Log.objects.filter(transient=transient, comment__startswith=ZTF_FORCED_PHOT_LOG_PREFIX)
           .order_by("-modified_date").first())
    if log is not None:
        times.append(log.modified_date)
    req = (FacilityRequest.objects.filter(transient=transient, allocation__facility="ztf")
           .exclude(state__in=(FacilityRequest.STATE_DRAFT, FacilityRequest.STATE_FAILED))
           .order_by("-created_date").first())
    if req is not None:
        times.append(req.submitted_at or req.created_date)
    return max(times) if times else None


def queried_within(transient, hours: float, now=None) -> bool:
    last = last_queried(transient)
    now = now or timezone.now()
    return last is not None and last > now - datetime.timedelta(hours=hours)


def _via_allocation(transient, user: User) -> Optional[Outcome]:
    """Submit through the first usable 'ztf' allocation; None when there is none (legacy path)."""
    from YSE_App.facilities import FacilityValidationError
    from YSE_App.services import facility_requests as fr

    allocation = fr.allocations_for_user(user).filter(facility="ztf").order_by("-end_date").first()
    if allocation is None:
        return None
    existing = fr.open_request_for(allocation, transient)
    if existing is not None:
        return Outcome("refused", "error: a ZTF forced-photometry request is already %s (submitted %s)" % (
            existing.get_state_display().lower(),
            (existing.submitted_at or existing.created_date).strftime("%Y-%m-%d %H:%M UT")), via="allocation")
    try:
        req = fr.submit_request(allocation, transient, user, {})
    except (FacilityValidationError, fr.FacilityRequestError) as exc:
        return Outcome("failed", "error: %s" % exc, via="allocation")
    return Outcome(
        "submitted", "success: ZTF forced photometry request %d is %s; the light curve is ingested when the "
        "service finishes" % (req.pk, req.get_state_display().lower()), via="allocation")


def submit_forced_phot(transient, user: User) -> Outcome:
    """Request forced photometry: allocation path first, then the legacy script. Never raises."""
    outcome = _via_allocation(transient, user)
    if outcome is not None:
        if outcome.ok:
            Log.objects.create(
                created_by=user, modified_by=user, transient=transient,
                comment="%s requested by %s via the facility queue: %s" % (
                    ZTF_FORCED_PHOT_LOG_PREFIX, user.username, outcome.message))
            record_query(transient, user, via="allocation", message=outcome.message)
        return outcome

    # Not run within the last 12 hours. Only earlier forced-phot comments count (#399).
    from YSE_App.data_ingest import ZTF_Forced_Phot

    now = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
    old_log = Log.objects.filter(
        transient=transient, comment__startswith=ZTF_FORCED_PHOT_LOG_PREFIX,
        modified_date__gt=now - datetime.timedelta(hours=LEGACY_THROTTLE_HOURS)).order_by("-modified_date").first()
    if old_log is not None:
        hours_ago = (now - old_log.modified_date).total_seconds() / 3600.
        return Outcome("refused", "error: forced phot was requested %.1f hours ago (must be >12)" % hours_ago,
                       via="legacy")

    try:
        ztf = ZTF_Forced_Phot.ZTF_Forced_Phot(
            ztf_email_address=ZTF_Forced_Phot.ztf_account_address(), ztf_email_password=settings.SMTP_PASSWORD,
            ztf_email_imapserver="imap.gmail.com", ztf_user_address=ZTF_Forced_Phot.ztf_account_address(),
            ztf_user_password=settings.ZTFPASS)
        log_file_name = ztf.run_ztf_fp(
            all_jd=False, days=60, decl=transient.dec, directory_path=settings.ZTFTMPDIR,
            do_plot=False, emailcheck=0, fivemindelay=60, jdend=None,
            jdstart=None, logfile=None, mjdend=None, mjdstart=None,
            plotfile=None, ra=transient.ra, skip_clean=False, source_name=transient.slug,
            verbose=False)
    except Exception as exc:  # noqa: BLE001 - network, IMAP or config failure inside the legacy script
        logger.exception("ZTF forced phot request failed for %s", transient.slug)
        # A submit error (ZTF's reply text, credentials scrubbed) or a filesystem error names what to
        # fix; anything else shows only its type (#411).
        if isinstance(exc, (ZTF_Forced_Phot.ZTFForcedPhotSubmitError, OSError)):
            detail = "%s: %s" % (type(exc).__name__, exc)
        else:
            detail = type(exc).__name__
        return Outcome("failed", "error: ZTF forced photometry request failed (%s); nothing was submitted" % detail,
                       http_status=502, via="legacy")
    if not log_file_name:
        return Outcome("failed", "error: ZTF did not accept the forced photometry request (no job log returned); "
                                 "nothing was submitted", http_status=502, via="legacy")

    Log.objects.create(
        created_by=user, modified_by=user, transient=transient,
        comment="""%s requested by %s
log_file_name=%s
job_submitted=%s""" % (ZTF_FORCED_PHOT_LOG_PREFIX, user.username, log_file_name, now.isoformat()))
    record_query(transient, user, via="legacy", message=str(log_file_name))
    return Outcome("submitted", "success: ZTF forced photometry requested; a comment was added to this transient",
                   via="legacy")
