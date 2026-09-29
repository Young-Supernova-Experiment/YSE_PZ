"""Submit, execute, poll and cancel facility requests (#299, #300).

``submit_request()`` validates the parameters with the allocation's facility
adapter, creates a :class:`FacilityRequest`, links a ``TransientFollowup`` when
the ``Requested`` status exists, starts an :class:`ExternalServiceRun` on the
allocation's service and dispatches it to the job queue. The run's handler
(``execute_run`` in :mod:`YSE_App.services.external_services`) finds the
runner registered here for facility services and calls the adapter's
``submit``; the outcome lands on the request (state, external id, log) and on
the run (result / error). ``poll_request`` / ``cancel_request`` /
``mark_request`` advance the state afterwards; reaching ``complete`` charges
``hours_charged`` to the allocation once.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from YSE_App.facilities import FacilityError, FacilityValidationError, get_facility
from YSE_App.jobs import job
from YSE_App.models.allocation_models import Allocation, FacilityRequest
from YSE_App.models.enum_models import FollowupStatus
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.transient_models import Transient
from YSE_App.services import external_services as runs
from YSE_App.services.allocations import allocations_for_user, charge_request, ensure_service, refund_request

log = logging.getLogger(__name__)

POLL_JOB_KIND = "facility.poll"

#: FacilityRequest state -> FollowupStatus name (applied only when that status row exists)
FOLLOWUP_STATUS_FOR_STATE = {
    FacilityRequest.STATE_SUBMITTED: "Requested",
    FacilityRequest.STATE_ACCEPTED: "Requested",
    FacilityRequest.STATE_RUNNING: "InProcess",
    FacilityRequest.STATE_COMPLETE: "Successful",
    FacilityRequest.STATE_FAILED: "Failed",
    FacilityRequest.STATE_CANCELLED: "Failed",
}


class FacilityRequestError(Exception):
    """A request cannot be created or changed (permissions, state, adapter)."""


# --- creation -----------------------------------------------------------------

def _window(params: Dict, now=None):
    now = now or timezone.now()
    start = _parse_dt(params.get("start")) or now
    stop = _parse_dt(params.get("end")) or (start + datetime.timedelta(days=7))
    if stop <= start:
        stop = start + datetime.timedelta(days=1)
    return start, stop


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace("Z", ""))
        except ValueError:
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


def _attach_followup(user: User, transient: Transient, params: Dict):
    """A ``TransientFollowup`` (status Requested) so the request shows in the follow-up boxes; None if the status is absent."""
    status = FollowupStatus.objects.filter(name="Requested").first()
    if status is None:
        return None
    from YSE_App.services.followup_requests import create_or_attach_request

    start, stop = _window(params)
    try:
        followup, _child, _created = create_or_attach_request(
            user, transient, status=status, valid_start=start, valid_stop=stop,
            comment=str(params.get("comment") or "")[:500],
        )
    except Exception:  # noqa: BLE001 - the facility request must not fail because of the follow-up bookkeeping
        log.exception("could not attach a follow-up for %s", transient.name)
        return None
    return followup


def submit_request(allocation: Allocation, transient: Transient, user: User, params: Optional[Dict] = None,
                   *, attach_followup: bool = True, dispatch: bool = True) -> FacilityRequest:
    """Validate, record and queue one request. Raises :class:`FacilityValidationError` or :class:`FacilityRequestError`."""
    if not allocation.usable_by(user):
        raise FacilityRequestError("you may not submit requests against %s" % allocation.name)
    facility = get_facility(allocation.facility)
    if facility is None:
        raise FacilityRequestError("allocation %s has no facility API bound" % allocation.name)
    if not facility.can("submit"):
        raise FacilityRequestError("facility %s cannot submit requests" % facility.slug)
    cleaned = facility.validate(params or {}, allocation)
    hours = facility.estimate_hours(cleaned, allocation)
    if allocation.hours_allocated and hours > allocation.hours_remaining + 1e-9:
        raise FacilityValidationError({"exposure_time": "this request needs %.2f h but only %.2f h remain on %s"
                                       % (hours, allocation.hours_remaining, allocation.name)})
    service = ensure_service(allocation, user)
    with transaction.atomic():
        followup = _attach_followup(user, transient, cleaned) if attach_followup else None
        request = FacilityRequest.objects.create(
            allocation=allocation, transient=transient, followup=followup, payload=cleaned,
            submitted_by=user, hours_charged=hours, created_by=user, modified_by=user,
        )
        request.add_log("created", "by %s via %s" % (user.username, facility.slug))
        try:
            run, _token = runs.start_run(service, user, {"facility_request_id": request.pk, "parameters": cleaned},
                                         transient=transient, target=request, dispatch=False)
        except runs.ExternalServiceError as exc:
            raise FacilityRequestError(str(exc))
        request.run = run
        request.state = FacilityRequest.STATE_QUEUED
        request.state_detail = "waiting for the job runner"
        request.add_log("queued", "run %s" % run.uuid)
        request.save(update_fields=["run", "state", "state_detail", "log", "modified_date"])
    if dispatch:
        if not runs.dispatch_run(run):
            request.set_state(FacilityRequest.STATE_FAILED, "could not enqueue the submission job")
    request.refresh_from_db()
    return request


# --- execution (job runner) ---------------------------------------------------

def _request_for_run(run: ExternalServiceRun) -> Optional[FacilityRequest]:
    request = getattr(run, "facility_request", None)
    if request is not None:
        return request
    request_id = (run.request_payload or {}).get("facility_request_id")
    if request_id:
        return FacilityRequest.objects.filter(pk=request_id).select_related("allocation", "transient").first()
    return None


def run_facility_submission(run: ExternalServiceRun) -> Dict:
    """Runner for facility services: send the request and record what happened. Never raises for adapter errors."""
    request = _request_for_run(run)
    if request is None:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="no facility request for this run")
        return {"handled": True, "ok": False}
    if request.state not in (FacilityRequest.STATE_QUEUED, FacilityRequest.STATE_DRAFT):
        return {"handled": True, "ok": True, "skipped": request.state}
    facility = get_facility(request.allocation.facility)
    run.mark_running()
    if facility is None:
        _fail(request, run, "facility %r is not registered" % request.allocation.facility)
        return {"handled": True, "ok": False}
    try:
        result = facility.submit(request)
    except FacilityError as exc:
        _fail(request, run, str(exc))
        return {"handled": True, "ok": False}
    except Exception as exc:  # noqa: BLE001 - recorded on the request, not retried (no double submission)
        log.exception("facility %s submit failed for request %s", facility.slug, request.pk)
        _fail(request, run, "%s: %s" % (type(exc).__name__, exc))
        return {"handled": True, "ok": False}
    if result.hours is not None:
        request.hours_charged = result.hours
    request.set_state(result.state or FacilityRequest.STATE_SUBMITTED, result.detail,
                      external_id=result.external_id, external_url=result.external_url, save=False)
    if not request.submitted_at:
        request.submitted_at = timezone.now()
    request.save(update_fields=["state", "state_detail", "external_id", "external_url", "submitted_at",
                                "hours_charged", "log", "modified_date"])
    runs.record_completion(run, ExternalServiceRun.STATUS_SUCCEEDED, result=result.as_dict(),
                           external_id=result.external_id)
    _after_state_change(request)
    return {"handled": True, "ok": True, "state": request.state, "external_id": request.external_id}


def _fail(request: FacilityRequest, run: ExternalServiceRun, error: str) -> None:
    request.set_state(FacilityRequest.STATE_FAILED, error)
    if not run.is_finished:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
    _after_state_change(request)


def _after_state_change(request: FacilityRequest) -> None:
    """Follow-up status, hours accounting and a notification to the requester."""
    if request.state == FacilityRequest.STATE_COMPLETE:
        charge_request(request)
    elif request.state == FacilityRequest.STATE_CANCELLED:
        refund_request(request)
    name = FOLLOWUP_STATUS_FOR_STATE.get(request.state)
    if name and request.followup_id:
        status = FollowupStatus.objects.filter(name=name).first()
        if status is not None and request.followup.status_id != status.pk:
            request.followup.status = status
            request.followup.save(update_fields=["status", "modified_date"])
    if request.state in (FacilityRequest.STATE_FAILED, FacilityRequest.STATE_COMPLETE, FacilityRequest.STATE_CANCELLED):
        _notify_requester(request)


def _notify_requester(request: FacilityRequest) -> None:
    try:
        from YSE_App.services.notify import notify

        notify([request.submitted_by],
               "Facility request for %s on %s is %s%s" % (
                   request.transient.name, request.allocation.name, request.get_state_display().lower(),
                   (": " + request.state_detail[:200]) if request.state_detail else ""),
               "/transient_detail/%s/#followup_tab" % request.transient.slug,
               "followup_status", subject="Facility request %s" % request.state, transient=request.transient)
    except Exception:  # noqa: BLE001 - notifications are best effort
        log.exception("could not notify %s about facility request %s", request.submitted_by, request.pk)


runs.register_runner("kind:" + ExternalService.KIND_FACILITY, run_facility_submission)


# --- after submission ---------------------------------------------------------

def poll_request(request: FacilityRequest) -> FacilityRequest:
    """Ask the facility for the current state of an open request."""
    facility = get_facility(request.allocation.facility)
    if facility is None or not facility.can("status"):
        raise FacilityRequestError("facility %r has no status endpoint" % request.allocation.facility)
    request.last_polled = timezone.now()
    try:
        status = facility.get_status(request)
    except FacilityError as exc:
        request.add_log("poll_error", str(exc))
        request.save(update_fields=["last_polled", "log", "modified_date"])
        raise FacilityRequestError(str(exc))
    if status.state != request.state:
        request.set_state(status.state, status.detail, save=False)
    else:
        request.add_log("polled", status.detail)
    request.save(update_fields=["state", "state_detail", "last_polled", "log", "submitted_at", "modified_date"])
    _after_state_change(request)
    return request


def cancel_request(request: FacilityRequest, user: User, reason: str = "") -> FacilityRequest:
    """Cancel at the facility when it supports it, then locally."""
    if request.is_final:
        raise FacilityRequestError("request is already %s" % request.state)
    facility = get_facility(request.allocation.facility)
    detail = reason or "cancelled by %s" % user.username
    if request.is_open and facility is not None and facility.can("delete") and request.external_id:
        try:
            status = facility.delete(request)
            detail = status.detail or detail
        except FacilityError as exc:
            raise FacilityRequestError(str(exc))
    if request.run_id and not request.run.is_finished:
        runs.record_completion(request.run, ExternalServiceRun.STATUS_CANCELLED, error=detail)
    request.set_state(FacilityRequest.STATE_CANCELLED, detail)
    _after_state_change(request)
    return request


def mark_request(request: FacilityRequest, state: str, user: User, detail: str = "") -> FacilityRequest:
    """A person records the outcome of a request whose facility has no status API."""
    if state not in (FacilityRequest.STATE_COMPLETE, FacilityRequest.STATE_FAILED, FacilityRequest.STATE_ACCEPTED,
                     FacilityRequest.STATE_RUNNING):
        raise FacilityRequestError("cannot mark a request %r by hand" % state)
    if request.is_final:
        raise FacilityRequestError("request is already %s" % request.state)
    request.set_state(state, detail or "marked %s by %s" % (state, user.username))
    _after_state_change(request)
    return request


def can_manage(request: FacilityRequest, user: User) -> bool:
    return bool(user.is_authenticated and (user.is_staff or user.is_superuser or request.submitted_by_id == user.id))


def poll_open_requests(limit: int = 200) -> Dict[str, int]:
    """Poll every open request whose facility has a status endpoint (cron / ``manage.py poll_facility_requests``)."""
    counts = {"polled": 0, "changed": 0, "errors": 0, "skipped": 0}
    qs = FacilityRequest.objects.filter(state__in=FacilityRequest.OPEN_STATES).select_related(
        "allocation", "allocation__telescope", "allocation__credential", "transient", "submitted_by")[:limit]
    for request in qs:
        facility = get_facility(request.allocation.facility)
        if facility is None or not facility.can("status"):
            counts["skipped"] += 1
            continue
        before = request.state
        try:
            poll_request(request)
        except FacilityRequestError:
            counts["errors"] += 1
            continue
        counts["polled"] += 1
        if request.state != before:
            counts["changed"] += 1
    return counts


@job(POLL_JOB_KIND, max_attempts=1)
def poll_job(payload, job=None):
    request_id = (payload or {}).get("facility_request_id")
    if request_id:
        request = FacilityRequest.objects.select_related("allocation", "transient").get(pk=request_id)
        poll_request(request)
        return {"state": request.state}
    return poll_open_requests(int((payload or {}).get("limit") or 200))


def requests_for_transient(transient, user: Optional[User] = None):
    qs = FacilityRequest.objects.filter(transient=transient).select_related(
        "allocation", "allocation__telescope", "submitted_by", "run")
    return qs


__all__ = [
    "FacilityRequestError", "FOLLOWUP_STATUS_FOR_STATE", "POLL_JOB_KIND", "allocations_for_user", "can_manage",
    "cancel_request", "mark_request", "poll_job", "poll_open_requests", "poll_request", "requests_for_transient",
    "run_facility_submission", "submit_request",
]
