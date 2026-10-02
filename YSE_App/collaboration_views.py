"""Pages and form endpoints for transient interests (#290) and data access requests (#293)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import DataAccessRequest, Transient, TransientInterest
from YSE_App.services import data_access as dar
from YSE_App.services import interests as svc


def _safe_next(request, fallback):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and (candidate.startswith("/") or url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure())):
        return candidate
    return fallback


def _error_text(exc) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return " ".join(str(v[0] if isinstance(v, (list, tuple)) else v) for v in detail.values())
    if isinstance(detail, (list, tuple)):
        return " ".join(str(v) for v in detail)
    return str(detail or exc)


def interests_context(request, transient, user_groups=None):
    """Context for the "Working on this" box (used by the transient detail view)."""
    show_withdrawn = request.GET.get("interests") == "all"
    if user_groups is None:
        user_groups = svc.selectable_groups(request.user)
    return {
        "interests": list(svc.interest_queryset(transient.id, include_withdrawn=show_withdrawn)),
        "interest_groups": list(user_groups),
        "interest_roles": TransientInterest.ROLE_CHOICES,
        "interest_statuses": TransientInterest.STATUS_CHOICES,
        "interests_show_withdrawn": show_withdrawn,
    }


def data_access_context(request, transient, kinds=None, user_groups=None):
    """Context for the restricted-data hint on the detail page / its fragments.

    ``hidden_rows`` (all kinds asked for), plus ``hidden_rows_spectrum`` /
    ``hidden_rows_photometry`` for the tab templates and ``access_target_groups``
    for the request form. One query for the user's groups, one per kind.
    """
    if user_groups is None:
        user_groups = list(request.user.groups.order_by("name")) if request.user.is_authenticated else []
    rows = dar.hidden_summary(transient, request.user, kinds=kinds, user_groups=user_groups)
    return {
        "hidden_rows": rows,
        "hidden_rows_spectrum": [r for r in rows if r["kind"] == "spectrum"],
        "hidden_rows_photometry": [r for r in rows if r["kind"] == "photometry"],
        "access_target_groups": [g for g in user_groups if g.name != PUBLIC_COLLABORATION_GROUP_NAME],
    }


def collaboration_context(request, transient):
    """Interests box + restricted-data hints for the full detail page (shares the user-groups query)."""
    user_groups = list(request.user.groups.order_by("name")) if request.user.is_authenticated else []
    ctx = interests_context(request, transient, user_groups=user_groups)
    ctx.update(data_access_context(request, transient, user_groups=user_groups))
    return ctx


# --- interests ---------------------------------------------------------------

@login_required
@require_POST
def interest_register(request, transient_id):
    transient = get_object_or_404(Transient, pk=transient_id)
    fallback = reverse("transient_detail", kwargs={"slug": transient.slug}) + "#summary_tab"
    group = None
    group_id = (request.POST.get("group") or "").strip()
    if group_id:
        group = get_object_or_404(Group, pk=group_id)
    try:
        interest = svc.register_interest(
            transient, request.user, request.POST.get("title", ""), group=group,
            role=request.POST.get("role") or TransientInterest.ROLE_LEAD,
            description=(request.POST.get("description") or "").strip(),
        )
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "Interest not registered: %s" % _error_text(exc))
    else:
        messages.success(request, "Interest registered: %s." % interest.title)
    return redirect(_safe_next(request, fallback))


@login_required
@require_POST
def interest_status(request, interest_id):
    interest = get_object_or_404(TransientInterest.objects.select_related("transient"), pk=interest_id)
    fallback = reverse("transient_detail", kwargs={"slug": interest.transient.slug}) + "#summary_tab"
    doi = request.POST.get("doi")
    try:
        svc.update_interest_status(interest, request.user, request.POST.get("status", ""), doi=doi)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "Interest not updated: %s" % _error_text(exc))
    else:
        messages.success(request, "Interest '%s' is now %s." % (interest.title, interest.status_label.lower()))
    return redirect(_safe_next(request, fallback))


@login_required
def my_interests(request):
    show_all = request.GET.get("show") == "all"
    interests = list(svc.interests_for_user(request.user, include_withdrawn=show_all))
    return render(request, "YSE_App/my_interests.html", {
        "interests": interests,
        "show_all": show_all,
        "statuses": [(v, label) for v, label in TransientInterest.STATUS_CHOICES],
    })


# --- data access requests ------------------------------------------------------

@login_required
@require_POST
def data_access_request_create(request, transient_id):
    transient = get_object_or_404(Transient, pk=transient_id)
    kind = request.POST.get("kind", "")
    tab = "spectra_tab" if kind == DataAccessRequest.KIND_SPECTRUM else "photometry_tab"
    fallback = reverse("transient_detail", kwargs={"slug": transient.slug}) + "#" + tab
    owner_group = get_object_or_404(Group, pk=request.POST.get("owner_group") or 0)
    target_group = None
    target_id = (request.POST.get("target_group") or "").strip()
    if target_id:
        target_group = get_object_or_404(Group, pk=target_id)
    dataset_id = (request.POST.get("dataset_id") or "").strip() or None
    try:
        req = dar.request_access(
            request.user, transient, kind, owner_group, target_group=target_group,
            message=request.POST.get("message", ""), dataset_id=int(dataset_id) if dataset_id else None,
        )
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "Access request not sent: %s" % _error_text(exc))
    else:
        messages.success(
            request, "Access request #%d sent to %s; its members were notified." % (req.pk, owner_group.name),
        )
    return redirect(_safe_next(request, fallback))


@login_required
def data_access_requests(request):
    box = request.GET.get("box") or "inbox"
    if box not in ("inbox", "decided", "mine"):
        box = "inbox"
    if box == "mine":
        qs = dar.requests_by(request.user)
    elif box == "decided":
        qs = dar.requests_to_decide(request.user, pending_only=False).exclude(status=DataAccessRequest.STATUS_PENDING)
    else:
        qs = dar.requests_to_decide(request.user)
    qs = dar.filter_requests(qs, request.GET)
    rows = list(qs[:500])
    for row in rows:
        row.can_decide = row.is_pending and dar.user_can_decide(request.user, row)
    highlight = request.GET.get("request") or ""
    return render(request, "YSE_App/data_access_requests.html", {
        "rows": rows,
        "box": box,
        "highlight": int(highlight) if highlight.isdigit() else None,
        "pending_count": dar.pending_count_for(request.user),
    })


@login_required
@require_GET
def data_access_pending_count(request):
    """JSON ``{"pending": n}`` for the user-menu badge (fetched after page load, cached a minute)."""
    return JsonResponse({"pending": dar.pending_count_for(request.user)})


@login_required
@require_POST
def data_access_decide(request, request_id):
    req = get_object_or_404(DataAccessRequest.objects.select_related("owner_group", "transient"), pk=request_id)
    fallback = reverse("data_access_requests") + "?box=inbox"
    decision = request.POST.get("decision", "")
    if decision not in ("accept", "decline"):
        messages.error(request, "Choose accept or decline.")
        return redirect(_safe_next(request, fallback))
    try:
        dar.decide(req, request.user, decision == "accept", note=request.POST.get("note", ""))
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "Request #%d not decided: %s" % (req.pk, _error_text(exc)))
    else:
        if req.status == DataAccessRequest.STATUS_ACCEPTED:
            messages.success(request, "Request #%d accepted: %s now sees %d %s dataset%s on %s." % (
                req.pk, req.target_group.name, req.granted_count, req.kind_label.lower(),
                "" if req.granted_count == 1 else "s", req.transient.name))
        else:
            messages.success(request, "Request #%d declined." % req.pk)
    return redirect(_safe_next(request, fallback))
