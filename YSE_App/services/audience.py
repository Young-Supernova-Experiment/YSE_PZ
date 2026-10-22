"""Create-time audience resolution for comments and follow-ups (v1)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from django.contrib.auth.models import Group, User
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.services import groups as group_svc
from YSE_App.services.visibility import (
    default_comment_audience_groups,
    object_has_groups,
)


def selectable_audience_groups(user: User, transient_id: int):
    """Collaboration groups the user may assign to a comment at create time (the eligibility rule).

    Any group the user belongs to (Ryan, 2026-10-22). It used to be only the groups on the
    transient's restricted data, which is also the default audience, so the picker offered
    nothing beyond the defaults. The default when no audience is sent is unchanged
    (``default_comment_audience_groups``), and the picker still leaves out system, inactive
    and hidden groups.
    """
    return user.groups.all().order_by("name")


def picker_audience_groups(user: User, transient_id: int) -> List[Group]:
    """Groups offered in the comment picker (#440).

    ``selectable_audience_groups`` minus system groups, inactive groups and the groups the
    user hides in /settings/ (never empty only because of those). Public, when selectable, is
    always offered (#440). Eligibility is unchanged: the server still accepts any selectable
    group.
    """
    selectable = list(selectable_audience_groups(user, transient_id))
    shown = group_svc.picker_groups(user, selectable)
    public_ids = group_svc.public_group_ids(user)
    shown_ids = {g.id for g in shown}
    missing_public = [g for g in selectable if g.id in public_ids and g.id not in shown_ids]
    if missing_public:
        shown = sorted(shown + missing_public, key=lambda g: g.name)
    return shown


def comment_picker_initial(user: User, groups: List[Group], transient_id: Optional[int] = None) -> List[int]:
    """Pre-checked comment groups: the user's saved defaults among ``groups``; else the groups
    on the transient's restricted data (the default audience); else all of ``groups``.

    The picker offers every non-hidden group, but only the default audience starts checked.
    """
    groups = list(groups)
    wanted = group_svc.default_group_ids(user, group_svc.KIND_COMMENT)
    chosen = [g for g in groups if g.id in wanted] if wanted else []
    if not chosen and transient_id is not None:
        default_ids = {g.id for g in default_comment_audience_groups(user, transient_id)}
        chosen = [g for g in groups if g.id in default_ids]
    return [g.pk for g in (chosen or groups)]


def _default_comment_groups(user: User, transient_id: int) -> List[Group]:
    """Server default when the client sends no audience: narrowed by pickers and saved defaults."""
    return group_svc.narrow_default_audience(
        user, default_comment_audience_groups(user, transient_id), group_svc.KIND_COMMENT)


def _groups_from_ids(
    user: User,
    transient_id: int,
    audience_group_ids: Optional[Iterable[int]],
) -> List[Group]:
    if audience_group_ids is None:
        return []
    ids = list(audience_group_ids)
    if not ids:
        return []
    allowed = selectable_audience_groups(user, transient_id)
    selected = list(allowed.filter(id__in=ids))
    if not selected:
        raise ValidationError(
            {"audience_groups": "Select at least one valid collaboration group."}
        )
    user_ids = set(user.groups.values_list("id", flat=True))
    if any(group.id not in user_ids for group in selected):
        raise PermissionDenied(
            {"message": "You cannot assign collaboration groups you do not belong to."}
        )
    return selected


def resolve_comment_audience(
    user: User,
    transient_id: int,
    *,
    is_public: bool,
    audience_group_ids: Optional[Iterable[int]] = None,
    audience_groups: Optional[Iterable[Group]] = None,
) -> Tuple[bool, List[Group]]:
    """
    Return ``(is_public, audience_groups)`` for a new transient comment.

    - ``is_public=True``: all collaborators who can open the transient.
    - Explicit empty group selection: creator-only (``is_public=False``, no groups).
    - No explicit audience in the request: private to shared collaboration groups.
    """
    if is_public:
        return True, []

    explicit_audience = audience_groups is not None or audience_group_ids is not None

    if audience_groups is not None:
        selected = list(audience_groups)
    elif audience_group_ids is not None:
        selected = _groups_from_ids(user, transient_id, audience_group_ids)
    else:
        selected = _default_comment_groups(user, transient_id)
        if not selected:
            return True, []
        return False, selected

    if not selected:
        if explicit_audience:
            return False, []
        fallback = _default_comment_groups(user, transient_id)
        if not fallback:
            return True, []
        return False, fallback
    return False, selected


def linked_telescope_resource(classical=None, too=None, queued=None):
    """Return the single linked observing resource, if any."""
    return classical or too or queued


def resource_is_public(resource) -> bool:
    """Ungrouped telescope resources are visible to all authorized users."""
    if resource is None:
        return True
    return not object_has_groups(resource)


def resource_audience_group_ids(resource) -> Set[int]:
    if resource is None:
        return set()
    return set(resource.groups.values_list("id", flat=True))


def resource_is_creator_only(resource) -> bool:
    """
    True when the observing resource is private to the requester only.

    Set ``creator_only`` on ClassicalResource / ToOResource / QueuedResource rows
    (BooleanField, default False) when that workflow is needed.
    """
    if resource is None:
        return False
    return bool(getattr(resource, "creator_only", False))


def eligible_followup_audience_groups(user: User, resource) -> List[Group]:
    """Collaboration groups the user may assign for a restricted follow-up."""
    user_groups = list(user.groups.order_by("name"))
    if resource_is_public(resource):
        return user_groups
    resource_ids = resource_audience_group_ids(resource)
    return [
        group
        for group in user_groups
        if group.name != PUBLIC_COLLABORATION_GROUP_NAME and group.id in resource_ids
    ]


def build_followup_audience_choices(user: User, resource) -> List[Dict[str, Any]]:
    """The user's groups with enabled/checked flags for the follow-up picker.

    System groups other than Public are left out (#440). Inactive and user-hidden groups
    come last with ``hidden=True`` (the template folds them away, unchecked). ``is_default``
    marks the user's saved follow-up defaults; when none of those is enabled, every enabled
    shown group is checked, as before.
    """
    eligible_ids = {group.id for group in eligible_followup_audience_groups(user, resource)}
    resource_public = resource_is_public(resource)
    shown, more = group_svc.partition_picker_groups(user, user.groups.order_by("name"))
    # Public is always offered and never folded away (#440), whatever the settings say.
    public_ids = group_svc.public_group_ids(user)
    shown = shown + [g for g in more if g.id in public_ids]
    more = [g for g in more if g.id not in public_ids]
    default_ids = group_svc.default_group_ids(user, group_svc.KIND_FOLLOWUP)
    choices: List[Dict[str, Any]] = []
    for group, hidden in [(g, False) for g in shown] + [(g, True) for g in more]:
        is_public_group = group.name == PUBLIC_COLLABORATION_GROUP_NAME
        if is_public_group:
            enabled = resource_public
        else:
            enabled = group.id in eligible_ids
        choices.append(
            {
                "group": group,
                "enabled": enabled,
                "checked": False,
                "is_public_group": is_public_group,
                "hidden": hidden,
                "is_default": (group.id in default_ids) and not hidden,
            }
        )
    candidates = [c for c in choices if c["enabled"] and not c["hidden"]]
    preferred = [c for c in candidates if c["is_default"]]
    for choice in preferred or candidates or [c for c in choices if c["enabled"]]:
        choice["checked"] = True
    return choices


def build_followup_resource_audience_map(form) -> Dict[str, Dict[str, Any]]:
    """Map ``field_name:pk`` to resource audience metadata for client-side sync."""
    resource_map: Dict[str, Dict[str, Any]] = {}
    for field_name in ("classical_resource", "too_resource", "queued_resource"):
        for resource in form.fields[field_name].queryset.prefetch_related("groups"):
            resource_map[f"{field_name}:{resource.pk}"] = {
                "is_public": resource_is_public(resource),
                "group_ids": list(resource.groups.values_list("pk", flat=True)),
                "creator_only": bool(getattr(resource, "creator_only", False)),
            }
    return resource_map


def _followup_groups_from_ids(
    user: User,
    linked_resource,
    audience_group_ids: Iterable[int],
) -> List[Group]:
    ids = list(audience_group_ids)
    if not ids:
        return []
    eligible_ids = {group.id for group in eligible_followup_audience_groups(user, linked_resource)}
    selected = list(user.groups.filter(id__in=ids))
    if not selected:
        raise ValidationError(
            {"audience_groups": "Select at least one valid collaboration group."}
        )
    invalid = [group for group in selected if group.id not in eligible_ids]
    if invalid:
        raise ValidationError(
            {
                "audience_groups": (
                    "One or more selected groups cannot see the linked observing resource."
                )
            }
        )
    return selected


def resolve_followup_audience(
    user: User,
    transient_id: int,
    *,
    audience_group_ids: Optional[Iterable[int]] = None,
    audience_groups: Optional[Iterable[Group]] = None,
    linked_resource=None,
    explicit_audience: bool = True,
) -> Tuple[bool, List[Group]]:
    """
    Return ``(is_public, audience_groups)`` for a new follow-up request.

    New follow-ups are always stored with ``is_public=False``; audience is set
    via collaboration groups. Select the ``Public`` group to share with all
    members of that group (only when the linked resource is itself public).

    Creator-only (no groups) is allowed only for creator-only observing resources.
    """
    eligible = eligible_followup_audience_groups(user, linked_resource)
    eligible_ids = {group.id for group in eligible}

    if audience_groups is not None:
        selected = list(audience_groups)
    elif audience_group_ids is not None:
        selected = _followup_groups_from_ids(user, linked_resource, audience_group_ids)
    elif explicit_audience:
        selected = []
    else:
        selected = group_svc.narrow_default_audience(user, eligible, group_svc.KIND_FOLLOWUP)

    if not selected:
        if resource_is_creator_only(linked_resource):
            return False, []
        raise ValidationError(
            {"audience_groups": "Select at least one collaboration group."}
        )

    invalid = [group for group in selected if group.id not in eligible_ids]
    if invalid:
        raise ValidationError(
            {
                "audience_groups": (
                    "One or more selected groups cannot see the linked observing resource."
                )
            }
        )
    if any(
        group.name == PUBLIC_COLLABORATION_GROUP_NAME for group in selected
    ) and not resource_is_public(linked_resource):
        raise ValidationError(
            {
                "audience_groups": (
                    "The Public group can only be selected for public observing resources."
                )
            }
        )
    return False, selected
