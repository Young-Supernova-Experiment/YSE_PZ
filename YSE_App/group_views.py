"""Team pages, invitation links and the user settings page (#445, #440).

Invite flow (and why it is safe with the onboarding gate):

1. An owner creates a link ``/teams/join/<token>/`` on ``/teams/<id>/``. Only the
   token's SHA-256 is stored; the link is shown once.
2. ``group_invite_link`` (the link itself) never adds anyone to a group. A signed-out
   visitor is sent to the login page and comes back to the link. A signed-in visitor's
   session remembers the token's hash and is redirected to the token-free
   ``/teams/join/``. This view is ``onboarding_exempt`` only so the token is not copied
   into the gate's ``?next=`` URLs. For the same reason it is ``login_exempt`` (on
   experimental, so the login redirect is the view's own, with ``no-referrer``) and
   skipped by experimental's staff two-factor check (``/teams/join/`` is not).
3. ``group_invite_accept`` (``/teams/join/``) is an ordinary, gated page. A brand-new user
   is therefore sent through the User Agreement and the profile first, and comes back
   to ``/teams/join/``; only then can they confirm, and only a POST adds them to the
   group. Nobody gains a group's data access before accepting the agreement.

Owner actions live here, not on the admin site, so non-staff owners do not need staff
MFA (#566). System groups are never managed here.

Any onboarded user can create a team (throttled, see ``services/groups.py``); owners can add
an existing user by exact username or email; members who are not owners can leave.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, User
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from YSE_App.group_forms import GroupProfileForm, InvitationForm, MemberAddForm, TeamCreateForm, UserPreferenceForm
from YSE_App.models.group_models import GroupInvitation, UserPreference
from YSE_App import onboarding
from YSE_App.onboarding import onboarding_exempt
from YSE_App.services import groups as group_svc
from YSE_App.services import membership
from YSE_App.signup_forms import MembershipRequestForm

try:  # experimental (#548): deny-by-default login middleware
    from YSE_App.login_required import login_exempt
except ImportError:  # develop: no such middleware; keep the marker for the backport
    def login_exempt(view):
        view.login_exempt = True
        return view

INVITE_SESSION_KEY = "_yse_group_invite"
NEW_LINK_SESSION_KEY = "_yse_group_invite_new_link"


def _no_referrer(response):
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "no-store"
    return response


def _member_rows(group):
    owners = {u.pk for u in group_svc.owners_of(group)}
    rows = []
    for user in group.user_set.filter(is_active=True).select_related("researcher_profile").order_by("username"):
        rows.append({"user": user, "name": group_svc.display_name(user), "is_owner": user.pk in owners})
    return rows


def _visible_team_or_404(request, group_id):
    group = get_object_or_404(Group.objects.select_related("group_profile"), pk=group_id)
    if group_svc.is_system_group(group):
        raise Http404("System groups have no team page.")
    user = request.user
    member = user.groups.filter(pk=group.pk).exists()
    if not (member or user.is_staff or user.is_superuser or group_svc.is_owner(user, group)):
        raise Http404("Not a team you belong to.")
    return group


def _require_manager(request, group):
    if not group_svc.can_manage(request.user, group):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("Only the group's owners and staff can do this.")


def _onboarding_redirect(request):
    """POSTs pass the gate, so views that change membership check it themselves."""
    if onboarding.gate_enabled():
        step = onboarding.pending_step(request)
        if step == "agreement":
            return onboarding.redirect_with_next("agreement", request)
        if step == "profile":
            return onboarding.redirect_with_next("researcher_profile_edit", request)
    return None


@login_required
@require_GET
def team_list(request):
    # Signup-form requests go to the owners once onboarding is done (the gate sends new users here).
    released = membership.release_held(request.user)
    if released:
        messages.info(request, "Your membership requests from signup were sent to the team owners.")
    teams = group_svc.teams_for(request.user)
    owned = set(Group.objects.filter(group_profile__owners=request.user).values_list("id", flat=True))
    member_of = set(request.user.groups.values_list("id", flat=True))
    rows = [{"group": g, "profile": getattr(g, "group_profile", None), "is_owner": g.id in owned,
             "is_member": g.id in member_of} for g in teams]
    # Membership requests (#448): teams the user can ask to join, and their own requests.
    requests_open = membership.requests_enabled()
    my_requests = list(membership.requests_for_user(request.user)[:50])
    pending_ids = {r.group_id for r in my_requests if r.is_pending}
    requestable = ([{"group": g, "profile": getattr(g, "group_profile", None), "pending": g.id in pending_ids}
                    for g in membership.requestable_groups(request.user)] if requests_open else [])
    return render(request, "YSE_App/teams.html", {
        "rows": rows, "requests_open": requests_open, "requestable": requestable, "my_requests": my_requests,
        "request_form": MembershipRequestForm(),
        "create_enabled": group_svc.create_enabled(),
        "create_problem": group_svc.create_problem(request.user) if group_svc.create_enabled() else None,
        "create_form": TeamCreateForm(),
        "held_requests": list(membership.held_requests(request.user)),
    })


@login_required
@require_GET
def team_detail(request, group_id):
    group = _visible_team_or_404(request, group_id)
    profile = group_svc.profile_for(group)
    manage = group_svc.can_manage(request.user, group)
    new_link = None
    if manage:
        pending = request.session.pop(NEW_LINK_SESSION_KEY, None)
        if pending and pending.get("group") == group.pk:
            new_link = pending.get("url")
    response = render(request, "YSE_App/team_detail.html", {
        "group": group,
        "profile": profile,
        "owners": group_svc.owners_of(group),
        "can_manage": manage,
        "members": _member_rows(group) if manage else [],
        "invitations": list(group_svc.invitations_for(group)[:50]) if manage else [],
        "invite_form": InvitationForm() if manage else None,
        "profile_form": GroupProfileForm(instance=profile) if manage else None,
        "new_link": new_link,
        "pending_requests": list(membership.pending_for_group(group)) if manage else [],
        "member_add_form": MemberAddForm() if manage else None,
        "is_member": request.user.groups.filter(pk=group.pk).exists(),
        "is_owner": group_svc.is_owner(request.user, group),
    })
    return _no_referrer(response) if new_link else response


@login_required
@require_POST
def team_update(request, group_id):
    group = _visible_team_or_404(request, group_id)
    _require_manager(request, group)
    form = GroupProfileForm(request.POST, instance=group_svc.profile_for(group))
    if form.is_valid():
        form.save()
        messages.success(request, "Team details saved.")
    else:
        messages.error(request, "Not saved: check the description.")
    return redirect("team_detail", group.pk)


@login_required
@require_POST
def team_invite_create(request, group_id):
    group = _visible_team_or_404(request, group_id)
    _require_manager(request, group)
    form = InvitationForm(request.POST)
    if not form.is_valid():
        errors = "; ".join("%s" % e for errs in form.errors.values() for e in errs)
        messages.error(request, "No link created: %s" % errors)
        return redirect("team_detail", group.pk)
    try:
        invitation, token = group_svc.create_invitation(
            request.user, group, days=form.cleaned_data["days"], max_uses=form.cleaned_data["max_uses"])
    except group_svc.InvitationError as exc:
        messages.error(request, str(exc))
        return redirect("team_detail", group.pk)
    # Shown once on the next page view (the session is server-side); never stored in the database.
    request.session[NEW_LINK_SESSION_KEY] = {
        "group": group.pk,
        "url": request.build_absolute_uri(reverse("group_invite_link", args=[token])),
    }
    messages.success(request, "Invitation link created. Copy it now: it is shown only once.")
    return redirect("team_detail", group.pk)


@login_required
@require_POST
def team_invite_revoke(request, group_id, invitation_id):
    group = _visible_team_or_404(request, group_id)
    _require_manager(request, group)
    invitation = get_object_or_404(GroupInvitation, pk=invitation_id, group=group)
    group_svc.revoke_invitation(request.user, invitation)
    messages.success(request, "Invitation revoked.")
    return redirect("team_detail", group.pk)


@login_required
@require_POST
def team_member_remove(request, group_id, user_id):
    group = _visible_team_or_404(request, group_id)
    _require_manager(request, group)
    member = get_object_or_404(User, pk=user_id, groups=group)
    try:
        group_svc.remove_member(request.user, group, member)
    except group_svc.InvitationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "%s was removed from %s." % (group_svc.display_name(member), group.name))
    return redirect("team_detail", group.pk)


@login_required
@require_POST
def team_create(request):
    if not group_svc.create_enabled():
        raise Http404("Creating teams is not enabled.")
    pending = _onboarding_redirect(request)
    if pending is not None:
        return pending
    form = TeamCreateForm(request.POST)
    if not form.is_valid():
        errors = "; ".join("%s" % e for errs in form.errors.values() for e in errs)
        messages.error(request, "Team not created: %s" % errors)
        return redirect("team_list")
    try:
        group = group_svc.create_team(request.user, form.cleaned_data["name"], form.cleaned_data.get("description"))
    except group_svc.TeamError as exc:
        messages.error(request, "Team not created: %s" % exc)
        return redirect("team_list")
    messages.success(request, "Team %s created. You are its owner: add members or send invitation links here."
                     % group.name)
    return redirect("team_detail", group.pk)


@login_required
@require_POST
def team_member_add(request, group_id):
    group = _visible_team_or_404(request, group_id)
    _require_manager(request, group)
    pending = _onboarding_redirect(request)
    if pending is not None:
        return pending
    form = MemberAddForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a username or email address.")
        return redirect("team_detail", group.pk)
    try:
        member = group_svc.add_member(request.user, group, form.cleaned_data["identifier"])
    except group_svc.TeamError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "%s is now a member of %s." % (group_svc.display_name(member), group.name))
    return redirect("team_detail", group.pk)


@login_required
@require_POST
def team_leave(request, group_id):
    group = _visible_team_or_404(request, group_id)
    try:
        group_svc.leave_team(request.user, group)
    except group_svc.TeamError as exc:
        messages.error(request, str(exc))
        return redirect("team_detail", group.pk)
    messages.success(request, "You left %s." % group.name)
    return redirect("team_list")


@login_exempt  # signed out: the view's own login redirect keeps Referrer-Policy: no-referrer
@onboarding_exempt
@require_GET
def group_invite_link(request, token):
    """The emailed/pasted link: remember the invitation in the session, never join here."""
    if not request.user.is_authenticated:
        return _no_referrer(redirect_to_login(request.get_full_path()))
    request.session[INVITE_SESSION_KEY] = group_svc.hash_token(token)
    return _no_referrer(redirect("group_invite_accept"))


@login_required
def group_invite_accept(request):
    """Confirm and join (gated: the agreement and profile come first for a new user)."""
    # The gate redirects GETs; POSTs pass it, so check here too: a new user must accept the
    # agreement and complete the profile before they can join (and see the team's data).
    if onboarding.gate_enabled():
        step = onboarding.pending_step(request)
        if step == "agreement":
            return onboarding.redirect_with_next("agreement", request)
        if step == "profile":
            return onboarding.redirect_with_next("researcher_profile_edit", request)
    token_hash = request.session.get(INVITE_SESSION_KEY)
    invitation = group_svc.find_invitation(token_hash, hashed=True) if token_hash else None
    if request.method == "POST":
        if not token_hash:
            messages.error(request, "There is no pending invitation; open the invitation link again.")
            return redirect("team_list")
        try:
            group, joined = group_svc.accept_invitation(request.user, token_hash)
        except group_svc.InvitationError as exc:
            request.session.pop(INVITE_SESSION_KEY, None)
            messages.error(request, str(exc))
            return redirect("team_list")
        request.session.pop(INVITE_SESSION_KEY, None)
        group_svc.forget_preferences(request.user)
        if joined:
            messages.success(request, "You joined %s." % group.name)
        else:
            messages.info(request, "You are already a member of %s." % group.name)
        return redirect("team_detail", group.pk)
    if request.method not in ("GET", "HEAD"):
        return HttpResponseBadRequest("GET or POST")
    already = bool(invitation and request.user.groups.filter(pk=invitation.group_id).exists())
    problem = None if already else (group_svc.invitation_problem(invitation) if token_hash else
                                    "There is no pending invitation; open the invitation link again.")
    inviter = invitation.created_by if invitation is not None else None
    return _no_referrer(render(request, "YSE_App/group_invite_accept.html", {
        "invitation": invitation if not problem else None,
        "group": invitation.group if invitation is not None and not problem else None,
        "inviter_name": group_svc.display_name(inviter) if inviter is not None else "",
        "already_member": already,
        "problem": problem,
    }))


@login_required
def user_settings(request):
    """Per-user picker preferences (#440); notification settings stay on their own page."""
    instance = group_svc.preferences_for(request.user) or UserPreference(user=request.user)
    if request.method == "POST":
        form = UserPreferenceForm(request.POST, instance=instance, user=request.user)
        if form.is_valid():
            pref = form.save(commit=False)
            pref.user = request.user
            pref.comment_default_public = bool(form.cleaned_data.get("comment_default_public"))
            pref.save()
            form.save_m2m()
            group_svc.forget_preferences(request.user)
            messages.success(request, "Settings saved.")
            return redirect("user_settings")
    elif request.method in ("GET", "HEAD"):
        form = UserPreferenceForm(instance=instance, user=request.user)
    else:
        return HttpResponseBadRequest("GET or POST")
    from YSE_App.services.signup import signup_enabled
    return render(request, "YSE_App/user_settings.html", {"form": form, "signup_enabled": signup_enabled()})
