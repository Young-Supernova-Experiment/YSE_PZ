"""Per-dataset opt-in to automatic publishing of restricted data (#691).

Automatic publishing rules (``AutoPublisher``) have no acting user, so they send
public data only. A restricted photometry set or spectrum is included only
while a row exists here for it. Rows are created and removed only through
:func:`YSE_App.services.autopublish_optin.set_opt_in`, which requires a PI, a
deputy or staff (``services.data_sharing.share_role``) for the dataset, and
both are logged by django-auditlog. Nothing is opted in by default.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q

from YSE_App.models.phot_models import TransientPhotometry
from YSE_App.models.spectra_models import TransientSpectrum

__all__ = ["AutopublishOptIn"]


class AutopublishOptIn(models.Model):
    photometry = models.OneToOneField(
        TransientPhotometry, null=True, blank=True, on_delete=models.CASCADE, related_name="autopublish_optin")
    spectrum = models.OneToOneField(
        TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE, related_name="autopublish_optin")
    opted_in_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(Q(photometry__isnull=False, spectrum__isnull=True)
                       | Q(photometry__isnull=True, spectrum__isnull=False)),
                name="yse_autopublishoptin_one_dataset"),
        ]

    def __str__(self):
        return "autopublish opt-in for %s" % (self.photometry or self.spectrum)


auditlog.register(AutopublishOptIn)
