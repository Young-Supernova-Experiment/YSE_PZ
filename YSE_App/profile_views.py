"""User agreement, researcher profile and "My students" pages (#627, #628, #630)."""

from __future__ import annotations

import datetime
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from YSE_App import onboarding
from YSE_App.models import AgreementAcceptance, Mentorship, ResearcherMetrics, ResearcherProfile, UserAgreement
from YSE_App.models.researcher_models import DEMOGRAPHIC_FIELDS, ENCRYPTED_FIELDS, STUDENT_LEVELS
from YSE_App.profile_forms import (
    AgreementForm, ResearchConsentChoiceForm, ResearcherProfileForm, StudentForm, StudentUpdateForm,
)
from YSE_App.services import research_consent as rc
from YSE_App.services import research_crypto
from YSE_App.services import researcher_profile as rp

CONFIRM_INTERVAL = datetime.timedelta(days=365)
ACKNOWLEDGEMENTS_PLACEHOLDER = "{acknowledgements_url}"


def render_agreement_body(agreement: UserAgreement) -> str:
    """The stored HTML with the acknowledgements link resolved for this stack's URL prefix."""
    return agreement.body.replace(ACKNOWLEDGEMENTS_PLACEHOLDER, reverse("acknowledgements"))


@login_required
def agreement(request):
    current_id = onboarding.current_agreement_id()
    current = UserAgreement.objects.filter(pk=current_id).first() if current_id else None
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    accepted = None
    if current is not None:
        accepted = AgreementAcceptance.objects.filter(user=request.user, agreement=current).first()
    if request.method == "POST":
        if current is None:
            return redirect(onboarding.safe_next(request, next_url))
        form = AgreementForm(request.POST)
        if form.is_valid():
            AgreementAcceptance.objects.get_or_create(user=request.user, agreement=current)
            onboarding.clear_session_flag(request)
            messages.success(request, "Thank you: you accepted the YSE-PZ User Agreement (%s)." % current.version)
            destination = onboarding.safe_next(request, next_url)
            if rc.needs_prompt(request.user):
                # The voluntary research consent form, offered once (#744). Not a gate:
                # consenting and declining both continue to ``destination``.
                return redirect("%s?%s" % (reverse("research_consent"), urlencode({"next": destination})))
            return redirect(destination)
    elif request.method in ("GET", "HEAD"):
        if accepted is not None and next_url:
            # Already accepted (a second tab, the Back button, a restored session):
            # carry on to the page asked for instead of stopping here (#634).
            return redirect(onboarding.safe_next(request, next_url))
        form = AgreementForm(initial={"next": next_url})
    else:
        return HttpResponseBadRequest("GET or POST")
    return render(request, "YSE_App/agreement.html", {
        "continue_url": onboarding.safe_next(request, next_url),
        "agreement": current,
        "agreement_body": render_agreement_body(current) if current else "",
        "accepted": accepted,
        "form": form,
        "next_url": next_url,
    })


def _encrypted_rows(user, key_available):
    status = rp.encrypted_status(user)
    labels = {"country": "Country", "phd_year": "PhD year", "gender": "Gender"}
    return [{"name": name, "label": labels[name], "row": status.get(name)} for name in ENCRYPTED_FIELDS], status


@login_required
def research_consent(request):
    """The voluntary research consent form (#744): consent, decline, or later withdraw.

    Never a gate: both choices continue to ``next`` (default: the profile page).
    """
    user = request.user
    form_row = rc.current_form()
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    continue_url = onboarding.safe_next(request, next_url, fallback=reverse("researcher_profile"))
    state = rc.status(user)
    if request.method == "POST":
        if form_row is None:
            return redirect(continue_url)
        form = ResearchConsentChoiceForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data["action"]
            decision = {"consent": rc.CONSENTED, "decline": rc.DECLINED, "withdraw": rc.WITHDRAWN}[action]
            try:
                _record, deleted = rc.record_decision(user, decision, form=form_row)
            except rc.ConsentError:
                messages.info(request, "Nothing changed: that choice does not apply to your current status.")
                return redirect(continue_url)
            if decision == rc.CONSENTED:
                messages.success(request, "Thank you for taking part. You can add the optional demographic "
                                          "information on your profile.")
            elif decision == rc.DECLINED:
                messages.success(request, "You declined to take part in the study. You can change this on your "
                                          "profile at any time.")
            else:
                messages.success(request, "You withdrew from the study. Your activity since the form went "
                                          "live will not be used in any research dataset.")
            if deleted:
                messages.info(request, "Your stored demographic information was deleted.")
            return redirect(continue_url)
    elif request.method in ("GET", "HEAD"):
        form = ResearchConsentChoiceForm(initial={"next": next_url})
    else:
        return HttpResponseBadRequest("GET or POST")
    return render(request, "YSE_App/research_consent.html", {
        "consent_form": form_row,
        "form": form,
        "state": state,
        "latest": rc.latest_record(user),
        "next_url": next_url,
        "continue_url": continue_url,
    })


@login_required
@require_GET
def researcher_profile(request):
    user = request.user
    profile = rp.get_profile(user)
    key_available = research_crypto.key_available()
    encrypted_rows, _status = _encrypted_rows(user, key_available)
    missing = rp.missing_fields(user, profile, key_available) if profile else ["profile"]
    can_mentor = bool(profile and profile.can_mentor)
    confirm_due = bool(profile and (profile.profile_confirmed_at is None
                                    or timezone.now() - profile.profile_confirmed_at > CONFIRM_INTERVAL))
    return render(request, "YSE_App/researcher_profile.html", {
        "profile": profile,
        "encrypted_rows": encrypted_rows,
        "key_available": key_available,
        "missing": rp.labels(m for m in missing if m != "profile"),
        "incomplete": bool(missing),
        "advisor": rp.current_advisor(user),
        "mentors": Mentorship.objects.filter(mentee=user).select_related("mentor").order_by("-start_year", "-id"),
        "can_mentor": can_mentor,
        "students": list(rp.my_students(user)) if can_mentor else [],
        "student_form": StudentForm(mentor=user) if can_mentor else None,
        "kind_choices": Mentorship.KIND_CHOICES,
        "metrics": ResearcherMetrics.objects.filter(user=user).order_by("-fetched_at").first(),
        "confirm_due": confirm_due,
        "observing": list(profile.observing_experience.select_related("telescope")) if profile else [],
        "research_form": rc.current_form(),
        "research_state": rc.status(user),
        "research_latest": rc.latest_record(user),
        "demographic_fields": DEMOGRAPHIC_FIELDS,
    })


@login_required
def researcher_profile_edit(request):
    user = request.user
    profile = rp.get_profile(user) or ResearcherProfile(user=user)
    key_available = research_crypto.key_available()
    encrypted_rows, status = _encrypted_rows(user, key_available)
    advisor = rp.current_advisor(user)
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    research_consented = rc.has_consented(user)
    form_kwargs = dict(user=user, key_available=key_available, encrypted_status=status, advisor=advisor,
                       research_consented=research_consented, instance=profile)
    if request.method == "POST":
        # Snapshot first: ModelForm validation writes the new values onto ``profile``.
        before = rp.snapshot(profile) if profile.pk else {name: "" for name in rp.PLAIN_FIELDS}
        form = ResearcherProfileForm(request.POST, **form_kwargs)
        if form.is_valid():
            _save_profile(request, form, before)
            missing = rp.missing_fields(user)
            if missing:
                messages.warning(request, "Saved. Still required: %s." % ", ".join(l for _n, l in rp.labels(missing)))
                url = reverse("researcher_profile_edit")
                return redirect("%s?%s" % (url, urlencode({"next": next_url})) if next_url else url)
            messages.success(request, "Profile saved.")
            return redirect(onboarding.safe_next(request, next_url, fallback=reverse("researcher_profile")))
    elif request.method in ("GET", "HEAD"):
        form = ResearcherProfileForm(**form_kwargs)
    else:
        return HttpResponseBadRequest("GET or POST")
    missing = rp.missing_fields(user, profile if profile.pk else None, key_available)
    return render(request, "YSE_App/researcher_profile_edit.html", {
        "form": form,
        "profile": profile,
        "key_available": key_available,
        "encrypted_rows": encrypted_rows,
        "missing": rp.labels(m for m in missing if m != "profile"),
        "is_new": not profile.pk,
        "next_url": next_url,
        "student_levels": sorted(STUDENT_LEVELS),
        "research_consented": research_consented,
        "demographic_fields": DEMOGRAPHIC_FIELDS,
    })


@transaction.atomic
def _save_profile(request, form, before):
    user = request.user
    obj = form.save(commit=False)
    obj.user = user
    obj.save()
    form.save_m2m()
    rp.record_plain_changes(user, before, rp.snapshot(obj), changed_by=user)
    to_set, to_clear = form.encrypted_changes()
    for name in to_clear:
        rp.clear_encrypted(user, name, changed_by=user)
    for name, value in to_set.items():
        try:
            rp.set_encrypted(user, name, value, changed_by=user)
        except research_crypto.ResearchKeyError:
            messages.warning(request, "Encrypted fields are not available yet; %s was not stored." % name)
    if obj.career_level in STUDENT_LEVELS:
        data = form.cleaned_data
        rp.set_current_advisor(user, data.get("advisor_user"), data.get("advisor_name", ""), changed_by=user)
    if not obj.profile_completed_at and not rp.missing_fields(user, obj):
        obj.profile_completed_at = timezone.now()
        obj.profile_confirmed_at = obj.profile_confirmed_at or obj.profile_completed_at
        obj.save(update_fields=["profile_completed_at", "profile_confirmed_at", "updated_at"])
    onboarding.clear_session_flag(request)


@login_required
@require_POST
def researcher_profile_confirm(request):
    profile = rp.get_profile(request.user)
    if profile is None:
        return redirect("researcher_profile_edit")
    profile.profile_confirmed_at = timezone.now()
    profile.save(update_fields=["profile_confirmed_at", "updated_at"])
    messages.success(request, "Thanks: your profile is confirmed as current.")
    return redirect("researcher_profile")


SUGGEST_MIN_CHARS = 2
SUGGEST_MAX_RESULTS = 10
SUGGEST_THROTTLE_REQUESTS = 40
SUGGEST_THROTTLE_SECONDS = 60


def _suggest_throttled(user):
    """True once ``user`` has made more than SUGGEST_THROTTLE_REQUESTS lookups in the window."""
    key = "yse:profile_suggest:%s" % user.pk
    if cache.add(key, 1, SUGGEST_THROTTLE_SECONDS):
        return False
    try:
        return cache.incr(key) > SUGGEST_THROTTLE_REQUESTS
    except ValueError:  # key expired between add and incr
        cache.add(key, 1, SUGGEST_THROTTLE_SECONDS)
        return False


@login_required
@require_GET
def researcher_advisor_suggest(request):
    """JSON ``{"results": [{"id", "name", "institution", "label"}]}``: active users matching ``q``.

    Type-ahead for the advisor and student pickers (#628, #638). Deliberately minimal: full name and
    institution only (never email, username or any encrypted field), at most 10 results, at least
    2 typed characters, and a per-user throttle (429).
    """
    q = (request.GET.get("q") or "").strip()[:64]
    if len(q) < SUGGEST_MIN_CHARS:
        return JsonResponse({"results": []})
    if _suggest_throttled(request.user):
        return JsonResponse({"results": [], "error": "Too many lookups; wait a moment."}, status=429)
    users = (User.objects.filter(is_active=True).exclude(pk=request.user.pk)
             .filter(Q(first_name__icontains=q) | Q(last_name__icontains=q)
                     | Q(researcher_profile__full_name__icontains=q))
             .select_related("researcher_profile").order_by("last_name", "first_name", "pk").distinct()[:SUGGEST_MAX_RESULTS])
    results, seen = [], {}
    for u in users:
        profile = getattr(u, "researcher_profile", None)
        name = ((profile.full_name if profile and profile.full_name else u.get_full_name()) or "").strip()
        if not name:
            continue
        institution = (profile.institution if profile else "") or ""
        label = "%s, %s" % (name, institution) if institution else name
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:  # keep labels unique so the picker can map a label to one user
            label = "%s [%d]" % (label, seen[label])
        results.append({"id": u.pk, "name": name, "institution": institution, "label": label})
    return JsonResponse({"results": results})


def _require_mentor(request):
    if not rp.can_manage_students(request.user):
        raise Http404("Only scientific staff and faculty manage students here.")


@login_required
@require_POST
def researcher_student_add(request):
    _require_mentor(request)
    form = StudentForm(request.POST, mentor=request.user)
    if form.is_valid():
        data = form.cleaned_data
        student = data["student_user"]
        Mentorship.objects.create(
            mentor=request.user, mentee=student, mentee_name="" if student else data["student_name"].strip(),
            kind=data["kind"], start_year=data.get("start_year"), end_year=data.get("end_year"),
            created_by=request.user)
        messages.success(request, "Student added.")
    else:
        errors = "; ".join("%s" % e for errs in form.errors.values() for e in errs)
        messages.error(request, "Student not added: %s" % errors)
    return redirect(reverse("researcher_profile") + "#my-students")


@login_required
@require_POST
def researcher_student_update(request, mentorship_id):
    _require_mentor(request)
    row = get_object_or_404(Mentorship, pk=mentorship_id, mentor=request.user)
    form = StudentUpdateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Not saved: enter a valid year.")
    elif form.cleaned_data.get("remove"):
        row.delete()
        messages.success(request, "Student removed from your list.")
    else:
        row.end_year = form.cleaned_data.get("end_year")
        if row.start_year and row.end_year and row.end_year < row.start_year:
            messages.error(request, "The end year is before the start year.")
        else:
            row.save(update_fields=["end_year"])
            messages.success(request, "Saved.")
    return redirect(reverse("researcher_profile") + "#my-students")


# The onboarding pages must never be gated themselves.
for _view in ("agreement", "research_consent", "researcher_profile", "researcher_profile_edit", "researcher_profile_confirm",
              "researcher_advisor_suggest", "researcher_student_add", "researcher_student_update"):
    globals()[_view].onboarding_exempt = True
del _view
