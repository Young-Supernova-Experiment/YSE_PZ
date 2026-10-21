"""Who may share private photometry and spectra (#447).

Ryan's rule (2026-10-13): only the data's PIs and the deputies they designated
may share private data, that is, decide data-access requests or add groups to
a restricted dataset. Group members and group owners get no sharing authority
from their group role; staff may act as administrators (a logged override).

* The PIs of a dataset come from :func:`YSE_App.services.data_pis.pis_for`
  (explicit PIs, else the allocation's PI, resolved at read time).
* A deputy is a :class:`~YSE_App.models.DataSharingDelegate` row: ``delegate``
  shares for ``pi``, for all of that PI's data or only for the datasets
  restricted to ``group``. PIs manage their own deputies
  (:func:`add_delegate` / :func:`remove_delegate`, the "Data sharing deputies"
  page); staff use the admin.
* Public data (no collaboration group other than Public: ZTF and ATLAS forced
  photometry, TNS and literature data) needs no PI and is never shared or
  requested (:func:`is_public_dataset`).

:func:`share_role` answers "may this user share this dataset, and as what";
:func:`shareable_q` is the same rule in SQL for the inbox and the badge.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import DataSharingDelegate, PrincipalInvestigator
from YSE_App.services.data_pis import effective_pi_q, pis_for

ROLE_PI = "pi"
ROLE_DEPUTY = "deputy"
ROLE_STAFF = "staff"


def is_staff(user) -> bool:
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _group_ids(dataset) -> Set[int]:
    if "groups" in getattr(dataset, "_prefetched_objects_cache", {}):
        return {g.pk for g in dataset.groups.all()}
    return set(dataset.groups.values_list("id", flat=True))


def is_public_dataset(dataset) -> bool:
    """Public data: no collaboration group other than Public. It needs no PI and is never shared or requested."""
    if not getattr(dataset, "pk", None):
        return True
    if "groups" in getattr(dataset, "_prefetched_objects_cache", {}):
        names = {g.name for g in dataset.groups.all()}
    else:
        names = set(dataset.groups.values_list("name", flat=True))
    return not (names - {PUBLIC_COLLABORATION_GROUP_NAME})


def own_pi_ids(user) -> List[int]:
    """Ids of the PI records linked to ``user``'s account."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return []
    return list(PrincipalInvestigator.objects.filter(user=user).values_list("id", flat=True))


def designations_of(user) -> List[Tuple[int, Optional[int]]]:
    """``[(pi_id, group_id or None)]``: whom ``user`` is a deputy for, and for which group's data."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return []
    return list(DataSharingDelegate.objects.filter(delegate=user).values_list("pi_id", "group_id"))


def share_role(user, dataset, *, pi_ids: Optional[Iterable[int]] = None,
               designations: Optional[Iterable[Tuple[int, Optional[int]]]] = None,
               staff_override: bool = True) -> Optional[str]:
    """``"pi"``, ``"deputy"``, ``"staff"`` (only with ``staff_override``) or None.

    ``pi_ids`` / ``designations`` (from :func:`own_pi_ids` / :func:`designations_of`) save
    queries when several datasets are checked for one user.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    mine = set(own_pi_ids(user) if pi_ids is None else pi_ids)
    deps = list(designations_of(user) if designations is None else designations)
    if mine or deps:
        dataset_pis = {p.pk for p in pis_for(dataset)}
        if dataset_pis & mine:
            return ROLE_PI
        if deps and dataset_pis:
            groups = None
            for pi_id, group_id in deps:
                if pi_id not in dataset_pis:
                    continue
                if group_id is None:
                    return ROLE_DEPUTY
                if groups is None:
                    groups = _group_ids(dataset)
                if group_id in groups:
                    return ROLE_DEPUTY
    if staff_override and is_staff(user):
        return ROLE_STAFF
    return None


def user_can_share(user, dataset) -> bool:
    return share_role(user, dataset) is not None


def shareable_q(user, model) -> Optional[Q]:
    """``Q`` on ``model``: the datasets ``user`` may share as a PI or a deputy (None: none; staff not included)."""
    mine = own_pi_ids(user)
    deps = designations_of(user)
    q = None
    if mine:
        q = effective_pi_q(model, mine)
    unscoped = sorted({pi for pi, group in deps if group is None})
    if unscoped:
        part = effective_pi_q(model, unscoped)
        q = part if q is None else q | part
    by_group: Dict[int, Set[int]] = {}
    for pi, group in deps:
        if group is not None and pi not in unscoped:
            by_group.setdefault(group, set()).add(pi)
    for group, pis in sorted(by_group.items()):
        part = effective_pi_q(model, sorted(pis)) & Q(pk__in=model.objects.filter(groups=group).values("pk"))
        q = part if q is None else q | part
    return q


def delegates_for(dataset, pis=None) -> List[User]:
    """Active deputies who may share ``dataset`` (for its PIs, scoped to one of its groups or unscoped)."""
    pi_ids = [p.pk for p in (pis_for(dataset) if pis is None else pis)]
    if not pi_ids:
        return []
    groups = _group_ids(dataset)
    rows = DataSharingDelegate.objects.filter(pi_id__in=pi_ids, delegate__is_active=True).filter(
        Q(group__isnull=True) | Q(group_id__in=groups)).select_related("delegate")
    seen: Dict[int, User] = {}
    for row in rows:
        seen[row.delegate_id] = row.delegate
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------------------
# managing deputies (the PI's own page; staff also use the admin)


def can_manage(user, pi: PrincipalInvestigator) -> bool:
    """The PI's own linked account, or staff."""
    if not getattr(user, "is_authenticated", False):
        return False
    return is_staff(user) or (pi.user_id is not None and pi.user_id == user.pk)


def managed_pis(user) -> List[PrincipalInvestigator]:
    """The PI records whose deputies ``user`` manages on the page (their own; staff use the admin for others)."""
    if not getattr(user, "is_authenticated", False):
        return []
    return list(PrincipalInvestigator.objects.filter(user=user).order_by("name", "id"))


def _invalidate(user_ids: Iterable[int]) -> None:
    from django.core.cache import cache

    from YSE_App.services.data_access import _pending_cache_key

    cache.delete_many([_pending_cache_key(i) for i in set(user_ids)])


@transaction.atomic
def add_delegate(actor, pi: PrincipalInvestigator, delegate: User, group: Optional[Group] = None,
                 note: str = "") -> DataSharingDelegate:
    if not can_manage(actor, pi):
        raise PermissionDenied({"message": "Only the PI (or staff) may designate deputies for %s." % pi.name})
    if delegate is None or not delegate.is_active:
        raise ValidationError({"delegate": "Choose an active account."})
    if pi.user_id is not None and delegate.pk == pi.user_id:
        raise ValidationError({"delegate": "The PI already shares their own data."})
    if group is not None and group.name == PUBLIC_COLLABORATION_GROUP_NAME:
        raise ValidationError({"group": "Public data is not shared through deputies; leave the group empty or pick "
                                        "a collaboration group."})
    existing = DataSharingDelegate.objects.filter(pi=pi, delegate=delegate, group=group).first()
    if existing is not None:
        return existing
    row = DataSharingDelegate.objects.create(pi=pi, delegate=delegate, group=group, note=(note or "").strip()[:255],
                                             created_by=actor)
    _invalidate([delegate.pk])
    return row


@transaction.atomic
def remove_delegate(actor, row: DataSharingDelegate) -> None:
    if not can_manage(actor, row.pi):
        raise PermissionDenied({"message": "Only the PI (or staff) may remove deputies for %s." % row.pi.name})
    delegate_id = row.delegate_id
    row.delete()
    _invalidate([delegate_id])


def check_group_change(user, dataset, new_groups) -> None:
    """Changing the groups of restricted data shares it (or un-restricts it): PIs, deputies and staff only (#447).

    Used by the REST serializers' ``update``. Public data and unchanged group sets pass.
    """
    if not getattr(dataset, "pk", None) or is_public_dataset(dataset):
        return
    current = _group_ids(dataset)
    wanted = {g.pk for g in new_groups}
    if current == wanted:
        return
    if share_role(user, dataset) is None:
        raise PermissionDenied({"message": "Only the data's PIs and their designated deputies (or an administrator) "
                                           "may change the groups of restricted data."})
