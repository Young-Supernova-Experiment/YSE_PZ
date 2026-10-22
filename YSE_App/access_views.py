"""Pages for read-time access rules (#648, PR 5). All return 404 while ``DATA_ACCESS_GRANTS_ENABLED`` is off.

* ``/data_access/``: the dashboard: the user's programs and standing grants, their datasets with
  grantees and groups (turn off going forward or backwards), and automatic-publishing opt-ins.
* ``/data_access/programs/``: the programs (allocations) the user may set access for.
* ``/data_access/programs/<id>/``: one program: its standing grants (stop), a program or
  night action, and (for the program's PI or staff) its program deputies.
* ``/transient_detail/<id>/data_access/rules/``: one object: a night action (all of the
  object's points from one night, any filter, and that night's spectra), an action per
  dataset, the rules on the datasets the user manages, and (staff) removing rules.

The work is done by ``services.access_actions`` and ``services.access_pages``.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied as PageForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.models import (
    Allocation,
    AllocationDelegate,
    ClassicalObservingDate,
    StandingAccessGrant,
    Transient,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.services import access_actions
from YSE_App.services import access_pages as pages


def _error_text(exc) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return " ".join(str(v[0] if isinstance(v, (list, tuple)) else v) for v in detail.values())
    if isinstance(detail, (list, tuple)):
        return " ".join(str(v) for v in detail)
    return str(detail or exc)


TURN_OFF_ACTIONS = ("stop", "remove_allow", "lift", "remove_group", "autopublish")


def _backwards(post) -> bool:
    return (post.get("direction") or "forward") == "backward"


def _post_dataset(post, transient=None):
    model = TransientSpectrum if post.get("kind") == "spectrum" else TransientPhotometry
    qs = model.objects.all() if transient is None else model.objects.filter(transient=transient)
    return get_object_or_404(qs, pk=post.get("dataset_id") or 0)


def _post_grantee(post):
    if post.get("grantee_group_id"):
        return pages.resolve_grantee(group=post.get("grantee_group_id"))
    return pages.resolve_grantee(user=post.get("grantee_username"))


def turn_off(request, action, *, allocation=None, transient=None) -> str:
    """The turn-off and opt-in actions shared by the dashboard, program and object pages; returns the message."""
    from django.contrib.auth.models import Group

    from YSE_App.services import autopublish_optin

    post = request.POST
    backwards = _backwards(post)
    when = ("including data taken earlier" if backwards else
            "going forward: data taken before now stay visible to them")
    if action == "stop":
        grants = StandingAccessGrant.objects.all() if allocation is None else StandingAccessGrant.objects.filter(
            allocation=allocation)
        grant = get_object_or_404(grants.select_related("allocation__principal_investigator"),
                                  pk=post.get("grant_id") or 0)
        n = access_actions.stop_standing_grant(request.user, grant, backwards=backwards)
        return ("Stopped backwards: %d rule%s it wrote removed, so earlier data are no longer covered." % (
            n, "" if n == 1 else "s")) if backwards else (
            "Stopped going forward: data taken from now on are not covered; earlier data keep the access it gave.")
    dataset = _post_dataset(post, transient)
    if action == "autopublish":
        enabled = post.get("enabled") == "1"
        state = autopublish_optin.set_opt_in(request.user, dataset, enabled)
        return "Automatic publishing %s for this dataset." % ("allowed" if state else "turned off")
    if action == "remove_group":
        group = get_object_or_404(Group, pk=post.get("group_id") or 0)
        n = access_actions.remove_dataset_group(request.user, dataset, group, backwards=backwards)
        return "Group %s removed from the dataset, %s%s." % (
            group.name, when, "" if backwards else " (%d item%s kept)" % (n, "" if n == 1 else "s"))
    group, user = _post_grantee(post)
    label = pages.grantee_label(group, user)
    if action == "lift":
        n = access_actions.remove_rules(request.user, dataset, effect="deny", grantee_group=group, grantee_user=user)
        return "Restriction lifted for %s on %d item%s." % (label, n, "" if n == 1 else "s")
    n = access_actions.remove_rules(request.user, dataset, effect="allow", grantee_group=group, grantee_user=user,
                                    backwards=backwards)
    if not backwards:
        return ("Access for %s turned off going forward: the data already taken stay visible to them. Choose "
                "'backwards' to remove those too." % label)
    return "Access for %s removed backwards from %d item%s." % (label, n, "" if n == 1 else "s")


def _grantee(post):
    kind = post.get("grantee_kind") or ("user" if post.get("username") else "group")
    if kind == "user":
        return pages.resolve_grantee(user=post.get("username"))
    return pages.resolve_grantee(group=post.get("group"))


@login_required
def programs(request):
    pages.require_enabled()
    return render(request, "YSE_App/access_programs.html", {
        "allocations": list(pages.managed_allocations(request.user)),
    })


def _classical_nights(allocation):
    rows = []
    for night in (ClassicalObservingDate.objects.filter(resource__allocation=allocation)
                  .select_related("resource__telescope__observatory").order_by("-obs_date")[:200]):
        offset = getattr(getattr(night.resource.telescope, "observatory", None), "utc_offset", 0) or 0
        local_date = night.obs_date.date()  # a classical night dated D is the night starting on the evening of D
        start, end = pages.night_window(local_date, offset)
        rows.append({"id": night.pk, "night": local_date, "start": start, "end": end})
    return rows


@login_required
def program(request, allocation_id):
    pages.require_enabled()
    allocation = get_object_or_404(Allocation.objects.select_related(
        "principal_investigator", "telescope__observatory"), pk=allocation_id)
    role = access_actions.program_role(request.user, allocation)
    if role is None:
        raise PageForbidden("Only the program's PI, its program deputies or an administrator may open this page.")
    page = reverse("access_program", kwargs={"allocation_id": allocation.pk})
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "grant":
                group, user = _grantee(request.POST)
                effect = request.POST.get("effect", "")
                night_start = night_end = classical = None
                scope = request.POST.get("scope", "program")
                if scope == "classical":
                    classical = get_object_or_404(ClassicalObservingDate, pk=request.POST.get("classical_night") or 0,
                                                  resource__allocation=allocation)
                    match = next(r for r in _classical_nights(allocation) if r["id"] == classical.pk)
                    night_start, night_end = match["start"], match["end"]
                elif scope == "night":
                    night_start, night_end = pages.parse_night(request.POST)
                grant, result = access_actions.program_action(
                    request.user, allocation, effect=effect, grantee_group=group, grantee_user=user,
                    night_start=night_start, night_end=night_end, classical_night=classical,
                    note=request.POST.get("note", ""))
                messages.success(request, "Saved; it also applies to data taken later. " + pages.describe(
                    result, effect, pages.grantee_label(group, user)))
            elif action in TURN_OFF_ACTIONS:
                messages.success(request, turn_off(request, action, allocation=allocation))
            elif action == "add_deputy":
                from django.contrib.auth.models import User

                username = (request.POST.get("username") or "").strip()
                deputy = User.objects.filter(username=username, is_active=True).first() if username else None
                if deputy is None:
                    raise ValidationError({"username": "No active account with that username."})
                pages.add_program_deputy(request.user, allocation, deputy, note=request.POST.get("note", ""))
                messages.success(request, "%s may now set access for this program's data." % deputy.username)
            elif action == "remove_deputy":
                row = get_object_or_404(AllocationDelegate, pk=request.POST.get("deputy_id") or 0,
                                        allocation=allocation)
                pages.remove_program_deputy(request.user, row)
                messages.success(request, "%s is no longer a program deputy." % row.user.username)
            else:
                messages.error(request, "Unknown action.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "Not changed: %s" % _error_text(exc))
        return redirect(page)
    grants = list(StandingAccessGrant.objects.filter(allocation=allocation)
                  .select_related("grantee_group", "grantee_user", "created_by", "stopped_by", "classical_night"))
    offset = getattr(getattr(allocation.telescope, "observatory", None), "utc_offset", 0) or 0
    return render(request, "YSE_App/access_program.html", {
        "allocation": allocation,
        "role": role,
        "grants": grants,
        "classical_nights": _classical_nights(allocation),
        "utc_offset": offset,
        "groups": pages.grantee_choices(),
        "can_manage_deputies": pages.can_manage_deputies(request.user, allocation),
        "deputies": list(AllocationDelegate.objects.filter(allocation=allocation).select_related("user", "added_by")),
        "linked_photometry": TransientPhotometry.objects.filter(allocation=allocation).count(),
        "linked_spectra": TransientSpectrum.objects.filter(allocation=allocation).count(),
    })


@login_required
def transient_rules(request, transient_id):
    pages.require_enabled()
    transient = get_object_or_404(Transient, pk=transient_id)
    page = reverse("access_transient_rules", kwargs={"transient_id": transient.pk})
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "dataset":
                kind = request.POST.get("kind")
                model = TransientSpectrum if kind == "spectrum" else TransientPhotometry
                dataset = get_object_or_404(model, pk=request.POST.get("dataset_id") or 0, transient=transient)
                group, user = _grantee(request.POST)
                effect = request.POST.get("effect", "")
                result = access_actions.dataset_action(request.user, dataset, effect=effect, grantee_group=group,
                                                       grantee_user=user)
                messages.success(request, pages.describe(result, effect, pages.grantee_label(group, user)))
            elif action == "night":
                start, end = pages.parse_night(request.POST)
                group, user = _grantee(request.POST)
                effect = request.POST.get("effect", "")
                result = access_actions.object_night_action(request.user, transient, start, end, effect=effect,
                                                            grantee_group=group, grantee_user=user)
                messages.success(request, pages.describe(result, effect, pages.grantee_label(group, user)))
            elif action in TURN_OFF_ACTIONS:
                messages.success(request, turn_off(request, action, transient=transient))
            else:
                messages.error(request, "Unknown action.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "Not changed: %s" % _error_text(exc))
        return redirect(page)
    phot_sets, spectra = pages.manageable_datasets(request.user, transient)
    return render(request, "YSE_App/access_transient_rules.html", {
        "transient": transient,
        "phot_sets": phot_sets,
        "spectra": spectra,
        "nights": pages.object_nights(transient, phot_sets, spectra),
        "rules": pages.rule_summary(phot_sets, spectra),
        "groups": pages.grantee_choices(),
        "is_staff": access_actions.is_staff(request.user),
    })


@login_required
def dashboard(request):
    """One page for a PI, deputy or program deputy: programs, standing grants, datasets, grantees, opt-ins."""
    pages.require_enabled()
    page = reverse("access_dashboard")
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action in TURN_OFF_ACTIONS:
                messages.success(request, turn_off(request, action))
            else:
                messages.error(request, "Unknown action.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "Not changed: %s" % _error_text(exc))
        return redirect(page)
    allocations = list(pages.managed_allocations(request.user, own_only=True))
    grants = list(StandingAccessGrant.objects.filter(allocation__in=allocations, stopped_at__isnull=True)
                  .select_related("allocation", "grantee_group", "grantee_user", "created_by"))
    datasets = pages.dashboard_datasets(request.user)
    from YSE_App.models import DataAccessRequest
    from YSE_App.services import data_access as dar

    accepted = [r for r in dar.requests_to_decide(request.user, pending_only=False).filter(
        status=DataAccessRequest.STATUS_ACCEPTED).order_by("-decided_at", "-id")[:100]
        if dar.can_revoke(request.user, r)][:50]
    return render(request, "YSE_App/access_dashboard.html", {
        "accepted_requests": accepted,
        "allocations": allocations,
        "grants": grants,
        "datasets": datasets,
        "forward_help": pages.FORWARD_HELP,
        "backward_help": pages.BACKWARD_HELP,
        "is_staff": access_actions.is_staff(request.user),
    })
