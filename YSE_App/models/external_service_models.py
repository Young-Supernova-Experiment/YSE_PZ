"""Registry of external services and a record of every call to them (#265).

``ExternalService`` describes something outside YSE-PZ that does work for us
(an analysis fitter, an annotation catalogue, an LLM summariser, an archive
check, a facility API). ``ExternalServiceRun`` records one request: what was
asked, for which target, by whom, its status, and where the results are. The
lifecycle and webhook-callback shape follow SkyPortal's analysis services
(BSD-3-Clause) in spirit; no code is copied from that project.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import Group
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from YSE_App.models.base import BaseModel
from YSE_App.models.credential_models import EncryptedCredential
from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient


class ExternalService(BaseModel):
    KIND_ANALYSIS = "analysis"
    KIND_ANNOTATION = "annotation"
    KIND_SUMMARY = "summary"
    KIND_ARCHIVE = "archive"
    KIND_FACILITY = "facility"
    KIND_BROKER = "broker"
    KIND_SHARING = "sharing"
    KIND_GENERIC = "generic"
    KIND_CHOICES = (
        (KIND_ANALYSIS, "Analysis (fits, classifiers)"),
        (KIND_ANNOTATION, "Annotation (catalogue cross-match)"),
        (KIND_SUMMARY, "Summary (LLM)"),
        (KIND_ARCHIVE, "Archive check"),
        (KIND_FACILITY, "Facility API"),
        (KIND_BROKER, "Alert broker"),
        (KIND_SHARING, "Sharing (TNS, Hermes)"),
        (KIND_GENERIC, "Generic"),
    )

    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_GENERIC)
    description = models.TextField(blank=True, default="")
    base_url = models.URLField(
        max_length=500, blank=True, default="",
        help_text="Endpoint the runner posts to; blank for in-process services.",
    )
    credential = models.ForeignKey(
        EncryptedCredential, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="external_services",
    )
    enabled = models.BooleanField(default=True)
    default_params = JSONTextField(default=dict, help_text="JSON merged under every run's request payload.")
    max_runs_per_user_per_day = models.PositiveIntegerField(
        default=0, help_text="0 = unlimited.",
    )
    groups = models.ManyToManyField(
        Group, blank=True, related_name="external_services",
        help_text="Groups that may trigger runs; empty = every authenticated user.",
    )

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return "%s [%s]" % (self.name, self.slug)

    def visible_to(self, user) -> bool:
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser or not self.groups.exists():
            return True
        return self.groups.filter(pk__in=user.groups.values_list("pk", flat=True)).exists()


def _artifact_upload_to(instance, filename):
    return "service_runs/%s/%s" % (instance.uuid, filename)


class ExternalServiceRun(BaseModel):
    """One request to an :class:`ExternalService`. ``created_by`` is the requester."""

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )
    FINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    service = models.ForeignKey(ExternalService, on_delete=models.PROTECT, related_name="runs")
    # Common case: a run about one transient. Any other target goes through the
    # generic FK, and target_ref is a free-form reference (a name, a URL, an
    # external id) for services that have no row in our database.
    transient = models.ForeignKey(
        Transient, null=True, blank=True, on_delete=models.SET_NULL, related_name="service_runs",
    )
    target_content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_content_type", "target_object_id")
    target_ref = models.CharField(max_length=255, blank=True, default="")

    request_payload = JSONTextField(default=dict)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    result = JSONTextField(default=dict)
    error = models.TextField(blank=True, default="")
    artifact_file = models.FileField(upload_to=_artifact_upload_to, blank=True, null=True, max_length=500)
    artifact_url = models.URLField(max_length=1000, blank=True, default="")
    external_id = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Identifier of this job at the remote service, if it gives one.",
    )
    attempts = models.PositiveIntegerField(default=0)
    # sha256 of the per-run callback token; the token itself is shown once at creation.
    callback_token_hash = models.CharField(max_length=64, blank=True, default="", editable=False)

    class Meta:
        ordering = ("-created_date",)
        indexes = [
            models.Index(fields=["service", "status"], name="yse_svcrun_service_status_idx"),
            models.Index(fields=["created_by", "created_date"], name="yse_svcrun_user_created_idx"),
        ]

    def __str__(self):
        return "%s run %s (%s)" % (self.service.slug, str(self.uuid)[:8], self.status)

    # -- convenience -------------------------------------------------------
    @property
    def is_finished(self) -> bool:
        return self.status in self.FINAL_STATUSES

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    @property
    def target_display(self) -> str:
        if self.transient_id:
            return self.transient.name
        if self.target_content_type_id and self.target_object_id:
            return "%s #%s" % (self.target_content_type.model, self.target_object_id)
        return self.target_ref or "-"

    @property
    def artifact_link(self) -> str:
        if self.artifact_url:
            return self.artifact_url
        if self.artifact_file:
            try:
                return self.artifact_file.url
            except ValueError:
                return ""
        return ""

    # -- state transitions (save immediately) ------------------------------
    def mark_running(self, external_id: str = ""):
        self.status = self.STATUS_RUNNING
        self.started_at = self.started_at or timezone.now()
        self.attempts = (self.attempts or 0) + 1
        if external_id:
            self.external_id = external_id
        self.save(update_fields=["status", "started_at", "attempts", "external_id", "modified_date"])

    def mark_succeeded(self, result=None, artifact_url: str = ""):
        self.status = self.STATUS_SUCCEEDED
        self.result = result if result is not None else {}
        self.error = ""
        if artifact_url:
            self.artifact_url = artifact_url
        self.finished_at = timezone.now()
        self.started_at = self.started_at or self.finished_at
        self.save(update_fields=["status", "result", "error", "artifact_url", "finished_at",
                                 "started_at", "modified_date"])

    def mark_failed(self, error: str, result=None):
        self.status = self.STATUS_FAILED
        self.error = (error or "")[:10000]
        if result is not None:
            self.result = result
        self.finished_at = timezone.now()
        self.started_at = self.started_at or self.finished_at
        self.save(update_fields=["status", "error", "result", "finished_at", "started_at", "modified_date"])

    def mark_cancelled(self, reason: str = ""):
        self.status = self.STATUS_CANCELLED
        if reason:
            self.error = reason[:10000]
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "error", "finished_at", "modified_date"])
