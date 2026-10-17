"""Access history for read-time rules (#648, PR 1b): dated group membership and dataset group shares.

Recorded from now on, whether or not ``DATA_ACCESS_GRANTS_ENABLED`` is on, so
the engine (PR 2) can apply Ryan's rule (2026-10-17): a person who leaves a
group keeps the points shared with it while they were a member, and does not
get points shared with it after they left.

* :func:`record_membership_change`: every ``User.groups`` add / remove / clear
  (from either side of the relation) opens or closes a
  :class:`~YSE_App.models.GroupMembershipPeriod`. Memberships from before
  tracking were seeded by migration 0044 with ``joined_at`` unknown.
* :func:`record_dataset_share_change`: adding a group to a photometry set's or
  spectrum's own ``groups`` stores (or moves forward) its
  :class:`~YSE_App.models.DatasetGroupShare` time; removing the group deletes
  the row. A group on a dataset with no row was shared before tracking.

Both are wired to ``m2m_changed`` in ``YSE_App.signals``. Writes that bypass
``m2m_changed`` (raw through-table inserts) are not recorded; none exist for
these two relations today.
"""

from __future__ import annotations

from typing import Iterable

from django.utils import timezone


def _open_periods(user_ids: Iterable[int], group_ids: Iterable[int]):
    from YSE_App.models import GroupMembershipPeriod

    return GroupMembershipPeriod.objects.filter(
        user_id__in=list(user_ids), group_id__in=list(group_ids), left_at__isnull=True)


def joined(pairs, at=None) -> None:
    """Open a period for each new ``(user_id, group_id)`` that has none open."""
    from YSE_App.models import GroupMembershipPeriod

    pairs = {(int(u), int(g)) for u, g in pairs}
    if not pairs:
        return
    at = at or timezone.now()
    users = {u for u, _ in pairs}
    groups = {g for _, g in pairs}
    have = set(_open_periods(users, groups).values_list("user_id", "group_id"))
    rows = [GroupMembershipPeriod(user_id=u, group_id=g, joined_at=at) for u, g in sorted(pairs - have)]
    if rows:
        GroupMembershipPeriod.objects.bulk_create(rows)


def left(pairs, at=None) -> None:
    """Close the open period of each ``(user_id, group_id)`` (a closed one is recorded if none was open)."""
    from YSE_App.models import GroupMembershipPeriod

    pairs = {(int(u), int(g)) for u, g in pairs}
    if not pairs:
        return
    at = at or timezone.now()
    users = {u for u, _ in pairs}
    groups = {g for _, g in pairs}
    open_rows = [row for row in _open_periods(users, groups) if (row.user_id, row.group_id) in pairs]
    closed = set()
    for row in open_rows:
        closed.add((row.user_id, row.group_id))
    if open_rows:
        GroupMembershipPeriod.objects.filter(pk__in=[r.pk for r in open_rows]).update(left_at=at)
    missing = [GroupMembershipPeriod(user_id=u, group_id=g, joined_at=None, left_at=at)
               for u, g in sorted(pairs - closed)]
    if missing:
        GroupMembershipPeriod.objects.bulk_create(missing)


def record_membership_change(instance, action, reverse, pk_set) -> None:
    """``m2m_changed`` on ``User.groups.through``: ``instance`` is a user (forward) or a group (reverse)."""
    memo = "_yse_membership_clear_pks"
    if action == "pre_clear":
        rel = instance.user_set if reverse else instance.groups
        setattr(instance, memo, list(rel.values_list("pk", flat=True)))
        return
    if action == "post_clear":
        pk_set = getattr(instance, memo, [])
        setattr(instance, memo, [])
        action = "post_remove"
    if action not in ("post_add", "post_remove") or not pk_set:
        return
    pairs = [(pk, instance.pk) if reverse else (instance.pk, pk) for pk in pk_set]
    if action == "post_add":
        joined(pairs)
    else:
        left(pairs)


def _dataset_field(model) -> str:
    from YSE_App.models import TransientPhotometry

    return "transient_photometry_id" if model is TransientPhotometry else "transient_spectrum_id"


def shared(model, pairs, at=None) -> None:
    """Record ``(dataset_id, group_id)`` pairs as shared now (moves an existing row's time forward)."""
    from YSE_App.models import DatasetGroupShare

    pairs = {(int(d), int(g)) for d, g in pairs}
    if not pairs:
        return
    at = at or timezone.now()
    field = _dataset_field(model)
    datasets = {d for d, _ in pairs}
    groups = {g for _, g in pairs}
    existing = DatasetGroupShare.objects.filter(**{field + "__in": datasets}, group_id__in=groups)
    have = {}
    for row in existing:
        key = (getattr(row, field), row.group_id)
        if key in pairs:
            have[key] = row.pk
    if have:
        DatasetGroupShare.objects.filter(pk__in=list(have.values())).update(shared_at=at)
    rows = [DatasetGroupShare(**{field: d}, group_id=g, shared_at=at) for d, g in sorted(pairs - set(have))]
    if rows:
        DatasetGroupShare.objects.bulk_create(rows, ignore_conflicts=True)


def unshared(model, pairs) -> None:
    """Forget the share time of ``(dataset_id, group_id)`` pairs whose group was removed from the dataset."""
    from YSE_App.models import DatasetGroupShare

    pairs = {(int(d), int(g)) for d, g in pairs}
    if not pairs:
        return
    field = _dataset_field(model)
    rows = DatasetGroupShare.objects.filter(
        **{field + "__in": {d for d, _ in pairs}}, group_id__in={g for _, g in pairs})
    pks = [row.pk for row in rows if (getattr(row, field), row.group_id) in pairs]
    if pks:
        DatasetGroupShare.objects.filter(pk__in=pks).delete()


def record_dataset_share_change(model, instance, action, reverse, pk_set) -> None:
    """``m2m_changed`` on ``<model>.groups.through``: ``instance`` is a dataset (forward) or a group (reverse)."""
    memo = "_yse_dataset_share_clear_pks"
    if action == "pre_clear":
        rel = getattr(instance, "%s_set" % model._meta.model_name) if reverse else instance.groups
        setattr(instance, memo, list(rel.values_list("pk", flat=True)))
        return
    if action == "post_clear":
        pk_set = getattr(instance, memo, [])
        setattr(instance, memo, [])
        action = "post_remove"
    if action not in ("post_add", "post_remove") or not pk_set:
        return
    pairs = [(pk, instance.pk) if reverse else (instance.pk, pk) for pk in pk_set]
    if action == "post_add":
        shared(model, pairs)
    else:
        unshared(model, pairs)
