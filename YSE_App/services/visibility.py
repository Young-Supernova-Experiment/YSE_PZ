"""Shared group-based visibility helpers for YSE data and comments."""

from __future__ import annotations

from enum import Enum
from typing import Iterable, List, Optional, Set, Union

from django.contrib.auth.models import Group, User
from django.db.models import Count, Exists, OuterRef, Q, QuerySet

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME


class ResourceVisibilityPolicy(str, Enum):
    """Default visibility when creating new rows (empty M2M = public on existing data)."""

    PUBLIC = "public"
    PRIVATE = "private"


def user_group_names(user: User) -> List[str]:
    if not user.is_authenticated:
        return []
    return list(user.groups.values_list("name", flat=True))


def group_access_plot_cache_token(user: User) -> str:
    """Cache suffix from collaboration-group membership (not user id).

    Users who share the same set of ``auth.Group`` names receive the same token,
    so plot HTML is reused across users with identical data access.
    """
    import hashlib

    if not user.is_authenticated:
        return "anon"
    # Staff see data by group like everyone else (``dataset_group_q`` has no
    # staff exemption), so their token carries their groups too.
    prefix = "staff" if (user.is_staff or user.is_superuser) else ""
    if is_service_reader(user):
        prefix += "svc"
    names = sorted(user_group_names(user))
    if not names:
        return f"{prefix}nogroups"
    digest = hashlib.sha256("|".join(names).encode()).hexdigest()[:12]
    return f"{prefix}g{digest}"


# Backward-compatible alias (deprecated name).
user_plot_cache_token = group_access_plot_cache_token


def get_user_group_query(user: User):
    """Same semantics as PhotometryService: ungrouped OR intersects user groups."""
    names = user_group_names(user)
    no_group = Q(groups__isnull=True)
    contains_group = Q(groups__name__in=names)
    return no_group, contains_group


# Data (photometry sets and spectra, transient and host) use a stricter group
# rule than observing resources (``get_user_group_query``):
#
# * ``Public`` shows a dataset only when it is the dataset's only group. A set
#   that also carries a team group (for example TNS points merged into the YSE
#   set of the same instrument and observation group) stays with that team;
#   with the plain rule every member of ``Public``, i.e. every user, saw it.
# * A dataset of the ``YSE`` observation group without a team group is YSE
#   data that was never labelled: only members of the ``YSE`` group see it.
YSE_COLLABORATION_GROUP_NAME = "YSE"
YSE_OBSERVATION_GROUP_NAME = "YSE"


def _open_dataset_q(model, names: Iterable[str]) -> Q:
    """Rows of ``model`` that are open data for a user with group ``names``.

    Open data has no group other than ``Public`` (``Public`` alone counts
    only for its members) and, for a user outside ``YSE``, is not unlabelled
    YSE data.
    """
    names = set(names)
    links = model.groups.through.objects.filter(**{model._meta.model_name + "_id": OuterRef("pk")})
    if PUBLIC_COLLABORATION_GROUP_NAME in names:
        links = links.exclude(group__name=PUBLIC_COLLABORATION_GROUP_NAME)
    q = Q(~Exists(links))
    if YSE_COLLABORATION_GROUP_NAME not in names:
        q &= ~Q(obs_group__name=YSE_OBSERVATION_GROUP_NAME)
    return q


def dataset_group_q(model, user: User, names: Optional[Iterable[str]] = None) -> Q:
    """The group rule for data rows of ``model``: open data, or a team group the user is in.

    Joins ``groups``, so querysets filtered with it need ``.distinct()``.
    """
    names = user_group_names(user) if names is None else list(names)
    team_names = [n for n in names if n != PUBLIC_COLLABORATION_GROUP_NAME]
    return _open_dataset_q(model, names) | Q(groups__name__in=team_names)


def is_service_reader(user) -> bool:
    """A cron or script account that reads all data: active, authenticated and named in
    ``settings.API_SERVICE_USERNAMES`` (the ``[main] dblogin`` ingest account). No query."""
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    from django.conf import settings

    return getattr(user, "username", None) in set(getattr(settings, "API_SERVICE_USERNAMES", ()) or ())


def hidden_photometry_exists(user: User, transient_ref: str = "pk") -> Q:
    """Condition: the transient at ``OuterRef(transient_ref)`` has photometry ``user`` may not see.

    Values derived from all of a transient's photometry (latest and peak
    magnitudes, ``TransientPhotStat``, the stored summary, fits) are shown to a
    viewer only when this is false; otherwise they are blank, never the
    all-data values. Service readers never have hidden photometry.
    """
    from YSE_App.models import TransientPhotometry

    on_transient = TransientPhotometry.objects.filter(transient_id=OuterRef(transient_ref))
    if is_service_reader(user):
        return Q(Exists(on_transient.none()))
    names = user_group_names(user) if getattr(user, "is_authenticated", False) else []
    visible = TransientPhotometry.objects.filter(dataset_group_q(TransientPhotometry, user, names=names)).values("pk")
    return Q(Exists(on_transient.exclude(pk__in=visible)))


def transients_with_hidden_photometry(user: User, transient_ids: Iterable[int]) -> Set[int]:
    """The ids among ``transient_ids`` with photometry ``user`` may not see (one query)."""
    ids = {int(t) for t in transient_ids if t is not None}
    if not ids or is_service_reader(user):
        return set()
    from YSE_App.models import Transient

    return set(
        Transient.objects.filter(pk__in=ids).filter(hidden_photometry_exists(user)).values_list("pk", flat=True)
    )


def user_has_hidden_photometry(user: User, transient_id: int) -> bool:
    return bool(transients_with_hidden_photometry(user, [transient_id]))


def object_has_groups(obj) -> bool:
    return obj.groups.exists()


def object_visible_to_user(user: User, obj) -> bool:
    """Object with optional ``groups`` M2M (photometry, spectrum, resource, log)."""
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    if not object_has_groups(obj):
        return True
    names = set(user_group_names(user))
    obj_names = set(obj.groups.values_list("name", flat=True))
    return bool(names.intersection(obj_names))


def transient_visible_group_names(transient_id: int) -> Set[str]:
    from YSE_App.models import TransientPhotometry, TransientSpectrum

    names: Set[str] = set()
    for qs in (
        TransientPhotometry.objects.filter(transient_id=transient_id),
        TransientSpectrum.objects.filter(transient_id=transient_id),
    ):
        for row in qs.prefetch_related("groups"):
            names.update(row.groups.values_list("name", flat=True))
    return names


def shared_groups_for_transient(user: User, transient_id: int) -> QuerySet[Group]:
    """Collaboration groups the user shares with restricted data on this transient."""
    on_transient = transient_visible_group_names(transient_id)
    if not on_transient:
        return user.groups.all()
    return user.groups.filter(name__in=on_transient)


def user_can_view_transient(user: User, transient_id: int) -> bool:
    """True if user may access this transient's collaboration surfaces (comments, etc.)."""
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    from YSE_App.data import PhotometryService, SpectraService

    if PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(
        user, transient_id
    ).exists():
        return True
    if SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(
        user, transient_id
    ).exists():
        return True
    return False


def default_comment_audience_groups(user: User, transient_id: int) -> List[Group]:
    """Private-by-default: groups user shares with transient's restricted data."""
    shared = list(shared_groups_for_transient(user, transient_id))
    if shared:
        return shared
    return list(user.groups.all())


def filter_transient_comments_for_user(queryset: QuerySet, user: User) -> QuerySet:
    """Filter transient-level comment logs for read access."""
    if user.is_staff or user.is_superuser:
        return queryset
    names = user_group_names(user)
    return queryset.annotate(
        _audience_group_count=Count("groups", distinct=True),
    ).filter(
        Q(is_public=True)
        | Q(groups__name__in=names)
        | Q(created_by=user, is_public=False, _audience_group_count=0)
    ).distinct()


def log_visible_to_user(user: User, log) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    if getattr(log, "is_public", True):
        return True
    if not object_has_groups(log):
        return log.created_by_id == user.id
    names = set(user_group_names(user))
    obj_names = set(log.groups.values_list("name", flat=True))
    return bool(names.intersection(obj_names))


def filter_transients_by_user_access(
    user: User, transients: Union[QuerySet, Iterable],
):
    """Post-filter transients (e.g. after SQL Explorer) by photometry/spectrum access."""
    if user.is_staff or user.is_superuser:
        return transients
    from YSE_App.models import Transient

    if isinstance(transients, QuerySet):
        ids = list(transients.values_list("id", flat=True))
        allowed = [
            pk
            for pk in ids
            if user_can_view_transient(user, pk)
        ]
        return transients.filter(id__in=allowed)
    return [t for t in transients if user_can_view_transient(user, t.id)]


# Transient detail shell allows broad metadata; export zip headers must stay narrow.
EXPORT_TRANSIENT_HEADER_ALLOWLIST = frozenset(
    {
        "name",
        "ra",
        "dec",
        "disc_date",
        "slug",
        "status",
        "obs_group",
        "mw_ebv",
        "redshift",
        "redshift_err",
        "redshift_source",
        "non_detect_limit",
        "non_detect_instrument",
        "non_detect_band",
        "non_detect_date",
        "TNS_name",
        "TNS_spec_class",
    }
)


def redact_transient_export_header_fields(fields: dict) -> dict:
    """Keep public-facing metadata keys only in bulk photometry export headers."""
    return {k: v for k, v in fields.items() if k in EXPORT_TRANSIENT_HEADER_ALLOWLIST}


def _user_in_public_group(user: User) -> bool:
    return PUBLIC_COLLABORATION_GROUP_NAME in user_group_names(user)


def _linked_followup_resource(followup):
    for attr in ("classical_resource", "too_resource", "queued_resource"):
        resource = getattr(followup, attr, None)
        if resource is not None:
            return resource
    return None


def user_can_see_linked_followup_resource(user: User, followup) -> bool:
    """Whether ``user`` may view the observing resource linked to a follow-up."""
    resource = _linked_followup_resource(followup)
    if resource is None:
        return True
    return object_visible_to_user(user, resource)


def _observing_resource_ids_visible_to_user(model, user: User) -> Set[int]:
    names = user_group_names(user)
    public_ids = set(
        model.objects.annotate(_resource_group_count=Count("groups"))
        .filter(_resource_group_count=0)
        .values_list("pk", flat=True)
    )
    grouped_ids = set(
        model.objects.filter(groups__name__in=names).values_list("pk", flat=True)
    )
    return public_ids | grouped_ids


def _linked_resource_visible_q(user: User) -> Q:
    """Q matching follow-ups whose linked observing resource is visible to ``user``."""
    from YSE_App.models import ClassicalResource, QueuedResource, ToOResource

    classical_ids = _observing_resource_ids_visible_to_user(ClassicalResource, user)
    too_ids = _observing_resource_ids_visible_to_user(ToOResource, user)
    queued_ids = _observing_resource_ids_visible_to_user(QueuedResource, user)
    q = Q(
        classical_resource__isnull=True,
        too_resource__isnull=True,
        queued_resource__isnull=True,
    )
    if classical_ids:
        q |= Q(classical_resource__in=classical_ids)
    if too_ids:
        q |= Q(too_resource__in=too_ids)
    if queued_ids:
        q |= Q(queued_resource__in=queued_ids)
    return q


def filter_transient_followups_for_user(queryset: QuerySet, user: User) -> QuerySet:
    """Filter follow-up rows for read access (default public for legacy rows)."""
    if user.is_staff or user.is_superuser:
        return queryset
    from YSE_App.data import PhotometryService, SpectraService
    from YSE_App.models import TransientFollowup

    names = user_group_names(user)
    phot_transients = PhotometryService.GetAuthorizedTransientPhotometry_ByUser(
        user
    ).values_list("transient_id", flat=True)
    spec_transients = SpectraService.GetAuthorizedTransientSpectrum_ByUser(
        user
    ).values_list("transient_id", flat=True)
    scoped = queryset.filter(
        Q(transient_id__in=phot_transients) | Q(transient_id__in=spec_transients),
    ).filter(_linked_resource_visible_q(user))
    followup_group_link = TransientFollowup.groups.through.objects.filter(
        transientfollowup_id=OuterRef("pk"),
    )
    by_audience = scoped.filter(
        Exists(followup_group_link),
        groups__name__in=names,
    )
    creator_only = scoped.filter(
        ~Exists(followup_group_link),
        is_public=False,
        requested_by=user,
    )
    child_requestor = scoped.filter(requests__requestor=user)
    # Legacy ``is_public=True`` follow-ups with no explicit groups. Kept as a
    # filter on the scoped queryset: materialising the matching pks first
    # put every public follow-up in the database into one ``pk IN (...)``
    # list, so the fragment's SQL grew with the table instead of with the
    # transient's own follow-ups.
    legacy = scoped.filter(~Exists(followup_group_link), is_public=True)
    if not _user_in_public_group(user):
        legacy = legacy.filter(requested_by=user)
    return (by_audience | creator_only | child_requestor | legacy).distinct()


def followup_visible_to_user(user: User, followup) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    transient_id = getattr(followup, "transient_id", None)
    if transient_id is not None and not user_can_view_transient(user, transient_id):
        return False
    if followup.requests.filter(requestor=user).exists():
        return True
    if not user_can_see_linked_followup_resource(user, followup):
        return False
    if object_has_groups(followup):
        names = set(user_group_names(user))
        obj_names = set(followup.groups.values_list("name", flat=True))
        return bool(names.intersection(obj_names))
    if getattr(followup, "is_public", False):
        if followup.requested_by_id == user.id:
            return True
        return _user_in_public_group(user)
    if followup.requested_by_id == user.id:
        return True
    return False
