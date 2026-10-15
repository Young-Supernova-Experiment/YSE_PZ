"""Submit, execute, modify, poll, cancel and harvest facility requests (#299, #300, #301).

``submit_request()`` validates the parameters with the allocation's facility
adapter, creates a :class:`FacilityRequest`, links a ``TransientFollowup`` when
the ``Requested`` status exists, starts an :class:`ExternalServiceRun` on the
allocation's service and dispatches it to the job queue. The run's handler
(``execute_run`` in :mod:`YSE_App.services.external_services`) finds the
runner registered here for facility services and calls the adapter's
``submit`` (or ``update`` for a modification run); the outcome lands on the
request (state, external id, log) and on the run (result / error). A transport
error before anything reached the facility (:class:`FacilityUnreachable`) is
retried by the job queue with backoff; any other error fails the request
without a retry, since a second attempt could submit twice.

``update_request`` re-validates changed parameters and queues a modification;
``poll_request`` / ``cancel_request`` / ``mark_request`` advance the state
afterwards. Reaching ``complete`` charges ``hours_charged`` to the allocation
once and, for a ``photometry``-kind request, queues ``facility.retrieve_data``
which ingests the adapter's light curve into the transient's photometry
through :func:`YSE_App.data_utils.add_transient_phot_util`.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from YSE_App.facilities import FacilityError, FacilityUnreachable, FacilityValidationError, get_facility
from YSE_App.jobs import JobRetry, enqueue, job
from YSE_App.jobs.runner import backoff_seconds
from YSE_App.models.allocation_models import Allocation, FacilityRequest
from YSE_App.models.enum_models import FollowupStatus
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.transient_models import Transient
from YSE_App.services import external_services as runs
from YSE_App.services.allocations import allocations_for_user, charge_request, ensure_service, refund_request

log = logging.getLogger(__name__)

POLL_JOB_KIND = "facility.poll"
RESULTS_JOB_KIND = "facility.retrieve_data"
ACTION_SUBMIT = "submit"
ACTION_UPDATE = "update"

#: FacilityRequest state -> FollowupStatus name (applied only when that status row exists)
FOLLOWUP_STATUS_FOR_STATE = {
    FacilityRequest.STATE_SUBMITTED: "Requested",
    FacilityRequest.STATE_ACCEPTED: "Requested",
    FacilityRequest.STATE_RUNNING: "InProcess",
    FacilityRequest.STATE_COMPLETE: "Successful",
    FacilityRequest.STATE_FAILED: "Failed",
    FacilityRequest.STATE_CANCELLED: "Failed",
}
#: states that notify the requester (accepted once, then the final outcome)
NOTIFY_STATES = (FacilityRequest.STATE_ACCEPTED, FacilityRequest.STATE_COMPLETE, FacilityRequest.STATE_FAILED,
                 FacilityRequest.STATE_CANCELLED)


class FacilityRequestError(Exception):
    """A request cannot be created or changed (permissions, state, adapter)."""


def submit_max_attempts() -> int:
    return max(1, int(getattr(settings, "FACILITY_SUBMIT_MAX_ATTEMPTS", 3) or 3))


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


def open_request_for(allocation: Allocation, transient: Transient) -> Optional[FacilityRequest]:
    """An unfinished request of this transient on this allocation (queued or open), if any."""
    return FacilityRequest.objects.filter(
        allocation=allocation, transient=transient,
        state__in=(FacilityRequest.STATE_QUEUED,) + FacilityRequest.OPEN_STATES,
    ).order_by("-created_date").first()


def submit_request(allocation: Allocation, transient: Transient, user: User, params: Optional[Dict] = None,
                   *, attach_followup: bool = True, dispatch: bool = True, followup=None) -> FacilityRequest:
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
    kind = FacilityRequest.KIND_PHOTOMETRY if facility.is_photometry else FacilityRequest.KIND_OBSERVATION
    service = ensure_service(allocation, user)
    with transaction.atomic():
        if followup is None and attach_followup and kind == FacilityRequest.KIND_OBSERVATION:
            followup = _attach_followup(user, transient, cleaned)
        request = FacilityRequest.objects.create(
            allocation=allocation, transient=transient, followup=followup, payload=cleaned, kind=kind,
            submitted_by=user, hours_charged=hours, created_by=user, modified_by=user,
        )
        request.add_log("created", "by %s via %s" % (user.username, facility.slug))
        run = _queue_run(request, service, user, ACTION_SUBMIT, cleaned)
    if dispatch:
        if not runs.dispatch_run(run):
            request.set_state(FacilityRequest.STATE_FAILED, "could not enqueue the submission job")
    request.refresh_from_db()
    return request


def _queue_run(request: FacilityRequest, service: ExternalService, user: User, action: str, params: Dict) -> ExternalServiceRun:
    try:
        run, _token = runs.start_run(service, user, {"facility_request_id": request.pk, "action": action, "parameters": params},
                                     transient=request.transient, target=request, dispatch=False)
    except runs.ExternalServiceError as exc:
        raise FacilityRequestError(str(exc))
    request.run = run
    request.state = FacilityRequest.STATE_QUEUED
    request.state_detail = "waiting for the job runner" if action == ACTION_SUBMIT else "modification waiting for the job runner"
    request.add_log("queued", "%s run %s" % (action, run.uuid))
    request.save(update_fields=["run", "state", "state_detail", "log", "modified_date"])
    return run


def update_request(request: FacilityRequest, user: User, params: Dict, *, dispatch: bool = True) -> FacilityRequest:
    """Re-validate ``params`` (merged over the current payload) and queue a modification at the facility."""
    if request.is_final:
        raise FacilityRequestError("request is already %s" % request.state)
    if request.state == FacilityRequest.STATE_QUEUED:
        raise FacilityRequestError("request is still waiting for the job runner; cancel it and submit again instead")
    facility = get_facility(request.allocation.facility)
    if facility is None or not facility.supports("update", request.allocation):
        raise FacilityRequestError("facility %r cannot modify submitted requests" % request.allocation.facility)
    merged = dict(request.payload or {})
    merged.update(params or {})
    cleaned = facility.validate(merged, request.allocation)
    hours = facility.estimate_hours(cleaned, request.allocation)
    allocation = request.allocation
    if allocation.hours_allocated and hours - request.hours_charged > allocation.hours_remaining + 1e-9:
        raise FacilityValidationError({"exposure_time": "the change needs %.2f h more but only %.2f h remain on %s"
                                       % (hours - request.hours_charged, allocation.hours_remaining, allocation.name)})
    service = ensure_service(allocation, user)
    with transaction.atomic():
        request.payload = cleaned
        request.hours_charged = hours
        request.modified_by = user
        request.add_log("modified", "by %s" % user.username)
        request.save(update_fields=["payload", "hours_charged", "modified_by", "log", "modified_date"])
        run = _queue_run(request, service, user, ACTION_UPDATE, cleaned)
    if dispatch:
        if not runs.dispatch_run(run):
            request.set_state(FacilityRequest.STATE_FAILED, "could not enqueue the modification job")
    request.refresh_from_db()
    return request


# --- execution (job runner) ---------------------------------------------------

def _request_for_run(run: ExternalServiceRun) -> Optional[FacilityRequest]:
    request_id = (run.request_payload or {}).get("facility_request_id")
    if request_id:
        return FacilityRequest.objects.filter(pk=request_id).select_related("allocation", "transient").first()
    return getattr(run, "facility_request", None)


def run_facility_submission(run: ExternalServiceRun, job=None) -> Dict:
    """Runner for facility services: send (or modify) the request and record what happened.

    Adapter errors are recorded on the request and never raised, except
    :class:`FacilityUnreachable`, which raises :class:`JobRetry` while the job
    has attempts left (bounded by ``FACILITY_SUBMIT_MAX_ATTEMPTS``).
    """
    request = _request_for_run(run)
    if request is None:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="no facility request for this run")
        return {"handled": True, "ok": False}
    if request.state not in (FacilityRequest.STATE_QUEUED, FacilityRequest.STATE_DRAFT):
        return {"handled": True, "ok": True, "skipped": request.state}
    action = (run.request_payload or {}).get("action") or ACTION_SUBMIT
    facility = get_facility(request.allocation.facility)
    run.mark_running()
    request.attempts = (request.attempts or 0) + 1
    request.save(update_fields=["attempts", "modified_date"])
    if facility is None:
        _fail(request, run, "facility %r is not registered" % request.allocation.facility)
        return {"handled": True, "ok": False}
    try:
        result = facility.update(request) if action == ACTION_UPDATE else facility.submit(request)
    except FacilityUnreachable as exc:
        attempts = job.attempts if job is not None else request.attempts
        max_attempts = min(job.max_attempts, submit_max_attempts()) if job is not None else submit_max_attempts()
        if attempts < max_attempts:
            delay = backoff_seconds(attempts, getattr(settings, "FACILITY_SUBMIT_BACKOFF_SECONDS", None))
            request.add_log("retry", "attempt %d/%d: %s; retrying in %.0f s" % (attempts, max_attempts, exc, delay), save=True)
            raise JobRetry(str(exc), delay=delay)
        _fail(request, run, "unreachable after %d attempts: %s" % (attempts, exc))
        return {"handled": True, "ok": False}
    except FacilityError as exc:
        _fail(request, run, str(exc))
        return {"handled": True, "ok": False}
    except Exception as exc:  # noqa: BLE001 - recorded on the request, not retried (no double submission)
        log.exception("facility %s %s failed for request %s", facility.slug, action, request.pk)
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
    return {"handled": True, "ok": True, "state": request.state, "external_id": request.external_id, "action": action}


def _fail(request: FacilityRequest, run: ExternalServiceRun, error: str) -> None:
    request.set_state(FacilityRequest.STATE_FAILED, error)
    if not run.is_finished:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
    _after_state_change(request)


def _after_state_change(request: FacilityRequest) -> None:
    """Follow-up status, hours accounting, result harvesting and notifications."""
    if request.state == FacilityRequest.STATE_COMPLETE:
        charge_request(request)
        if request.is_photometry and not request.results_ingested_at:
            facility = get_facility(request.allocation.facility)
            if facility is not None and facility.can("results"):
                enqueue(RESULTS_JOB_KIND, {"facility_request_id": request.pk}, created_by=request.submitted_by,
                        transient=request.transient)
    elif request.state == FacilityRequest.STATE_CANCELLED:
        refund_request(request)
    name = FOLLOWUP_STATUS_FOR_STATE.get(request.state)
    if name and request.followup_id:
        status = FollowupStatus.objects.filter(name=name).first()
        if status is not None and request.followup.status_id != status.pk:
            request.followup.status = status
            request.followup.save(update_fields=["status", "modified_date"])
    if request.state in NOTIFY_STATES:
        _notify(request)


def _recipients(request: FacilityRequest) -> List[User]:
    users = [request.submitted_by]
    if request.is_final and getattr(settings, "FACILITY_NOTIFY_GROUPS", False):
        for group in request.allocation.groups.all():
            users.extend(group.user_set.filter(is_active=True))
    return users


def _notify(request: FacilityRequest) -> None:
    try:
        from YSE_App.services.notify import notify

        notify(_recipients(request),
               "Facility request for %s on %s is %s%s" % (
                   request.transient.name, request.allocation.name, request.get_state_display().lower(),
                   (": " + request.state_detail[:200]) if request.state_detail else ""),
               "/transient_detail/%s/#followup_tab" % request.transient.slug,
               "followup_status", subject="Facility request %s" % request.state, transient=request.transient)
    except Exception:  # noqa: BLE001 - notifications are best effort
        log.exception("could not notify about facility request %s", request.pk)


runs.register_runner("kind:" + ExternalService.KIND_FACILITY, run_facility_submission)


# --- after submission ---------------------------------------------------------

def poll_request(request: FacilityRequest) -> FacilityRequest:
    """Ask the facility for the current state of an open request."""
    facility = get_facility(request.allocation.facility)
    if facility is None or not facility.supports("status", request.allocation):
        raise FacilityRequestError("facility %r has no status endpoint" % request.allocation.facility)
    request.last_polled = timezone.now()
    try:
        status = facility.get_status(request)
    except FacilityError as exc:
        request.add_log("poll_error", str(exc))
        request.save(update_fields=["last_polled", "log", "modified_date"])
        raise FacilityRequestError(str(exc))
    external_id = getattr(status, "external_id", "") or ""
    external_url = getattr(status, "external_url", "") or ""
    if status.state != request.state:
        request.set_state(status.state, status.detail, external_id=external_id, external_url=external_url, save=False)
    else:
        request.add_log("polled", status.detail)
        if external_id and not request.external_id:
            request.external_id = external_id[:255]
    request.save(update_fields=["state", "state_detail", "external_id", "external_url", "last_polled", "log",
                                "submitted_at", "modified_date"])
    _after_state_change(request)
    return request


def cancel_request(request: FacilityRequest, user: User, reason: str = "") -> FacilityRequest:
    """Cancel at the facility when it supports it, then locally."""
    if request.is_final:
        raise FacilityRequestError("request is already %s" % request.state)
    facility = get_facility(request.allocation.facility)
    detail = reason or "cancelled by %s" % user.username
    if request.is_open and facility is not None and facility.supports("delete", request.allocation) and request.external_id:
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


def _due_for_poll(request: FacilityRequest, facility, now) -> bool:
    minutes = int(getattr(facility, "poll_interval_minutes", 0) or 0)
    if not minutes or not request.last_polled:
        return True
    return request.last_polled <= now - datetime.timedelta(minutes=minutes)


def poll_open_requests(limit: int = 200, *, force: bool = False) -> Dict[str, int]:
    """Poll every open request whose facility has a status endpoint (cron / ``manage.py poll_facility_requests``).

    An adapter's ``poll_interval_minutes`` spaces the polls of one request; ``force`` ignores it.
    """
    counts = {"polled": 0, "changed": 0, "errors": 0, "skipped": 0}
    now = timezone.now()
    qs = FacilityRequest.objects.filter(state__in=FacilityRequest.OPEN_STATES).select_related(
        "allocation", "allocation__telescope", "allocation__credential", "transient", "submitted_by")[:limit]
    for request in qs:
        facility = get_facility(request.allocation.facility)
        if facility is None or not facility.supports("status", request.allocation):
            counts["skipped"] += 1
            continue
        if not force and not _due_for_poll(request, facility, now):
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
    return poll_open_requests(int((payload or {}).get("limit") or 200), force=bool((payload or {}).get("force")))


# --- results (forced photometry, #301) ----------------------------------------

def retrieve_results(request: FacilityRequest, user: Optional[User] = None) -> Dict:
    """Fetch the adapter's photometry for a complete request and file it under the transient."""
    facility = get_facility(request.allocation.facility)
    if facility is None or not facility.can("results"):
        raise FacilityRequestError("facility %r returns no photometry" % request.allocation.facility)
    if request.state != FacilityRequest.STATE_COMPLETE:
        raise FacilityRequestError("request is %s, not complete" % request.state)
    try:
        points = facility.fetch_results(request)
    except FacilityError as exc:
        request.add_log("results_error", str(exc), save=True)
        raise FacilityRequestError(str(exc))
    summary = ingest_points(request, points, user or request.submitted_by, instrument=facility.results_instrument,
                            obs_group=facility.results_obs_group)
    request.results_ingested_at = timezone.now()
    request.n_results = summary["points"]
    request.add_log("results", "%(points)d points ingested (%(bands)s)" % summary)
    request.save(update_fields=["results_ingested_at", "n_results", "log", "modified_date"])
    return summary


def ingest_points(request: FacilityRequest, points: List[Dict], user: User, *, instrument: str = "",
                  obs_group: str = "", clobber: bool = True) -> Dict:
    """Write photometry points through ``data_utils.add_transient_phot_util`` (PhotStat updates via signals)."""
    from YSE_App.data_utils import add_transient_phot_util

    photdata = {}
    bands = set()
    for index, point in enumerate(points):
        obs_date = point.get("obs_date")
        if isinstance(obs_date, datetime.datetime):
            obs_date = obs_date.astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat()
        elif not obs_date and point.get("mjd") is not None:
            obs_date = (datetime.datetime(1858, 11, 17) + datetime.timedelta(days=float(point["mjd"]))).isoformat()
        band = point.get("band") or "Unknown"
        bands.add(band)
        photdata[index] = {
            "obs_date": obs_date, "band": band, "mag": point.get("mag"), "mag_err": point.get("mag_err"),
            "flux": point.get("flux"), "flux_err": point.get("flux_err"), "flux_zero_point": point.get("flux_zero_point"),
            "forced": point.get("forced", True), "diffim": point.get("diffim", True), "data_quality": point.get("data_quality") or 0,
            "discovery_point": 0,
        }
    if not photdata:
        return {"points": 0, "bands": "none"}
    photdict = {
        "mjdmatchmin": float(getattr(settings, "FACILITY_RESULTS_MJD_MATCH_DAYS", 0.001) or 0.001), "clobber": clobber,
        "facility": {"instrument": point.get("instrument") or instrument or "Unknown",
                     "obs_group": point.get("obs_group") or obs_group or "Unknown", "photdata": photdata},
    }
    # two passes like the upload API: the first creates the TransientPhotometry rows, the second the points
    from YSE_App.models.phot_models import TransientPhotometry

    _response, new_photometry = add_transient_phot_util(photdict, request.transient, user, do_photdata=False)
    if new_photometry:
        TransientPhotometry.objects.bulk_create(new_photometry)
    add_transient_phot_util(photdict, request.transient, user, do_photdata=True)
    return {"points": len(photdata), "bands": ", ".join(sorted(bands))}


@job(RESULTS_JOB_KIND, max_attempts=3, backoff_seconds=600)
def retrieve_results_job(payload, job=None):
    request = FacilityRequest.objects.select_related("allocation", "transient", "submitted_by").get(
        pk=(payload or {}).get("facility_request_id"))
    if request.results_ingested_at:
        return {"skipped": "already ingested", "points": request.n_results}
    try:
        return retrieve_results(request)
    except FacilityRequestError as exc:
        raise JobRetry(str(exc))


def requests_for_transient(transient, user: Optional[User] = None):
    qs = FacilityRequest.objects.filter(transient=transient).select_related(
        "allocation", "allocation__telescope", "submitted_by", "run")
    return qs


__all__ = [
    "ACTION_SUBMIT", "ACTION_UPDATE", "FacilityRequestError", "FOLLOWUP_STATUS_FOR_STATE", "POLL_JOB_KIND",
    "RESULTS_JOB_KIND", "allocations_for_user", "can_manage", "cancel_request", "ingest_points", "mark_request",
    "open_request_for", "poll_job", "poll_open_requests", "poll_request", "requests_for_transient", "retrieve_results",
    "retrieve_results_job", "run_facility_submission", "submit_request", "update_request",
]
