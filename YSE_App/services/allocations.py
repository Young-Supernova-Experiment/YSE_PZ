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


# --- legacy resource accounting (#304) -----------------------------------------
# A TransientFollowup that reaches the Successful status charges its ToO / queued resource once
# (``usage_charged_at``); leaving that status refunds it. Facility requests charge their Allocation
# separately (charge_request above) and never carry a legacy resource, so nothing is counted twice.

SUCCESS_STATUS = "Successful"


def followup_usage_hours(followup) -> float:
    """Hours to charge: the follow-up's ``usage_hours``, else the hours of its facility requests."""
    if followup.usage_hours:
        return float(followup.usage_hours)
    total = 0.0
    for request in followup.facility_requests.all():
        total += float(request.hours_charged or 0.0)
    return round(total, 4)


def _resource_of(followup):
    if followup.too_resource_id:
        return followup.too_resource, "too"
    if followup.queued_resource_id:
        return followup.queued_resource, "queued"
    return None, ""


def _bump(model, pk, **deltas) -> None:
    from django.db.models.functions import Coalesce

    updates = {field: Coalesce(F(field), 0.0) + float(delta) for field, delta in deltas.items() if delta}
    if updates:
        model.objects.filter(pk=pk).update(**updates)


def record_followup_usage(followup, *, hours: Optional[float] = None) -> bool:
    """Charge the follow-up's resource once (1 ToO trigger + hours, or queued hours); False when already charged."""
    from YSE_App.models.followup_models import TransientFollowup

    resource, kind = _resource_of(followup)
    if resource is None or followup.usage_charged_at:
        return False
    hours = float(hours if hours is not None else followup_usage_hours(followup))
    now = timezone.now()
    with transaction.atomic():
        updated = TransientFollowup.objects.filter(pk=followup.pk, usage_charged_at__isnull=True).update(
            usage_charged_at=now, usage_hours=hours)
        if not updated:
            return False
        if kind == "too":
            _bump(type(resource), resource.pk, used_too_triggers=1.0, used_too_hours=hours)
        else:
            _bump(type(resource), resource.pk, used_hours=hours)
    followup.usage_charged_at = now
    followup.usage_hours = hours
    resource.refresh_from_db()
    return True


def refund_followup_usage(followup) -> bool:
    """Undo :func:`record_followup_usage` (a successful follow-up set back to another status)."""
    from YSE_App.models.followup_models import TransientFollowup

    resource, kind = _resource_of(followup)
    if resource is None or not followup.usage_charged_at:
        return False
    hours = float(followup.usage_hours or 0.0)
    with transaction.atomic():
        updated = TransientFollowup.objects.filter(pk=followup.pk, usage_charged_at__isnull=False).update(
            usage_charged_at=None)
        if not updated:
            return False
        if kind == "too":
            _bump(type(resource), resource.pk, used_too_triggers=-1.0, used_too_hours=-hours)
        else:
            _bump(type(resource), resource.pk, used_hours=-hours)
    followup.usage_charged_at = None
    resource.refresh_from_db()
    return True


def sync_followup_usage(followup) -> Optional[bool]:
    """Charge or refund according to the follow-up's status; called from the post_save signal."""
    status_name = followup.status.name if followup.status_id else ""
    if status_name == SUCCESS_STATUS:
        return record_followup_usage(followup)
    if followup.usage_charged_at:
        return not refund_followup_usage(followup)
    return None


def resource_remaining(resource) -> dict:
    """Awarded minus used for a legacy resource (``None`` where nothing was awarded)."""
    return {
        "hours": getattr(resource, "remaining_hours", None),
        "triggers": getattr(resource, "remaining_triggers", None),
    }


def estimate_resource_usage(resource) -> dict:
    """Successful follow-ups on ``resource`` that were never charged (the backfill's preview)."""
    from YSE_App.models.followup_models import TransientFollowup

    field = "too_resource" if resource.__class__.__name__ == "ToOResource" else "queued_resource"
    pending = TransientFollowup.objects.filter(**{field: resource}, status__name=SUCCESS_STATUS,
                                               usage_charged_at__isnull=True)
    hours = sum(followup_usage_hours(f) for f in pending)
    return {"followups": pending.count(), "hours": round(hours, 4), "queryset": pending}
