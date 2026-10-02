"""Allocations page (#305) and the facility-request hook on the transient detail page (#300)."""

from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from django.core.paginator import Paginator

from YSE_App.allocation_forms import AllocationForm
from YSE_App.facilities import FacilityValidationError, get_facility, registered_slugs
from YSE_App.models import Allocation, FacilityRequest, Transient
from YSE_App.services import facility_requests as fr


# --- Allocations page ---------------------------------------------------------

@staff_member_required
def allocations(request):
    now = timezone.now()
    qs = Allocation.objects.select_related("telescope", "instrument", "principal_investigator", "credential")
    qs = qs.annotate(
        n_requests=Count("requests", distinct=True),
        n_open=Count("requests", filter=Q(requests__state__in=FacilityRequest.OPEN_STATES), distinct=True),
    ).prefetch_related("groups")
    show = request.GET.get("show", "current")
    telescope = request.GET.get("telescope", "").strip()
    if show == "current":
        qs = qs.filter(is_active=True, end_date__gte=now)
    elif show == "inactive":
        qs = qs.filter(Q(is_active=False) | Q(end_date__lt=now))
    if telescope:
        qs = qs.filter(telescope__name=telescope)
    rows = []
    for allocation in qs:
        adapter = get_facility(allocation.facility) if allocation.facility else None
        rows.append({
            "a": allocation,
            "facility_name": adapter.name if adapter else ("unknown: %s" % allocation.facility if allocation.facility else ""),
            "current": allocation.is_current(now),
            "group_names": ", ".join(g.name for g in allocation.groups.all()) or "everyone",
        })
    context = {
        "rows": rows,
        "show": show,
        "telescope": telescope,
        "telescopes": sorted(set(Allocation.objects.values_list("telescope__name", flat=True))),
        "facilities": [get_facility(s).describe() for s in registered_slugs()],
    }
    return render(request, "YSE_App/allocations.html", context)


@staff_member_required
def allocation_create(request):
    return _allocation_form(request, None)


@staff_member_required
def allocation_edit(request, allocation_id):
    allocation = get_object_or_404(Allocation, pk=allocation_id)
    return _allocation_form(request, allocation)


def _allocation_form(request, allocation):
    if request.method == "POST":
        form = AllocationForm(request.POST, instance=allocation)
        if form.is_valid():
            saved = form.save(request.user)
            messages.success(request, "Allocation '%s' saved." % saved.name)
            return redirect("allocations")
    else:
        form = AllocationForm(instance=allocation)
    recent = []
    if allocation is not None:
        recent = FacilityRequest.objects.filter(allocation=allocation).select_related(
            "transient", "submitted_by")[:25]
    context = {
        "form": form,
        "allocation": allocation,
        "recent_requests": recent,
        "facilities": [get_facility(s).describe() for s in registered_slugs()],
    }
    return render(request, "YSE_App/allocation_form.html", context)


# --- transient detail hook ----------------------------------------------------

def facility_panel_context(user, transient):
    """Allocations the user may submit to (with their form schema) and the transient's requests."""
    options = []
    for allocation in fr.allocations_for_user(user):
        adapter = get_facility(allocation.facility)
        if adapter is None or not adapter.can("submit"):
            continue
        options.append({
            "allocation": allocation,
            "facility": adapter.describe(),
            "schema_json": json.dumps(adapter.form_schema(allocation), default=str),
            "hours_remaining": allocation.hours_remaining,
        })
    requests_qs = fr.requests_for_transient(transient, user)
    return {
        "facility_allocations": options,
        "facility_requests": [_request_row(r, user) for r in requests_qs],
    }


def _request_row(req, user):
    adapter = get_facility(req.allocation.facility)
    allocation = req.allocation
    can_status = bool(adapter and adapter.supports("status", allocation))
    return {
        "r": req,
        "can_manage": fr.can_manage(req, user),
        "can_poll": bool(can_status and req.is_open and (req.external_id or req.is_photometry)),
        "can_update": bool(adapter and adapter.supports("update", allocation) and req.is_open),
        "can_results": bool(adapter and adapter.can("results") and req.state == FacilityRequest.STATE_COMPLETE
                            and not req.results_ingested_at),
        "manual": bool(adapter is None or not can_status),
        "schema_json": json.dumps(adapter.form_schema(allocation), default=str) if adapter else "[]",
        "payload_json": json.dumps(req.payload or {}, default=str),
        "log_json": json.dumps(req.log or [], default=str),
    }


def _request_json(req):
    return {
        "id": req.pk, "state": req.state, "state_display": req.get_state_display(),
        "state_detail": req.state_detail, "external_id": req.external_id, "external_url": req.external_url,
        "allocation": req.allocation.name, "facility": req.allocation.facility, "kind": req.kind,
        "submitted_by": req.submitted_by.username, "hours_charged": req.hours_charged, "attempts": req.attempts,
        "n_results": req.n_results, "run": str(req.run.uuid) if req.run_id else None, "log": req.log or [],
    }


@login_required
def transient_facility_requests_fragment(request, transient_id):
    transient = get_object_or_404(Transient, pk=transient_id)
    ctx = facility_panel_context(request.user, transient)
    ctx["transient"] = transient
    return render(request, "YSE_App/transient_detail_facility_requests.html", ctx)


@login_required
@require_POST
def transient_facility_submit(request, transient_id):
    """``POST {allocation: id, parameters: {...}}`` (JSON or form) -> the new request as JSON."""
    transient = get_object_or_404(Transient, pk=transient_id)
    body = _body(request)
    try:
        allocation = Allocation.objects.select_related("telescope", "credential").get(pk=int(body.get("allocation") or 0))
    except (Allocation.DoesNotExist, TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "choose an allocation"}, status=400)
    params = body.get("parameters")
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "parameters must be a JSON object"}, status=400)
    if params is None:
        params = {k: v for k, v in body.items() if k not in ("allocation", "parameters", "csrfmiddlewaretoken")}
    if not isinstance(params, dict):
        return JsonResponse({"ok": False, "error": "parameters must be a JSON object"}, status=400)
    try:
        req = fr.submit_request(allocation, transient, request.user, params)
    except FacilityValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc), "errors": exc.errors}, status=400)
    except fr.FacilityRequestError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=403 if "may not" in str(exc) else 400)
    return JsonResponse({"ok": True, "request": _request_json(req)})


@login_required
@require_POST
def facility_request_action(request, request_id):
    """``POST {action: poll|cancel|complete|failed, detail: ...}`` on one request."""
    req = get_object_or_404(FacilityRequest.objects.select_related("allocation", "transient", "submitted_by", "run"),
                            pk=request_id)
    if not fr.can_manage(req, request.user):
        return JsonResponse({"ok": False, "error": "only the requester or staff may change this request"}, status=403)
    body = _body(request)
    action = str(body.get("action") or "").lower()
    detail = str(body.get("detail") or "")
    try:
        if action == "poll":
            fr.poll_request(req)
        elif action == "cancel":
            fr.cancel_request(req, request.user, detail)
        elif action in ("update", "modify"):
            params = body.get("parameters")
            if isinstance(params, str):
                params = json.loads(params or "{}")
            if not isinstance(params, dict):
                return JsonResponse({"ok": False, "error": "parameters must be a JSON object"}, status=400)
            fr.update_request(req, request.user, params)
        elif action in ("results", "retrieve"):
            fr.retrieve_results(req, request.user)
        elif action in ("complete", "failed", "accepted", "running"):
            fr.mark_request(req, action, request.user, detail)
        else:
            return JsonResponse({"ok": False, "error": "unknown action %r" % action}, status=400)
    except FacilityValidationError as exc:
        return JsonResponse({"ok": False, "error": str(exc), "errors": exc.errors}, status=400)
    except (fr.FacilityRequestError, ValueError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    req.refresh_from_db()
    return JsonResponse({"ok": True, "request": _request_json(req)})


@login_required
def facility_request_log(request, request_id):
    """The chronological log of one request as JSON (requester, allocation audience or staff)."""
    req = get_object_or_404(FacilityRequest.objects.select_related("allocation", "transient", "submitted_by"), pk=request_id)
    user = request.user
    if not (fr.can_manage(req, user) or req.allocation.usable_by(user)):
        return JsonResponse({"ok": False, "error": "not allowed"}, status=403)
    return JsonResponse({"ok": True, "request": _request_json(req)})


# --- facility requests list page (#300) ---------------------------------------

STATE_GROUPS = {
    "open": (FacilityRequest.STATE_QUEUED,) + FacilityRequest.OPEN_STATES,
    "final": FacilityRequest.FINAL_STATES,
}


@login_required
def facility_requests(request):
    """Every facility request the user may see, with state / facility / allocation / mine filters."""
    user = request.user
    qs = FacilityRequest.objects.select_related("allocation", "allocation__telescope", "transient", "submitted_by", "run")
    if not (user.is_staff or user.is_superuser):
        qs = qs.filter(Q(submitted_by=user) | Q(allocation__in=fr.allocations_for_user(user, facility_only=False))).distinct()
    state = request.GET.get("state", "open").strip().lower()
    facility = request.GET.get("facility", "").strip()
    allocation_id = request.GET.get("allocation", "").strip()
    transient_name = request.GET.get("transient", "").strip()
    mine = request.GET.get("mine") == "1"
    if state in STATE_GROUPS:
        qs = qs.filter(state__in=STATE_GROUPS[state])
    elif state and state != "all":
        qs = qs.filter(state=state)
    if facility:
        qs = qs.filter(allocation__facility=facility)
    if allocation_id.isdigit():
        qs = qs.filter(allocation_id=int(allocation_id))
    if transient_name:
        qs = qs.filter(transient__name__icontains=transient_name)
    if mine:
        qs = qs.filter(submitted_by=user)
    paginator = Paginator(qs.order_by("-created_date"), 50)
    page = paginator.get_page(request.GET.get("page"))
    counts = {"open": 0, "final": 0}
    base = FacilityRequest.objects.all() if (user.is_staff or user.is_superuser) else FacilityRequest.objects.filter(
        Q(submitted_by=user) | Q(allocation__in=fr.allocations_for_user(user, facility_only=False)))
    for row in base.values("state").annotate(n=Count("id", distinct=True)):
        counts["open" if row["state"] in STATE_GROUPS["open"] else "final"] += row["n"]
    context = {
        "rows": [_request_row(r, user) for r in page.object_list],
        "page": page,
        "state": state, "facility": facility, "allocation_id": allocation_id, "transient_name": transient_name, "mine": mine,
        "states": FacilityRequest.STATE_CHOICES,
        "facility_slugs": sorted(set(FacilityRequest.objects.values_list("allocation__facility", flat=True))),
        "allocations": Allocation.objects.filter(requests__isnull=False).distinct().order_by("name"),
        "counts": counts,
    }
    return render(request, "YSE_App/facility_requests.html", context)


def _body(request):
    if request.content_type and "json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {k: v for k, v in request.POST.items()}
