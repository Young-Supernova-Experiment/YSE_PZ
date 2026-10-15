"""Start, dispatch and complete :class:`ExternalServiceRun` rows (#265).

Consumers (analysis services #312, annotations #316, summaries #294, facility
fetches #298) call :func:`start_run` and, when the work is done, either
:func:`record_completion` (in-process runners) or let the remote service POST
to the run's callback URL (``external_service_run_callback`` view).

Dispatch to a worker is intentionally thin: :func:`dispatch_run` enqueues a
``external_service.run`` job on the background job queue (#263,
``YSE_App.services.job_queue``); :func:`execute_run` is the registered handler.
It looks up a *runner* (:func:`register_runner`) by the service's slug, then by
``kind:<kind>``; facility services (#298) register one in
:mod:`YSE_App.services.facility_requests`. Without a runner (#313 will add the
analysis ones) the run stays pending for an in-process caller to complete.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import secrets
from typing import Optional, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun

log = logging.getLogger(__name__)

CALLBACK_TOKEN_HEADER = "HTTP_X_RUN_TOKEN"  # the header a callback may carry: X-Run-Token
JOB_KIND = "external_service.run"  # job-queue kind that executes a run

try:  # the background job queue (#263)
    from YSE_App.services.job_queue import enqueue as _enqueue
    from YSE_App.services.job_queue import job as _job
except ImportError:  # pragma: no cover - queue package absent
    _enqueue = None

    def _job(kind, **options):
        return lambda func: func


class ExternalServiceError(Exception):
    """Base class for run-management errors."""


class ServiceDisabled(ExternalServiceError):
    """The service is switched off (or the user may not see it)."""


class RunLimitExceeded(ExternalServiceError):
    """The per-user daily cap for this service is reached (HTTP 429 in the API)."""

    def __init__(self, service: ExternalService, limit: int):
        self.service = service
        self.limit = limit
        super().__init__(
            "%s allows %d run(s) per user per day; the limit is reached." % (service.name, limit)
        )


class InvalidTransition(ExternalServiceError):
    """A completion was recorded for a run that is already finished."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_callback_token() -> str:
    return secrets.token_urlsafe(32)


def verify_callback_token(run: ExternalServiceRun, token: Optional[str]) -> bool:
    """Constant-time check of a presented token against the run's stored hash."""
    if not token or not run.callback_token_hash:
        return False
    return hmac.compare_digest(run.callback_token_hash, hash_token(token))


def callback_path(run: ExternalServiceRun) -> str:
    return reverse("external_service_run_callback", kwargs={"run_uuid": str(run.uuid)})


def callback_url(run: ExternalServiceRun, request=None) -> str:
    """Absolute callback URL: from the request when given, else YSE_PUBLIC_BASE_URL."""
    path = callback_path(run)
    if request is not None:
        return request.build_absolute_uri(path)
    from django.conf import settings

    base = getattr(settings, "YSE_PUBLIC_BASE_URL", "").rstrip("/")
    return base + path if base else path


def runs_today_for_user(service: ExternalService, user: User) -> int:
    since = timezone.now() - timezone.timedelta(days=1)
    return ExternalServiceRun.objects.filter(
        service=service, created_by=user, created_date__gte=since,
    ).exclude(status=ExternalServiceRun.STATUS_CANCELLED).count()


def start_run(
    service: ExternalService,
    user: User,
    payload: Optional[dict] = None,
    *,
    transient=None,
    target=None,
    target_ref: str = "",
    dispatch: bool = True,
    enforce_limit: bool = True,
) -> Tuple[ExternalServiceRun, str]:
    """Create a pending run and return ``(run, callback_token)``.

    The token is returned exactly once: only its sha256 is stored. Raises
    :class:`ServiceDisabled` or :class:`RunLimitExceeded`; a caller that shows
    the latter in the UI should map it to a 429. ``enforce_limit=False`` skips
    the daily cap (analysis services exempt staff, #314).
    """
    if not service.enabled or not service.visible_to(user):
        raise ServiceDisabled("%s is not available." % service.name)
    limit = service.max_runs_per_user_per_day or 0
    if enforce_limit and limit and runs_today_for_user(service, user) >= limit:
        raise RunLimitExceeded(service, limit)

    merged = dict(service.default_params or {})
    merged.update(payload or {})
    token = new_callback_token()
    run = ExternalServiceRun(
        service=service,
        transient=transient,
        target_ref=target_ref or "",
        request_payload=merged,
        callback_token_hash=hash_token(token),
        created_by=user,
        modified_by=user,
    )
    if target is not None:
        run.target = target
    with transaction.atomic():
        run.save()
    if dispatch:
        dispatch_run(run)
    return run, token


def dispatch_run(run: ExternalServiceRun) -> bool:
    """Enqueue a ``external_service.run`` job for ``run``; False if no queue is installed.

    With ``JOB_RUNNER_INLINE`` the handler runs immediately in this process.
    """
    if _enqueue is None:
        return False
    try:
        _enqueue(JOB_KIND, {"run_id": run.pk}, created_by=run.created_by, transient=run.transient)
    except Exception:  # pragma: no cover - depends on the queue backend
        log.exception("Could not enqueue external service run %s", run.uuid)
        return False
    return True


# --- runner registry ----------------------------------------------------------
# key: a service slug, or "kind:<kind>" for every service of that kind.
_RUNNERS = {}


def register_runner(key: str, func) -> None:
    """Make ``func(run) -> dict`` execute runs of the service ``key`` (slug) or ``kind:<kind>``."""
    if not key or not callable(func):
        raise ValueError("register_runner needs a key and a callable")
    _RUNNERS[key] = func


def unregister_runner(key: str) -> None:
    _RUNNERS.pop(key, None)


def get_runner(service: ExternalService):
    return _RUNNERS.get(service.slug) or _RUNNERS.get("kind:" + service.kind)


def _accepts_job(func) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return "job" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


@_job(JOB_KIND)
def execute_run(payload, job=None):
    """Job handler for :data:`JOB_KIND`: hand the run to its runner.

    A runner takes the run, does the work and records the outcome with
    :func:`record_completion` (or the run's ``mark_*`` methods); whatever it
    returns is stored as the job result. Without a runner (TODO #313 for the
    analysis services) the run stays pending and the job finishes with
    ``handled=False``.
    """
    run_id = payload.get("run_id") if isinstance(payload, dict) else payload
    run = ExternalServiceRun.objects.select_related("service").get(pk=run_id)
    runner = get_runner(run.service)
    if runner is None:
        log.info("execute_run: %s has no runner registered yet; leaving it %s", run, run.status)
        return {"run": str(run.uuid), "handled": False}
    # a runner that takes ``job`` sees the attempt counter (facility submissions retry on transport errors)
    result = runner(run, job=job) if _accepts_job(runner) else runner(run)
    out = {"run": str(run.uuid), "handled": True}
    if isinstance(result, dict):
        out.update(result)
    return out


def record_completion(
    run: ExternalServiceRun,
    status: str,
    *,
    result=None,
    error: str = "",
    artifact_url: str = "",
    external_id: str = "",
    allow_repeat: bool = False,
) -> ExternalServiceRun:
    """Move a run to a final status. Rejects a second completion unless ``allow_repeat``."""
    if status not in ExternalServiceRun.FINAL_STATUSES:
        raise ValueError("status must be one of %s" % ", ".join(ExternalServiceRun.FINAL_STATUSES))
    if run.is_finished and not allow_repeat:
        raise InvalidTransition("run %s is already %s" % (run.uuid, run.status))
    if external_id:
        run.external_id = external_id
        run.save(update_fields=["external_id"])
    if status == ExternalServiceRun.STATUS_SUCCEEDED:
        run.mark_succeeded(result=result, artifact_url=artifact_url)
    elif status == ExternalServiceRun.STATUS_FAILED:
        run.mark_failed(error or "failed", result=result)
    else:
        run.mark_cancelled(error)
    return run


def expire_runs(older_than_days: int, *, statuses=None, dry_run: bool = False) -> int:
    """Delete finished runs (and their artifact files) older than N days."""
    cutoff = timezone.now() - timezone.timedelta(days=older_than_days)
    qs = ExternalServiceRun.objects.filter(finished_at__lt=cutoff)
    qs = qs.filter(status__in=statuses or ExternalServiceRun.FINAL_STATUSES)
    count = qs.count()
    if dry_run:
        return count
    for run in qs.iterator():
        if run.artifact_file:
            try:
                run.artifact_file.delete(save=False)
            except Exception:  # pragma: no cover - storage specific
                log.warning("Could not delete artifact for run %s", run.uuid)
        run.delete()
    return count
