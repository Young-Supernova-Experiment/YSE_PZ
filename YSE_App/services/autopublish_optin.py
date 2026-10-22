"""Who may opt a restricted dataset in to automatic publishing (#691).

Only a PI or a deputy for the dataset (``data_sharing.share_role``) may opt it in; staff may not
opt data in on a PI's behalf (#708, Ryan 2026-10-17), but may still opt data out, and existing opt-ins stay
as they are. Public data need no opt-in; every change is written by django-auditlog.
"""

from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.models import AutopublishOptIn, TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing


def _field(dataset) -> str:
    if isinstance(dataset, TransientPhotometry):
        return "photometry"
    if isinstance(dataset, TransientSpectrum):
        return "spectrum"
    raise ValidationError({"dataset": "Only photometry sets and spectra can be opted in."})


def is_opted_in(dataset) -> bool:
    return AutopublishOptIn.objects.filter(**{_field(dataset): dataset}).exists()


@transaction.atomic
def set_opt_in(user, dataset, enabled: bool) -> bool:
    """Opt ``dataset`` in (or out) of automatic publishing; returns the new state."""
    field = _field(dataset)
    if enabled and data_sharing.is_public_dataset(dataset):
        raise ValidationError({"dataset": "Public data are always published; no opt-in is needed."})
    if enabled and data_sharing.share_role(user, dataset, staff_override=False) is None:
        raise PermissionDenied({"message": "Only the data's PIs and their designated deputies "
                                           "may allow automatic publishing of restricted data."})
    if not enabled and data_sharing.share_role(user, dataset) is None:
        raise PermissionDenied({"message": "Only the data's PIs, their deputies or an administrator "
                                           "may stop automatic publishing of restricted data."})
    rows = AutopublishOptIn.objects.filter(**{field: dataset})
    if enabled:
        if not rows.exists():
            AutopublishOptIn.objects.create(**{field: dataset}, opted_in_by=user)
        return True
    for row in rows:  # one by one so the removal is logged
        row.delete()
    return False
