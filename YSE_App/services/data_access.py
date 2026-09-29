"""Data access requests: hidden-data counts, request, decide (#291: #292, #293).

``hidden_summary(transient, user)`` tells the detail page how many restricted
photometry sets / spectra the user cannot see and which groups own them (never
the rows themselves). ``request_access`` files a :class:`DataAccessRequest` and
notifies the owner group's members (kind ``data_access``) with a link to the
inbox; ``decide`` accepts (adds the requester's ``target_group`` to the
datasets' ``groups`` M2M, so every existing access check honours it) or
declines, and notifies the requester. See ``models/data_access_models.py`` for
why the grant is a group on the dataset rather than a per-request table.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import DataAccessRequest, Transient, TransientPhotometry, TransientSpectrum
from YSE_App.services import notify as notify_service
from YSE_App.services.visibility import user_group_names

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


def requestable_groups(user: User) -> List[Group]:
    """Groups a requester may be granted into: their own groups, never Public."""
    if not user.is_authenticated:
        return []
    return list(user.groups.exclude(name=PUBLIC_COLLABORATION_GROUP_NAME).order_by("name"))


def _owner_names_by_dataset(transient_id: int, kind: str) -> Dict[int, Dict[int, str]]:
    """{dataset_id: {group_id: group_name}} for the restricted datasets of ``kind`` (one query)."""
    rows = _model(kind).objects.filter(transient_id=transient_id, groups__isnull=False).values_list(
        "id", "groups__id", "groups__name",
    )
    out: Dict[int, Dict[int, str]] = {}
    for dataset_id, group_id, name in rows:
        out.setdefault(dataset_id, {})[group_id] = name
    return out


def hidden_summary(transient: Transient, user: User, kinds: Optional[Iterable[str]] = None, *,
                   user_groups: Optional[Iterable[Group]] = None) -> List[dict]:
    """Rows for the detail-page hint: one per (kind, owner group) with hidden data.

    Each row: ``kind``, ``kind_label`` (pluralised), ``group`` (owner), ``count``,
    ``request`` (this user's latest request for that kind / group, or None) and
    ``can_request`` (the user has a non-Public group to be granted into).
    Nothing about the hidden rows themselves is exposed. One query per kind
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
    owner_names: Dict[int, str] = {}
    for kind in kinds:
        counts: Dict[int, int] = {}
        for owners in _owner_names_by_dataset(transient.id, kind).values():
            if set(owners.values()) & names:
                continue
            for group_id, name in owners.items():
                if name == PUBLIC_COLLABORATION_GROUP_NAME:
                    continue
                counts[group_id] = counts.get(group_id, 0) + 1
                owner_names[group_id] = name
        per_group[kind] = counts
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
                "request": latest.get((kind, group.pk)),
                "can_request": can_request,
            })
    return rows


def deciders_for(owner_group: Group) -> List[User]:
    return list(owner_group.user_set.filter(is_active=True).order_by("id"))


def user_can_decide(user: User, request: DataAccessRequest) -> bool:
    if not user.is_authenticated:
        return False
    if _is_staff(user):
        return True
    return user.groups.filter(pk=request.owner_group_id).exists()


def requests_to_decide(user: User, *, pending_only: bool = True):
    """Requests addressed to groups the user belongs to (staff: every request)."""
    qs = DataAccessRequest.objects.select_related("requester", "transient", "owner_group", "target_group", "decided_by")
    if not _is_staff(user):
        qs = qs.filter(owner_group__in=user.groups.all())
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


def invalidate_pending_counts(owner_group: Group) -> None:
    """Drop the cached badge counts of everyone who may decide requests to ``owner_group``."""
    from django.core.cache import cache

    ids = list(owner_group.user_set.values_list("id", flat=True))
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
    elif target_group.pk not in {g.pk for g in choices}:
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
    invalidate_pending_counts(owner_group)
    if notify_owners:
        notify_new_request(request)
    return request


def notify_new_request(request: DataAccessRequest):
    owners = deciders_for(request.owner_group)
    if not owners:
        return []
    n = len(hidden_datasets(request.requester, request.transient_id, request.dataset_kind))
    text = "%s asks %s for access to %d restricted %s on %s (to be shared with %s).%s\nAccept or decline: %s" % (
        request.requester.username, request.owner_group.name, n, KIND_LABELS[request.dataset_kind][1],
        request.transient.name, request.target_group.name,
        ("\n\"%s\"" % request.message) if request.message else "",
        notify_service.absolute_url(_request_url(request)),
    )
    return notify_service.notify(
        owners, text, _request_url(request), "data_access",
        subject="Data access request on %s" % request.transient.name, transient=request.transient,
        payload={"request_id": request.pk, "action": "new"}, created_by=request.requester,
        exclude=[request.requester_id],
    )


def _grant(request: DataAccessRequest) -> List[int]:
    """Add the target group to the owner group's restricted datasets of the request's kind; return their ids."""
    qs = restricted_datasets(request.transient_id, request.dataset_kind).filter(groups=request.owner_group)
    if request.dataset_id is not None:
        qs = qs.filter(pk=request.dataset_id)
    granted = []
    for row in qs.distinct():
        if not row.groups.filter(pk=request.target_group_id).exists():
            row.groups.add(request.target_group)
        granted.append(row.pk)
    return granted


@transaction.atomic
def decide(request: DataAccessRequest, user: User, accept: bool, *, note: str = "", notify_requester: bool = True):
    """Accept (grant the target group) or decline a pending request."""
    if not user_can_decide(user, request):
        raise PermissionDenied({"message": "Only members of %s (or staff) may decide this request." % request.owner_group.name})
    if not request.is_pending:
        raise ValidationError({"status": "This request was already %s." % request.status})
    request.decided_by = user
    request.decided_at = timezone.now()
    request.note = (note or "").strip()
    request.modified_by = user
    if accept:
        request.granted_dataset_ids = _grant(request)
        request.status = DataAccessRequest.STATUS_ACCEPTED
    else:
        request.status = DataAccessRequest.STATUS_DECLINED
    request.save()
    invalidate_pending_counts(request.owner_group)
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
