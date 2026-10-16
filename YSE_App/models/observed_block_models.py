"""What happened at the telescope (#666): the ``ObservedBlock`` and ``OutcomeReason`` tables of
the decision-trail schema draft (Platform Review, "Decision-trail schema" tab).

An ``ObservedBlock`` is one target's outcome on one night, planned (``planned_block``) or added
at the telescope. ``OutcomeReason`` is a lookup table, so admins can add reasons without a
migration; the seed codes match AstroScheduler's Failed/Punted reasons. Blocks never cascade
away when a follow-up is deleted.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from YSE_App.models.allocation_models import FacilityRequest
from YSE_App.models.base import BaseModel
from YSE_App.models.fields import JSONTextField
from YSE_App.models.followup_models import TransientFollowup
from YSE_App.models.night_plan_models import PlannedBlock
from YSE_App.models.telescope_resource_models import ClassicalObservingDate
from YSE_App.models.transient_models import Transient

__all__ = ["OutcomeReason", "ObservedBlock"]


class OutcomeReason(BaseModel):
    """Why something did not happen or changed (the draft's controlled vocabulary)."""

    class Category(models.TextChoices):
        WEATHER = "weather", "Weather"
        INSTRUMENT = "instrument", "Instrument"
        TELESCOPE = "telescope", "Telescope"
        TIME = "time", "Time"
        PRIORITY = "priority", "Priority"
        TARGET = "target", "Target"
        DATA = "data", "Data"
        OTHER = "other", "Other"

    code = models.SlugField(max_length=32, unique=True, help_text="Stable code")
    label = models.CharField(max_length=64, help_text="Display text")
    category = models.CharField(max_length=16, choices=Category.choices)
    applies_to = JSONTextField(default=list, help_text="Where it may be used: subset of [block, consideration, event]")
    active = models.BooleanField(default=True, help_text="Retired reasons stay for history")

    class Meta:
        ordering = ("label",)

    def __str__(self):
        return self.label


class ObservedBlock(BaseModel):
    """What actually happened for one target on a night (the observing-log row)."""

    class Kind(models.TextChoices):
        SCIENCE = "science", "Science"
        STANDARD = "standard", "Standard"
        TELLURIC = "telluric", "Telluric"
        CALIBRATION = "calibration", "Calibration"

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    class Source(models.TextChoices):
        TONIGHT = "tonight", "Tonight page"
        SLACK = "slack", "Slack"
        ASTROSCHEDULER = "astroscheduler", "AstroScheduler"
        IMPORT = "import", "Import"
        ADMIN = "admin", "Admin"

    # Block fields shared with PlannedBlock.
    followup = models.ForeignKey(
        TransientFollowup, null=True, blank=True, on_delete=models.SET_NULL, related_name="observed_blocks",
    )
    transient = models.ForeignKey(
        Transient, null=True, blank=True, on_delete=models.SET_NULL, related_name="observed_blocks",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    target_name = models.CharField(max_length=64, help_text="Name as shown at the telescope")
    ra = models.FloatField(null=True, blank=True, help_text="degrees")
    dec = models.FloatField(null=True, blank=True, help_text="degrees")
    mag = models.FloatField(null=True, blank=True, help_text="Expected or estimated magnitude")
    mag_band = models.CharField(max_length=8, null=True, blank=True)
    start_ut = models.DateTimeField(null=True, blank=True)
    end_ut = models.DateTimeField(null=True, blank=True)
    settings = JSONTextField(null=True, help_text="Per-block details")
    comments = models.TextField(null=True, blank=True)

    # ObservedBlock's own fields.
    night = models.ForeignKey(ClassicalObservingDate, on_delete=models.PROTECT, related_name="observed_blocks")
    planned_block = models.ForeignKey(
        PlannedBlock, null=True, blank=True, on_delete=models.SET_NULL, related_name="observed_blocks",
        help_text="The plan entry it executed; empty = added at the telescope",
    )
    outcome = models.CharField(max_length=8, choices=Outcome.choices)
    reason = models.ForeignKey(
        OutcomeReason, null=True, blank=True, on_delete=models.PROTECT, related_name="observed_blocks",
        help_text="Required unless the outcome is success",
    )
    achieved_sn = models.FloatField(null=True, blank=True)
    seeing = models.FloatField(null=True, blank=True, help_text="arcsec")
    facility_request = models.ForeignKey(
        FacilityRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="observed_blocks",
    )
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="recorded_observed_blocks")
    source = models.CharField(max_length=16, choices=Source.choices)

    class Meta:
        ordering = ("night", "end_ut", "id")

    def clean(self):
        if self.outcome != self.Outcome.SUCCESS and self.reason_id is None:
            raise ValidationError({"reason": "A reason is required unless the outcome is success."})

    def __str__(self):
        return "%s %s: %s" % (self.night, self.target_name, self.outcome)


auditlog.register(ObservedBlock)
