"""Write-authorization helpers for the REST API, the upload endpoints and SQL Explorer.

Every check here that could change who may write is behind a settings flag (see
``YSE_PZ/settings.py``, "Authorization hardening", and ``docs/ziggy-runbook.md``).
``UPLOAD_GROUP_CHECKS_ENFORCED`` defaults to on (#697); the other two default to off:

- ``API_WRITE_PERMISSIONS_ENFORCED``: unsafe methods on the legacy REST viewsets
  need staff, superuser or the Django model permission (granted to the
  ``API_WRITERS_GROUP`` group by migration 0032).
- ``UPLOAD_GROUP_CHECKS_ENFORCED``: the upload endpoints (``add_transient``,
  ``add_transient_phot``, ``add_transient_spec``, ``upload_spectrum``) need a
  trusted uploader, or a collaboration-group member who shares only with their
  own groups and replaces only rows they created. On by default; turn off with
  ``UPLOAD_GROUP_CHECKS_ENFORCED: False`` in ``[site_settings]`` or the env var ``=0``.
- ``EXPLORER_RESTRICTED_ACCESS``: SQL Explorer is limited to superusers and the
  ``EXPLORER_ACCESS_GROUP`` group instead of all staff.
"""

from __future__ import annotations

from typing import Iterable, Optional

from django.conf import settings
from rest_framework import permissions

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME

# Legacy ``ListCreateRetrieveUpdateViewSet`` subclasses that keep their own
# object-level checks, so the model-permission check is not added to them.
WRITE_PERMISSION_EXEMPT_VIEWSETS = frozenset({
    "AllocationViewSet",          # staff-only writes in perform_create/update
    "TransientInterestViewSet",   # owner-scoped
    "ProfileViewSet",             # self-service, queryset scoped to the user
    "LogViewSet",                 # owner-or-staff update (see api_views)
    "UserQueryViewSet",           # scoped to request.user
    "UserTelescopeToFollowViewSet",  # scoped to request.user
})

# Viewsets the web UI updates for every signed-in member (status and tag
# buttons, follow-up status, survey scheduling pages). PUT/PATCH stays open to
# authenticated users there; POST still needs the model permission.
MEMBER_UPDATE_VIEWSETS = frozenset({
    "TransientViewSet",
    "TransientFollowupViewSet",
    "SurveyObservationViewSet",
    "SurveyFieldMSBViewSet",
})

# Viewsets where the web UI creates rows for every signed-in member
# (the "new tag" button on the transient page).
MEMBER_CREATE_VIEWSETS = frozenset({
    "TransientTagViewSet",
})


def _flag(name: str) -> bool:
    return bool(getattr(settings, name, False))


def api_writers_group_name() -> str:
    return getattr(settings, "API_WRITERS_GROUP", "api_writers")


def user_in_group(user, name: str) -> bool:
    if not name or user is None or not getattr(user, "is_authenticated", False):
        return False
    return user.groups.filter(name=name).exists()


def is_trusted_writer(user) -> bool:
    """Superusers, staff and members of the configured service group."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user_in_group(user, api_writers_group_name())


class LegacyApiWritePermission(permissions.BasePermission):
    """Model permissions for unsafe methods on the legacy REST viewsets.

    Reads are unchanged. With ``API_WRITE_PERMISSIONS_ENFORCED`` off (default)
    this allows everything, so behaviour is as before.
    """

    perms_map = permissions.DjangoModelPermissions.perms_map
    message = "Writing this resource through the API needs the matching model permission."

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        if not _flag("API_WRITE_PERMISSIONS_ENFORCED"):
            return True
        user = request.user
        if user is None or not user.is_authenticated:
            return False
        if user.is_superuser or user.is_staff:
            return True
        view_name = type(view).__name__
        if request.method in ("PUT", "PATCH") and view_name in MEMBER_UPDATE_VIEWSETS:
            return True
        if request.method == "POST" and view_name in MEMBER_CREATE_VIEWSETS:
            return True
        # The class queryset avoids running a (possibly visibility-filtered) get_queryset.
        queryset = getattr(view, "queryset", None)
        model = queryset.model if queryset is not None else view.get_queryset().model
        kwargs = {"app_label": model._meta.app_label, "model_name": model._meta.model_name}
        perms = [p % kwargs for p in self.perms_map.get(request.method, ())]
        if not perms:
            return False
        return user.has_perms(perms)


def legacy_write_permission_applies(view) -> bool:
    return type(view).__name__ not in WRITE_PERMISSION_EXEMPT_VIEWSETS


# -- upload endpoints -----------------------------------------------------------

def _trusted_for_dataset(user, dataset) -> bool:
    """#707: the dataset's PIs and deputies, and its program's PI and program deputies."""
    if dataset is None or not getattr(dataset, "pk", None):
        return False
    from YSE_App.services.access_actions import dataset_role

    return dataset_role(user, dataset) is not None


def upload_denial(user, group_names: Iterable[str] = (), existing_rows: Iterable = (),
                  existing_dataset=None) -> Optional[str]:
    """Reason an upload is refused, or None if it may proceed.

    ``group_names``: collaboration groups the upload would share data with.
    ``existing_rows``: rows (with ``created_by_id``) the upload would replace or
    re-share. ``existing_dataset``: the existing photometry set or spectrum the
    upload targets, if any; its PIs and deputies, and its program's PI and
    program deputies, are trusted for it (#707). Always None while ``UPLOAD_GROUP_CHECKS_ENFORCED`` is turned off.
    """
    if not _flag("UPLOAD_GROUP_CHECKS_ENFORCED"):
        return None
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return "Authentication required."
    if is_trusted_writer(user):
        return None
    if existing_dataset is not None and _trusted_for_dataset(user, existing_dataset):
        return None
    own = set(user.groups.values_list("name", flat=True))
    if not (own - {PUBLIC_COLLABORATION_GROUP_NAME}):
        return "Uploads need membership in a collaboration group."
    outside = sorted({g for g in group_names if g} - own)
    if outside:
        return "Uploads may only be shared with groups you belong to (not: %s)." % ", ".join(outside)
    for row in existing_rows:
        if row is not None and getattr(row, "created_by_id", None) != user.id:
            return "Only the original uploader or staff may replace or re-share existing data."
    return None


def bulk_ingest_denial(user) -> Optional[str]:
    """``add_transient`` (bulk upsert used by the ingest crons) is for trusted writers when enforced."""
    if not _flag("UPLOAD_GROUP_CHECKS_ENFORCED"):
        return None
    if is_trusted_writer(user):
        return None
    return "Bulk transient ingest is limited to service accounts and staff."


# -- SQL Explorer ---------------------------------------------------------------

def explorer_access_allowed(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    if _flag("EXPLORER_RESTRICTED_ACCESS"):
        return user_in_group(user, getattr(settings, "EXPLORER_ACCESS_GROUP", "explorer_users"))
    return bool(user.is_staff)


# SQL Explorer for everyone (#723). With ``EXPLORER_OPEN_TO_ALL_USERS`` (off by default)
# and the query scope enforced, every signed-in active user may run and export queries,
# use the playground and the schema browser, and create, edit or delete their own saved
# queries; every query they run is rewritten to their visible data
# (``services.explorer_scope``). Full access (``explorer_access_allowed``) is unchanged.
# Open users never see the query log (everyone's SQL and email addresses) or the email
# export, and may not edit or delete queries they did not create.

_EXPLORER_OPEN_VIEW_DENIED = frozenset({"explorer_logs", "email_csv_query"})
_EXPLORER_OPEN_CHANGE_ALLOWED = frozenset({
    "explorer_index", "explorer_playground", "explorer_schema", "query_create", "download_sql",
})
_EXPLORER_OPEN_OWNER_ONLY = frozenset({"query_detail", "query_delete"})


def explorer_open_to_all() -> bool:
    if not _flag("EXPLORER_OPEN_TO_ALL_USERS"):
        return False
    from YSE_App.services.explorer_scope import scope_enforced

    return scope_enforced()


def explorer_open_access(user) -> bool:
    """A signed-in active user who may use Explorer only because it is open to everyone."""
    return bool(
        explorer_open_to_all() and user is not None and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def _explorer_url(request):
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None, {}
    return match.url_name, dict(match.kwargs or {})


def _explorer_query_owned(user, query_id) -> bool:
    from explorer.models import Query

    try:
        return Query.objects.filter(pk=int(query_id), created_by_user_id=user.pk).exists()
    except (TypeError, ValueError):
        return False


def explorer_view_allowed(request) -> bool:
    """``EXPLORER_PERMISSION_VIEW``: full access, or an open user outside the log and email pages."""
    user = getattr(request, "user", None)
    if explorer_access_allowed(user):
        return True
    if not explorer_open_access(user):
        return False
    url_name, _kwargs = _explorer_url(request)
    return url_name not in _EXPLORER_OPEN_VIEW_DENIED


def explorer_change_allowed(request) -> bool:
    """``EXPLORER_PERMISSION_CHANGE``: full access, or an open user on the playground, schema and
    new-query pages, and on their own saved queries."""
    user = getattr(request, "user", None)
    if explorer_access_allowed(user):
        return True
    if not explorer_open_access(user):
        return False
    url_name, kwargs = _explorer_url(request)
    if url_name == "explorer_playground" and request.GET.get("querylog_id"):
        from explorer.models import QueryLog

        try:
            return QueryLog.objects.filter(pk=int(request.GET["querylog_id"]), run_by_user_id=user.pk).exists()
        except (TypeError, ValueError):
            return False
    if url_name in _EXPLORER_OPEN_CHANGE_ALLOWED:
        return True
    if url_name in _EXPLORER_OPEN_OWNER_ONLY:
        return _explorer_query_owned(user, kwargs.get("query_id", kwargs.get("pk")))
    return False
