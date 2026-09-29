"""Notification queue (issues #266 and #320, part of #69).

``notify(users, text, url, kind)`` writes one ``Notification`` row per
recipient (the in-app list and badge read those rows) and enqueues one
``notifications.deliver`` job per notification that also needs email or a
Slack webhook post, so the request path never waits on SMTP or Slack. The
job handlers (``notifications.deliver``, ``notifications.prune``) live here
too; ``YSE_App.jobs`` imports this module on its first pass
(``DEFAULT_HANDLER_MODULES``).

Per-user switches: ``NotificationPreference`` (global in_app, email,
slack_webhook_url, plus the per-kind-group matrix in ``kinds``). Site
switches: ``NOTIFICATION_EMAIL_ENABLED`` (default: on when ``[SMTP_provider]``
holds credentials), ``NOTIFICATION_SLACK_ENABLED``,
``NOTIFICATION_EMAIL_SUBJECT_PREFIX``, ``NOTIFICATION_BASE_URL``; retention:
``NOTIFICATION_RETENTION_DAYS``, ``NOTIFICATION_UNREAD_RETENTION_DAYS``,
``JOB_RETENTION_DAYS``.

Every sender in the application goes through ``notify()``: comment mentions
(``YSE_App.services.notifications``), follow-up notices
(``YSE_App.services.followup_notices``), transient alerts and upload-failure
emails (``YSE_App.common.alert``, ``YSE_App.data_utils``). An HTML email body
is passed as ``html=`` and stored in ``Notification.payload["html"]``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Iterable, List, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives, send_mail
from django.utils import timezone
from django.utils.html import strip_tags

from YSE_App.jobs import JobRetry, enqueue, job
from YSE_App.models.job_models import Job
from YSE_App.models.notification_models import Notification, NotificationPreference

logger = logging.getLogger(__name__)

DELIVER_KIND = "notifications.deliver"
PRUNE_KIND = "notifications.prune"
IN_APP = "in_app"
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


def email_enabled() -> bool:
    return bool(_setting("NOTIFICATION_EMAIL_ENABLED", False))


def channels_for(user: User, pref: Optional[NotificationPreference] = None, kind: str = "system") -> List[str]:
    """Out-of-app channels a notification of ``kind`` to ``user`` must be delivered on.

    A channel applies when the site switch, the user's global switch and the
    switch for the kind's preference group are all on.
    """
    pref = pref or NotificationPreference.for_user(user)
    channels = []
    if pref.email and user.email and email_enabled() and pref.allows(kind, EMAIL):
        channels.append(EMAIL)
    if pref.slack_webhook_url and _setting("NOTIFICATION_SLACK_ENABLED", True) and pref.allows(kind, SLACK):
        channels.append(SLACK)
    return channels


def in_app_for(pref: NotificationPreference, kind: str = "system") -> bool:
    """True when the notification should show (unread) in the in-app list."""
    return bool(pref.in_app and pref.allows(kind, IN_APP))


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
           subject: str = "", transient=None, payload=None, created_by=None, html: str = "",
           exclude=None) -> List[Notification]:
    """Record a notification for each user and queue its email/Slack delivery.

    Returns the ``Notification`` rows created (one per recipient whose
    preferences accept at least one channel for ``kind``). Users with in-app
    off for the kind still get a row when email or Slack is on, pre-marked
    read so it does not count as unread. Delivery jobs are created only for
    channels that apply. ``html`` is an optional HTML email body (the plain
    ``text`` stays the text part); ``exclude`` lists users (or ids) to skip,
    typically the actor.
    """
    if not text:
        raise ValueError("notify() needs a non-empty text")
    kind = kind or "system"
    now = timezone.now()
    skip = {getattr(u, "pk", u) for u in (exclude or ()) if u is not None}
    if html:
        payload = dict(payload or {})
        payload["html"] = html
    created = []
    for user in _unique_users(users):
        if user.pk in skip:
            continue
        pref = NotificationPreference.for_user(user)
        channels = channels_for(user, pref, kind)
        in_app = in_app_for(pref, kind)
        if not in_app and not channels:
            continue
        notification = Notification.objects.create(
            recipient=user,
            kind=kind,
            subject=(subject or "")[:200],
            text=text,
            url=(url or "")[:500],
            transient=transient,
            payload=payload,
            delivered={IN_APP: {"sent_at": now.isoformat()}} if in_app else {},
            read_at=None if in_app else now,
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
    html = notification.html
    if html:
        message = EmailMultiAlternatives(email_subject(notification), email_body(notification), None, [user.email])
        message.attach_alternative(html, "text/html")
        message.send(fail_silently=False)
        return
    send_mail(email_subject(notification), email_body(notification), None, [user.email], fail_silently=False)


def html_to_text(html: str) -> str:
    """Plain-text fallback for a legacy HTML email body (tags dropped, blank lines squeezed)."""
    text = strip_tags((html or "").replace("<br>", "\n").replace("<br />", "\n").replace("</p>", "\n"))
    lines = [line.strip() for line in text.splitlines()]
    out = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


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


# --- retention ------------------------------------------------------------

def _days(name, default):
    try:
        return int(_setting(name, default))
    except (TypeError, ValueError):
        return int(default)


def prune(now=None, *, read_days=None, unread_days=None, job_days=None, dry_run=False) -> dict:
    """Delete old notification and job rows; returns counts per table.

    Read notifications older than ``NOTIFICATION_RETENTION_DAYS`` (default 90)
    and unread ones older than ``NOTIFICATION_UNREAD_RETENTION_DAYS`` (default
    365) go, as do finished jobs (``done`` / ``failed`` / ``cancelled``) older
    than ``JOB_RETENTION_DAYS`` (default 30). A value of 0 disables that
    part. ``dry_run`` only counts.
    """
    now = now or timezone.now()
    read_days = _days("NOTIFICATION_RETENTION_DAYS", 90) if read_days is None else int(read_days)
    unread_days = _days("NOTIFICATION_UNREAD_RETENTION_DAYS", 365) if unread_days is None else int(unread_days)
    job_days = _days("JOB_RETENTION_DAYS", 30) if job_days is None else int(job_days)
    result = {"notifications_read": 0, "notifications_unread": 0, "jobs": 0}

    def _apply(qs, key):
        n = qs.count() if dry_run else qs.delete()[0]
        result[key] = n

    if read_days > 0:
        cutoff = now - datetime.timedelta(days=read_days)
        _apply(Notification.objects.filter(read_at__isnull=False, created__lt=cutoff), "notifications_read")
    if unread_days > 0:
        cutoff = now - datetime.timedelta(days=unread_days)
        _apply(Notification.objects.filter(read_at__isnull=True, created__lt=cutoff), "notifications_unread")
    if job_days > 0:
        cutoff = now - datetime.timedelta(days=job_days)
        _apply(Job.objects.filter(status__in=Job.FINAL_STATUSES, finished_at__lt=cutoff), "jobs")
    logger.info("notification prune%s: %d read + %d unread notifications, %d finished jobs",
                " (dry run)" if dry_run else "", result["notifications_read"],
                result["notifications_unread"], result["jobs"])
    return result


@job(PRUNE_KIND, max_attempts=1)
def prune_job(payload, job=None):
    """Job handler: ``prune()`` with optional ``read_days`` / ``unread_days`` / ``job_days`` overrides."""
    payload = payload or {}
    return prune(read_days=payload.get("read_days"), unread_days=payload.get("unread_days"),
                 job_days=payload.get("job_days"), dry_run=bool(payload.get("dry_run")))
