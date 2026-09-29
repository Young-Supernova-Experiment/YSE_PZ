"""Background job queue rows (issue #263).

A ``Job`` is one unit of deferred work: a registered handler name (``kind``),
a JSON payload, and the bookkeeping the runner needs to claim it exactly once,
retry it with backoff and keep the result or the traceback. See
``YSE_App.jobs`` for the API (``enqueue``, ``@job``) and
``docs/background-jobs.md`` for operations.
"""

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient

__all__ = ["Job", "default_max_attempts"]


def default_max_attempts():
    return int(getattr(settings, "JOB_RUNNER_MAX_ATTEMPTS", 3) or 1)


class Job(models.Model):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (QUEUED, "Queued"),
        (RUNNING, "Running"),
        (DONE, "Done"),
        (FAILED, "Failed"),
        (CANCELLED, "Cancelled"),
    )
    ACTIVE_STATUSES = (QUEUED, RUNNING)
    FINAL_STATUSES = (DONE, FAILED, CANCELLED)

    kind = models.CharField(max_length=100, db_index=True)
    payload = JSONTextField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=QUEUED, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=default_max_attempts)
    run_after = models.DateTimeField(default=timezone.now, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=128, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    result = JSONTextField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    transient = models.ForeignKey(
        Transient, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["status", "run_after"], name="yse_job_status_run_after_idx"),
        ]

    def __str__(self):
        return "Job %s %s [%s]" % (self.pk, self.kind, self.status)

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES

    @property
    def can_retry(self):
        return self.status in (self.FAILED, self.CANCELLED)

    @property
    def can_cancel(self):
        return self.status == self.QUEUED

    def requeue(self, *, reset_attempts=True, run_after=None):
        """Put a failed/cancelled job back on the queue (admin "retry")."""
        self.status = self.QUEUED
        if reset_attempts:
            self.attempts = 0
        self.run_after = run_after or timezone.now()
        self.locked_at = None
        self.locked_by = ""
        self.started_at = None
        self.finished_at = None
        self.error = ""
        self.result = None
        self.save()

    def cancel(self):
        """Cancel a queued job; running or finished jobs are left alone."""
        updated = Job.objects.filter(pk=self.pk, status=self.QUEUED).update(
            status=self.CANCELLED, finished_at=timezone.now(), updated_at=timezone.now()
        )
        if updated:
            self.refresh_from_db()
        return bool(updated)
