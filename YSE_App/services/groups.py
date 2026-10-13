"""Group profiles, ownership, invitation links and picker filtering (#445, #440).

System groups
    ``settings.YSE_SYSTEM_GROUP_NAMES`` (default: Public, api_writers, explorer_users), any
    group whose ``GroupProfile.is_system`` is set, and any group that holds model permissions.
    They cannot be owned, joined by invitation or managed on the team pages; staff manage
    their membership in the Django admin.

Pickers
    Audience pickers (comments, follow-ups, interests, data-access targets) leave out system
    groups, except those in ``settings.YSE_PICKER_VISIBLE_SYSTEM_GROUPS`` (default: Public,
    which is the follow-up "share with everyone" option). Inactive groups and the groups a
    user hides in /settings/ are left out of the main list as well, unless that would leave
    nothing to choose. Pickers only narrow what is offered: who can see existing data, and
    the server-side eligibility checks, are unchanged.

Invitations
    ``create_invitation`` returns the raw token once; only its SHA-256 is stored.
    ``accept_invitation`` locks the invitation row, so the use limit holds under concurrency.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from YSE_App.models.group_models import GroupInvitation, GroupInvitationUse, GroupProfile, UserPreference

DEFAULT_SYSTEM_GROUP_NAMES = ("Public", "api_writers", "explorer_users")
DEFAULT_PICKER_VISIBLE_SYSTEM_GROUPS = ("Public",)
TOKEN_BYTES = 32

KIND_COMMENT = "comment"
KIND_FOLLOWUP = "followup"


class InvitationError(Exception):
    """An invitation cannot be created or used; ``str(exc)`` is safe to show the user."""


# ---------------------------------------------------------------------------
# settings


def system_group_names() -> Tuple[str, ...]:
    return tuple(getattr(settings, "YSE_SYSTEM_GROUP_NAMES", DEFAULT_SYSTEM_GROUP_NAMES))


def picker_visible_system_group_names() -> Tuple[str, ...]:
    return tuple(getattr(settings, "YSE_PICKER_VISIBLE_SYSTEM_GROUPS", DEFAULT_PICKER_VISIBLE_SYSTEM_GROUPS))


def invite_default_days() -> int:
    return int(getattr(settings, "GROUP_INVITE_DEFAULT_DAYS", 14))


def invite_max_days() -> int:
    return int(getattr(settings, "GROUP_INVITE_MAX_DAYS", 60))


def invite_max_uses_limit() -> int:
    return int(getattr(settings, "GROUP_INVITE_MAX_USES_LIMIT", 50))


def invite_purge_days() -> int:
    return int(getattr(settings, "GROUP_INVITE_PURGE_DAYS", 90))


# ---------------------------------------------------------------------------
# profiles and system groups


def profile_for(group: Group) -> GroupProfile:
    """The group's profile, created on first use (groups made by older code have none)."""
    profile = GroupProfile.objects.filter(group=group).first()
    if profile is not None:
        return profile
    try:
        with transaction.atomic():
            return GroupProfile.objects.create(group=group, is_system=_is_system_by_rule(group))
    except IntegrityError:
        return GroupProfile.objects.get(group=group)


def ensure_all_profiles() -> Tuple[int, int]:
    """Create missing profiles and mark system groups. Returns (created, marked_system)."""
    created = marked = 0
    system_ids = system_group_ids()
    existing = dict(GroupProfile.objects.values_list("group_id", "is_system"))
    for group in Group.objects.all().only("id", "name"):
        is_system = group.id in system_ids
        if group.id not in existing:
            GroupProfile.objects.get_or_create(group=group, defaults={"is_system": is_system})
            created += 1
        elif is_system and not existing[group.id]:
            GroupProfile.objects.filter(group=group).update(is_system=True)
            marked += 1
    return created, marked


def _is_system_by_rule(group: Group) -> bool:
    return group.name in system_group_names() or group.permissions.exists()


def _group_meta(user: Optional[User] = None) -> dict:
    """System / picker-excluded / inactive group ids, memoised on ``user`` (one request) when given."""
    cached = getattr(user, "_yse_group_meta", None) if user is not None else None
    if cached is not None:
        return cached
    q = Q(name__in=system_group_names()) | Q(group_profile__is_system=True) | Q(permissions__isnull=False)
    rows = set(Group.objects.filter(q).values_list("id", "name").distinct())
    visible = set(picker_visible_system_group_names())
    meta = {
        "system": {pk for pk, _name in rows},
        "excluded": {pk for pk, name in rows if name not in visible},
        "inactive": set(GroupProfile.objects.filter(is_active=False).values_list("group_id", flat=True)),
    }
    if user is not None:
        try:
            user._yse_group_meta = meta
        except AttributeError:  # AnonymousUser and similar
            pass
    return meta


def system_group_ids(user: Optional[User] = None) -> Set[int]:
    return set(_group_meta(user)["system"])


def is_system_group(group: Group) -> bool:
    if group.name in system_group_names():
        return True
    return Group.objects.filter(pk=group.pk).filter(
        Q(group_profile__is_system=True) | Q(permissions__isnull=False)).exists()


def inactive_group_ids(user: Optional[User] = None) -> Set[int]:
    return set(_group_meta(user)["inactive"])


def is_group_active(group: Group) -> bool:
    return not GroupProfile.objects.filter(group=group, is_active=False).exists()


# ---------------------------------------------------------------------------
# ownership


def is_owner(user: User, group: Group) -> bool:
    if not user.is_authenticated:
        return False
    return GroupProfile.objects.filter(group=group, owners=user).exists()


def can_manage(user: User, group: Group) -> bool:
    """Owners and staff manage a team; nobody manages a system group here."""
    if not user.is_authenticated or not user.is_active:
        return False
    if is_system_group(group):
        return False
    return bool(user.is_staff or user.is_superuser or is_owner(user, group))


def owners_of(group: Group) -> List[User]:
    profile = GroupProfile.objects.filter(group=group).first()
    if profile is None:
        return []
    return list(profile.owners.filter(is_active=True).order_by("username"))


def teams_for(user: User) -> List[Group]:
    """Non-system groups the user belongs to or owns (staff: every non-system group)."""
    system = system_group_ids(user)
    if user.is_staff or user.is_superuser:
        qs = Group.objects.all()
    else:
        qs = Group.objects.filter(Q(user=user) | Q(group_profile__owners=user))
    return [g for g in qs.distinct().select_related("group_profile").order_by("name") if g.id not in system]


def remove_member(actor: User, group: Group, member: User) -> None:
    if not can_manage(actor, group):
        raise PermissionError("You cannot manage this group.")
    if is_owner(member, group):
        raise InvitationError("Owners are changed by staff in the admin; remove the owner there first.")
    member.groups.remove(group)
    _notify_owners(group, "%s was removed from %s." % (display_name(member), group.name), exclude=[actor])


# ---------------------------------------------------------------------------
# invitations


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_invitation(actor: User, group: Group, *, days: Optional[int] = None,
                      max_uses: int = 1) -> Tuple[GroupInvitation, str]:
    """Create an invitation; returns ``(invitation, raw_token)``. The raw token is not stored."""
    if is_system_group(group):
        raise InvitationError("System groups cannot be joined by invitation.")
    if not can_manage(actor, group):
        raise PermissionError("Only the group's owners and staff can invite people.")
    days = invite_default_days() if days is None else int(days)
    if days < 1 or days > invite_max_days():
        raise InvitationError("The link must expire in 1 to %d days." % invite_max_days())
    max_uses = int(max_uses)
    if max_uses < 1 or max_uses > invite_max_uses_limit():
        raise InvitationError("An invitation can be used 1 to %d times." % invite_max_uses_limit())
    token = secrets.token_urlsafe(TOKEN_BYTES)
    invitation = GroupInvitation.objects.create(
        group=group, token_hash=hash_token(token), created_by=actor,
        expires_at=timezone.now() + datetime.timedelta(days=days), max_uses=max_uses,
    )
    return invitation, token


def find_invitation(token_or_hash: str, *, hashed: bool = False) -> Optional[GroupInvitation]:
    if not token_or_hash:
        return None
    digest = token_or_hash if hashed else hash_token(token_or_hash)
    return GroupInvitation.objects.filter(token_hash=digest).select_related("group").first()


def invitation_problem(invitation: Optional[GroupInvitation]) -> Optional[str]:
    """Why this invitation cannot be used now, or None."""
    if invitation is None:
        return "This invitation link is not valid."
    if invitation.is_revoked:
        return "This invitation was revoked."
    if invitation.is_expired:
        return "This invitation has expired."
    if invitation.is_used_up:
        return "This invitation has already been used."
    if is_system_group(invitation.group):
        return "This group cannot be joined by invitation."
    return None


def accept_invitation(user: User, token_hash: str) -> Tuple[Group, bool]:
    """Add ``user`` to the invitation's group. Returns ``(group, joined)``.

    ``joined`` is False when the user was already a member (no use is recorded).
    Raises ``InvitationError`` when the invitation cannot be used.
    """
    if not user.is_authenticated or not user.is_active:
        raise InvitationError("Sign in to accept this invitation.")
    with transaction.atomic():
        invitation = (GroupInvitation.objects.select_for_update()
                      .filter(token_hash=token_hash).select_related("group").first())
        if invitation is not None and user.groups.filter(pk=invitation.group_id).exists():
            return invitation.group, False
        problem = invitation_problem(invitation)
        if problem:
            raise InvitationError(problem)
        _, created = GroupInvitationUse.objects.get_or_create(invitation=invitation, used_by=user)
        if not created:  # the user left the group and reopened the same link
            raise InvitationError("You have already used this invitation.")
        user.groups.add(invitation.group)
    group = invitation.group
    _notify_owners(group, "%s joined %s through an invitation link." % (display_name(user), group.name), exclude=[user])
    return group, True


def revoke_invitation(actor: User, invitation: GroupInvitation) -> None:
    if not can_manage(actor, invitation.group):
        raise PermissionError("You cannot manage this group.")
    if invitation.revoked_at is None:
        invitation.revoked_at = timezone.now()
        invitation.revoked_by = actor
        invitation.save(update_fields=["revoked_at", "revoked_by"])


def invitations_for(group: Group):
    return (GroupInvitation.objects.filter(group=group).annotate(_use_count=Count("uses"))
            .select_related("created_by").order_by("-created_at", "-id"))


def purge_invitations(older_than_days: Optional[int] = None, *, dry_run: bool = False) -> int:
    """Delete invitations that expired, were revoked or were used up more than N days ago.

    Open invitations are never deleted. The use rows go with them (CASCADE); group
    membership is not affected.
    """
    days = invite_purge_days() if older_than_days is None else int(older_than_days)
    cutoff = timezone.now() - datetime.timedelta(days=days)
    candidates = (GroupInvitation.objects
                  .annotate(_use_count=Count("uses"), _last_use=Max("uses__used_at"))
                  .filter(Q(expires_at__lt=cutoff) | Q(revoked_at__lt=cutoff) | Q(_last_use__lt=cutoff)))
    ids = [inv.pk for inv in candidates
           if inv.expires_at < cutoff
           or (inv.revoked_at is not None and inv.revoked_at < cutoff)
           or (inv._use_count >= inv.max_uses and inv._last_use is not None and inv._last_use < cutoff)]
    if not dry_run and ids:
        GroupInvitation.objects.filter(pk__in=ids).delete()
    return len(ids)


def display_name(user: User) -> str:
    profile = getattr(user, "researcher_profile", None)
    try:
        if profile is not None and profile.full_name:
            return profile.full_name
    except Exception:  # noqa: BLE001 - a missing related row is fine
        pass
    return user.get_full_name() or user.get_username()


def _notify_owners(group: Group, text: str, exclude: Iterable[User] = ()) -> None:
    owners = owners_of(group)
    if not owners:
        return
    try:
        from django.urls import reverse

        from YSE_App.services.notify import notify
        notify(owners, text, url=reverse("team_detail", args=[group.pk]), kind="system",
               subject="Team membership: %s" % group.name, exclude=list(exclude))
    except Exception:  # noqa: BLE001 - membership changes never fail on notification delivery
        import logging
        logging.getLogger(__name__).exception("team owner notification failed")


# ---------------------------------------------------------------------------
# user preferences and pickers


def preferences_for(user: User) -> Optional[UserPreference]:
    """The user's saved preferences, or None (never creates a row). Memoised on the user object."""
    if not getattr(user, "is_authenticated", False):
        return None
    if hasattr(user, "_yse_pref_cache"):
        return user._yse_pref_cache
    pref = UserPreference.objects.filter(user=user).first()
    user._yse_pref_cache = pref
    return pref


def forget_preferences(user: User) -> None:
    """Drop the per-request memo (preferences and group flags) after a change."""
    for attr in ("_yse_pref_cache", "_yse_group_meta"):
        if hasattr(user, attr):
            delattr(user, attr)


def _pref_ids(user: User, attr: str) -> Set[int]:
    pref = preferences_for(user)
    if pref is None:
        return set()
    cache_attr = "_yse_ids_%s" % attr
    if not hasattr(pref, cache_attr):
        setattr(pref, cache_attr, set(getattr(pref, attr).values_list("id", flat=True)))
    return getattr(pref, cache_attr)


def hidden_group_ids(user: User) -> Set[int]:
    return _pref_ids(user, "hidden_groups")


def default_group_ids(user: User, kind: str) -> Set[int]:
    attr = "comment_default_groups" if kind == KIND_COMMENT else "followup_default_groups"
    return _pref_ids(user, attr)


def comment_default_public(user: User) -> bool:
    pref = preferences_for(user)
    return bool(pref and pref.comment_default_public)


def picker_excluded_system_ids(user: Optional[User] = None) -> Set[int]:
    """System groups left out of pickers (all of them except YSE_PICKER_VISIBLE_SYSTEM_GROUPS)."""
    return set(_group_meta(user)["excluded"])


def partition_picker_groups(user: User, groups: Iterable[Group]) -> Tuple[List[Group], List[Group]]:
    """``(shown, more)``: system groups are dropped; inactive and user-hidden groups go to ``more``.

    If nothing would be shown, everything goes back into ``shown`` so a picker is never empty
    only because of a preference or an inactive flag.
    """
    groups = list(groups)
    if not groups:
        return [], []
    excluded = picker_excluded_system_ids(user)
    kept = [g for g in groups if g.id not in excluded]
    soft = inactive_group_ids(user) | hidden_group_ids(user)
    shown = [g for g in kept if g.id not in soft]
    more = [g for g in kept if g.id in soft]
    if not shown:
        return kept, []
    return shown, more


def picker_groups(user: User, groups: Iterable[Group]) -> List[Group]:
    """Groups to offer in a simple picker (system, inactive and user-hidden groups left out)."""
    return partition_picker_groups(user, groups)[0]


def preferred_defaults(user: User, groups: Sequence[Group], kind: str) -> List[Group]:
    """The user's saved defaults among ``groups``; all of ``groups`` when none apply."""
    groups = list(groups)
    wanted = default_group_ids(user, kind)
    if wanted:
        chosen = [g for g in groups if g.id in wanted]
        if chosen:
            return chosen
    return groups


def narrow_default_audience(user: User, groups: Sequence[Group], kind: str) -> List[Group]:
    """Server-side default audience: pickers' filtering plus saved defaults, never empty if ``groups`` isn't."""
    groups = list(groups)
    if not groups:
        return groups
    return preferred_defaults(user, picker_groups(user, groups), kind) or groups
