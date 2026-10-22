"""Night plans: versioned schedules for one classical night (#661).

The ``NightPlan`` and ``PlannedBlock`` tables of the decision-trail schema draft (Platform
Review, "Decision-trail schema" tab), implemented ahead of the Stage 5 review (Ryan,
2026-10-15). One deviation: the draft's ``PlannedBlock.standard`` FK to a ``StandardStar``
table is left out until that table exists; standards are ``kind=standard`` blocks named by
``target_name``. Plans and blocks never cascade away when a follow-up is deleted.

Each save from the planner is a new ``NightPlan`` version (``services/night_plans.py``); at most
one version per night is ``approved`` at a time.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import User
from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.fields import JSONTextField
from YSE_App.models.followup_models import TransientFollowup
from YSE_App.models.telescope_resource_models import ClassicalObservingDate
from YSE_App.models.transient_models import Transient

__all__ = ["NightPlan", "PlannedBlock"]


class NightPlan(BaseModel):
    """One version of a night's schedule; each iteration with the PI is a new version."""

    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        PROPOSED = "proposed", "Proposed"
        APPROVED = "approved", "Approved"
        EXECUTING = "executing", "Executing"
        CLOSED = "closed", "Closed"
        SUPERSEDED = "superseded", "Superseded"

    night = models.ForeignKey(ClassicalObservingDate, on_delete=models.CASCADE, related_name="plans")
    version = models.PositiveSmallIntegerField(help_text="1, 2, ... per night")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions",
        help_text="The version this one revised",
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT)
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="night_plans")
    approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_night_plans",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    solver = models.CharField(max_length=64, null=True, blank=True, help_text='e.g. "AstroScheduler 1.2.0"')
    solver_inputs = JSONTextField(null=True, help_text="Inputs and their hash, so the plan can be reproduced")
    notes = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ("night", "version")
        constraints = [
            models.UniqueConstraint(fields=["night", "version"], name="yse_nightplan_night_version"),
        ]

    def __str__(self):
        return "%s v%d (%s)" % (self.night, self.version, self.state)


class PlannedBlock(BaseModel):
    """One scheduled block in one plan version."""

    class Kind(models.TextChoices):
        SCIENCE = "science", "Science"
        STANDARD = "standard", "Standard"
        TELLURIC = "telluric", "Telluric"
        CALIBRATION = "calibration", "Calibration"

    # Block fields shared with the draft's ObservedBlock.
    followup = models.ForeignKey(
        TransientFollowup, null=True, blank=True, on_delete=models.SET_NULL, related_name="planned_blocks",
    )
    transient = models.ForeignKey(
        Transient, null=True, blank=True, on_delete=models.SET_NULL, related_name="planned_blocks",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    target_name = models.CharField(max_length=64, help_text="Name as shown at the telescope")
    ra = models.FloatField(null=True, blank=True, help_text="degrees")
    dec = models.FloatField(null=True, blank=True, help_text="degrees")
    mag = models.FloatField(null=True, blank=True, help_text="Expected or estimated magnitude")
    mag_band = models.CharField(max_length=8, null=True, blank=True)
    start_ut = models.DateTimeField(null=True, blank=True)
    end_ut = models.DateTimeField(null=True, blank=True)
    settings = JSONTextField(null=True, help_text="Per-block details: airmass, overheads, exposure source")
    comments = models.TextField(null=True, blank=True)

    # PlannedBlock's own fields.
    plan = models.ForeignKey(NightPlan, on_delete=models.CASCADE, related_name="blocks")
    order = models.PositiveSmallIntegerField(help_text="1, 2, ... within the plan")
    requested_sequence = JSONTextField(default=list, help_text="Planned exposures: [{channel, n, exptime_s}]")
    time_with_overhead_s = models.PositiveIntegerField(null=True, blank=True)
    setting_ut = models.DateTimeField(null=True, blank=True, help_text="When the target sets below the limit")
    wrap = models.CharField(max_length=8, null=True, blank=True, help_text="Azimuth wrap (Keck): N or S")
    warnings = models.TextField(null=True, blank=True)
    alternative_for = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="alternatives",
    )
    priority_at_plan = models.FloatField(null=True, blank=True, help_text="Follow-up priority when planned (1-5)")

    class Meta:
        ordering = ("plan", "order")
        constraints = [
            models.UniqueConstraint(fields=["plan", "order"], name="yse_plannedblock_plan_order"),
        ]

    def __str__(self):
        return "%s #%d %s" % (self.plan, self.order, self.target_name)


auditlog.register(NightPlan)
