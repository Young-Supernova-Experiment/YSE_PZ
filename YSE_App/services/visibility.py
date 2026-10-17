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
    if user.is_staff or user.is_superuser:
        return "staff"
    names = sorted(user_group_names(user))
    if not names:
        return "nogroups"
    digest = hashlib.sha256("|".join(names).encode()).hexdigest()[:12]
    return f"g{digest}"


# Backward-compatible alias (deprecated name).
user_plot_cache_token = group_access_plot_cache_token


def get_user_group_query(user: User, names: Optional[Iterable[str]] = None):
    """Same semantics as PhotometryService: ungrouped OR intersects user groups.

    ``names`` is the user's group names when the caller already has them, so
    one membership query serves several group filters.
    """
    names = user_group_names(user) if names is None else list(names)
    no_group = Q(groups__isnull=True)
    contains_group = Q(groups__name__in=names)
    return no_group, contains_group


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


def transient_access_q(user: User, names: Optional[Iterable[str]] = None) -> Q:
    """Condition on ``Transient`` rows: the user may see some of its photometry or spectra.

    The set-based form of :func:`user_can_view_transient` for an authenticated,
    non-staff user: a photometry set or spectrum on the transient that has no
    collaboration groups, or shares one with the user (the
    ``GetAuthorizedTransientPhotometry_ByUser_ByTransient`` and
    ``GetAuthorizedTransientSpectrum_ByUser_ByTransient`` rule, flagged spectra
    included). A transient with neither is not visible. Two correlated EXISTS
    subqueries; the group names are read once (#451).
    """
    from YSE_App.models import TransientPhotometry, TransientSpectrum

    no_group, contains_group = get_user_group_query(user, names=names)
    phot_q = no_group | contains_group
    extra = _extra_photometry_grants_q(user)
    if extra is not None:  # read-time grants (#648)
        phot_q |= extra
    visible_phot = TransientPhotometry.objects.filter(
        phot_q, transient_id=OuterRef("pk"),
    )
    visible_spec = TransientSpectrum.objects.filter(
        no_group | contains_group, transient_id=OuterRef("pk"),
    )
    return Q(Exists(visible_phot)) | Q(Exists(visible_spec))


def filter_transients_by_user_access(
    user: User, transients: Union[QuerySet, Iterable],
):
    """Post-filter transients (e.g. after SQL Explorer) by photometry/spectrum access.

    Same rows as calling :func:`user_can_view_transient` per transient, in a
    fixed number of queries: a QuerySet gets the :func:`transient_access_q`
    condition (no query until it is evaluated) and an iterable of transients
    is checked with one query for all of them. Staff and superusers see
    everything; anonymous users see nothing.
    """
    if user.is_staff or user.is_superuser:
        return transients
    if isinstance(transients, QuerySet):
        if not user.is_authenticated:
            return transients.none()
        return transients.filter(transient_access_q(user))
    transients = list(transients)
    if not user.is_authenticated or not transients:
        return []
    from YSE_App.models import Transient

    visible = set(
        Transient.objects.filter(pk__in={t.id for t in transients})
        .filter(transient_access_q(user))
        .values_list("pk", flat=True)
    )
    return [t for t in transients if t.id in visible]


# ----------------------------------------------------------------- listing (#607)
#
# Ryan's rule (issue #607, 2026-10-11): an object with a TNS name is public
# and is always listed; any other object is listed only when the user may see
# some of its data (``transient_access_q``); staff see everything. Basic
# information (name, position, status) is shown for every listed object, and
# photometry, spectra and every value derived from them keep their group
# restrictions (``services.photstat_visibility``).
#
# Read-time grants (#648) plug in at ``_extra_access_grants_q`` (objects) and
# ``_extra_photometry_grants_q`` (photometry sets): both return ``None`` today,
# so nothing changes until #648 fills them in; the listing, data-access and
# hidden-photometry conditions below already consult them.

TNS_NAME_SQL_REGEX = r'^20[0-9]{2}[a-zA-Z]{1,5}$'


def user_group_ids(user: User) -> List[int]:
    if not getattr(user, 'is_authenticated', False):
        return []
    return list(user.groups.values_list('id', flat=True))


def user_sees_everything(user) -> bool:
    """Staff and superusers bypass collaboration-group restrictions."""
    return bool(user is not None and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)))


def _extra_access_grants_q(user: User) -> Optional[Q]:
    """Extra ``Transient`` rows ``user`` may see through a per-user grant (#648); ``None`` today."""
    return None


def _extra_photometry_grants_q(user: User) -> Optional[Q]:
    """Extra ``TransientPhotometry`` sets ``user`` may see through a per-user grant (#648); ``None`` today."""
    return None


def tns_named_q() -> Q:
    """``Transient`` rows with a TNS designation as their name or as one of their aliases."""
    from YSE_App.models import AlternateTransientNames

    alias = AlternateTransientNames.objects.filter(
        transient_id=OuterRef('pk'), name__regex=TNS_NAME_SQL_REGEX,
    )
    return Q(name__regex=TNS_NAME_SQL_REGEX) | Q(Exists(alias))


def listed_transient_q(user: User, names: Optional[Iterable[str]] = None) -> Q:
    """Condition on ``Transient`` rows: the object is listed for ``user`` (authenticated, non-staff)."""
    q = tns_named_q() | transient_access_q(user, names=names)
    extra = _extra_access_grants_q(user)
    if extra is not None:
        q |= extra
    return q


def filter_listed_transients(user: User, transients: Union[QuerySet, Iterable]):
    """The objects ``user`` may see listed: every list, table, API and export (#607).

    Same calling convention as :func:`filter_transients_by_user_access`: a
    QuerySet stays lazy, an iterable is checked with one query. Staff and
    superusers see everything; anonymous users see nothing (machine clients
    authenticate with a token or Basic auth and are users like any other).
    Use :func:`filter_transients_by_user_access` instead for surfaces that
    show the object's *data* (comments, annotations, interests, runs).
    """
    if user_sees_everything(user):
        return transients
    authenticated = getattr(user, 'is_authenticated', False)
    if isinstance(transients, QuerySet):
        if not authenticated:
            return transients.none()
        return transients.filter(listed_transient_q(user))
    transients = list(transients)
    if not authenticated or not transients:
        return []
    from YSE_App.models import Transient

    listed = set(
        Transient.objects.filter(pk__in={t.id for t in transients})
        .filter(listed_transient_q(user))
        .values_list('pk', flat=True)
    )
    return [t for t in transients if t.id in listed]


def user_can_list_transient(user: User, transient_id: int) -> bool:
    if user_sees_everything(user):
        return True
    if not getattr(user, 'is_authenticated', False):
        return False
    from YSE_App.models import Transient

    return Transient.objects.filter(pk=transient_id).filter(listed_transient_q(user)).exists()


def hidden_photometry_exists(user: User, transient_ref: str = 'pk'):
    """``Exists``: the transient at ``OuterRef(transient_ref)`` has a photometry set ``user`` may not see.

    A set is restricted when it has at least one collaboration group, and
    visible when one of them is the user's (the ``PhotometryService`` rule).
    Callers skip it for staff, who never have hidden photometry. Without a
    hidden set the stored all-data ``TransientPhotStat`` row is exactly what
    the user may see.
    """
    from YSE_App.models import TransientPhotometry

    through = TransientPhotometry.groups.through
    restricted = TransientPhotometry.objects.filter(transient_id=OuterRef(transient_ref)).filter(
        Exists(through.objects.filter(transientphotometry_id=OuterRef('pk')))
    )
    gids = user_group_ids(user)
    if gids:
        restricted = restricted.filter(~Exists(
            through.objects.filter(transientphotometry_id=OuterRef('pk'), group_id__in=gids)
        ))
    extra = _extra_photometry_grants_q(user)
    if extra is not None:
        restricted = restricted.exclude(extra)
    return Exists(restricted)


def visible_photometry_ids_q(user: User, prefix: str = 'photometry_id') -> Optional[Q]:
    """Condition on photometry *points*: their set is one ``user`` may see; ``None`` for staff.

    ``prefix`` names the column holding the ``TransientPhotometry`` id
    (``photometry_id`` on ``TransientPhotData``). One ``IN`` subquery, so
    rows are never duplicated by the group join.
    """
    if user_sees_everything(user):
        return None
    from YSE_App.models import TransientPhotometry

    no_group, contains_group = get_user_group_query(user)
    phot_q = no_group | contains_group
    extra = _extra_photometry_grants_q(user)
    if extra is not None:
        phot_q |= extra
    return Q(**{prefix + '__in': TransientPhotometry.objects.filter(phot_q).values('pk')})


def transients_with_hidden_photometry(user: User, transient_ids: Iterable[int]) -> Set[int]:
    """The ids among ``transient_ids`` with photometry ``user`` may not see (one query; staff: none)."""
    ids = {int(t) for t in transient_ids if t is not None}
    if not ids or user_sees_everything(user):
        return set()
    from YSE_App.models import Transient

    return set(
        Transient.objects.filter(pk__in=ids).filter(hidden_photometry_exists(user))
        .values_list('pk', flat=True)
    )


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
