"""Transient interests: "I am working on a paper about this transient" (#288: #289, #290).

A :class:`TransientInterest` is one collaborator's registered intention to
publish on a transient, one row per planned paper (``title``). Registering
one posts an automatic comment through the ordinary comment model
(``YSE_App.services.comments.create_transient_comment``), so the intention
shows in the comments panel, reaches Slack and the mention/notification flow
like any other comment, and tells the other collaborators who is working on
what before they spend telescope time or writing effort on the same object.

The idea follows SkyPortal's ``SourceInterest`` (BSD-3-Clause); no code is
copied. ``SourceInterest`` is kept as an alias of the model for readers who
know it under that name.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import Group, User
from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.transient_models import Transient

__all__ = ["TransientInterest", "SourceInterest"]


class TransientInterest(BaseModel):
    """One user's intention to publish on a transient (one row per planned paper)."""

    STATUS_PLANNED = "planned"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_SUBMITTED = "submitted"
    STATUS_PUBLISHED = "published"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = (
        (STATUS_PLANNED, "Planned"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_WITHDRAWN, "Withdrawn"),
    )
    # Interests that still claim the transient (shown by default on the detail page).
    OPEN_STATUSES = (STATUS_PLANNED, STATUS_IN_PROGRESS, STATUS_SUBMITTED)
    # Everything but withdrawn is listed; withdrawn rows are hidden unless asked for.
    VISIBLE_STATUSES = OPEN_STATUSES + (STATUS_PUBLISHED,)

    ROLE_LEAD = "lead"
    ROLE_COAUTHOR = "coauthor"
    ROLE_OBSERVER = "observer"
    ROLE_CHOICES = (
        (ROLE_LEAD, "Lead author"),
        (ROLE_COAUTHOR, "Co-author"),
        (ROLE_OBSERVER, "Observer / data contributor"),
    )

    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="interests")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transient_interests")
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL, related_name="transient_interests",
        help_text="Collaboration the paper is written under; also the audience of the automatic comment.",
    )
    title = models.CharField(max_length=200, help_text="Planned paper or project, e.g. 'Nebular spectroscopy of 2026abc'.")
    description = models.TextField(blank=True, default="", help_text="Short description: scope, data needed, timeline.")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_LEAD)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PLANNED, db_index=True)
    doi = models.CharField(max_length=128, blank=True, default="", help_text="DOI or arXiv id once published.")

    class Meta:
        ordering = ("-created_date", "-id")
        unique_together = (("transient", "user", "title"),)
        indexes = [
            models.Index(fields=["transient", "status"], name="yse_interest_transient_status"),
            models.Index(fields=["user", "status"], name="yse_interest_user_status"),
        ]

    def __str__(self):
        return "%s: %s (%s, %s)" % (self.transient.name, self.title, self.user.username, self.status)

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_withdrawn(self):
        return self.status == self.STATUS_WITHDRAWN

    @property
    def status_label(self):
        return dict(self.STATUS_CHOICES).get(self.status, self.status)

    @property
    def role_label(self):
        return dict(self.ROLE_CHOICES).get(self.role, self.role)

    @property
    def user_display(self):
        full = ("%s %s" % (self.user.first_name, self.user.last_name)).strip()
        return full or self.user.username


# Name used by SkyPortal and in the roadmap issues.
SourceInterest = TransientInterest

auditlog.register(TransientInterest)
