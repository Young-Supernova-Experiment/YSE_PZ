"""Telescope allocations and the facility requests submitted against them (#303, #298).

An :class:`Allocation` is awarded time on one telescope (optionally one
instrument): who owns it (PI, audience groups), how much time it holds, when it
is valid, which facility API it talks to (``facility`` slug, see
:mod:`YSE_App.facilities`) with which encrypted credentials (#264) and which
request parameters it fills in by default. A :class:`FacilityRequest` is one
observation request for one transient submitted against an allocation; the
transport (the HTTP call, the email, the Slack post) is recorded as an
:class:`~YSE_App.models.external_service_models.ExternalServiceRun` (#265)
executed through the background job queue (#263).

The shape follows SkyPortal's ``Allocation`` / ``FollowupRequest`` /
``FacilityTransaction`` (BSD-3-Clause) in spirit; no code is copied.
"""

from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import Group, User
from django.db import models
from django.utils import timezone

from YSE_App.models.base import BaseModel
from YSE_App.models.credential_models import EncryptedCredential
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.fields import JSONTextField
from YSE_App.models.followup_models import TransientFollowup
from YSE_App.models.instrument_models import Instrument
from YSE_App.models.principal_investigator_models import PrincipalInvestigator
from YSE_App.models.telescope_models import Telescope
from YSE_App.models.transient_models import Transient


class Allocation(BaseModel):
    """Awarded time on a telescope, bound to a facility API and its credentials."""

    name = models.CharField(max_length=128, help_text="Label, e.g. 'LCO 2026B YSE spectroscopy'.")
    telescope = models.ForeignKey(Telescope, on_delete=models.CASCADE, related_name="allocations")
    instrument = models.ForeignKey(
        Instrument, null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations",
    )
    principal_investigator = models.ForeignKey(
        PrincipalInvestigator, null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations",
    )
    groups = models.ManyToManyField(
        Group, blank=True, related_name="allocations",
        help_text="Groups whose members may submit requests; empty = every authenticated user.",
    )
    proposal_id = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Program / proposal identifier at the facility (e.g. an LCO proposal code).",
    )
    hours_allocated = models.FloatField(default=0.0)
    hours_used = models.FloatField(default=0.0)
    start_date = models.DateTimeField(help_text="Semester start.")
    end_date = models.DateTimeField(help_text="Semester end.")
    facility = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Facility API slug (see YSE_App.facilities); blank = manual allocation, no API.",
    )
    credential = models.ForeignKey(
        EncryptedCredential, null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations",
        help_text="Encrypted credentials the facility API uses (#264).",
    )
    endpoint_url = models.URLField(
        max_length=500, blank=True, default="",
        help_text="Where requests are sent when the facility needs an endpoint (GENERIC API mode).",
    )
    default_request_params = JSONTextField(
        default=dict, help_text="JSON merged under every request's parameters.",
    )
    service = models.OneToOneField(
        ExternalService, null=True, blank=True, on_delete=models.SET_NULL, related_name="allocation",
        editable=False, help_text="External service that records this allocation's runs (created on demand).",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-end_date", "name")

    def __str__(self):
        return "%s (%s)" % (self.name, self.telescope.name)

    # -- derived ------------------------------------------------------------
    @property
    def hours_remaining(self) -> float:
        return round((self.hours_allocated or 0.0) - (self.hours_used or 0.0), 3)

    @property
    def percent_used(self) -> int:
        if not self.hours_allocated:
            return 0
        return int(max(0, min(100, round(100.0 * (self.hours_used or 0.0) / self.hours_allocated))))

    @property
    def has_credential(self) -> bool:
        return bool(self.credential_id and self.credential.has_secret)

    @property
    def has_facility(self) -> bool:
        return bool(self.facility)

    def is_current(self, now=None) -> bool:
        now = now or timezone.now()
        return self.start_date <= now <= self.end_date

    def usable_by(self, user: Optional[User], now=None) -> bool:
        """Active, in its date range and open to ``user`` (staff, or a member of a group; no groups = everyone)."""
        if user is None or not user.is_authenticated or not self.is_active or not self.is_current(now):
            return False
        if user.is_staff or user.is_superuser:
            return True
        group_ids = set(self.groups.values_list("pk", flat=True))
        if not group_ids:
            return True
        return user.groups.filter(pk__in=group_ids).exists()

    def secret(self, touch: bool = False) -> dict:
        """Decrypted credential payload, ``{}`` when no credential is bound."""
        if not self.credential_id:
            return {}
        return self.credential.get_secret(touch=touch)

    def service_slug(self) -> str:
        return "allocation-%d" % self.pk


class FacilityRequest(BaseModel):
    """One observation request for a transient, submitted against an allocation."""

    STATE_DRAFT = "draft"
    STATE_QUEUED = "queued"
    STATE_SUBMITTED = "submitted"
    STATE_ACCEPTED = "accepted"
    STATE_RUNNING = "running"
    STATE_COMPLETE = "complete"
    STATE_FAILED = "failed"
    STATE_CANCELLED = "cancelled"
    STATE_CHOICES = (
        (STATE_DRAFT, "Draft"),
        (STATE_QUEUED, "Queued"),
        (STATE_SUBMITTED, "Submitted"),
        (STATE_ACCEPTED, "Accepted"),
        (STATE_RUNNING, "Running"),
        (STATE_COMPLETE, "Complete"),
        (STATE_FAILED, "Failed"),
        (STATE_CANCELLED, "Cancelled"),
    )
    OPEN_STATES = (STATE_SUBMITTED, STATE_ACCEPTED, STATE_RUNNING)
    FINAL_STATES = (STATE_COMPLETE, STATE_FAILED, STATE_CANCELLED)

    allocation = models.ForeignKey(Allocation, on_delete=models.PROTECT, related_name="requests")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="facility_requests")
    followup = models.ForeignKey(
        TransientFollowup, null=True, blank=True, on_delete=models.SET_NULL, related_name="facility_requests",
    )
    run = models.OneToOneField(
        ExternalServiceRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="facility_request",
    )
    payload = JSONTextField(default=dict, help_text="Validated request parameters.")
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=STATE_DRAFT, db_index=True)
    state_detail = models.TextField(blank=True, default="")
    external_id = models.CharField(max_length=255, blank=True, default="")
    external_url = models.URLField(max_length=1000, blank=True, default="")
    submitted_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="facility_requests")
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_polled = models.DateTimeField(null=True, blank=True)
    hours_charged = models.FloatField(
        default=0.0, help_text="Hours this request costs the allocation; charged once when it completes.",
    )
    charged_at = models.DateTimeField(null=True, blank=True, editable=False)
    log = JSONTextField(default=list, help_text="Chronological list of {at, event, detail}.")
    # Queue bookkeeping (#300) and forced-photometry results (#301).
    KIND_OBSERVATION = "observation"
    KIND_PHOTOMETRY = "photometry"
    KIND_CHOICES = ((KIND_OBSERVATION, "Observation"), (KIND_PHOTOMETRY, "Forced photometry"))
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_OBSERVATION)
    attempts = models.PositiveIntegerField(default=0, help_text="Submission attempts made by the job runner.")
    results_ingested_at = models.DateTimeField(null=True, blank=True, editable=False)
    n_results = models.PositiveIntegerField(default=0, help_text="Photometry points ingested from the facility.")

    class Meta:
        ordering = ("-created_date",)
        indexes = [
            models.Index(fields=["allocation", "state"], name="yse_facreq_alloc_state_idx"),
            models.Index(fields=["transient", "state"], name="yse_facreq_transient_state_idx"),
        ]

    def __str__(self):
        return "%s -> %s [%s]" % (self.transient.name, self.allocation.name, self.state)

    @property
    def facility(self) -> str:
        return self.allocation.facility

    @property
    def is_open(self) -> bool:
        return self.state in self.OPEN_STATES

    @property
    def is_final(self) -> bool:
        return self.state in self.FINAL_STATES

    @property
    def is_photometry(self) -> bool:
        return self.kind == self.KIND_PHOTOMETRY

    @property
    def can_retry(self) -> bool:
        """A failed or cancelled request may be resubmitted as a new request; this one is history."""
        return self.state in (self.STATE_FAILED, self.STATE_CANCELLED)

    def add_log(self, event: str, detail: str = "", save: bool = False) -> None:
        entries = list(self.log or [])
        entries.append({"at": timezone.now().isoformat(), "event": event, "detail": (detail or "")[:2000]})
        self.log = entries[-200:]
        if save:
            self.save(update_fields=["log", "modified_date"])

    def set_state(self, state: str, detail: str = "", *, external_id: str = "", external_url: str = "",
                  save: bool = True) -> None:
        if state not in dict(self.STATE_CHOICES):
            raise ValueError("unknown facility request state %r" % state)
        self.state = state
        self.state_detail = (detail or "")[:5000]
        if external_id:
            self.external_id = external_id[:255]
        if external_url:
            self.external_url = external_url[:1000]
        if state == self.STATE_SUBMITTED and not self.submitted_at:
            self.submitted_at = timezone.now()
        self.add_log(state, detail)
        if save:
            self.save(update_fields=["state", "state_detail", "external_id", "external_url",
                                     "submitted_at", "log", "modified_date"])
