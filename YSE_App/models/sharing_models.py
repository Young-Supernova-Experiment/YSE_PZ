"""Sharing services: reporting transients to TNS (and later Hermes) (#324, #325).

``SharingService`` is one configured reporting channel: the bot credential
(an :class:`EncryptedCredential` holding ``tns_bot_id``, ``tns_bot_name`` and
``tns_api_key``), the TNS reporting group, default coauthors, the instruments
and observation groups (streams / surveys) whose data may be reported, who may
use it and whether it talks to the sandbox. ``SharingSubmission`` records one
report: the exact payload sent, the TNS report id, the reply, the outcome and
who asked for it. ``AutoPublisher`` is a per-group rule that queues
submissions for qualifying transients without a human clicking.

The shape follows SkyPortal's sharing services and submission queue
(BSD-3-Clause) in spirit; no code is copied from that project.
"""

from __future__ import annotations

import re

from auditlog.registry import auditlog
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone

from YSE_App.models.base import BaseModel
from YSE_App.models.credential_models import EncryptedCredential
from YSE_App.models.enum_models import ObservationGroup
from YSE_App.models.fields import JSONTextField
from YSE_App.models.instrument_models import Instrument
from YSE_App.models.job_models import Job
from YSE_App.models.transient_models import AlternateTransientNames, Transient

__all__ = ["SharingService", "SharingSubmission", "AutoPublisher", "TNS_NAME_RE", "is_tns_name"]

# A TNS designation: 2026abc (no AT/SN prefix); YSE-PZ stores the bare designation.
TNS_NAME_RE = re.compile(r"^20\d\d[a-zA-Z]{1,5}$")


def is_tns_name(name) -> bool:
    return bool(name) and bool(TNS_NAME_RE.match(str(name).strip()))


class SharingService(BaseModel):
    KIND_TNS = "tns"
    KIND_HERMES = "hermes"
    KIND_CHOICES = ((KIND_TNS, "TNS (Transient Name Server)"), (KIND_HERMES, "Hermes / SCiMMA"))

    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_TNS)
    description = models.TextField(blank=True, default="")
    credential = models.ForeignKey(
        EncryptedCredential, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sharing_services",
        help_text="TNS: JSON secret with tns_bot_id, tns_bot_name, tns_api_key. Hermes: token.",
    )
    tns_group_id = models.CharField(
        max_length=16, blank=True, default="",
        help_text="TNS reporting group id (the 'groupid' of AT and classification reports).",
    )
    tns_group_name = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Reporting group name shown after the coauthors, e.g. 'YSE'.",
    )
    default_coauthors = models.TextField(
        blank=True, default="",
        help_text="Default reporter / classifier list, e.g. 'R. J. Foley (UCSC), D. O. Jones (Hawaii)'.",
    )
    default_remarks = models.TextField(blank=True, default="")
    allowed_instruments = models.ManyToManyField(
        Instrument, blank=True, related_name="sharing_services",
        help_text="Instruments whose photometry/spectra may be reported; empty = every instrument.",
    )
    allowed_obs_groups = models.ManyToManyField(
        ObservationGroup, blank=True, related_name="sharing_services",
        help_text="Observation groups (streams / surveys) whose data may be reported; empty = all.",
    )
    groups = models.ManyToManyField(
        Group, blank=True, related_name="sharing_services",
        help_text="Groups that may report through this service; empty = every authenticated user. Staff always.",
    )
    enabled = models.BooleanField(default=True)
    testing = models.BooleanField(
        default=True,
        help_text="Send to the TNS sandbox (sandbox.wis-tns.org). Switch off for production reports.",
    )
    hermes_topic = models.CharField(max_length=128, blank=True, default="")
    config = JSONTextField(
        default=dict,
        help_text="Optional JSON: instrument_ids {YSE instrument name: TNS id}, filter_ids {band name: TNS id}, "
                  "proprietary_period_days, rename_transient (bool), at_type.",
    )

    class Meta:
        ordering = ("name",)
        verbose_name = "sharing service"

    def __str__(self):
        return "%s [%s%s]" % (self.name, self.kind, ", sandbox" if self.testing else "")

    # -- access ------------------------------------------------------------
    def visible_to(self, user) -> bool:
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        if not self.groups.exists():
            return True
        return self.groups.filter(pk__in=user.groups.values_list("pk", flat=True)).exists()

    @classmethod
    def for_user(cls, user, kind=None):
        qs = cls.objects.filter(enabled=True).prefetch_related("groups")
        if kind:
            qs = qs.filter(kind=kind)
        return [s for s in qs if s.visible_to(user)]

    # -- configuration helpers --------------------------------------------
    def config_value(self, key, default=None):
        cfg = self.config if isinstance(self.config, dict) else {}
        value = cfg.get(key)
        return default if value is None else value

    @property
    def rename_transient(self) -> bool:
        from django.conf import settings

        return bool(self.config_value("rename_transient", getattr(settings, "SHARING_RENAME_ON_ACCEPT", True)))

    @property
    def has_credential(self) -> bool:
        return bool(self.credential_id and self.credential.has_secret and self.credential.is_active)

    def api_base_url(self) -> str:
        """The TNS API root: sandbox while ``testing`` is on, production otherwise."""
        from django.conf import settings

        if self.testing:
            return getattr(settings, "TNS_SANDBOX_API_URL", "https://sandbox.wis-tns.org/api").rstrip("/")
        return getattr(settings, "TNS_API_URL", "https://www.wis-tns.org/api").rstrip("/")

    def site_url(self) -> str:
        return "https://sandbox.wis-tns.org" if self.testing else "https://www.wis-tns.org"

    def object_url(self, tns_name: str) -> str:
        return "%s/object/%s" % (self.site_url(), tns_name)

    def instrument_allowed(self, instrument) -> bool:
        if instrument is None:
            return False
        if not self.pk:
            return True
        allowed = self.allowed_instruments.values_list("pk", flat=True)
        return not allowed or instrument.pk in set(allowed)

    def allowed_instrument_ids(self):
        return set(self.allowed_instruments.values_list("pk", flat=True)) if self.pk else set()

    def allowed_obs_group_ids(self):
        return set(self.allowed_obs_groups.values_list("pk", flat=True)) if self.pk else set()

    def reporter_string(self, coauthors: str = "") -> str:
        """``coauthors`` (or the defaults) followed by 'on behalf of <group>' when a group name is set."""
        authors = (coauthors or self.default_coauthors or "").strip().rstrip(",")
        if self.tns_group_name:
            suffix = "on behalf of %s" % self.tns_group_name
            return "%s, %s" % (authors, suffix) if authors else suffix
        return authors


class SharingSubmission(BaseModel):
    """One report sent (or to be sent) through a :class:`SharingService`."""

    KIND_DISCOVERY = "discovery"
    KIND_CLASSIFICATION = "classification"
    KIND_HERMES = "hermes"
    KIND_CHOICES = (
        (KIND_DISCOVERY, "TNS discovery (AT) report"),
        (KIND_CLASSIFICATION, "TNS classification report"),
        (KIND_HERMES, "Hermes message"),
    )

    STATUS_PENDING = "pending"
    STATUS_SUBMITTED = "submitted"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SUBMITTED, "Submitted, awaiting reply"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_FAILED, "Failed"),
    )
    FINAL_STATUSES = (STATUS_ACCEPTED, STATUS_REJECTED, STATUS_FAILED)
    RETRYABLE_STATUSES = (STATUS_REJECTED, STATUS_FAILED)

    service = models.ForeignKey(SharingService, on_delete=models.PROTECT, related_name="submissions")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="sharing_submissions")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_DISCOVERY)
    payload = JSONTextField(default=dict, help_text="The report exactly as sent (bulk-report 'data').")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    external_id = models.CharField(
        max_length=64, blank=True, default="", help_text="TNS report_id (or Hermes message uuid).",
    )
    tns_name = models.CharField(
        max_length=64, blank=True, default="", help_text="Object name TNS assigned or confirmed.",
    )
    response = JSONTextField(default=dict, help_text="Last reply from the service.")
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    job = models.ForeignKey(
        Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="sharing_submissions",
        help_text="The queue job currently handling this submission.",
    )
    auto_publisher = models.ForeignKey(
        "AutoPublisher", null=True, blank=True, on_delete=models.SET_NULL, related_name="submissions",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_date",)
        indexes = [
            models.Index(fields=["service", "status"], name="yse_sharesub_svc_status_idx"),
            models.Index(fields=["transient", "kind"], name="yse_sharesub_trans_kind_idx"),
        ]

    def __str__(self):
        return "%s %s of %s (%s)" % (self.service.slug, self.kind, self.transient.name, self.status)

    @property
    def is_final(self) -> bool:
        return self.status in self.FINAL_STATUSES

    @property
    def can_retry(self) -> bool:
        return self.status in self.RETRYABLE_STATUSES

    @property
    def tns_object_url(self) -> str:
        return self.service.object_url(self.tns_name) if self.tns_name else ""

    @property
    def report_url(self) -> str:
        if self.external_id and self.service.kind == SharingService.KIND_TNS:
            return "%s/bulk-report/%s" % (self.service.site_url(), self.external_id)
        return ""

    def error_summary(self, length: int = 160) -> str:
        text = (self.error or "").strip().splitlines()
        text = text[0] if text else ""
        return text if len(text) <= length else text[: length - 3] + "..."

    # -- transitions (save immediately) ------------------------------------
    def mark_submitted(self, report_id, response=None):
        self.status = self.STATUS_SUBMITTED
        self.external_id = str(report_id or "")
        self.response = response if response is not None else {}
        self.error = ""
        self.submitted_at = timezone.now()
        self.attempts = (self.attempts or 0) + 1
        self.save(update_fields=["status", "external_id", "response", "error", "submitted_at",
                                 "attempts", "modified_date"])

    def mark_accepted(self, tns_name: str, response=None):
        self.status = self.STATUS_ACCEPTED
        self.tns_name = tns_name or self.tns_name
        if response is not None:
            self.response = response
        self.error = ""
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "tns_name", "response", "error", "finished_at", "modified_date"])

    def mark_rejected(self, error: str, response=None):
        self.status = self.STATUS_REJECTED
        self.error = (error or "rejected")[:10000]
        if response is not None:
            self.response = response
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "error", "response", "finished_at", "modified_date"])

    def mark_failed(self, error: str, response=None):
        self.status = self.STATUS_FAILED
        self.error = (error or "failed")[:10000]
        if response is not None:
            self.response = response
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "error", "response", "finished_at", "modified_date"])

    def reset_for_retry(self):
        self.status = self.STATUS_PENDING
        self.error = ""
        self.response = {}
        self.external_id = ""
        self.finished_at = None
        self.job = None
        self.save(update_fields=["status", "error", "response", "external_id", "finished_at", "job",
                                 "modified_date"])


class AutoPublisher(BaseModel):
    """A per-group rule that queues submissions for qualifying transients (#325).

    ``criteria`` is a JSON object; every present key must hold::

        {"statuses": ["Following", "Interesting"],   # Transient.status names
         "classes": ["SN Ia"],                      # best_spec_class names (classification rules)
         "min_detections": 2,                       # PhotStat num_det_global
         "instruments": ["GPC1"],                   # a detection from one of these instruments
         "obs_groups": ["YSE"],                     # Transient.obs_group names
         "max_age_days": 30,                        # disc_date (or created) within N days
         "tags": ["YSE"]}                           # any of these TransientTag names

    Discovery rules only fire for transients without a TNS-style name;
    classification rules only for transients with one and a ``best_spec_class``.
    """

    service = models.ForeignKey(SharingService, on_delete=models.CASCADE, related_name="auto_publishers")
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.CASCADE, related_name="auto_publishers",
        help_text="The group this rule belongs to (its members are notified of the outcomes).",
    )
    name = models.CharField(max_length=128)
    kind = models.CharField(
        max_length=16, choices=SharingSubmission.KIND_CHOICES[:2], default=SharingSubmission.KIND_DISCOVERY,
    )
    criteria = JSONTextField(default=dict)
    tns_enabled = models.BooleanField(default=True, help_text="Queue TNS reports.")
    hermes_enabled = models.BooleanField(default=False, help_text="Also publish to Hermes (not wired yet).")
    enabled = models.BooleanField(default=True)
    coauthors = models.TextField(blank=True, default="", help_text="Overrides the service default when set.")
    remarks = models.TextField(blank=True, default="")
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("service", "name")
        verbose_name = "auto-publisher rule"

    def __str__(self):
        return "%s (%s, %s)" % (self.name, self.service.slug, self.kind)

    def criteria_dict(self) -> dict:
        return self.criteria if isinstance(self.criteria, dict) else {}

    def candidate_queryset(self):
        """Transients passing the cheap (database) part of the criteria."""
        from django.db.models import Q

        crit = self.criteria_dict()
        qs = Transient.objects.all()
        if crit.get("statuses"):
            qs = qs.filter(status__name__in=list(crit["statuses"]))
        if crit.get("obs_groups"):
            qs = qs.filter(obs_group__name__in=list(crit["obs_groups"]))
        if crit.get("tags"):
            qs = qs.filter(tags__name__in=list(crit["tags"]))
        if crit.get("classes"):
            qs = qs.filter(best_spec_class__name__in=list(crit["classes"]))
        if crit.get("instruments"):
            qs = qs.filter(transientphotometry__instrument__name__in=list(crit["instruments"]))
        if crit.get("max_age_days"):
            since = timezone.now() - timezone.timedelta(days=float(crit["max_age_days"]))
            qs = qs.filter(Q(disc_date__gte=since) | Q(disc_date__isnull=True, created_date__gte=since))
        if self.kind == SharingSubmission.KIND_CLASSIFICATION:
            qs = qs.filter(best_spec_class__isnull=False)
        return qs.distinct().order_by("-created_date")

    def matches(self, transient) -> bool:
        """The full rule for one transient (database criteria plus the per-row checks)."""
        crit = self.criteria_dict()
        has_tns = is_tns_name(transient.name) or AlternateTransientNames.objects.filter(
            transient=transient, name__regex=r"^20[0-9]{2}[a-zA-Z]{1,5}$").exists()
        if self.kind == SharingSubmission.KIND_DISCOVERY and has_tns:
            return False
        if self.kind == SharingSubmission.KIND_CLASSIFICATION and (not has_tns or not transient.best_spec_class_id):
            return False
        if crit.get("statuses") and (transient.status is None or transient.status.name not in crit["statuses"]):
            return False
        if crit.get("obs_groups") and (transient.obs_group is None or transient.obs_group.name not in crit["obs_groups"]):
            return False
        if crit.get("classes") and (transient.best_spec_class is None or transient.best_spec_class.name not in crit["classes"]):
            return False
        if crit.get("tags") and not transient.tags.filter(name__in=list(crit["tags"])).exists():
            return False
        if crit.get("instruments") and not transient.transientphotometry_set.filter(
                instrument__name__in=list(crit["instruments"])).exists():
            return False
        if crit.get("max_age_days"):
            since = timezone.now() - timezone.timedelta(days=float(crit["max_age_days"]))
            anchor = transient.disc_date or transient.created_date
            if anchor is None or anchor < since:
                return False
        min_det = crit.get("min_detections")
        if min_det:
            from YSE_App.services.photstat import stat_for_transient

            stat = stat_for_transient(transient.pk)
            if stat is None or (stat.num_det_global or 0) < int(min_det):
                return False
        return True

    def qualifying_transients(self, limit: int = 200):
        """Dry run: the transients this rule would report right now."""
        out = []
        for transient in self.candidate_queryset().select_related("status", "obs_group", "best_spec_class")[: limit * 3]:
            if self.matches(transient):
                out.append(transient)
                if len(out) >= limit:
                    break
        return out


auditlog.register(SharingService)
auditlog.register(AutoPublisher)
