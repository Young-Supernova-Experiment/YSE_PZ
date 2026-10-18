"""Shared group-based visibility helpers for YSE data and comments."""

from __future__ import annotations

from enum import Enum
from typing import Iterable, List, NamedTuple, Optional, Set, Union

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
    if user_sees_everything(user):
        return "service"  # service readers (#706); staff share plots by group membership like everyone
    from YSE_App.services import access_rules

    # Read-time rules (#648): the rules version, and a per-user part for personal rules
    # or left groups, so cached plots never cross users with different access. Empty
    # with DATA_ACCESS_GRANTS_ENABLED off.
    suffix = access_rules.cache_token_suffix(user)
    names = sorted(user_group_names(user))
    if not names:
        return "nogroups" + suffix
    digest = hashlib.sha256("|".join(names).encode()).hexdigest()[:12]
    return f"g{digest}" + suffix


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
    """Object with optional ``groups`` M2M (photometry, spectrum, resource, log).

    Transient photometry sets and spectra are decided by
    :func:`dataset_visible_to_user` (the central read rule, #648).
    """
    if not user.is_authenticated:
        return False
    if _dataset_model(obj) is not None:
        # Data: staff follow the rule like everyone (#706); only service readers see everything.
        return user_sees_everything(user) or dataset_visible_to_user(user, obj)
    if user.is_staff or user.is_superuser:
        return True  # observing resources and other administrative objects
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
    """True if user may access this transient's collaboration surfaces (comments, interests, etc.).

    Staff keep these (moderation and coordination are administrative, #706); the transient's
    *data* follow :func:`photometry_visible_q` / :func:`spectrum_visible_q` for staff too.
    """
    if not user.is_authenticated:
        return False
    if _collaboration_admin(user):
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

    names = user_group_names(user) if names is None else list(names)
    visible_phot = TransientPhotometry.objects.filter(
        photometry_visible_q(user, names=names), transient_id=OuterRef("pk"),
    )
    visible_spec = TransientSpectrum.objects.filter(
        spectrum_visible_q(user, names=names), transient_id=OuterRef("pk"),
    )
    q = Q(Exists(visible_phot)) | Q(Exists(visible_spec))
    points = photometry_points_visible_q(user)
    if points is not None:
        # Per-point rules (#648): a transient with any visible point, also in a partly visible set.
        from YSE_App.models import TransientPhotData

        q |= Q(Exists(TransientPhotData.objects.filter(points, photometry__transient_id=OuterRef("pk"))))
    return q


def filter_transients_by_user_access(
    user: User, transients: Union[QuerySet, Iterable],
):
    """Post-filter transients by photometry/spectrum access, for collaboration surfaces.

    Same rows as calling :func:`user_can_view_transient` per transient, in a
    fixed number of queries: a QuerySet gets the :func:`transient_access_q`
    condition (no query until it is evaluated) and an iterable of transients
    is checked with one query for all of them. Staff and service readers see
    everything here (comments, interests, favorites, annotations; #706 keeps
    these administrative); anonymous users see nothing. Surfaces that show
    data derived from photometry use :func:`filter_transients_by_data_access`.
    """
    if _collaboration_admin(user):
        return transients
    return _filter_by_access(user, transients)


def filter_transients_by_data_access(user: User, transients: Union[QuerySet, Iterable]):
    """:func:`filter_transients_by_user_access` without the staff shortcut (#706): for data-derived surfaces."""
    if user_sees_everything(user):
        return transients
    return _filter_by_access(user, transients)


def _filter_by_access(user: User, transients: Union[QuerySet, Iterable]):
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
# some of its data (``transient_access_q``); service readers see everything (staff do
# not, #706). Basic
# information (name, position, status) is shown for every listed object, and
# photometry, spectra and every value derived from them keep their group
# restrictions (``services.photstat_visibility``).
#
# Read-time rules (#648) plug in at ``_photometry_access_rules`` /
# ``_spectrum_access_rules`` (datasets, read through ``photometry_visible_q``
# / ``spectrum_visible_q``) and ``photometry_points_visible_q`` (points); with
# ``DATA_ACCESS_GRANTS_ENABLED`` off they change nothing.
# ``_extra_access_grants_q`` (objects) stays ``None``.

TNS_NAME_SQL_REGEX = r'^20[0-9]{2}[a-zA-Z]{1,5}$'


def user_group_ids(user: User) -> List[int]:
    if not getattr(user, 'is_authenticated', False):
        return []
    return list(user.groups.values_list('id', flat=True))


def _collaboration_admin(user) -> bool:
    """Staff, superusers and service readers: they see every transient's collaboration surfaces (not its data)."""
    return bool(user is not None and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)
                                      or user_sees_everything(user)))


def is_service_reader(user) -> bool:
    """A cron or script account that reads all data (#706): active, authenticated, and named in
    ``settings.DATA_SERVICE_READER_USERNAMES`` (the ``[main] dblogin`` ingest account plus
    ``[site_settings] DATA_SERVICE_READERS``). No query."""
    if user is None or not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return False
    from django.conf import settings

    readers = set(getattr(settings, 'DATA_SERVICE_READER_USERNAMES', ()) or ())
    readers |= set(getattr(settings, 'API_SERVICE_USERNAMES', ()) or ())  # the cron account (#524)
    return getattr(user, 'username', None) in readers


def user_sees_everything(user) -> bool:
    """Whether ``user`` bypasses data restrictions: only service readers (#706).

    Staff and superusers see only the data they have access to, like everyone
    else (Ryan, 2026-10-17); they keep their administrative actions, which are
    checked separately (``is_staff`` / ``data_sharing.share_role`` staff override).
    """
    return is_service_reader(user)


def _extra_access_grants_q(user: User) -> Optional[Q]:
    """Extra ``Transient`` rows ``user`` may see through a per-user grant (#648); ``None`` today."""
    return None


def _extra_photometry_grants_q(user: User) -> Optional[Q]:
    """Extra ``TransientPhotometry`` sets ``user`` may see (#648): the ``allow`` of
    :func:`_photometry_access_rules`, kept for older callers."""
    return _photometry_access_rules(user).allow


# ------------------------------------------- central dataset read rule (#648)
#
# Every read of transient photometry sets / points and transient spectra
# decides visibility with ``photometry_visible_q`` / ``spectrum_visible_q``
# (or a helper below built on them): the data services, the listing and
# data-access conditions, plots, tables, exports and the API. The rule today
# is the collaboration-group rule: a dataset with no groups, or one sharing a
# group (by name) with the user. Read-time grants and restrictions (#648)
# plug in at ``_photometry_access_rules`` / ``_spectrum_access_rules``
# (``services.access_rules``); both return ``NO_RULES`` with the flag off or no
# applicable rule, so the conditions (and their SQL) are exactly the group rule.
# These functions never bypass anyone: callers that let service readers see
# everything check ``user_sees_everything`` first.


class AccessRules(NamedTuple):
    """Read-time rules for one user on one dataset model (#648).

    Both are conditions on the dataset model (``TransientPhotometry`` or
    ``TransientSpectrum``), best written as ``pk__in`` / ``<fk>_id__in``
    lookups so they never join through ``groups``:

    * ``allow``: rows the user may also see (OR'd onto the group rule);
    * ``deny``: rows the user may not see even when the group rule or
      ``allow`` shows them. A deny never hides public data
      (:func:`_public_dataset_q`).
    """

    allow: Optional[Q] = None
    deny: Optional[Q] = None

    @property
    def empty(self) -> bool:
        return self.allow is None and self.deny is None


NO_RULES = AccessRules()


def _photometry_access_rules(user: User) -> AccessRules:
    """Per-point rules on ``TransientPhotometry`` sets for ``user`` (#648, ``services.access_rules``).

    ``NO_RULES`` with ``DATA_ACCESS_GRANTS_ENABLED`` off, for service readers, anonymous
    users and users to whom no rule applies. At set level a set is visible
    only when every one of its points is.
    """
    from YSE_App.services import access_rules

    return access_rules.photometry_rules(user)


def _spectrum_access_rules(user: User) -> AccessRules:
    """Per-spectrum rules on ``TransientSpectrum`` rows for ``user`` (#648, ``services.access_rules``)."""
    from YSE_App.services import access_rules

    return access_rules.spectrum_rules(user)


def photometry_points_visible_q(user: User) -> Optional[Q]:
    """Condition on ``TransientPhotData`` for a user with per-point rules (#648), else ``None``.

    ``None`` means "use the set-level rule" (no rule applies, the flag is off,
    service reader or anonymous); then point reads keep their old SQL exactly.
    """
    from YSE_App.services import access_rules

    summary = access_rules.active_summary(user)
    if summary is None:
        return None
    return access_rules.point_visible_q(summary)


def _public_dataset_q(model, names: Iterable[str]) -> Q:
    """Rows of ``model`` that are public for this user, so no deny may hide them.

    Public data (Ryan, #648, 2026-10-17) can never be restricted from anyone:
    a dataset with no collaboration group, and, for a member of the ``Public``
    group, a dataset whose only group is ``Public``. Both are rows the group
    rule already shows the user, so this only limits what a deny removes.
    """
    through = model.groups.through
    links = through.objects.filter(**{model._meta.model_name + '_id': OuterRef('pk')})
    if PUBLIC_COLLABORATION_GROUP_NAME in set(names):
        links = links.exclude(group__name=PUBLIC_COLLABORATION_GROUP_NAME)
    return Q(~Exists(links))


def _dataset_visible_q(model, rules: AccessRules, user: User, names: Optional[Iterable[str]]) -> Q:
    names = user_group_names(user) if names is None else list(names)
    no_group, contains_group = get_user_group_query(user, names=names)
    q = no_group | contains_group
    if rules.allow is not None:
        q |= rules.allow
    if rules.deny is not None:
        q = _public_dataset_q(model, names) | (q & ~rules.deny)
    return q


def photometry_visible_q(user: User, names: Optional[Iterable[str]] = None) -> Q:
    """Condition on ``TransientPhotometry`` rows: the sets ``user`` may see (the central rule).

    ``names`` is the user's group names when the caller already has them.
    Joins ``groups``, so querysets filtered with it need ``.distinct()`` (or
    an ``IN`` / ``EXISTS`` wrapper) as before.
    """
    from YSE_App.models import TransientPhotometry

    return _dataset_visible_q(TransientPhotometry, _photometry_access_rules(user), user, names)


def spectrum_visible_q(user: User, names: Optional[Iterable[str]] = None) -> Q:
    """Condition on ``TransientSpectrum`` rows: the spectra ``user`` may see (the central rule)."""
    from YSE_App.models import TransientSpectrum

    return _dataset_visible_q(TransientSpectrum, _spectrum_access_rules(user), user, names)


def _dataset_model(obj):
    """``TransientPhotometry`` / ``TransientSpectrum`` for such an instance, else ``None``."""
    from YSE_App.models import TransientPhotometry, TransientSpectrum

    for model in (TransientPhotometry, TransientSpectrum):
        if isinstance(obj, model):
            return model
    return None


def _access_rules_for(model, user: User) -> AccessRules:
    from YSE_App.models import TransientPhotometry

    if model is TransientPhotometry:
        return _photometry_access_rules(user)
    return _spectrum_access_rules(user)


def dataset_visible_to_user(user: User, obj, names: Optional[Iterable[str]] = None) -> bool:
    """Whether the central rule shows one photometry set or spectrum to ``user`` (no staff bypass).

    Without rules for the user this is the group rule in Python (the queries
    of the old ``object_visible_to_user`` check; none when ``names`` is given
    and ``groups`` is prefetched). With rules it asks the database with
    :func:`photometry_visible_q` / :func:`spectrum_visible_q`.
    """
    model = _dataset_model(obj)
    rules = _access_rules_for(model, user)
    if not rules.empty:
        return model.objects.filter(pk=obj.pk).filter(
            _dataset_visible_q(model, rules, user, names)).exists()
    if names is None:
        if not object_has_groups(obj):
            return True
        names = user_group_names(user)
        obj_names = set(obj.groups.values_list("name", flat=True))
    else:
        obj_names = {g.name for g in obj.groups.all()}
    return not obj_names or bool(set(names) & obj_names)


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
    QuerySet stays lazy, an iterable is checked with one query. Service
    readers see everything; staff and superusers are listed what anyone with
    their groups is (#706); anonymous users see nothing (machine clients
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
    """Condition: the transient at ``OuterRef(transient_ref)`` has photometry ``user`` may not see.

    Without access rules: a set is restricted when it has at least one
    collaboration group, and visible when one of them is the user's (the
    ``PhotometryService`` rule). With rules: any point hidden from the user
    (access is per point, #714). Callers skip it for service readers. Without a
    hidden set the stored all-data ``TransientPhotStat`` row is exactly what
    the user may see.
    """
    from YSE_App.models import TransientPhotometry

    on_transient = TransientPhotometry.objects.filter(transient_id=OuterRef(transient_ref))
    points = photometry_points_visible_q(user)
    if points is not None:
        # Per point (#648, #714): a point the user may not see, or a header (with no points) they may not see.
        from YSE_App.models import TransientPhotData

        visible = TransientPhotometry.objects.filter(photometry_visible_q(user)).values('pk')
        hidden_point = TransientPhotData.objects.filter(photometry__transient_id=OuterRef(transient_ref)).exclude(
            pk__in=TransientPhotData.objects.filter(points).values('pk'))
        return Q(Exists(hidden_point)) | Q(Exists(on_transient.exclude(pk__in=visible)))
    # No rules: the group rule's complement by group id, the indexed form #607 tuned.
    through = TransientPhotometry.groups.through
    restricted = on_transient.filter(
        Exists(through.objects.filter(transientphotometry_id=OuterRef('pk')))
    )
    gids = user_group_ids(user)
    if gids:
        restricted = restricted.filter(~Exists(
            through.objects.filter(transientphotometry_id=OuterRef('pk'), group_id__in=gids)
        ))
    return Exists(restricted)


def visible_photometry_ids_q(user: User, prefix: str = 'photometry_id') -> Optional[Q]:
    """Condition on photometry *points*: their set is one ``user`` may see; ``None`` for staff.

    ``prefix`` names the column holding the ``TransientPhotometry`` id
    (``photometry_id`` on ``TransientPhotData``). One ``IN`` subquery, so
    rows are never duplicated by the group join.
    """
    if user_sees_everything(user):
        return None
    from YSE_App.models import TransientPhotData, TransientPhotometry

    points = photometry_points_visible_q(user)
    if points is not None and (prefix == 'photometry_id' or prefix.endswith('__photometry_id')):
        # Per-point rules (#648): the exact points, also in a partly visible set.
        point_path = prefix[:-len('photometry_id')] + 'pk'
        return Q(**{point_path + '__in': TransientPhotData.objects.filter(points).values('pk')})
    visible = TransientPhotometry.objects.filter(photometry_visible_q(user)).values('pk')
    return Q(**{prefix + '__in': visible})


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
