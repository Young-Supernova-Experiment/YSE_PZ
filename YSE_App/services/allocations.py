"""Allocation helpers: the external service behind an allocation and usage accounting (#304)."""

from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from YSE_App.models.allocation_models import Allocation, FacilityRequest
from YSE_App.models.external_service_models import ExternalService


def ensure_service(allocation: Allocation, user: Optional[User] = None) -> ExternalService:
    """The :class:`ExternalService` that records this allocation's runs; created and synced on demand."""
    actor = user or allocation.modified_by or allocation.created_by
    service = allocation.service
    if service is None:
        service, _created = ExternalService.objects.get_or_create(
            slug=allocation.service_slug(),
            defaults={
                "name": "Allocation: %s" % allocation.name,
                "kind": ExternalService.KIND_FACILITY,
                "created_by": actor,
                "modified_by": actor,
            },
        )
        Allocation.objects.filter(pk=allocation.pk).update(service=service)
        allocation.service = service
    changed = []
    wanted = {
        "name": ("Allocation: %s" % allocation.name)[:128],
        "kind": ExternalService.KIND_FACILITY,
        "description": "Facility '%s' for %s" % (allocation.facility or "manual", allocation.telescope.name),
        "base_url": allocation.endpoint_url or "",
        "credential_id": allocation.credential_id,
        "enabled": bool(allocation.is_active and allocation.facility),
        "default_params": dict(allocation.default_request_params or {}),
    }
    for field, value in wanted.items():
        if getattr(service, field) != value:
            setattr(service, field, value)
            changed.append(field)
    if changed:
        service.modified_by = actor
        service.save(update_fields=changed + ["modified_by", "modified_date"])
    return service


def remaining_hours(allocation: Allocation) -> float:
    return allocation.hours_remaining


def record_usage(allocation: Allocation, hours: float) -> Allocation:
    """Add ``hours`` to ``hours_used`` atomically (negative values give time back)."""
    if not hours:
        return allocation
    Allocation.objects.filter(pk=allocation.pk).update(hours_used=F("hours_used") + float(hours))
    allocation.refresh_from_db(fields=["hours_used"])
    return allocation


def charge_request(request: FacilityRequest) -> bool:
    """Charge ``request.hours_charged`` to its allocation once; False when already charged or zero."""
    if request.charged_at or not request.hours_charged:
        return False
    with transaction.atomic():
        updated = FacilityRequest.objects.filter(pk=request.pk, charged_at__isnull=True).update(
            charged_at=timezone.now())
        if not updated:
            return False
        record_usage(request.allocation, request.hours_charged)
    request.charged_at = timezone.now()
    return True


def refund_request(request: FacilityRequest) -> bool:
    """Undo :func:`charge_request` (a completed request that is later cancelled)."""
    if not request.charged_at or not request.hours_charged:
        return False
    with transaction.atomic():
        updated = FacilityRequest.objects.filter(pk=request.pk, charged_at__isnull=False).update(charged_at=None)
        if not updated:
            return False
        record_usage(request.allocation, -request.hours_charged)
    request.charged_at = None
    return True


def allocations_for_user(user: User, *, facility_only: bool = True, now=None):
    """Active, current allocations ``user`` may submit to (staff: all of them)."""
    now = now or timezone.now()
    qs = Allocation.objects.filter(is_active=True, start_date__lte=now, end_date__gte=now)
    if facility_only:
        qs = qs.exclude(facility="")
    qs = qs.select_related("telescope", "instrument", "principal_investigator", "credential")
    if user.is_staff or user.is_superuser:
        return qs
    from django.db.models import Q

    return qs.filter(Q(groups__isnull=True) | Q(groups__in=user.groups.all())).distinct()
