"""Follow-up request notices (issue #69; part of #320).

When a ``TransientFollowup`` is created, everyone who follows its telescope
(``UserTelescopeToFollow``, the "Followup Notifications Requested for These
Telescopes" box on the personal dashboard) gets a ``followup_request``
notification through ``notify()``: an in-app row and, per preference, the
same HTML email ``YSE_App.common.alert.SendFollowingNotice`` used to send.
The requester is not notified about their own request.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from django.contrib.auth.models import User
from django.utils.html import escape

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


def followup_notice_html(transient_name: str, transient_slug: str, telescope_name: str, base_url: str) -> str:
    return """\
<html>
<head></head>
<body>
<h2>%s Followup requested for <a href='%stransient_detail/%s/'>%s</a></h2>
<br />
<p>Go to <a href='%sdashboard/'>YSE Dashboard</a></p>
</body>
</html>
""" % (escape(telescope_name), base_url, transient_slug, escape(transient_name), base_url)


def notify_followup_created(followup, *, recipients: Optional[List[User]] = None, exclude_requester: bool = True):
    """``followup_request`` notifications for a new follow-up; returns the rows created."""
    telescope = followup_telescope(followup)
    if telescope is None:
        return []
    users = recipients if recipients is not None else telescope_followers(telescope)
    if not users:
        return []
    transient = followup.transient
    exclude = [followup.created_by_id] if exclude_requester and followup.created_by_id else []
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
