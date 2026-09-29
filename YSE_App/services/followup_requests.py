"""Create/attach TransientFollowupRequest children and display helpers.

Parent ``TransientFollowup`` holds status, resource, and window. Each form
or API submit inserts a new child (same user may have many). Display
priority is the minimum of each requestor's most recent child priority
(1.0 highest, 5.0 lowest).
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
# Import model modules directly. ``from YSE_App.models import TransientFollowupRequest``
# fails during URLconf load: views -> yse_pa -> table_utils -> view_utils ->
# serializers -> this module, while ``YSE_App.models`` package namespace is stale
# or still initializing.
from YSE_App.models.enum_models import FollowupStatus
from YSE_App.models.followup_models import TransientFollowup, TransientFollowupRequest
from YSE_App.models.transient_models import Transient

DEFAULT_PRIORITY = 4.0
PRIORITY_MIN = 1.0
PRIORITY_MAX = 5.0
TERMINAL_FOLLOWUP_STATUSES = frozenset({"Successful", "Failed", "StatusDeleted"})


def validate_priority(priority: float) -> float:
    """Return ``priority`` if it is in ``[1.0, 5.0]``.

    Parameters
    ----------
    priority:
        Requested observing priority (1.0 highest).

    Returns
    -------
    float
        The same value, coerced to float.

    Raises
    ------
    ValidationError
        If ``priority`` is outside 1.0–5.0.
    """
    value = float(priority)
    if value < PRIORITY_MIN or value > PRIORITY_MAX:
        raise ValidationError(
            f"priority must be between {PRIORITY_MIN} and {PRIORITY_MAX} (got {value})."
        )
    return value


def find_active_parent(
    transient: Transient,
    *,
    classical_resource=None,
    too_resource=None,
    queued_resource=None,
) -> Optional[TransientFollowup]:
    """Return the newest non-terminal parent for this transient+resource.

    Parameters
    ----------
    transient:
        Target transient.
    classical_resource, too_resource, queued_resource:
        Resource FKs to match exactly (including None).

    Returns
    -------
    TransientFollowup or None
        Newest active parent, or None if none exists.
    """
    return (
        TransientFollowup.objects.filter(
            transient=transient,
            classical_resource=classical_resource,
            too_resource=too_resource,
            queued_resource=queued_resource,
        )
        .exclude(status__name__in=TERMINAL_FOLLOWUP_STATUSES)
        .order_by("-id")
        .first()
    )


def effective_priority(followup: TransientFollowup) -> Optional[float]:
    """Min of each requestor's most recent child priority.

    Parameters
    ----------
    followup:
        Parent follow-up. Prefetch ``requests`` to avoid extra queries.

    Returns
    -------
    float or None
        Consolidated priority, or the denormalized parent value if no children.
    """
    requests = list(followup.requests.all())
    if not requests:
        return followup.priority
    latest_by_user = {}
    for req in sorted(requests, key=lambda row: (row.requested_at, row.id), reverse=True):
        if req.requestor_id not in latest_by_user:
            latest_by_user[req.requestor_id] = req.priority
    if not latest_by_user:
        return followup.priority
    return min(latest_by_user.values())


def recompute_parent_priority(followup: TransientFollowup) -> Optional[float]:
    """Write ``effective_priority`` onto the parent and return it.

    Parameters
    ----------
    followup:
        Parent to update. ``priority`` is saved with ``update_fields``.

    Returns
    -------
    float or None
        New denormalized priority.
    """
    priority = effective_priority(followup)
    if followup.priority != priority:
        followup.priority = priority
        followup.save(update_fields=["priority"])
    return priority


def format_requestors(followup: TransientFollowup) -> str:
    """Comma-separated unique requestor usernames in first-request order.

    Parameters
    ----------
    followup:
        Parent follow-up.

    Returns
    -------
    str
        Display string, empty if there are no children.
    """
    seen = []
    for req in _ordered_requests(followup):
        name = str(req.requestor)
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


def format_comments(followup: TransientFollowup) -> str:
    """Join child comments as ``user: text``.

    Empty comments are omitted. Order is ``requested_at`` ascending.

    Parameters
    ----------
    followup:
        Parent follow-up.

    Returns
    -------
    str
        Semicolon-separated labeled comments.
    """
    parts: List[str] = []
    for req in _ordered_requests(followup):
        text = (req.comment or "").strip()
        if not text:
            continue
        parts.append(f"{req.requestor}: {text}")
    return "; ".join(parts)


def create_or_attach_request(
    user: User,
    transient: Transient,
    *,
    status: FollowupStatus,
    valid_start,
    valid_stop,
    priority: float = DEFAULT_PRIORITY,
    comment: str = "",
    classical_resource=None,
    too_resource=None,
    queued_resource=None,
    offset_star_ra=None,
    offset_star_dec=None,
    offset_north=None,
    offset_east=None,
) -> Tuple[TransientFollowup, TransientFollowupRequest, bool]:
    """Insert a child request; create an active parent only if needed.

    Attaching to an existing parent does not change parent status, window,
    or offsets. ``requested_by`` is set on the first child only.

    Parameters
    ----------
    user:
        Requestor (also stamps ``created_by`` / ``modified_by``).
    transient:
        Target transient.
    status:
        Used only when creating a new parent.
    valid_start, valid_stop:
        Validity window for a new parent.
    priority:
        Child priority, 1.0–5.0 (default 4.0).
    comment:
        Child comment (may be empty).
    classical_resource, too_resource, queued_resource:
        Resource FKs that key the active parent.
    offset_star_ra, offset_star_dec, offset_north, offset_east:
        Offset fields for a new parent.

    Returns
    -------
    tuple
        ``(parent, child, created_parent)``.

    Raises
    ------
    ValidationError
        If ``priority`` is outside 1.0–5.0.
    """
    priority = validate_priority(priority)
    comment = comment or ""

    parent = find_active_parent(
        transient,
        classical_resource=classical_resource,
        too_resource=too_resource,
        queued_resource=queued_resource,
    )
    created_parent = parent is None
    if created_parent:
        parent = TransientFollowup(
            transient=transient,
            status=status,
            valid_start=valid_start,
            valid_stop=valid_stop,
            classical_resource=classical_resource,
            too_resource=too_resource,
            queued_resource=queued_resource,
            offset_star_ra=offset_star_ra,
            offset_star_dec=offset_star_dec,
            offset_north=offset_north,
            offset_east=offset_east,
            requested_by=user,
            created_by=user,
            modified_by=user,
        )
        parent.save()
    else:
        parent.modified_by = user
        parent.save(update_fields=["modified_by", "modified_date"])
        if parent.requested_by_id is None:
            parent.requested_by = user
            parent.save(update_fields=["requested_by"])

    child = TransientFollowupRequest(
        followup=parent,
        requestor=user,
        priority=priority,
        comment=comment,
        created_by=user,
        modified_by=user,
    )
    child.save()
    recompute_parent_priority(parent)
    parent.refresh_from_db()
    return parent, child, created_parent


def _ordered_requests(followup: TransientFollowup) -> Iterable[TransientFollowupRequest]:
    requests = followup.requests.all()
    if isinstance(requests, QuerySet):
        return requests.select_related("requestor").order_by("requested_at", "id")
    return sorted(requests, key=lambda row: (row.requested_at, row.id))
