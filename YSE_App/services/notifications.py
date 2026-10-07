"""Comment mentions -> notifications (issues #266, #322; part of #320).

``@username`` and ``@channel`` (everyone who can see the comment) are kept
from the original email sender; ``#instrument`` (case-insensitive, spaces and
punctuation ignored, e.g. ``#Binospec`` or ``#gpc1``) notifies the users who
hold an active ``TelescopeResource`` on that instrument's telescope (members
of the resource's groups, the PI by email) plus everyone following the
telescope with ``UserTelescopeToFollow``. Recipients are filtered by
``log_visible_to_user`` and the comment's author is excluded. Delivery goes
through ``YSE_App.services.notify.notify`` (in-app row, email/Slack per
preference); the optional Slack channel mirror is unchanged.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.html import escape

from YSE_App.models import Log
from YSE_App.models.instrument_models import Instrument
from YSE_App.models.profile_models import UserTelescopeToFollow
from YSE_App.models.telescope_resource_models import ClassicalResource, QueuedResource, ToOResource
from YSE_App.services import notify as notify_service
from YSE_App.services.visibility import log_visible_to_user

USER_MENTION_RE = re.compile(r"(?<![\w.])@(\w+)")
INSTRUMENT_MENTION_RE = re.compile(r"(?<![\w&])#([A-Za-z][\w.+\-]*)")
CHANNEL = "channel"


def _comment_base_url() -> str:
    if getattr(settings, "YSE_PUBLIC_BASE_URL", None):
        return settings.YSE_PUBLIC_BASE_URL.rstrip("/") + "/"
    if settings.DEBUG:
        return "http://127.0.0.1:8000/"
    return "https://ziggy.ucolick.org/yse/"


def normalise_instrument_name(name: str) -> str:
    """``"Keck I - LRIS"`` -> ``"keckilris"``: the key mention tokens are matched on."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class Mentions:
    """What a comment mentions: usernames, ``@channel`` and ``#instrument`` tokens."""

    def __init__(self, usernames: List[str], channel: bool, instrument_tokens: List[str]):
        self.usernames = usernames
        self.channel = channel
        self.instrument_tokens = instrument_tokens

    def __bool__(self):
        return bool(self.usernames or self.channel or self.instrument_tokens)


def parse_mentions(comment_text: str) -> Mentions:
    text = comment_text or ""
    usernames, seen = [], set()
    channel = False
    for name in USER_MENTION_RE.findall(text):
        if name.lower() == CHANNEL:
            channel = True
        elif name not in seen:
            seen.add(name)
            usernames.append(name)
    tokens, seen_tokens = [], set()
    for token in INSTRUMENT_MENTION_RE.findall(text):
        key = normalise_instrument_name(token)
        if key and key not in seen_tokens:
            seen_tokens.add(key)
            tokens.append(token)
    return Mentions(usernames, channel, tokens)


def match_instruments(tokens: Iterable[str]) -> List[Instrument]:
    """Instruments whose normalised name equals one of the ``#`` tokens."""
    keys = {normalise_instrument_name(t) for t in tokens if t}
    keys.discard("")
    if not keys:
        return []
    matched = []
    for instrument in Instrument.objects.select_related("telescope").order_by("name", "id"):
        if normalise_instrument_name(instrument.name) in keys:
            matched.append(instrument)
    return matched


def instrument_mention_users(instrument: Instrument, now=None) -> List[User]:
    """Users to alert for ``#instrument``: active-resource group members and PIs, telescope followers."""
    now = now or timezone.now()
    telescope = instrument.telescope
    users: Dict[int, User] = {}
    for model in (ClassicalResource, ToOResource, QueuedResource):
        resources = (
            model.objects.filter(telescope=telescope, end_date_valid__gte=now)
            .select_related("principal_investigator").prefetch_related("groups")
        )
        for resource in resources:
            for group in resource.groups.all():
                for user in group.user_set.filter(is_active=True):
                    users[user.pk] = user
            pi = resource.principal_investigator
            if pi is not None and pi.email:
                for user in User.objects.filter(email__iexact=pi.email, is_active=True):
                    users[user.pk] = user
    follows = UserTelescopeToFollow.objects.filter(telescope=telescope).select_related("profile__user")
    for follow in follows:
        user = follow.profile.user
        if user.is_active:
            users[user.pk] = user
    return list(users.values())


def collect_mention_users(comment_text: str, log: Optional[Log] = None) -> Tuple[List[User], List[Instrument]]:
    """Users mentioned in ``comment_text`` (and the instruments matched), visible-to-``log`` filtered.

    ``@channel`` addresses every active user who may view the comment; the
    author is *not* removed here (``notify_comment_mentions`` does that).
    """
    mentions = parse_mentions(comment_text)
    users: Dict[int, User] = {}
    if mentions.channel:
        for user in User.objects.filter(is_active=True).order_by("id"):
            users[user.pk] = user
    else:
        for username in mentions.usernames:
            user = User.objects.filter(username=username, is_active=True).first()
            if user is not None:
                users[user.pk] = user
    instruments = match_instruments(mentions.instrument_tokens)
    for instrument in instruments:
        for user in instrument_mention_users(instrument):
            users[user.pk] = user
    recipients = []
    for user in users.values():
        if log is not None and not log_visible_to_user(user, log):
            continue
        recipients.append(user)
    return recipients, instruments


def collect_mention_emails(comment_text: str, log: Optional[Log] = None) -> List[str]:
    """Sorted unique emails of the mentioned users (kept for callers of the old email sender)."""
    users, _instruments = collect_mention_users(comment_text, log=log)
    return sorted({u.email for u in users if u.email})


def comment_email_html(log: Log, base_url: str) -> str:
    """The HTML body the original email sender used, unchanged apart from escaping."""
    transient_name = log.transient.name
    return """\
<html>
<head></head>
<body>
<h1>Comment added!</h1>
<p>
<a href='%stransient_detail/%s/'>%s</a><br>
%s says:<br>
%s <br>
</p>
<br />
<p>Go to <a href='%sdashboard/'>YSE Dashboard</a></p>
</body>
</html>
""" % (
        base_url,
        log.transient.slug,
        escape(transient_name),
        escape(str(log.created_by)),
        escape(log.comment).replace("\n", "<br>"),
        base_url,
    )


def notify_comment_mentions(log: Log):
    """Create ``comment_mention`` notifications (and queue email/Slack) for a comment's mentions."""
    if not log.transient_id or not log.comment:
        return []
    users, instruments = collect_mention_users(log.comment, log=log)
    if not users:
        return []
    transient = log.transient
    base_url = notify_service.base_url()
    subject = "new comment added to event %s" % transient.name
    text = "%s commented on %s:\n%s" % (log.created_by, transient.name, log.comment)
    payload = {"log_id": log.pk, "mentioned_instruments": [i.name for i in instruments]}
    return notify_service.notify(
        users, text, "/transient_detail/%s/" % transient.slug, "comment_mention",
        subject=subject, transient=transient, payload=payload, created_by=log.created_by,
        html=comment_email_html(log, base_url), exclude=[log.created_by_id],
    )


# Old name, kept for imports elsewhere.
notify_email_mentions = notify_comment_mentions


def notify_slack_transient_comment(log: Log) -> None:
    if not getattr(settings, "SLACK_ENABLED", False):
        return
    from YSE_App.integrations.slack.outbound import post_transient_comment

    post_transient_comment(log)


def notify_transient_comment(log: Log) -> None:
    notify_comment_mentions(log)
    notify_slack_transient_comment(log)


# --- autocomplete ----------------------------------------------------------

def mention_suggestions(query: str = "", limit: int = 10, *, users: bool = True, instruments: bool = True) -> List[dict]:
    """``@user`` / ``#instrument`` completions for the comment box (``/notifications/mention_suggest.json``)."""
    q = (query or "").strip().lstrip("@#")
    out: List[dict] = []
    if users:
        qs = User.objects.filter(is_active=True).exclude(username="admin").order_by("username")
        if q:
            from django.db.models import Q

            qs = qs.filter(Q(username__istartswith=q) | Q(first_name__istartswith=q) | Q(last_name__istartswith=q))
        for user in qs[:limit]:
            full = ("%s %s" % (user.first_name, user.last_name)).strip()
            out.append({"type": "user", "value": "@%s" % user.username,
                        "label": ("%s (%s)" % (full, user.username)) if full else user.username})
        if not q or CHANNEL.startswith(q.lower()):
            out.append({"type": "user", "value": "@channel", "label": "everyone who can see this comment"})
    if instruments:
        qs = Instrument.objects.select_related("telescope").order_by("name")
        if q:
            qs = qs.filter(name__icontains=q)
        for instrument in qs[:limit]:
            out.append({"type": "instrument", "value": "#%s" % re.sub(r"\s+", "", instrument.name),
                        "label": "%s (%s)" % (instrument.name, instrument.telescope.name)})
    return out
