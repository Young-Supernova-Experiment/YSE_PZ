"""Feedback button (#443): the user's POST and the staff review pages.

* ``POST /feedback/`` (login + CSRF; 404 while ``FEEDBACK_ENABLED`` is off)
  stores a :class:`~YSE_App.models.Feedback` row and nothing else.
* ``/feedback/review/`` and ``/feedback/review/<id>/`` (staff only) list the
  rows and let staff edit a draft and press "Create GitHub issue", the only
  path that posts to GitHub.
"""

from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from YSE_App.models import Feedback
from YSE_App.services import feedback as svc


def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _safe_next(request):
    candidate = request.POST.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return candidate
    return reverse("dashboard")


def _reply(request, ok, message, status=200):
    if _wants_json(request):
        return JsonResponse({"ok": ok, "message": message}, status=status)
    (messages.success if ok else messages.error)(request, message)
    return redirect(_safe_next(request))


@login_required
@require_POST
def feedback_submit(request):
    if not getattr(settings, "FEEDBACK_ENABLED", False):
        raise Http404
    text = (request.POST.get("text") or "").strip()
    title = (request.POST.get("title") or "").strip()
    kind = request.POST.get("kind") or Feedback.KIND_BUG
    if kind not in dict(Feedback.KIND_CHOICES):
        kind = Feedback.KIND_BUG
    if not text:
        return _reply(request, False, "Please describe the problem or idea.", status=400)
    if len(text) > svc.MAX_TEXT or len(title) > svc.MAX_TITLE:
        return _reply(request, False, "That is too long; please shorten it.", status=400)
    if svc.is_throttled(request.user):
        return _reply(request, False, "You have sent a lot of feedback in the last hour; please try again later.",
                      status=429)
    page_url = svc.clean_page_url(request.POST.get("page_url", "")) if request.POST.get("include_page") else ""
    Feedback.objects.create(user=request.user, kind=kind, title=title, text=text, page_url=page_url)
    return _reply(request, True, "Thank you. Your feedback was saved for the YSE-PZ team to review.")


@login_required
@staff_required
def feedback_review_list(request):
    status = request.GET.get("status") or ""
    rows = Feedback.objects.select_related("user").all()
    if status in dict(Feedback.STATUS_CHOICES):
        rows = rows.filter(status=status)
    return render(request, "YSE_App/feedback_review_list.html", {
        "rows": rows[:500],
        "status": status,
        "status_choices": Feedback.STATUS_CHOICES,
    })


@login_required
@staff_required
@require_http_methods(["GET", "POST"])
def feedback_review_detail(request, pk):
    item = get_object_or_404(Feedback.objects.select_related("user"), pk=pk)
    title = svc.default_issue_title(item)
    body = svc.default_issue_body(item)
    if request.method == "POST":
        action = request.POST.get("action") or ""
        title = (request.POST.get("issue_title") or "").strip()[:svc.MAX_TITLE]
        body = (request.POST.get("issue_body") or "").strip()
        if action == "create_issue":
            if not title or not body:
                messages.error(request, "The issue needs a title and a body.")
            else:
                with transaction.atomic():
                    # Row lock: a double click cannot create two issues.
                    item = Feedback.objects.select_for_update().get(pk=item.pk)
                    if item.github_issue_url:
                        messages.error(request, "This feedback already has a GitHub issue.")
                        return redirect("feedback_review_detail", pk=item.pk)
                    try:
                        url = svc.create_github_issue(title, body)
                    except svc.FeedbackGitHubError as exc:
                        url = ""
                        messages.error(request, "GitHub issue not created: %s" % exc)
                    if url:
                        item.github_issue_url = url
                        _mark(item, request.user, Feedback.STATUS_POSTED)
                        messages.success(request, "GitHub issue created.")
                        return redirect("feedback_review_detail", pk=item.pk)
        elif action in (Feedback.STATUS_REVIEWED, Feedback.STATUS_DISMISSED, Feedback.STATUS_NEW):
            if item.status == Feedback.STATUS_POSTED:
                messages.error(request, "Posted feedback keeps its status.")
            else:
                _mark(item, request.user, action)
                messages.success(request, "Marked %s." % item.get_status_display().lower())
            return redirect("feedback_review_detail", pk=item.pk)
        else:
            messages.error(request, "Unknown action.")
    return render(request, "YSE_App/feedback_review_detail.html", {
        "item": item,
        "issue_title": title,
        "issue_body": body,
        "github_repo": getattr(settings, "FEEDBACK_GITHUB_REPO", ""),
        "token_configured": bool(svc.github_token()) if not item.github_issue_url else True,
    })


def _mark(item, user, status):
    item.status = status
    item.reviewed_by = user
    item.reviewed_at = timezone.now()
    item.save(update_fields=["status", "reviewed_by", "reviewed_at", "github_issue_url"])
