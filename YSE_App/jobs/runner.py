"""Claim, run and retry queued jobs (issue #263).

Claiming is race-safe on every backend: on MySQL 8 the candidate row is
locked with ``SELECT ... FOR UPDATE SKIP LOCKED`` so several workers do not
even contend; everywhere else (sqlite in tests, MariaDB) the claim is a
conditional ``UPDATE ... WHERE status='queued'`` whose row count says who won.

A handler that raises is retried with exponential backoff until
``max_attempts``; ``JobRetry`` chooses the delay, ``JobFailed`` fails at once.
"""

from __future__ import annotations

import datetime
import logging
import os
import signal
import socket
import time
import traceback
from typing import Iterable, List, Optional

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, F
from django.utils import timezone

from YSE_App.jobs.registry import autodiscover, get_handler
from YSE_App.models.job_models import Job

logger = logging.getLogger(__name__)


class JobRetry(Exception):
    """Raised by a handler to retry later; ``delay`` seconds (None = default backoff)."""

    def __init__(self, message="retry", delay: Optional[float] = None):
        super().__init__(message)
        self.delay = delay


class JobFailed(Exception):
    """Raised by a handler to fail the job without further attempts."""


def setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def default_worker_id() -> str:
    return "%s:%s" % (socket.gethostname(), os.getpid())


def backoff_seconds(attempt: int, base: Optional[float] = None) -> float:
    """Delay before attempt ``attempt + 1``: base * 2**(attempt-1), capped."""
    base = float(base if base is not None else setting("JOB_RUNNER_BACKOFF_SECONDS", 60))
    cap = float(setting("JOB_RUNNER_BACKOFF_MAX_SECONDS", 3600))
    return min(cap, base * (2 ** max(0, attempt - 1)))


def enqueue(kind: str, payload=None, *, run_after=None, delay: Optional[float] = None,
            max_attempts: Optional[int] = None, created_by=None, transient=None,
            inline: Optional[bool] = None) -> Job:
    """Create a queued ``Job`` and return it.

    ``run_after`` (aware datetime) or ``delay`` (seconds) postpones it.
    ``max_attempts`` defaults to the handler's setting, then
    ``JOB_RUNNER_MAX_ATTEMPTS``. With ``JOB_RUNNER_INLINE`` (or ``inline=True``)
    the job runs immediately in this process: development and tests.
    """
    if run_after is None:
        run_after = timezone.now()
        if delay:
            run_after = run_after + datetime.timedelta(seconds=float(delay))
    if max_attempts is None:
        handler = get_handler(kind)
        if handler is not None and handler.max_attempts:
            max_attempts = handler.max_attempts
        else:
            max_attempts = int(setting("JOB_RUNNER_MAX_ATTEMPTS", 3))
    job = Job.objects.create(
        kind=kind,
        payload=payload if payload is not None else {},
        run_after=run_after,
        max_attempts=max(1, int(max_attempts)),
        created_by=created_by if getattr(created_by, "pk", None) else None,
        transient=transient,
    )
    if inline is None:
        inline = bool(setting("JOB_RUNNER_INLINE", False))
    if inline:
        claimed = claim_job(job.pk, worker_id="inline:%s" % os.getpid())
        if claimed is not None:
            execute(claimed)
            job.refresh_from_db()
    return job


def claim_job(job_id: Optional[int] = None, *, worker_id: Optional[str] = None,
              kinds: Optional[Iterable[str]] = None, now=None) -> Optional[Job]:
    """Atomically move one due queued job to ``running`` and return it."""
    now = now or timezone.now()
    worker_id = worker_id or default_worker_id()
    qs = Job.objects.filter(status=Job.QUEUED, run_after__lte=now)
    if job_id is not None:
        qs = qs.filter(pk=job_id)
    if kinds:
        qs = qs.filter(kind__in=list(kinds))
    qs = qs.order_by("run_after", "id")
    with transaction.atomic():
        candidates = qs
        if connection.features.has_select_for_update_skip_locked:
            candidates = candidates.select_for_update(skip_locked=True)
        elif connection.features.has_select_for_update:
            candidates = candidates.select_for_update()
        candidate = candidates.values_list("pk", flat=True).first()
        if candidate is None:
            return None
        updated = Job.objects.filter(pk=candidate, status=Job.QUEUED).update(
            status=Job.RUNNING, locked_at=now, locked_by=worker_id,
            started_at=now, attempts=F("attempts") + 1, updated_at=now,
        )
        if not updated:
            return None
    return Job.objects.get(pk=candidate)


def execute(job: Job) -> Job:
    """Run the handler for an already-claimed ``running`` job and record the outcome."""
    handler = get_handler(job.kind)
    started = time.monotonic()
    if handler is None:
        return _finish(job, Job.FAILED, error="no handler registered for job kind %r" % job.kind)
    try:
        result = handler(job.payload if job.payload is not None else {}, job=job)
    except JobFailed as exc:
        return _finish(job, Job.FAILED, error="%s\n%s" % (exc, traceback.format_exc()))
    except JobRetry as exc:
        return _retry_or_fail(job, handler, str(exc), delay=exc.delay)
    except Exception:  # noqa: BLE001 - any handler error is recorded on the row
        return _retry_or_fail(job, handler, traceback.format_exc())
    else:
        logger.info("job %s %s done in %.2fs", job.pk, job.kind, time.monotonic() - started)
        return _finish(job, Job.DONE, result=result)


def _retry_or_fail(job: Job, handler, error: str, delay: Optional[float] = None) -> Job:
    if job.attempts < job.max_attempts:
        if delay is None:
            delay = backoff_seconds(job.attempts, getattr(handler, "backoff_seconds", None))
        job.status = Job.QUEUED
        job.run_after = timezone.now() + datetime.timedelta(seconds=float(delay))
        job.error = error
        job.locked_at = None
        job.locked_by = ""
        job.finished_at = None
        job.save(update_fields=["status", "run_after", "error", "locked_at", "locked_by",
                                "finished_at", "updated_at"])
        logger.warning("job %s %s failed on attempt %d/%d; retry in %.0fs",
                       job.pk, job.kind, job.attempts, job.max_attempts, float(delay))
        return job
    logger.error("job %s %s failed after %d attempts", job.pk, job.kind, job.attempts)
    return _finish(job, Job.FAILED, error=error)


def _finish(job: Job, status: str, *, result=None, error: str = "") -> Job:
    job.status = status
    job.result = result
    job.error = error or ""
    job.finished_at = timezone.now()
    job.locked_at = None
    job.locked_by = ""
    job.save(update_fields=["status", "result", "error", "finished_at", "locked_at",
                            "locked_by", "updated_at"])
    return job


def reap_stale(stale_minutes: Optional[int] = None, now=None) -> int:
    """Requeue ``running`` jobs whose worker died (locked longer than ``stale_minutes``)."""
    minutes = int(stale_minutes if stale_minutes is not None else setting("JOB_RUNNER_STALE_MINUTES", 60))
    if minutes <= 0:
        return 0
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(minutes=minutes)
    stale = Job.objects.filter(status=Job.RUNNING, locked_at__lt=cutoff)
    count = 0
    for job in stale:
        note = "worker %s did not finish within %d min; requeued" % (job.locked_by or "?", minutes)
        if job.attempts < job.max_attempts:
            job.status = Job.QUEUED
            job.run_after = now
            job.error = note
            job.locked_at = None
            job.locked_by = ""
            job.save(update_fields=["status", "run_after", "error", "locked_at", "locked_by", "updated_at"])
        else:
            _finish(job, Job.FAILED, error=note + "; no attempts left")
        count += 1
    if count:
        logger.warning("job runner: reaped %d stale running job(s)", count)
    return count


class PassResult:
    def __init__(self):
        self.jobs: List[Job] = []
        self.reaped = 0

    @property
    def done(self):
        return sum(1 for j in self.jobs if j.status == Job.DONE)

    @property
    def failed(self):
        return sum(1 for j in self.jobs if j.status == Job.FAILED)

    @property
    def retried(self):
        return sum(1 for j in self.jobs if j.status == Job.QUEUED)

    def summary(self):
        return "ran %d job(s): %d done, %d failed, %d to retry; %d stale reaped" % (
            len(self.jobs), self.done, self.failed, self.retried, self.reaped)


def run_pass(*, limit: Optional[int] = None, budget_seconds: Optional[float] = None,
             worker_id: Optional[str] = None, kinds: Optional[Iterable[str]] = None,
             stop=None) -> PassResult:
    """Claim and run due jobs until none are left, ``limit`` is hit or the time budget is spent."""
    autodiscover()
    result = PassResult()
    result.reaped = reap_stale()
    limit = int(limit if limit is not None else setting("JOB_RUNNER_PASS_LIMIT", 100))
    deadline = time.monotonic() + float(budget_seconds) if budget_seconds else None
    while len(result.jobs) < limit:
        if stop is not None and stop():
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        job = claim_job(worker_id=worker_id, kinds=kinds)
        if job is None:
            break
        result.jobs.append(execute(job))
    return result


def run_forever(*, sleep_seconds: float = 5.0, worker_id: Optional[str] = None,
                kinds: Optional[Iterable[str]] = None, limit: Optional[int] = None,
                max_passes: Optional[int] = None, log=None) -> int:
    """Run passes until SIGTERM/SIGINT; returns the number of passes made."""
    stopping = {"flag": False}

    def _stop(signum, frame):
        stopping["flag"] = True

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _stop)
        except (ValueError, OSError):  # not the main thread
            pass
    passes = 0
    try:
        while not stopping["flag"]:
            result = run_pass(limit=limit, worker_id=worker_id, kinds=kinds, stop=lambda: stopping["flag"])
            passes += 1
            if log is not None and result.jobs:
                log(result.summary())
            if max_passes is not None and passes >= max_passes:
                break
            if not result.jobs:
                slept = 0.0
                while slept < sleep_seconds and not stopping["flag"]:
                    time.sleep(min(0.5, sleep_seconds - slept))
                    slept += 0.5
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return passes


def queue_counts():
    """{status: count} for the status page and the cron log line."""
    counts = {status: 0 for status, _ in Job.STATUS_CHOICES}
    # order_by() clears Meta.ordering, which would otherwise join the GROUP BY.
    for row in Job.objects.order_by().values("status").annotate(n=Count("id")):
        counts[row["status"]] = row["n"]
    counts["due"] = Job.objects.filter(status=Job.QUEUED, run_after__lte=timezone.now()).count()
    return counts
