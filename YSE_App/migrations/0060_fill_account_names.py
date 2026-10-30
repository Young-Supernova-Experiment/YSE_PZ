"""Fill empty account given/family names from unambiguous profile full names.

Data only (``auth_user.first_name`` / ``last_name``), and only for accounts where both are
empty: two plain words, or three where one of the first two is an initial (see
``YSE_App/common/names.py``). Everyone else is asked by the onboarding gate. Safe on the
shared YSE_test database (no schema change; develop ignores these columns). Reversing
does nothing: the filled names are valid either way.
"""

from django.db import migrations

from YSE_App.common.names import split_full_name


def fill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    ResearcherProfile = apps.get_model("YSE_App", "ResearcherProfile")
    rows = (ResearcherProfile.objects.filter(user__first_name="", user__last_name="")
            .exclude(full_name="").values_list("user_id", "full_name"))
    for user_id, full_name in rows.iterator():
        parts = split_full_name(full_name)
        if parts is not None:
            User.objects.filter(pk=user_id, first_name="", last_name="").update(
                first_name=parts[0][:150], last_name=parts[1][:150])


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0059_team_deputies_requests'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
