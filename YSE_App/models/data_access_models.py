"""Data access requests for group-restricted photometry and spectra (#291: #292, #293).

Visibility of a ``TransientPhotometry`` / ``TransientSpectrum`` row is its
``groups`` M2M: an empty set is public, otherwise a user must share a group
(``YSE_App.data.PhotometryService`` / ``SpectraService``,
``YSE_App.services.visibility``). The detail page hides rows the user cannot
see; a :class:`DataAccessRequest` lets that user ask the owning group for
access and gives the owners an accept / decline workflow with notifications.

**Grant mechanism.** Accepting a request adds the requester's chosen
collaboration group (``target_group``) to the ``groups`` of the restricted
datasets of that kind on the transient that ``owner_group`` owns. A
per-request grant table was considered and rejected as the *less* safe
option: every access check in the application (the two data services, the
visibility helpers, the DRF ``get_queryset`` overrides, the plot cache token
keyed by group names, the export code) would need a second permission source,
and a missed one would leak data or hide it inconsistently. Adding a group is
the mechanism the checks already enforce; the request row records which
datasets were granted (``granted_dataset_ids``) and to which group, so the
grant is auditable and reversible, and the ``Public`` group is never a valid
target so a grant cannot make data world-readable by accident.

Who may decide (#447): the data's PIs (``services.data_pis.pis_for``, with
the allocation fallback) and the deputies they designated
(``DataSharingDelegate``); staff only as a logged administrator override
(``decided_as``). Members and owners of ``owner_group`` do not decide. Follows
SkyPortal's ``DataAccessRequest`` (BSD-3-Clause) in spirit; no code is copied.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import Group, User
from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient

__all__ = ["DataAccessRequest"]


class DataAccessRequest(BaseModel):
    KIND_PHOTOMETRY = "photometry"
    KIND_SPECTRUM = "spectrum"
    KIND_CHOICES = (
        (KIND_PHOTOMETRY, "Photometry"),
        (KIND_SPECTRUM, "Spectrum"),
    )

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
    )

    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name="data_access_requests")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="data_access_requests")
    dataset_kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    dataset_id = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="One TransientPhotometry / TransientSpectrum id; empty = every restricted dataset of this kind "
                  "on the transient that the owner group holds.",
    )
    owner_group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="data_access_requests_owned",
        help_text="Group the restricted data is restricted to; the data's PIs and their deputies decide.",
    )
    target_group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="data_access_requests_granted",
        help_text="Requester's collaboration group that gains access on acceptance (never Public).",
    )
    message = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    decided_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="data_access_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    # In what capacity ``decided_by`` decided (#447): as a PI, as a PI's deputy, or as an administrator
    # (the logged staff override). Nullable: rows decided before #447 have none.
    DECIDED_AS_PI = "pi"
    DECIDED_AS_DEPUTY = "deputy"
    DECIDED_AS_STAFF = "staff"
    DECIDED_AS_CHOICES = (
        (DECIDED_AS_PI, "PI"),
        (DECIDED_AS_DEPUTY, "PI's deputy"),
        (DECIDED_AS_STAFF, "Administrator override"),
    )
    decided_as = models.CharField(max_length=8, choices=DECIDED_AS_CHOICES, null=True, blank=True)
    note = models.TextField(blank=True, default="", help_text="Decision note shown to the requester.")
    granted_dataset_ids = JSONTextField(
        null=True, blank=True,
        help_text="Ids of the datasets the target group was added to on acceptance (for audit / revocation).",
    )

    class Meta:
        ordering = ("-created_date", "-id")
        indexes = [
            models.Index(fields=["owner_group", "status"], name="yse_dar_owner_status"),
            models.Index(fields=["requester", "status"], name="yse_dar_requester_status"),
            models.Index(fields=["transient", "dataset_kind"], name="yse_dar_transient_kind"),
        ]

    def __str__(self):
        return "DataAccessRequest %s: %s asks %s for %s on %s (%s)" % (
            self.pk, self.requester.username, self.owner_group.name, self.dataset_kind,
            self.transient.name, self.status,
        )

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING

    @property
    def status_label(self):
        return dict(self.STATUS_CHOICES).get(self.status, self.status)

    @property
    def kind_label(self):
        return dict(self.KIND_CHOICES).get(self.dataset_kind, self.dataset_kind)

    @property
    def decided_as_label(self):
        return dict(self.DECIDED_AS_CHOICES).get(self.decided_as, "") if self.decided_as else ""

    @property
    def is_staff_override(self):
        return self.decided_as == self.DECIDED_AS_STAFF

    @property
    def granted_count(self):
        ids = self.granted_dataset_ids
        return len(ids) if isinstance(ids, list) else 0


auditlog.register(DataAccessRequest)
