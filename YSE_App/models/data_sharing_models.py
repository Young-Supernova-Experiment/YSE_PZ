"""PI-designated deputies who may share private data (#447).

Only the PIs of private photometry and spectra (#446, ``services.data_pis``)
and the deputies they designate may decide data-access requests, that is,
share the data with another group. A :class:`DataSharingDelegate` row is one
such designation: ``delegate`` may share the data that ``pi`` leads, either
all of it (``group`` empty) or only the datasets restricted to ``group``.

Designations are data: the PI's linked account (``PrincipalInvestigator.user``)
manages them on the "Data sharing deputies" page, staff in the admin. Group
members and group owners (#445) get no sharing authority from their group
role; staff keep a logged administrator override (``DataAccessRequest.decided_as``).
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import Group, User
from django.db import models

from YSE_App.models.principal_investigator_models import PrincipalInvestigator

__all__ = ["DataSharingDelegate"]


class DataSharingDelegate(models.Model):
    pi = models.ForeignKey(
        PrincipalInvestigator, on_delete=models.CASCADE, related_name="sharing_delegates",
        help_text="The PI on whose behalf the deputy shares.",
    )
    delegate = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="data_sharing_delegations",
        help_text="The deputy: may decide data-access requests for this PI's private data.",
    )
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.CASCADE, related_name="data_sharing_delegates",
        help_text="Only the PI's data restricted to this group; empty = all of the PI's data.",
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("pi__name", "delegate__username", "id")
        constraints = [
            models.UniqueConstraint(fields=["pi", "delegate", "group"], name="yse_datasharingdelegate_unique"),
        ]

    def __str__(self):
        scope = self.group.name if self.group_id else "all data"
        return "%s shares for %s (%s)" % (self.delegate.username, self.pi.name, scope)


auditlog.register(DataSharingDelegate)
