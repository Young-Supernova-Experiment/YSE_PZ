"""Team membership requests (#448).

A signed-in user asks to join a research team; the team's owners (``GroupProfile.owners``,
#445) or staff approve or deny the request. Approving adds the user to the group.

Requestable teams are active, non-system groups that have at least one active owner (so
someone can decide). System groups (Public, api_writers, explorer_users, permission groups)
can never be requested. The invitation flow (YSE_App/services/groups.py) is unchanged.

Owners are told about a new request, and the requester about the decision, through
``notify()`` (kind ``team_request``, in the "Interests and data access" preference group:
in-app, email and Slack by default, per each user's notification preferences).
Requests are opened only while ``SELF_SIGNUP_ENABLED`` is on; pending ones can still be
decided or cancelled after it is turned off. Decided and cancelled requests are deleted
after ``GROUP_REQUEST_PURGE_DAYS`` (``manage.py purge_group_membership_requests``).

Teams chosen on the signup form (at most ``GROUP_SIGNUP_MAX_REQUESTS``) become requests at
once, but ``held``: owners do not see them and cannot approve them until the new user has
accepted the User Agreement and completed their profile, so nobody gains a team's data
access before the agreement. ``release_held`` (called when the profile is completed and
on ``/teams/``) un-holds them and notifies the owners. Held requests never released are
purged after ``GROUP_REQUEST_PURGE_DAYS``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Iterable, List, Optional

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from YSE_App.models.group_models import GroupMembershipRequest
from YSE_App.services import groups as group_svc

logger = logging.getLogger(__name__)

MESSAGE_MAX_LENGTH = 1000


class MembershipRequestError(Exception):
    """A request cannot be made or decided; ``str(exc)`` is safe to show the user."""


def requests_enabled() -> bool:
    return bool(getattr(settings, "SELF_SIGNUP_ENABLED", False))


def purge_days() -> int:
    return int(getattr(settings, "GROUP_REQUEST_PURGE_DAYS", 90))


def requests_per_day() -> int:
    return int(getattr(settings, "GROUP_REQUESTS_PER_USER_PER_DAY", 10))


def signup_max_requests() -> int:
    return int(getattr(settings, "GROUP_SIGNUP_MAX_REQUESTS", 5))


def _requestable_qs():
    return (Group.objects
            .filter(group_profile__is_active=True, group_profile__is_system=False,
                    group_profile__owners__is_active=True)
            .distinct().select_related("group_profile").order_by("name"))


def requestable_groups(user: User) -> List[Group]:
    """Teams ``user`` may ask to join: active, non-system, with an active owner, not yet a member."""
    if not getattr(user, "is_authenticated", False):
        return []
    system = group_svc.system_group_ids(user)
    qs = _requestable_qs().exclude(id__in=user.groups.values("id"))
    return [g for g in qs if g.id not in system]


def signup_groups() -> List[Group]:
    """Teams offered on the signup form (the same rule, for someone with no account yet)."""
    if not requests_enabled():
        return []
    system = group_svc.system_group_ids()
    return [g for g in _requestable_qs() if g.id not in system]


def is_requestable(user: User, group: Group) -> bool:
    return any(g.pk == group.pk for g in requestable_groups(user))


def pending_request(user: User, group: Group) -> Optional[GroupMembershipRequest]:
    return GroupMembershipRequest.objects.filter(
        user=user, group=group, status=GroupMembershipRequest.STATUS_PENDING).first()


def requests_for_user(user: User):
    return GroupMembershipRequest.objects.filter(user=user).select_related("group").order_by("-created_at", "-id")


def pending_for_group(group: Group):
    """Pending requests the owners can act on (held signup requests are left out)."""
    return (GroupMembershipRequest.objects.filter(group=group, status=GroupMembershipRequest.STATUS_PENDING)
            .exclude(held=True)
            .select_related("user", "user__researcher_profile").order_by("created_at", "id"))


def _rate_key(user: User) -> str:
    return "yse:groupreq:%s:%s" % (user.pk, timezone.now().strftime("%Y%m%d"))


def _over_daily_limit(user: User) -> bool:
    limit = requests_per_day()
    if limit <= 0:
        return False
    key = _rate_key(user)
    cache.add(key, 0, 86400)
    try:
        count = cache.incr(key)
    except ValueError:  # evicted between add and incr
        cache.set(key, 1, 86400)
        count = 1
    return count > limit


def create_request(user: User, group: Group, message: str = "", *, hold: bool = False) -> GroupMembershipRequest:
    """Open a pending request (or return the user's existing pending one for this group).

    ``hold=True`` (signup form): the request waits, unseen by the owners, until
    ``release_held`` finds the user onboarded.
    """
    if not requests_enabled():
        raise MembershipRequestError("Membership requests are not open on this site.")
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        raise MembershipRequestError("Sign in to request membership.")
    if group_svc.is_system_group(group):
        raise MembershipRequestError("This group cannot be requested.")
    if user.groups.filter(pk=group.pk).exists():
        raise MembershipRequestError("You are already a member of %s." % group.name)
    existing = pending_request(user, group)
    if existing is not None:
        return existing
    if not is_requestable(user, group):
        raise MembershipRequestError("This team is not taking membership requests.")
    if _over_daily_limit(user):
        raise MembershipRequestError("You have made too many requests today; try again tomorrow.")
    message = (message or "").strip()[:MESSAGE_MAX_LENGTH]
    with transaction.atomic():
        # Lock the user's row so two simultaneous submissions cannot both create a request.
        User.objects.select_for_update().filter(pk=user.pk).first()
        existing = pending_request(user, group)
        if existing is not None:
            return existing
        req = GroupMembershipRequest.objects.create(user=user, group=group, message=message,
                                                    held=True if hold else None)
    if not hold:
        _notify_new_request(req)
    return req


def _notify_new_request(req: GroupMembershipRequest) -> None:
    group = req.group
    _notify(group_svc.owners_of(group),
            "%s asked to join %s." % (group_svc.display_name(req.user), group.name),
            reverse("team_detail", args=[group.pk]), "Team membership request: %s" % group.name,
            exclude=[req.user])


def create_signup_requests(user: User, groups: Iterable[Group], message: str = "") -> List[GroupMembershipRequest]:
    """Held requests for the teams chosen on the signup form; teams that cannot be requested are skipped."""
    made = []
    for group in list(groups)[:signup_max_requests()]:
        try:
            made.append(create_request(user, group, message, hold=True))
        except MembershipRequestError:
            logger.info("signup team request skipped for group %s", group.pk)
    return made


def held_requests(user: User):
    return GroupMembershipRequest.objects.filter(
        user=user, status=GroupMembershipRequest.STATUS_PENDING, held=True).select_related("group")


def release_held(user: User) -> int:
    """Un-hold the user's signup requests once they are onboarded and notify the owners."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return 0
    held = list(held_requests(user))
    if not held:
        return 0
    from YSE_App import onboarding
    if not onboarding.is_onboarded(user):
        return 0
    released = 0
    for req in held:
        # Conditional update: two page views at once release (and notify) only once.
        if GroupMembershipRequest.objects.filter(pk=req.pk, held=True,
                                                 status=GroupMembershipRequest.STATUS_PENDING).update(held=False):
            released += 1
            _notify_new_request(req)
    return released


def can_decide(actor: User, req: GroupMembershipRequest) -> bool:
    return group_svc.can_manage(actor, req.group)


def decide(actor: User, req: GroupMembershipRequest, approve: bool) -> GroupMembershipRequest:
    """Approve (adds the user to the group) or deny a pending request."""
    if not can_decide(actor, req):
        raise PermissionError("Only the team's owners and staff can decide requests.")
    with transaction.atomic():
        locked = GroupMembershipRequest.objects.select_for_update().select_related("group", "user").get(pk=req.pk)
        if not locked.is_pending:
            raise MembershipRequestError("This request was already %s." % locked.status)
        if locked.held:
            raise MembershipRequestError("This person has not finished signing up yet.")
        if group_svc.is_system_group(locked.group):
            raise MembershipRequestError("This group cannot be joined by request.")
        locked.status = locked.STATUS_APPROVED if approve else locked.STATUS_DENIED
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "decided_by", "decided_at"])
        if approve and locked.user.is_active:
            locked.user.groups.add(locked.group)
    group = locked.group
    if approve:
        _notify([locked.user], "Your request to join %s was approved." % group.name,
                reverse("team_detail", args=[group.pk]), "Team membership: %s" % group.name)
    else:
        _notify([locked.user], "Your request to join %s was not approved." % group.name,
                reverse("team_list"), "Team membership: %s" % group.name)
    return locked


def cancel(user: User, req: GroupMembershipRequest) -> None:
    if req.user_id != user.pk:
        raise PermissionError("Not your request.")
    updated = GroupMembershipRequest.objects.filter(pk=req.pk, status=GroupMembershipRequest.STATUS_PENDING).update(
        status=GroupMembershipRequest.STATUS_CANCELLED, decided_at=timezone.now())
    if not updated:
        raise MembershipRequestError("This request is no longer pending.")


def purge_requests(older_than_days: Optional[int] = None, *, dry_run: bool = False) -> int:
    """Delete approved, denied and cancelled requests decided more than N days ago.

    Pending requests are never deleted, except held signup requests that were never
    released (the user did not finish onboarding) and are older than N days. Group
    membership is not affected.
    """
    days = purge_days() if older_than_days is None else int(older_than_days)
    cutoff = timezone.now() - datetime.timedelta(days=days)
    qs = GroupMembershipRequest.objects.filter(
        Q(decided_at__lt=cutoff, status__in=(GroupMembershipRequest.STATUS_APPROVED,
                                             GroupMembershipRequest.STATUS_DENIED,
                                             GroupMembershipRequest.STATUS_CANCELLED))
        | Q(status=GroupMembershipRequest.STATUS_PENDING, held=True, created_at__lt=cutoff))
    count = qs.count()
    if not dry_run and count:
        qs.delete()
    return count


def _notify(users: Iterable[User], text: str, url: str, subject: str, exclude=()) -> None:
    users = [u for u in users if u is not None]
    if not users:
        return
    try:
        from YSE_App.services.notify import notify
        notify(users, text, url=url, kind="team_request", subject=subject, exclude=list(exclude))
    except Exception:  # noqa: BLE001 - a request never fails on notification delivery
        logger.exception("membership request notification failed")
