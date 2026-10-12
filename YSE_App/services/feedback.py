"""Feedback button helpers (#443): page-URL cleaning, the per-user limit, and
the staff-only "Create GitHub issue" call.

Posting happens only from :func:`YSE_App.feedback_views.feedback_review_detail`
when a staff member presses the button; nothing here runs on submission.

The GitHub token is an ``EncryptedCredential`` with
``service = settings.FEEDBACK_GITHUB_CREDENTIAL_SERVICE`` holding
``{"token": "..."}`` (a fine-grained token limited to Issues: write on the
repository). It is never read from settings.ini or code.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_PAGE_URL = 500
MAX_TEXT = 5000
MAX_TITLE = 200
# Query parameters dropped from the stored page URL (substring match, case-insensitive).
SENSITIVE_PARAM_PARTS = ("token", "key", "secret", "password", "passwd", "auth", "code", "sig", "session", "csrf")


class FeedbackGitHubError(Exception):
    """The GitHub issue could not be created (no token, HTTP error, bad reply)."""


def clean_page_url(raw: str) -> str:
    """Keep only the site path and non-sensitive query of ``raw`` (never a host or fragment)."""
    if not raw:
        return ""
    try:
        parts = urlsplit(str(raw).strip())
    except ValueError:
        return ""
    # One leading slash, no backslashes: the result is always a same-site path.
    path = "/" + (parts.path or "").replace("\\", "/").lstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not any(p in k.lower() for p in SENSITIVE_PARAM_PARTS)]
    cleaned = path + ("?" + urlencode(query) if query else "")
    return cleaned[:MAX_PAGE_URL]


def max_per_hour() -> int:
    return max(0, int(getattr(settings, "FEEDBACK_MAX_PER_HOUR", 5)))


def is_throttled(user) -> bool:
    """True once ``user`` has sent FEEDBACK_MAX_PER_HOUR items in the last hour.

    Counted from the Feedback table itself, so the limit holds across web
    processes without a shared cache.
    """
    from YSE_App.models import Feedback

    limit = max_per_hour()
    since = timezone.now() - datetime.timedelta(hours=1)
    return Feedback.objects.filter(user=user, created__gte=since).count() >= limit


def default_issue_title(feedback) -> str:
    title = (feedback.title or "").strip() or (feedback.text or "").strip().splitlines()[0][:80]
    return ("[%s] %s" % (feedback.get_kind_display(), title))[:MAX_TITLE]


def default_issue_body(feedback) -> str:
    """Draft body for staff to edit: the text and the page path, no reporter identity."""
    lines = [(feedback.text or "").strip(), "", "---"]
    if feedback.page_url:
        lines.append("Page: `%s`" % feedback.page_url.replace("`", ""))
    lines.append("Type: %s" % feedback.get_kind_display())
    lines.append("Sent with the YSE-PZ Feedback button and reviewed by staff before posting (feedback #%s)."
                 % feedback.pk)
    return "\n".join(lines)


def github_token(touch: bool = False) -> str:
    from YSE_App.models import EncryptedCredential

    service = getattr(settings, "FEEDBACK_GITHUB_CREDENTIAL_SERVICE", "github_feedback")
    for row in EncryptedCredential.objects.filter(service=service, is_active=True).order_by("pk"):
        if not row.has_secret:
            continue
        try:
            secret = row.get_secret(touch=touch) or {}
        except Exception as exc:  # noqa: BLE001 - key missing or rotated
            logger.warning("feedback GitHub credential %s unreadable: %s", row.pk, exc.__class__.__name__)
            continue
        token = str(secret.get("token") or "").strip()
        if token:
            return token
    return ""


def create_github_issue(title: str, body: str, *, token: Optional[str] = None) -> str:
    """Create the issue and return its ``html_url``; raise :class:`FeedbackGitHubError`."""
    token = token if token is not None else github_token(touch=True)
    if not token:
        raise FeedbackGitHubError(
            "No GitHub token is configured (EncryptedCredential service '%s')."
            % getattr(settings, "FEEDBACK_GITHUB_CREDENTIAL_SERVICE", "github_feedback"))
    repo = getattr(settings, "FEEDBACK_GITHUB_REPO", "Young-Supernova-Experiment/YSE_PZ")
    payload = {"title": title[:MAX_TITLE] or "Feedback", "body": body}
    label = getattr(settings, "FEEDBACK_GITHUB_LABEL", "feedback")
    if label:
        payload["labels"] = [label]
    try:
        response = requests.post(
            "%s/repos/%s/issues" % (GITHUB_API, repo),
            json=payload,
            headers={
                "Authorization": "Bearer %s" % token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "YSE-PZ-feedback",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise FeedbackGitHubError("GitHub request failed: %s" % exc.__class__.__name__) from exc
    if response.status_code != 201:
        raise FeedbackGitHubError("GitHub returned HTTP %s." % response.status_code)
    try:
        url = response.json().get("html_url") or ""
    except ValueError as exc:
        raise FeedbackGitHubError("GitHub returned a non-JSON reply.") from exc
    if not url:
        raise FeedbackGitHubError("GitHub reply had no issue URL.")
    return url
