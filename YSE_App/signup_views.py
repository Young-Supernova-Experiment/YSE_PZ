"""Self-service signup, email verification and team membership requests (#448).

Flow: ``/signup/`` creates an active account and signs the person in; the onboarding gate
(#630) then sends them through the User Agreement and the researcher profile, after which
they land on ``/teams/``, where they can ask to join teams. The team's owners approve or
deny on the team page. Teams ticked on the signup form (optional) become requests at once,
held until onboarding is done (``membership.release_held``).

Gate exemptions: ``signup`` and ``signup_verify_email`` are in
``onboarding.EXEMPT_URL_NAMES`` and decorated ``onboarding_exempt``. They are also marked
``login_exempt`` (``YSE_App.login_required`` on experimental, #548; on develop, which has
no login-required middleware, the marker below only sets the attribute), so the backport
keeps them reachable signed out.

The signup page returns 404 while ``SELF_SIGNUP_ENABLED`` is off. The email confirmation
pages (``/settings/email/`` and the emailed link) work for every account, because the
onboarding gate requires a confirmed address from everyone (``ONBOARDING_EMAIL_REQUIRED``). Membership requests are
separate: they follow ``GROUP_REQUESTS_ENABLED`` (on by default) and each team's
``accepts_requests`` switch; owners and deputies decide them.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from django.contrib.auth.models import Group

from YSE_App import onboarding
from YSE_App.models.group_models import GroupMembershipRequest
from YSE_App.onboarding import onboarding_exempt
from YSE_App.services import groups as group_svc
from YSE_App.services import membership
from YSE_App.services import signup as signup_svc
from YSE_App.signup_forms import EmailConfirmForm, MembershipRequestForm, SignupForm

try:  # experimental (#548): deny-by-default login middleware
    from YSE_App.login_required import login_exempt
except ImportError:  # develop: no such middleware; keep the marker for the backport
    def login_exempt(view):
        view.login_exempt = True
        return view

SIGNUP_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _require_signup_enabled():
    if not signup_svc.signup_enabled():
        raise Http404("Self-service signup is not enabled.")


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@login_exempt
@onboarding_exempt
@never_cache
@csrf_protect
def signup(request):
    _require_signup_enabled()
    if request.user.is_authenticated:
        return redirect("team_list")
    if request.method == "POST":
        form = SignupForm(request.POST)
        if not signup_svc.register_attempt(request):
            messages.error(request, "Too many signup attempts from your network. Please try again in an hour.")
            return render(request, "YSE_App/signup.html", {"form": SignupForm()}, status=429)
        if form.is_spam():
            # Pretend it worked; never create anything.
            return render(request, "YSE_App/signup_done.html", {"verification": signup_svc.verification_enabled()})
        if form.is_valid():
            if not signup_svc.site_quota_available():
                messages.error(request, "Signups are paused for a while. Please try again later.")
                return render(request, "YSE_App/signup.html", {"form": form}, status=429)
            email = form.cleaned_data["email"]
            verify = signup_svc.verification_enabled()
            try:
                with transaction.atomic():
                    user = form.save(commit=False)
                    user.is_active = True  # no staff approval (Ryan, 2026-10-13)
                    user.is_staff = False
                    user.is_superuser = False
                    if verify:
                        user.email = ""  # stored only once confirmed
                    user.save()
            except IntegrityError:
                form.add_error("username", "A user with that username already exists.")
                return render(request, "YSE_App/signup.html", {"form": form})
            signup_svc.register_account_created()
            login(request, user, backend=SIGNUP_BACKEND)
            chosen = form.cleaned_data.get("teams") or []
            if chosen:
                made = membership.create_signup_requests(user, chosen, form.cleaned_data.get("team_message") or "")
                if made and not membership.release_held(user):
                    messages.info(request, "Your requests to join %s go to the team owners once you have "
                                           "accepted the User Agreement and completed your profile."
                                  % ", ".join(r.group.name for r in made))
            if verify:
                sent = signup_svc.register_email_send(user) and signup_svc.send_verification_email(request, user, email)
                if sent:
                    messages.info(request, "Welcome! We sent a link to your email address: open it and confirm the address "
                                           "to continue.")
                else:
                    messages.warning(request, "Welcome! We could not send the confirmation email; you can resend it from "
                                              "your settings page.")
            else:
                messages.success(request, "Welcome to YSE-PZ!")
            # /teams/ is gated: the agreement and the profile come first.
            return redirect("team_list")
    elif request.method in ("GET", "HEAD"):
        form = SignupForm()
    else:
        return HttpResponseBadRequest("GET or POST")
    return render(request, "YSE_App/signup.html", {"form": form})


@login_exempt
@onboarding_exempt
@never_cache
@csrf_protect
def signup_verify_email(request, token):
    """The emailed link. GET shows a Confirm button (mail scanners prefetch GETs); POST stores the address."""
    found = signup_svc.read_token(token)
    if found is None:
        return _no_store(render(request, "YSE_App/signup_verify.html", {"problem": "invalid"}, status=400))
    user, email = found
    if request.method == "POST":
        if signup_svc.confirm_email(user, email):
            messages.success(request, "Your email address is confirmed.")
            return _no_store(render(request, "YSE_App/signup_verify.html", {"confirmed": True}))
        return _no_store(render(request, "YSE_App/signup_verify.html", {"problem": "in_use"}, status=409))
    if request.method not in ("GET", "HEAD"):
        return HttpResponseBadRequest("GET or POST")
    return _no_store(render(request, "YSE_App/signup_verify.html", {"username": user.get_username(), "token": token}))


@login_required
@onboarding_exempt
@never_cache
def account_email(request):
    """Send (or re-send) a confirmation link for an address the signed-in user enters.

    Also the gate's first step for an account with no address (``?next=`` is kept so the
    page can offer to continue once the address is confirmed).
    """
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.method == "POST":
        form = EmailConfirmForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].strip()
            if not signup_svc.verification_enabled():
                if signup_svc.confirm_email(request.user, email):
                    messages.success(request, "Email address saved.")
                    if next_url:
                        return redirect(onboarding.safe_next(request, next_url))
                else:
                    messages.error(request, "That address is already used by another account.")
                return _email_page_redirect(next_url)
            if not signup_svc.register_email_send(request.user):
                messages.error(request, "Too many confirmation emails this hour; please try again later.")
            elif signup_svc.send_verification_email(request, request.user, email):
                messages.info(request, "We sent a confirmation link to that address. Open it, confirm, then come "
                                       "back here and continue.")
            else:
                messages.error(request, "The confirmation email could not be sent; please try again later.")
            return _email_page_redirect(next_url)
    elif request.method in ("GET", "HEAD"):
        form = EmailConfirmForm()
    else:
        return HttpResponseBadRequest("GET or POST")
    return render(request, "YSE_App/account_email.html", {
        "form": form, "has_email": bool(request.user.email),
        "verification": signup_svc.verification_enabled(),
        "required": onboarding.gate_enabled() and onboarding.email_required() and not request.user.email,
        "next_url": next_url,
        "continue_url": onboarding.safe_next(request, next_url) if request.user.email and next_url else "",
    })


def _email_page_redirect(next_url):
    from urllib.parse import urlencode
    url = reverse("account_email")
    return redirect("%s?%s" % (url, urlencode({"next": next_url})) if next_url else url)


def _onboarding_redirect(request):
    """POSTs pass the gate; a user who has not onboarded must not open or decide requests."""
    return onboarding.step_redirect(request)


@login_required
@require_POST
def team_request_create(request):
    if not membership.requests_enabled():
        raise Http404("Membership requests are not enabled.")
    pending = _onboarding_redirect(request)
    if pending is not None:
        return pending
    form = MembershipRequestForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Request not sent: check the note (at most %d characters)." % membership.MESSAGE_MAX_LENGTH)
        return redirect("team_list")
    group = Group.objects.filter(pk=form.cleaned_data["group"]).first()
    if group is None:
        messages.error(request, "This team is not taking membership requests.")
        return redirect("team_list")
    try:
        membership.create_request(request.user, group, form.cleaned_data.get("message") or "")
    except membership.MembershipRequestError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Request sent to the owners of %s." % group.name)
    return redirect("team_list")


@login_required
@require_POST
def team_request_cancel(request, request_id):
    req = get_object_or_404(GroupMembershipRequest, pk=request_id, user=request.user)
    try:
        membership.cancel(request.user, req)
    except membership.MembershipRequestError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Request withdrawn.")
    return redirect("team_list")


@login_required
@require_POST
def team_request_decide(request, group_id, request_id):
    group = get_object_or_404(Group, pk=group_id)
    req = get_object_or_404(GroupMembershipRequest.objects.select_related("group", "user"), pk=request_id, group=group)
    if not membership.can_decide(request.user, req):
        raise Http404("Not a team you manage.")
    pending = _onboarding_redirect(request)
    if pending is not None:
        return pending
    action = request.POST.get("action")
    if action not in ("approve", "deny"):
        return HttpResponseBadRequest("action must be approve or deny")
    try:
        membership.decide(request.user, req, approve=(action == "approve"))
    except membership.MembershipRequestError as exc:
        messages.error(request, str(exc))
    else:
        name = group_svc.display_name(req.user)
        if action == "approve":
            messages.success(request, "%s is now a member of %s." % (name, group.name))
        else:
            messages.success(request, "Request from %s denied." % name)
    return redirect("team_detail", group.pk)
