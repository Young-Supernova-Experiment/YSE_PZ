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

from YSE_App.models import DataAccessRequest, Transient, TransientInterest
from YSE_App.services import data_access as dar
from YSE_App.services import interests as svc
from YSE_App.services.groups import picker_groups


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
        "interest_groups": picker_groups(request.user, user_groups),
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
        "access_target_groups": dar.requestable_groups(request.user, user_groups=user_groups),
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
            notify_pis=_checked(request.POST.get("notify_pis")),
            request_data=_checked(request.POST.get("request_data")),
        )
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "Interest not registered: %s" % _error_text(exc))
    else:
        messages.success(request, "Interest registered: %s." % interest.title)
        outcome = getattr(interest, "pi_outcome", None)
        if outcome:
            if outcome["requests"]:
                messages.success(request, "Data access requested from %s; the PIs and group members were notified." % (
                    ", ".join(sorted({r.owner_group.name for r in outcome["requests"]}))))
            for reason in outcome["skipped"]:
                messages.warning(request, "No access request for %s" % reason)
    return redirect(_safe_next(request, fallback))


def _checked(value) -> bool:
    return str(value or "").strip().lower() in ("1", "on", "true", "yes")


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
            request, "Access request #%d sent: the data's PIs and their deputies were notified%s." % (
                req.pk, "" if dar.named_deciders_for(req) else " (no PI assigned, so an administrator decides)"),
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
    staff = request.user.is_staff or request.user.is_superuser
    for row in rows:
        row.decider_role = dar.decider_role(request.user, row) if row.is_pending and box != "mine" else None
        row.can_decide = row.decider_role is not None
        row.staff_override = row.decider_role == "staff"
        # #447: requests whose data has no PI (with an account) and no deputy can only be decided by staff
        row.can_revoke = bool(row.is_accepted and box != "mine" and dar.can_revoke(request.user, row))
        row.no_named_decider = bool(staff and row.is_pending and box == "inbox" and not dar.named_deciders_for(row))
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
        if req.is_staff_override:
            messages.warning(request, "You decided request #%d as an administrator; the override is logged." % req.pk)
    return redirect(_safe_next(request, fallback))


@login_required
@require_POST
def data_access_revoke(request, request_id):
    """Revoke an accepted request; ``direction`` is ``forward`` (default) or ``backward`` (also earlier data)."""
    req = get_object_or_404(DataAccessRequest.objects.select_related("owner_group", "target_group", "transient"),
                            pk=request_id)
    fallback = reverse("data_access_requests") + "?box=decided"
    backwards = (request.POST.get("direction") or "forward") == "backward"
    try:
        dar.revoke(req, request.user, backwards=backwards, note=request.POST.get("note", ""))
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "Request #%d not revoked: %s" % (req.pk, _error_text(exc)))
    else:
        messages.success(request, "Request #%d revoked %s: %s %s." % (
            req.pk, "backwards" if backwards else "going forward", req.target_group.name,
            "no longer sees the earlier data either" if backwards else
            "keeps the data taken before now and does not see later data"))
        skipped = getattr(req, "revoke_skipped", 0)
        if skipped:
            messages.warning(request, "%d dataset%s left unchanged (public, not yours to change, or shared with the "
                                      "group before this request was accepted); remove the group by hand from the "
                                      "data access dashboard if you intend that." % (skipped, "" if skipped == 1 else "s"))
    return redirect(_safe_next(request, fallback))


# --- data sharing deputies (#447) -----------------------------------------------

@login_required
def data_sharing_deputies(request):
    """A PI's deputies: who may share the PI's private data, for all of it or for one group's data.

    Lists the PI records linked to the signed-in account; POST ``action=add`` (``pi``, ``username``,
    optional ``group`` / ``note``) or ``action=remove`` (``delegate_id``). Staff manage any PI in the admin.
    """
    from django.contrib.auth.models import User

    from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
    from YSE_App.models import DataSharingDelegate, PrincipalInvestigator
    from YSE_App.services import data_sharing

    page = reverse("data_sharing_deputies")
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "add":
                pi = get_object_or_404(PrincipalInvestigator, pk=request.POST.get("pi") or 0)
                username = (request.POST.get("username") or "").strip()
                delegate = User.objects.filter(username=username, is_active=True).first() if username else None
                if delegate is None:
                    raise ValidationError({"username": "No active account with that username."})
                group_id = (request.POST.get("group") or "").strip()
                group = get_object_or_404(Group, pk=group_id) if group_id else None
                row = data_sharing.add_delegate(request.user, pi, delegate, group, note=request.POST.get("note", ""))
                messages.success(request, "%s may now share %s's data%s." % (
                    row.delegate.username, pi.name, " restricted to %s" % group.name if group else ""))
            elif action == "remove":
                row = get_object_or_404(DataSharingDelegate.objects.select_related("pi", "delegate"),
                                        pk=request.POST.get("delegate_id") or 0)
                data_sharing.remove_delegate(request.user, row)
                messages.success(request, "%s no longer shares %s's data." % (row.delegate.username, row.pi.name))
            else:
                messages.error(request, "Unknown action.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "Not changed: %s" % _error_text(exc))
        return redirect(page)
    pis = data_sharing.managed_pis(request.user)
    rows = DataSharingDelegate.objects.filter(pi__in=pis).select_related("pi", "delegate", "group")
    by_pi = {pi.pk: [] for pi in pis}
    for row in rows:
        by_pi.setdefault(row.pi_id, []).append(row)
    deputy_for = DataSharingDelegate.objects.filter(delegate=request.user).select_related("pi", "group")
    return render(request, "YSE_App/data_sharing_deputies.html", {
        "pis": [(pi, by_pi.get(pi.pk, [])) for pi in pis],
        "deputy_for": list(deputy_for),
        "groups": list(Group.objects.exclude(name=PUBLIC_COLLABORATION_GROUP_NAME).order_by("name")),
    })
