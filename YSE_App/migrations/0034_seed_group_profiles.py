"""One GroupProfile per existing group (#445, #440): no owners, all active.

System groups (the names in ``settings.YSE_SYSTEM_GROUP_NAMES``, by default
Public, api_writers and explorer_users, plus any group that holds model
permissions) are marked ``is_system``. Owners are assigned by staff in the
Django admin afterwards (Ryan, 2026-10-11): this migration creates no owner rows.

Idempotent (``get_or_create``): develop and experimental share the YSE_test
database. ``manage.py ensure_group_profiles`` does the same at any time.
Named so it never collides with experimental's ``0034_transient_photstat_view``.
"""

from django.conf import settings
from django.db import migrations

DEFAULT_SYSTEM_GROUP_NAMES = ("Public", "api_writers", "explorer_users")


def seed(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    GroupProfile = apps.get_model("YSE_App", "GroupProfile")
    names = set(getattr(settings, "YSE_SYSTEM_GROUP_NAMES", DEFAULT_SYSTEM_GROUP_NAMES))
    with_permissions = set(
        Group.objects.filter(permissions__isnull=False).values_list("id", flat=True).distinct()
    )
    for group in Group.objects.all().only("id", "name"):
        is_system = group.name in names or group.id in with_permissions
        profile, created = GroupProfile.objects.get_or_create(
            group_id=group.id, defaults={"is_system": is_system, "is_active": True},
        )
        if not created and is_system and not profile.is_system:
            profile.is_system = True
            profile.save(update_fields=["is_system"])


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0033_group_profiles'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
