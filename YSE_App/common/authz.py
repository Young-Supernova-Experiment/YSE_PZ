"""Write-authorization helpers for the REST API, the upload endpoints and SQL Explorer.

Every check here that could change who may write is behind a settings flag whose
default keeps the previous behaviour (see ``YSE_PZ/settings.py``, "Authorization
hardening", and ``docs/ziggy-runbook.md``):

- ``API_WRITE_PERMISSIONS_ENFORCED``: unsafe methods on the legacy REST viewsets
  need staff, superuser or the Django model permission (granted to the
  ``API_WRITERS_GROUP`` group by migration 0032).
- ``UPLOAD_GROUP_CHECKS_ENFORCED``: the upload endpoints (``add_transient``,
  ``add_transient_phot``, ``add_transient_spec``, ``upload_spectrum``) need a
  trusted uploader, or a collaboration-group member who shares only with their
  own groups and replaces only rows they created.
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

def upload_denial(user, group_names: Iterable[str] = (), existing_rows: Iterable = ()) -> Optional[str]:
    """Reason an upload is refused, or None if it may proceed.

    ``group_names``: collaboration groups the upload would share data with.
    ``existing_rows``: rows (with ``created_by_id``) the upload would replace or
    re-share. Always None while ``UPLOAD_GROUP_CHECKS_ENFORCED`` is off.
    """
    if not _flag("UPLOAD_GROUP_CHECKS_ENFORCED"):
        return None
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return "Authentication required."
    if is_trusted_writer(user):
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
