"""Follow-up request notices (issue #69; part of #320).

When a ``TransientFollowup`` is created, everyone who follows its telescope
(``UserTelescopeToFollow``, the "Followup Notifications Requested for These
Telescopes" box on the personal dashboard) gets a ``followup_request``
notification through ``notify()``: an in-app row and, per preference, the
same HTML email ``YSE_App.common.alert.SendFollowingNotice`` used to send.
The requester is not notified about their own request.

Two more notices live here so the whole follow-up trail stays in one module (the
planned ``FollowupEvent`` writer can call them instead of the signals):

* ``notify_request_added`` - a new ``TransientFollowupRequest`` on an existing
  follow-up (another requester, or a new priority) sends a ``followup_request``
  "updated" notice to the telescope's followers and to earlier requesters. A
  person is told once per ``FOLLOWUP_NOTICE_COLLAPSE_SECONDS`` (default 600)
  per follow-up, so a burst of submits is one notice.
* ``notify_status_changed`` - a changed ``TransientFollowup.status`` sends
  ``followup_status`` to ``requested_by`` and everyone in ``followup.requests``.

Both skip the actor and anyone who may not see the follow-up (``followup_visible_to_user``
plus the transient listing rule), and never put priorities or comments in the text.
"""

from __future__ import annotations

import datetime
import logging
from typing import List, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.html import escape

from YSE_App.models.notification_models import Notification
from YSE_App.models.profile_models import UserTelescopeToFollow
from YSE_App.services import notify as notify_service

logger = logging.getLogger(__name__)


def followup_telescope(followup):
    """The telescope of whichever resource the follow-up targets (classical, ToO or queued)."""
    for attr in ("classical_resource", "too_resource", "queued_resource"):
        resource = getattr(followup, attr, None)
        if resource is not None:
            return resource.telescope
    return None


def telescope_followers(telescope) -> List[User]:
    users = {}
    follows = UserTelescopeToFollow.objects.filter(telescope=telescope).select_related("profile__user")
    for follow in follows:
        user = follow.profile.user
        if user.is_active:
            users[user.pk] = user
    return list(users.values())


def followup_notice_html(transient_name: str, transient_slug: str, telescope_name: str, base_url: str,
                         updated: bool = False) -> str:
    return """\
<html>
<head></head>
<body>
<h2>%s Followup %s for <a href='%stransient_detail/%s/'>%s</a></h2>
<br />
<p>Go to <a href='%sdashboard/'>YSE Dashboard</a></p>
</body>
</html>
""" % (escape(telescope_name), "request updated" if updated else "requested", base_url, transient_slug,
       escape(transient_name), base_url)


def notify_followup_created(followup, *, recipients: Optional[List[User]] = None, exclude_requester: bool = True):
    """``followup_request`` notifications for a new follow-up; returns the rows created."""
    telescope = followup_telescope(followup)
    if telescope is None:
        return []
    users = recipients if recipients is not None else telescope_followers(telescope)
    exclude = [followup.created_by_id] if exclude_requester and followup.created_by_id else []
    # Only people who may see the follow-up and list the transient (#730, same rule as the other notices).
    users = _visible_recipients(users, followup, set(exclude))
    if not users:
        return []
    transient = followup.transient
    base_url = notify_service.base_url()
    subject = "New %s request for %s" % (telescope.name, transient.name)
    text = "%s follow-up requested for %s by %s." % (telescope.name, transient.name, followup.created_by)
    return notify_service.notify(
        users, text, "/transient_detail/%s/" % transient.slug, "followup_request",
        subject=subject, transient=transient, created_by=followup.created_by,
        payload={"followup_id": followup.pk, "telescope": telescope.name},
        html=followup_notice_html(transient.name, transient.slug, telescope.name, base_url),
        exclude=exclude,
    )


def notify_followup_created_safely(followup):
    """post_save hook body: a notification problem must never break saving the follow-up."""
    try:
        return notify_followup_created(followup)
    except Exception:  # noqa: BLE001 - logged, the save itself already succeeded
        logger.exception("follow-up notice for TransientFollowup %s failed", getattr(followup, "pk", "?"))
        return []


# --- updated requests and status changes (#69) ----------------------------------------

def collapse_seconds() -> int:
    try:
        return max(0, int(getattr(settings, "FOLLOWUP_NOTICE_COLLAPSE_SECONDS", 600)))
    except (TypeError, ValueError):
        return 600


def _may_see(user: User, followup) -> bool:
    """Same rules as the follow-up list: the follow-up is visible and the transient is listed."""
    from YSE_App.services.visibility import followup_visible_to_user, user_can_list_transient

    return followup_visible_to_user(user, followup) and user_can_list_transient(user, followup.transient_id)


def _visible_recipients(users, followup, skip_ids) -> List[User]:
    out = {}
    for user in users:
        if user is None or not user.pk or user.pk in skip_ids or not user.is_active or user.pk in out:
            continue
        if _may_see(user, followup):
            out[user.pk] = user
    return list(out.values())


def _requesters(followup) -> List[User]:
    users = [row.requestor for row in followup.requests.select_related("requestor")]
    if followup.requested_by_id:
        users.append(followup.requested_by)
    return users


def _recently_told(user_ids, followup, event: str) -> set:
    window = collapse_seconds()
    if not window or not user_ids:
        return set()
    since = timezone.now() - datetime.timedelta(seconds=window)
    rows = Notification.objects.filter(
        recipient_id__in=user_ids, kind="followup_request", transient_id=followup.transient_id, created__gte=since,
    ).values_list("recipient_id", "payload")
    return {rid for rid, payload in rows
            if isinstance(payload, dict) and payload.get("followup_id") == followup.pk and payload.get("event") == event}


def _is_first_request_by_creator(row) -> bool:
    """The request that a brand-new follow-up is created with: the "new request" notice covers it."""
    followup = row.followup
    return followup.created_by_id == row.requestor_id and not followup.requests.exclude(pk=row.pk).exists()


def notify_request_added(row, *, recipients: Optional[List[User]] = None):
    """``followup_request`` "updated" notices for a new ``TransientFollowupRequest``; returns the rows created."""
    followup = row.followup
    if _is_first_request_by_creator(row):
        return []
    telescope = followup_telescope(followup)
    if telescope is None:
        return []
    actor_id = row.created_by_id or row.requestor_id
    candidates = recipients if recipients is not None else telescope_followers(telescope) + _requesters(followup)
    users = _visible_recipients(candidates, followup, {actor_id})
    told = _recently_told([u.pk for u in users], followup, "updated")
    users = [u for u in users if u.pk not in told]
    if not users:
        return []
    transient = followup.transient
    actor = row.created_by if row.created_by_id else row.requestor
    return notify_service.notify(
        users,
        "%s follow-up request for %s updated: another request was added by %s." % (telescope.name, transient.name, actor),
        "/transient_detail/%s/" % transient.slug, "followup_request",
        subject="%s request updated for %s" % (telescope.name, transient.name),
        transient=transient, created_by=actor,
        payload={"followup_id": followup.pk, "telescope": telescope.name, "event": "updated"},
        html=followup_notice_html(transient.name, transient.slug, telescope.name, notify_service.base_url(), updated=True),
        exclude=[actor_id],
    )


def _status_name(followup) -> str:
    """Name of the current status; tolerates a status object assigned to ``status_id`` (the API does)."""
    raw = followup.status_id
    if not raw:
        return ""
    name = getattr(raw, "name", None)
    if name is not None:
        return name
    return followup.status.name


def notify_status_changed(followup, old_status: str, *, actor_id=None, skip_ids=()):
    """``followup_status`` notices to the requesters of ``followup``; returns the rows created."""
    new_status = _status_name(followup)
    if not old_status or old_status == new_status:
        return []
    skip = {actor_id} | set(skip_ids)
    users = _visible_recipients(_requesters(followup), followup, skip)
    if not users:
        return []
    transient = followup.transient
    telescope = followup_telescope(followup)
    where = (" on %s" % telescope.name) if telescope is not None else ""
    return notify_service.notify(
        users, "Follow-up%s for %s: status %s -> %s." % (where, transient.name, old_status, new_status or "?"),
        "/transient_detail/%s/#followup_tab" % transient.slug, "followup_status",
        subject="Follow-up status for %s: %s" % (transient.name, new_status or "?"), transient=transient,
        created_by=getattr(followup, "modified_by", None),
        payload={"followup_id": followup.pk, "old_status": old_status, "status": new_status},
        exclude=list(skip),
    )


def notify_request_added_safely(row):
    try:
        return notify_request_added(row)
    except Exception:  # noqa: BLE001 - logged, the save itself already succeeded
        logger.exception("follow-up update notice for TransientFollowupRequest %s failed", getattr(row, "pk", "?"))
        return []


def notify_status_changed_safely(followup, old_status: str):
    """post_save hook body (see signals.py). ``_notice_actor_id`` / ``_notice_skip_ids`` on the
    instance let a system caller (facility poller) say who the actor is and who it already told."""
    try:
        actor_id = getattr(followup, "_notice_actor_id", getattr(followup, "modified_by_id", None))
        return notify_status_changed(followup, old_status, actor_id=actor_id,
                                     skip_ids=getattr(followup, "_notice_skip_ids", ()))
    except Exception:  # noqa: BLE001
        logger.exception("follow-up status notice for TransientFollowup %s failed", getattr(followup, "pk", "?"))
        return []
