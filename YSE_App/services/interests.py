"""Transient interests: register, update, list (#288: #289, #290).

``register_interest`` creates the :class:`TransientInterest`, posts the
automatic comment ("<user> registered an interest: <title>") through
``create_transient_comment`` so it appears in the comments panel and goes
through the Slack mirror and mention flow, and notifies the transient's other
open interest holders (kind ``interest``). Status changes to *withdrawn* and
*published* post a comment too; the others are silent.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from django.contrib.auth.models import Group, User
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.models import Transient, TransientInterest
from YSE_App.services import notify as notify_service
from YSE_App.services.comments import create_transient_comment
from YSE_App.services.visibility import user_can_view_transient

COMMENT_TEMPLATES = {
    "registered": "%(user)s registered an interest: %(title)s",
    TransientInterest.STATUS_WITHDRAWN: "%(user)s withdrew the interest: %(title)s",
    TransientInterest.STATUS_PUBLISHED: "%(user)s published: %(title)s%(doi)s",
}


def _display(user: User) -> str:
    full = ("%s %s" % (user.first_name, user.last_name)).strip()
    return full or user.username


def interest_queryset(transient_id: int, *, include_withdrawn: bool = False):
    """Interests on a transient, newest first; withdrawn ones only when asked for."""
    qs = TransientInterest.objects.filter(transient_id=transient_id).select_related("user", "group")
    if not include_withdrawn:
        qs = qs.filter(status__in=TransientInterest.VISIBLE_STATUSES)
    return qs.order_by("status", "-created_date")


def interests_for_user(user: User, *, include_withdrawn: bool = True):
    qs = TransientInterest.objects.filter(user=user).select_related("transient", "group")
    if not include_withdrawn:
        qs = qs.filter(status__in=TransientInterest.VISIBLE_STATUSES)
    return qs.order_by("-modified_date")


def selectable_groups(user: User) -> List[Group]:
    """Collaboration groups a user may register a paper under (all of theirs)."""
    return list(user.groups.order_by("name"))


def _comment_audience(user: User, transient: Transient, group: Optional[Group]):
    """(is_public, audience_groups) for the automatic comment.

    With a group: private to that group. Without one: the comment helper's
    default (the groups the user shares with the transient's restricted data,
    or public when there are none).
    """
    if group is not None:
        return False, [group]
    return None, None


def _post_comment(interest: TransientInterest, key: str, user: User):
    template = COMMENT_TEMPLATES[key]
    doi = (" (%s)" % interest.doi) if interest.doi else ""
    text = template % {"user": _display(user), "title": interest.title, "doi": doi}
    is_public, audience = _comment_audience(user, interest.transient, interest.group)
    return create_transient_comment(
        transient=interest.transient, comment=text, user=user, is_public=is_public, audience_groups=audience,
    )


def other_interest_holders(interest: TransientInterest) -> List[User]:
    """Active users with an open interest on the same transient (the actor excluded)."""
    rows = (
        TransientInterest.objects.filter(
            transient_id=interest.transient_id, status__in=TransientInterest.OPEN_STATUSES,
        )
        .exclude(user_id=interest.user_id)
        .select_related("user")
    )
    seen, users = set(), []
    for row in rows:
        if row.user.is_active and row.user_id not in seen:
            seen.add(row.user_id)
            users.append(row.user)
    return users


def notify_interest_holders(interest: TransientInterest, text: str, *, actor: User):
    users = other_interest_holders(interest)
    if not users:
        return []
    transient = interest.transient
    return notify_service.notify(
        users, text, "/transient_detail/%s/" % transient.slug, "interest",
        subject="Interest on %s" % transient.name, transient=transient,
        payload={"interest_id": interest.pk, "status": interest.status}, created_by=actor,
        exclude=[actor.pk],
    )


@transaction.atomic
def register_interest(
    transient: Transient, user: User, title: str, *,
    group: Optional[Group] = None, role: str = TransientInterest.ROLE_LEAD, description: str = "",
    status: str = TransientInterest.STATUS_PLANNED, comment: bool = True, notify_others: bool = True,
) -> TransientInterest:
    """Register (or revive a withdrawn) interest and post the automatic comment."""
    title = (title or "").strip()
    if not title:
        raise ValidationError({"title": "Give the planned paper a short title."})
    if len(title) > 200:
        raise ValidationError({"title": "Keep the title under 200 characters."})
    if not user_can_view_transient(user, transient.id):
        raise PermissionDenied({"message": "You do not have access to this transient."})
    if group is not None and not user.groups.filter(pk=group.pk).exists():
        raise PermissionDenied({"message": "You can only register a paper under a group you belong to."})
    if role not in dict(TransientInterest.ROLE_CHOICES):
        raise ValidationError({"role": "Unknown role."})
    if status not in TransientInterest.OPEN_STATUSES:
        raise ValidationError({"status": "A new interest starts as planned, in progress or submitted."})

    existing = TransientInterest.objects.filter(transient=transient, user=user, title=title).first()
    if existing is not None:
        if not existing.is_withdrawn:
            raise ValidationError({"title": "You already registered an interest with this title on this transient."})
        existing.status = status
        existing.group = group
        existing.role = role
        existing.description = description or existing.description
        existing.modified_by = user
        existing.save()
        interest = existing
    else:
        interest = TransientInterest.objects.create(
            transient=transient, user=user, group=group, title=title, description=description or "",
            role=role, status=status, created_by=user, modified_by=user,
        )
    if comment:
        _post_comment(interest, "registered", user)
    if notify_others:
        notify_interest_holders(
            interest, "%s registered an interest on %s: %s" % (_display(user), transient.name, title), actor=user,
        )
    return interest


def user_can_edit_interest(user: User, interest: TransientInterest) -> bool:
    return bool(user.is_authenticated and (user.is_staff or user.is_superuser or interest.user_id == user.pk))


@transaction.atomic
def update_interest_status(
    interest: TransientInterest, user: User, status: str, *, doi: Optional[str] = None, comment: bool = True,
) -> TransientInterest:
    """Move an interest to ``status`` (owner or staff); withdrawn / published post a comment."""
    if not user_can_edit_interest(user, interest):
        raise PermissionDenied({"message": "Only the person who registered the interest (or staff) may change it."})
    if status not in dict(TransientInterest.STATUS_CHOICES):
        raise ValidationError({"status": "Unknown status."})
    if doi is not None:
        interest.doi = doi.strip()[:128]
    changed = interest.status != status
    interest.status = status
    interest.modified_by = user
    interest.save()
    if changed and comment and status in COMMENT_TEMPLATES:
        _post_comment(interest, status, user)
    if changed and status == TransientInterest.STATUS_WITHDRAWN:
        notify_interest_holders(
            interest, "%s withdrew the interest on %s: %s" % (_display(user), interest.transient.name, interest.title),
            actor=user,
        )
    return interest


def open_interest_counts(transient_ids: Iterable[int]) -> dict:
    """{transient_id: number of open interests} for a set of transients (one query)."""
    from django.db.models import Count

    ids = list(transient_ids)
    if not ids:
        return {}
    rows = (
        TransientInterest.objects.filter(transient_id__in=ids, status__in=TransientInterest.OPEN_STATUSES)
        .values("transient_id").annotate(n=Count("id"))
    )
    return {row["transient_id"]: row["n"] for row in rows}
