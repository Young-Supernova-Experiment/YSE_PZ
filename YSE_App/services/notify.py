"""Notification queue (issue #266, part of #69).

``notify(users, text, url, kind)`` writes one ``Notification`` row per
recipient (the in-app list and badge read those rows) and enqueues one
``notifications.deliver`` job per notification that also needs email or a
Slack webhook post, so the request path never waits on SMTP or Slack. The
job handler lives here too; ``YSE_App.jobs`` imports this module on its
first pass (``DEFAULT_HANDLER_MODULES``).

Per-user switches: ``NotificationPreference`` (in_app, email,
slack_webhook_url). Site switches: ``NOTIFICATION_EMAIL_ENABLED`` (default
False until SMTP is configured), ``NOTIFICATION_SLACK_ENABLED``,
``NOTIFICATION_EMAIL_SUBJECT_PREFIX``, ``NOTIFICATION_BASE_URL``.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.utils import timezone

from YSE_App.jobs import JobRetry, enqueue, job
from YSE_App.models.notification_models import Notification, NotificationPreference

logger = logging.getLogger(__name__)

DELIVER_KIND = "notifications.deliver"
EMAIL = "email"
SLACK = "slack"
CHANNELS = (EMAIL, SLACK)


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def base_url() -> str:
    url = _setting("NOTIFICATION_BASE_URL", "") or _setting("YSE_PUBLIC_BASE_URL", "")
    if not url:
        from YSE_App.services.notifications import _comment_base_url

        url = _comment_base_url()
    return str(url).rstrip("/") + "/"


def absolute_url(url: str) -> str:
    """Make a stored (usually site-relative) notification URL absolute for email/Slack."""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return base_url() + url.lstrip("/")


def channels_for(user: User, pref: Optional[NotificationPreference] = None) -> List[str]:
    """Out-of-app channels a notification to ``user`` must be delivered on."""
    pref = pref or NotificationPreference.for_user(user)
    channels = []
    if pref.email and user.email and _setting("NOTIFICATION_EMAIL_ENABLED", False):
        channels.append(EMAIL)
    if pref.slack_webhook_url and _setting("NOTIFICATION_SLACK_ENABLED", True):
        channels.append(SLACK)
    return channels


def _unique_users(users) -> List[User]:
    if isinstance(users, User):
        users = [users]
    seen = set()
    out = []
    for user in users:
        if user is None or not getattr(user, "pk", None) or user.pk in seen:
            continue
        if not getattr(user, "is_active", True):
            continue
        seen.add(user.pk)
        out.append(user)
    return out


def notify(users: Iterable[User], text: str, url: str = "", kind: str = "system", *,
           subject: str = "", transient=None, payload=None, created_by=None) -> List[Notification]:
    """Record a notification for each user and queue its email/Slack delivery.

    Returns the ``Notification`` rows created (one per recipient whose
    preferences accept at least one channel). Users with ``in_app`` off still
    get a row when email or Slack is on, pre-marked read so it does not count
    as unread. Delivery jobs are created only for channels that apply.
    """
    if not text:
        raise ValueError("notify() needs a non-empty text")
    now = timezone.now()
    created = []
    for user in _unique_users(users):
        pref = NotificationPreference.for_user(user)
        channels = channels_for(user, pref)
        if not pref.in_app and not channels:
            continue
        notification = Notification.objects.create(
            recipient=user,
            kind=kind or "system",
            subject=(subject or "")[:200],
            text=text,
            url=(url or "")[:500],
            transient=transient,
            payload=payload,
            delivered={"in_app": {"sent_at": now.isoformat()}} if pref.in_app else {},
            read_at=None if pref.in_app else now,
        )
        if channels:
            enqueue(DELIVER_KIND, {"notification_id": notification.pk, "channels": channels},
                    created_by=created_by, transient=transient)
        created.append(notification)
    return created


def unread_count(user: User) -> int:
    if not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_all_read(user: User) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())


# --- delivery -------------------------------------------------------------

def email_subject(notification: Notification) -> str:
    prefix = _setting("NOTIFICATION_EMAIL_SUBJECT_PREFIX", "[YSE-PZ] ")
    subject = notification.subject or notification.text.strip().splitlines()[0][:80]
    return ("%s %s" % (prefix.strip(), subject)).strip()


def email_body(notification: Notification) -> str:
    lines = [notification.text.strip()]
    link = absolute_url(notification.url)
    if link:
        lines += ["", link]
    lines += ["", "-- ", "YSE-PZ notifications: %snotifications/preferences/" % base_url()]
    return "\n".join(lines)


def send_email(notification: Notification) -> None:
    user = notification.recipient
    if not user.email:
        raise ValueError("recipient %s has no email address" % user.username)
    send_mail(email_subject(notification), email_body(notification), None, [user.email], fail_silently=False)


def slack_text(notification: Notification) -> str:
    text = notification.text.strip()
    link = absolute_url(notification.url)
    if link:
        text = "%s\n<%s|Open in YSE-PZ>" % (text, link)
    if notification.subject:
        text = "*%s*\n%s" % (notification.subject, text)
    return text


def send_slack_webhook(notification: Notification) -> None:
    import requests

    pref = NotificationPreference.for_user(notification.recipient)
    if not pref.slack_webhook_url:
        raise ValueError("recipient %s has no Slack webhook" % notification.recipient.username)
    timeout = float(_setting("NOTIFICATION_SLACK_TIMEOUT_SECONDS", 10))
    response = requests.post(pref.slack_webhook_url, json={"text": slack_text(notification)}, timeout=timeout)
    response.raise_for_status()


SENDERS = {EMAIL: send_email, SLACK: send_slack_webhook}


@job(DELIVER_KIND)
def deliver_notification(payload, job=None):
    """Job handler: deliver one notification on the channels in the payload.

    Each channel's outcome is written to ``Notification.delivered``; a channel
    that already has ``sent_at`` is skipped on retry. Any failed channel makes
    the job retry (``JobRetry``); the error text stays on the row either way.
    """
    notification_id = int(payload["notification_id"])
    channels = [c for c in payload.get("channels") or CHANNELS if c in SENDERS]
    notification = Notification.objects.select_related("recipient").filter(pk=notification_id).first()
    if notification is None:
        return {"skipped": "notification %s no longer exists" % notification_id}
    delivered = dict(notification.delivered or {})
    errors = {}
    for channel in channels:
        state = dict(delivered.get(channel) or {})
        if state.get("sent_at"):
            continue
        try:
            SENDERS[channel](notification)
        except Exception as exc:  # noqa: BLE001 - recorded on the row, job retries
            logger.warning("notification %s: %s delivery failed: %s", notification_id, channel, exc)
            state["error"] = str(exc)[:500]
            state["attempts"] = int(state.get("attempts") or 0) + 1
            state["failed_at"] = timezone.now().isoformat()
            errors[channel] = str(exc)
        else:
            state.pop("error", None)
            state["sent_at"] = timezone.now().isoformat()
        delivered[channel] = state
    notification.delivered = delivered
    notification.save(update_fields=["delivered"])
    if errors:
        raise JobRetry("; ".join("%s: %s" % (k, v) for k, v in sorted(errors.items())))
    return {"channels": channels, "delivered": delivered}
