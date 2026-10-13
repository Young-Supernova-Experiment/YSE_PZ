"""Data access requests: hidden-data counts, request, decide (#291: #292, #293).

``hidden_summary(transient, user)`` tells the detail page how many restricted
photometry sets / spectra the user cannot see and which groups own them (never
the rows themselves). ``request_access`` files a :class:`DataAccessRequest` and
notifies the owner group's members (kind ``data_access``) with a link to the
inbox; ``decide`` accepts (adds the requester's ``target_group`` to the
datasets' ``groups`` M2M, so every existing access check honours it) or
declines, and notifies the requester. See ``models/data_access_models.py`` for
why the grant is a group on the dataset rather than a per-request table.

Who decides (#447, Ryan 2026-10-13): only the PIs of the requested datasets
(``services.data_pis.pis_for``: explicit PIs, else the allocation's PI; accounts
linked through ``PrincipalInvestigator.user``) and the deputies those PIs
designated (``DataSharingDelegate``, ``designated_sharers_for``). Members and
owners of the owner group do not decide. Staff may decide as administrators;
that override is recorded (``DataAccessRequest.decided_as = "staff"``) and
logged. A request whose data has no PI with an account and no deputy can only
be decided by staff; the inbox says so. Accepting shares only the datasets the
decider may share (staff: all of them). Public data (no collaboration group
other than Public) is never requested (``hidden_summary`` only covers
restricted data).
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import DataAccessRequest, Transient, TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing
from YSE_App.services import notify as notify_service
from YSE_App.services.data_pis import ALLOCATION_PI_PATHS, pi_users_for
from YSE_App.services.visibility import user_group_names

logger = logging.getLogger("YSE_App.data_access")

DATASET_MODELS = {
    DataAccessRequest.KIND_PHOTOMETRY: TransientPhotometry,
    DataAccessRequest.KIND_SPECTRUM: TransientSpectrum,
}
KIND_LABELS = {
    DataAccessRequest.KIND_PHOTOMETRY: ("photometry set", "photometry sets"),
    DataAccessRequest.KIND_SPECTRUM: ("spectrum", "spectra"),
}
INBOX_URL = "/data_access_requests/"


def _model(kind: str):
    try:
        return DATASET_MODELS[kind]
    except KeyError:
        raise ValidationError({"dataset_kind": "Unknown dataset kind %r." % (kind,)})


def _is_staff(user: User) -> bool:
    return bool(user.is_staff or user.is_superuser)


def restricted_datasets(transient_id: int, kind: str):
    """Datasets of ``kind`` on the transient that carry at least one group."""
    return (
        _model(kind).objects.filter(transient_id=transient_id)
        .annotate(_gc=Count("groups", distinct=True)).filter(_gc__gt=0)
        .prefetch_related("groups")
    )


def hidden_datasets(user: User, transient_id: int, kind: str) -> list:
    """Restricted datasets of ``kind`` the user cannot see.

    Membership is by group name exactly as ``PhotometryService`` /
    ``SpectraService`` decide it, so staff without the group are "hidden from"
    too: that is what the detail page shows them.
    """
    if not user.is_authenticated:
        return []
    names = set(user_group_names(user))
    hidden = []
    for row in restricted_datasets(transient_id, kind):
        owners = {g.name for g in row.groups.all()}
        if not owners & names:
            hidden.append(row)
    return hidden


def count_hidden(transient_id: int, user: User) -> Dict[str, Dict[str, int]]:
    """{kind: {group_name: n_hidden}} for both dataset kinds (a dataset with two owner groups counts under each)."""
    out: Dict[str, Dict[str, int]] = {}
    for kind in DATASET_MODELS:
        by_group: Dict[str, int] = {}
        for row in hidden_datasets(user, transient_id, kind):
            for group in row.groups.all():
                by_group[group.name] = by_group.get(group.name, 0) + 1
        out[kind] = by_group
    return out


def eligible_target_groups(user: User, user_groups=None) -> List[Group]:
    """Groups a requester may be granted into: their own research teams, never Public or a system group.

    ``user_groups`` (the user's groups, already loaded) saves a query.
    """
    if not user.is_authenticated:
        return []
    from YSE_App.services.groups import system_group_ids

    system = system_group_ids(user)
    if user_groups is None:
        user_groups = user.groups.order_by("name")
    return sorted((g for g in user_groups if g.name != PUBLIC_COLLABORATION_GROUP_NAME and g.id not in system),
                  key=lambda g: g.name)


def requestable_groups(user: User, user_groups=None) -> List[Group]:
    """The target-group picker: ``eligible_target_groups`` minus inactive and user-hidden groups (#440)."""
    from YSE_App.services.groups import picker_groups

    return picker_groups(user, eligible_target_groups(user, user_groups))


def _owner_names_by_dataset(transient_id: int, kind: str):
    """({dataset_id: {group_id: group_name}}, {dataset_id: {PI names}}) for the restricted datasets of ``kind``.

    One query: the PI names (#446) come along with the owner groups, and so do the
    allocation PIs' names, used for datasets without an explicit PI (#447 fallback).
    """
    fallback = tuple("%s__name" % path for path in ALLOCATION_PI_PATHS)
    rows = _model(kind).objects.filter(transient_id=transient_id, groups__isnull=False).values_list(
        "id", "groups__id", "groups__name", "pis__name", *fallback,
    )
    out: Dict[int, Dict[int, str]] = {}
    pis: Dict[int, set] = {}
    alloc: Dict[int, set] = {}
    for dataset_id, group_id, name, pi_name, *alloc_names in rows:
        out.setdefault(dataset_id, {})[group_id] = name
        if pi_name:
            pis.setdefault(dataset_id, set()).add(pi_name)
        alloc.setdefault(dataset_id, set()).update(n for n in alloc_names if n)
    for dataset_id, names in alloc.items():
        if names and dataset_id not in pis:
            pis[dataset_id] = names
    return out, pis


def hidden_summary(transient: Transient, user: User, kinds: Optional[Iterable[str]] = None, *,
                   user_groups: Optional[Iterable[Group]] = None) -> List[dict]:
    """Rows for the detail-page hint: one per (kind, owner group) with hidden data.

    Each row: ``kind``, ``kind_label`` (pluralised), ``group`` (owner), ``count``,
    ``pis`` (the PI names of those datasets, #446, with the allocation fallback of #447; names only;
    empty means "no PI assigned": only an administrator can share that data),
    ``request`` (this user's latest request for that kind / group, or None) and
    ``can_request`` (the user has a non-Public group to be granted into).
    Nothing else about the hidden rows themselves is exposed. One query per kind
    plus one for the user's groups (pass ``user_groups`` to skip that one);
    the requests are looked up only when something is hidden.
    """
    if not user.is_authenticated:
        return []
    kinds = list(kinds) if kinds is not None else list(DATASET_MODELS)
    user_groups = list(user_groups) if user_groups is not None else list(user.groups.all())
    names = {g.name for g in user_groups}
    rows: List[dict] = []
    per_group: Dict[str, Dict[int, int]] = {}
    pi_names: Dict[str, Dict[int, set]] = {}
    owner_names: Dict[int, str] = {}
    for kind in kinds:
        counts: Dict[int, int] = {}
        kind_pis: Dict[int, set] = {}
        by_dataset, dataset_pis = _owner_names_by_dataset(transient.id, kind)
        for dataset_id, owners in by_dataset.items():
            if set(owners.values()) & names:
                continue
            for group_id, name in owners.items():
                if name == PUBLIC_COLLABORATION_GROUP_NAME:
                    continue
                counts[group_id] = counts.get(group_id, 0) + 1
                owner_names[group_id] = name
                kind_pis.setdefault(group_id, set()).update(dataset_pis.get(dataset_id, ()))
        per_group[kind] = counts
        pi_names[kind] = kind_pis
    if not any(per_group.values()):
        return []
    groups = {g.pk: g for g in Group.objects.filter(pk__in=owner_names)}
    latest = {}
    for req in DataAccessRequest.objects.filter(
        requester=user, transient=transient, dataset_kind__in=kinds,
    ).select_related("owner_group", "target_group", "decided_by").order_by("created_date", "id"):
        latest[(req.dataset_kind, req.owner_group_id)] = req
    can_request = any(g.name != PUBLIC_COLLABORATION_GROUP_NAME for g in user_groups)
    for kind in kinds:
        by_group = {groups[gid]: n for gid, n in per_group[kind].items() if gid in groups}
        for group, n in sorted(by_group.items(), key=lambda kv: kv[0].name):
            singular, plural = KIND_LABELS[kind]
            rows.append({
                "kind": kind,
                "kind_label": singular if n == 1 else plural,
                "group": group,
                "count": n,
                "pis": sorted(pi_names[kind].get(group.pk, ())),
                "request": latest.get((kind, group.pk)),
                "can_request": can_request,
            })
    return rows


def request_datasets(request: DataAccessRequest):
    """The restricted datasets a request covers: its kind, on its transient, owned by its owner group."""
    qs = restricted_datasets(request.transient_id, request.dataset_kind).filter(groups=request.owner_group_id)
    if request.dataset_id is not None:
        qs = qs.filter(pk=request.dataset_id)
    return qs.distinct()


def designated_sharers_for(request: DataAccessRequest, datasets) -> List[User]:
    """Active deputies (#447) whom the datasets' PIs designated, for all their data or for one of the datasets' groups."""
    users: List[User] = []
    for dataset in datasets:
        users.extend(data_sharing.delegates_for(dataset))
    return _unique_users(users)


def _unique_users(users: Iterable[User]) -> List[User]:
    seen, out = set(), []
    for user in users:
        if user is not None and user.pk not in seen and user.is_active:
            seen.add(user.pk)
            out.append(user)
    return out


def named_deciders_for(request: DataAccessRequest) -> List[User]:
    """People who may share the requested data: its PIs' accounts (#446, with the allocation fallback) and their
    designated deputies (#447)."""
    datasets = list(request_datasets(request))
    return _unique_users(list(pi_users_for(datasets)) + list(designated_sharers_for(request, datasets)))


def deciders_for(request: DataAccessRequest) -> List[User]:
    """Who is asked to decide a request: the data's PIs and their deputies only (#447).

    Group members and group owners are not deciders. When nobody is named, only staff can decide
    (``notify_new_request`` then tells the active staff).
    """
    return named_deciders_for(request)


def _staff_users() -> List[User]:
    return list(User.objects.filter(Q(is_staff=True) | Q(is_superuser=True), is_active=True).order_by("id"))


def shareable_datasets(user: User, request: DataAccessRequest) -> List:
    """The request's datasets ``user`` may share as a PI or a deputy (no staff override)."""
    if not getattr(user, "is_authenticated", False):
        return []
    pi_ids = data_sharing.own_pi_ids(user)
    designations = data_sharing.designations_of(user)
    if not pi_ids and not designations:
        return []
    return [row for row in request_datasets(request)
            if data_sharing.share_role(user, row, pi_ids=pi_ids, designations=designations,
                                       staff_override=False)]


def decider_role(user: User, request: DataAccessRequest) -> Optional[str]:
    """``"pi"`` / ``"deputy"`` (for at least one requested dataset), ``"staff"`` (administrator override) or None."""
    if not getattr(user, "is_authenticated", False):
        return None
    pi_ids = data_sharing.own_pi_ids(user)
    designations = data_sharing.designations_of(user)
    role = None
    if pi_ids or designations:
        for row in request_datasets(request):
            got = data_sharing.share_role(user, row, pi_ids=pi_ids, designations=designations, staff_override=False)
            if got == data_sharing.ROLE_PI:
                return got
            role = role or got
    if role:
        return role
    if _is_staff(user):
        return data_sharing.ROLE_STAFF
    return None


def user_can_decide(user: User, request: DataAccessRequest) -> bool:
    """The data's PIs and deputies; staff as administrators (#447). Group membership or ownership is not enough."""
    return decider_role(user, request) is not None


def _shareable_request_annotations(user: User) -> Dict[str, Exists]:
    """Exists() annotations: the user may share (PI or deputy) data a request covers, per dataset kind."""
    out = {}
    for kind, model in DATASET_MODELS.items():
        q = data_sharing.shareable_q(user, model)
        if q is None:
            continue
        base = model.objects.filter(
            transient_id=OuterRef("transient_id"), groups=OuterRef("owner_group_id"),
        ).filter(q)
        out["_share_any_%s" % kind] = Exists(base)
        out["_share_one_%s" % kind] = Exists(base.filter(pk=OuterRef("dataset_id")))
    return out


def requests_to_decide(user: User, *, pending_only: bool = True):
    """Requests for data the user is a PI or a deputy of (staff: every request, as administrators)."""
    qs = DataAccessRequest.objects.select_related("requester", "transient", "owner_group", "target_group", "decided_by")
    if not getattr(user, "is_authenticated", False):
        return qs.none()
    if not _is_staff(user):
        annotations = _shareable_request_annotations(user)
        if not annotations:
            return qs.none()
        qs = qs.annotate(**annotations)
        allowed = Q(pk__in=[])
        for kind in DATASET_MODELS:
            if "_share_any_%s" % kind not in annotations:
                continue
            allowed |= Q(dataset_kind=kind, dataset_id__isnull=True, **{"_share_any_%s" % kind: True})
            allowed |= Q(dataset_kind=kind, **{"_share_one_%s" % kind: True})
        qs = qs.filter(allowed)
    if pending_only:
        qs = qs.filter(status=DataAccessRequest.STATUS_PENDING)
    return qs


def requests_by(user: User):
    return DataAccessRequest.objects.filter(requester=user).select_related(
        "transient", "owner_group", "target_group", "decided_by",
    )


PENDING_CACHE_SECONDS = 60


def _pending_cache_key(user_id: int) -> str:
    return "yse_dar_pending_%s" % user_id


def pending_count_for(user: User) -> int:
    """Pending requests the user may decide; cached for a minute (the menu badge asks on every page)."""
    if not getattr(user, "is_authenticated", False):
        return 0
    from django.core.cache import cache

    key = _pending_cache_key(user.pk)
    value = cache.get(key)
    if value is None:
        value = requests_to_decide(user).count()
        cache.set(key, value, PENDING_CACHE_SECONDS)
    return int(value)


def invalidate_pending_counts(extra_user_ids: Iterable[int] = ()) -> None:
    """Drop the cached badge counts of the deciders of a request (``extra_user_ids``: its PIs and deputies) and staff."""
    from django.core.cache import cache

    ids = list(extra_user_ids)
    ids += list(User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).values_list("id", flat=True))
    cache.delete_many([_pending_cache_key(i) for i in set(ids)])


def _request_url(request: DataAccessRequest) -> str:
    return "%s?request=%s" % (INBOX_URL, request.pk)


@transaction.atomic
def request_access(
    user: User, transient: Transient, kind: str, owner_group: Group, *,
    target_group: Optional[Group] = None, message: str = "", dataset_id: Optional[int] = None,
    notify_owners: bool = True,
) -> DataAccessRequest:
    """File a request for the restricted ``kind`` data that ``owner_group`` holds on ``transient``."""
    model = _model(kind)
    if not user.is_authenticated:
        raise PermissionDenied({"message": "Log in to request access."})
    hidden = hidden_datasets(user, transient.id, kind)
    if dataset_id is not None:
        hidden = [row for row in hidden if row.pk == int(dataset_id)]
    owned = [row for row in hidden if any(g.pk == owner_group.pk for g in row.groups.all())]
    if not owned:
        raise ValidationError({
            "owner_group": "%s holds no restricted %s on %s that you cannot already see."
                           % (owner_group.name, KIND_LABELS[kind][1], transient.name),
        })
    choices = requestable_groups(user)
    if target_group is None:
        if len(choices) != 1:
            raise ValidationError({
                "target_group": "Choose which of your collaboration groups should receive access."
                                if choices else
                                "You belong to no collaboration group (other than Public) that access could be "
                                "granted to; ask an administrator to add you to one first.",
            })
        target_group = choices[0]
    elif target_group.pk not in {g.pk for g in eligible_target_groups(user)}:
        raise PermissionDenied({"message": "Access can only be granted to a collaboration group you belong to (not Public)."})
    if target_group.pk == owner_group.pk:
        raise ValidationError({"target_group": "That group already owns the data."})
    duplicate = DataAccessRequest.objects.filter(
        requester=user, transient=transient, dataset_kind=kind, owner_group=owner_group,
        dataset_id=dataset_id, status=DataAccessRequest.STATUS_PENDING,
    ).first()
    if duplicate is not None:
        raise ValidationError({"message": "You already have a pending request for this data (#%s)." % duplicate.pk})
    del model  # validated above
    request = DataAccessRequest.objects.create(
        requester=user, transient=transient, dataset_kind=kind, dataset_id=dataset_id, owner_group=owner_group,
        target_group=target_group, message=(message or "").strip(), created_by=user, modified_by=user,
    )
    invalidate_pending_counts([u.pk for u in named_deciders_for(request)])
    if notify_owners:
        notify_new_request(request)
    return request


def notify_new_request(request: DataAccessRequest):
    """Tell the data's PIs and deputies; when there are none, the active staff (only they can decide then)."""
    owners = deciders_for(request)
    no_pi = not owners
    if no_pi:
        owners = _staff_users()
    if not owners:
        return []
    n = len(hidden_datasets(request.requester, request.transient_id, request.dataset_kind))
    text = "%s asks for access to %d restricted %s of %s on %s (to be shared with %s).%s%s\nAccept or decline: %s" % (
        request.requester.username, n, KIND_LABELS[request.dataset_kind][1], request.owner_group.name,
        request.transient.name, request.target_group.name,
        "\nNo PI assigned (or no PI with an account): only administrators can decide this request." if no_pi else "",
        ("\n\"%s\"" % request.message) if request.message else "",
        notify_service.absolute_url(_request_url(request)),
    )
    return notify_service.notify(
        owners, text, _request_url(request), "data_access",
        subject="Data access request on %s" % request.transient.name, transient=request.transient,
        payload={"request_id": request.pk, "action": "new"}, created_by=request.requester,
        exclude=[request.requester_id],
    )


def _grant(request: DataAccessRequest, rows=None) -> List[int]:
    """Add the target group to ``rows`` (default: every dataset the request covers); return their ids."""
    if rows is None:
        rows = request_datasets(request)
    granted = []
    for row in rows:
        if not row.groups.filter(pk=request.target_group_id).exists():
            row.groups.add(request.target_group)
        granted.append(row.pk)
    return granted


@transaction.atomic
def decide(request: DataAccessRequest, user: User, accept: bool, *, note: str = "", notify_requester: bool = True):
    """Accept (grant the target group) or decline a pending request.

    PIs and deputies share only the datasets they may share; staff decide as administrators (all the
    datasets), which is recorded in ``decided_as`` and logged.
    """
    role = decider_role(user, request)
    if role is None:
        raise PermissionDenied({"message": "Only the data's PIs and the deputies they designated (or an "
                                           "administrator) may decide this request."})
    if not request.is_pending:
        raise ValidationError({"status": "This request was already %s." % request.status})
    rows = None if role == data_sharing.ROLE_STAFF else shareable_datasets(user, request)
    request.decided_by = user
    request.decided_as = role
    request.decided_at = timezone.now()
    request.note = (note or "").strip()
    request.modified_by = user
    if accept:
        request.granted_dataset_ids = _grant(request, rows)
        request.status = DataAccessRequest.STATUS_ACCEPTED
    else:
        request.status = DataAccessRequest.STATUS_DECLINED
    request.save()
    if role == data_sharing.ROLE_STAFF:
        logger.warning(
            "data access request %s %s by staff user id %s as an administrator override (transient id %s, %s, "
            "owner group id %s, target group id %s)", request.pk, request.status, user.pk, request.transient_id,
            request.dataset_kind, request.owner_group_id, request.target_group_id,
        )
    invalidate_pending_counts([u.pk for u in named_deciders_for(request)])
    if notify_requester:
        notify_decision(request)
    return request


def notify_decision(request: DataAccessRequest):
    transient = request.transient
    if request.status == DataAccessRequest.STATUS_ACCEPTED:
        text = "%s accepted your request: %s now sees %d restricted %s on %s." % (
            request.owner_group.name, request.target_group.name, request.granted_count,
            KIND_LABELS[request.dataset_kind][1], transient.name,
        )
        url = "/transient_detail/%s/" % transient.slug
    else:
        text = "%s declined your request for %s on %s." % (
            request.owner_group.name, KIND_LABELS[request.dataset_kind][1], transient.name,
        )
        url = _request_url(request)
    if request.note:
        text += "\nNote: %s" % request.note
    return notify_service.notify(
        [request.requester], text, url, "data_access",
        subject="Data access %s on %s" % (request.status, transient.name), transient=transient,
        payload={"request_id": request.pk, "action": request.status}, created_by=request.decided_by,
    )


def filter_requests(qs, params: dict):
    """Apply the ``?status=``, ``?transient=``, ``?kind=`` query filters shared by the page and the API."""
    status = (params.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)
    transient = (params.get("transient") or "").strip()
    if transient:
        qs = qs.filter(Q(transient_id=transient) if transient.isdigit() else Q(transient__name=transient))
    kind = (params.get("kind") or "").strip()
    if kind:
        qs = qs.filter(dataset_kind=kind)
    return qs
