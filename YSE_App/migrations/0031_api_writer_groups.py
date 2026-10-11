"""Groups for write authorization (#520, #523, #525). Data only, no schema change.

- ``API_WRITERS_GROUP`` (default ``api_writers``): add/change permissions on the
  models the ingest crons and scripts write through the REST API, and the
  "trusted uploader" role for the upload endpoints. Used only when
  ``API_WRITE_PERMISSIONS_ENFORCED`` / ``UPLOAD_GROUP_CHECKS_ENFORCED`` are on.
- ``EXPLORER_ACCESS_GROUP`` (default ``explorer_users``): SQL Explorer access
  when ``EXPLORER_RESTRICTED_ACCESS`` is on.

Accounts named by ``settings.API_SERVICE_USERNAMES`` (the ``[main] dblogin``
cron account) are added to the writers group when they exist. No user is
created or named here. Reversing leaves the groups in place.
"""

from django.conf import settings
from django.db import migrations

# Models written over HTTP by crons, CLI scripts or staff tools (see the PR's
# caller inventory). Add and change only; nothing calls DELETE on these.
WRITER_MODELS = (
    "transient",
    "alternatetransientnames",
    "transienttag",
    "transientwebresource",
    "transientfollowup",
    "transientobservationtask",
    "transientphotometry",
    "transientphotdata",
    "transientspectrum",
    "transientspecdata",
    "host",
    "hostwebresource",
    "hostphotometry",
    "hostphotdata",
    "hostspectrum",
    "hostspecdata",
    "observatory",
    "telescope",
    "instrument",
    "photometricband",
    "surveyfield",
    "surveyfieldmsb",
    "surveyobservation",
    "classicalobservingdate",
    "gwcandidate",
    "gwcandidateimage",
)


def forwards(apps, schema_editor):
    from django.contrib.auth.management import create_permissions

    # Permission rows are normally created after migrate; make sure they exist now.
    app_config = apps.get_app_config("YSE_App")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    User = apps.get_model("auth", "User")

    writers, _ = Group.objects.get_or_create(name=getattr(settings, "API_WRITERS_GROUP", "api_writers"))
    codenames = ["%s_%s" % (action, model) for model in WRITER_MODELS for action in ("add", "change")]
    perms = Permission.objects.filter(content_type__app_label="YSE_App", codename__in=codenames)
    writers.permissions.add(*perms)

    Group.objects.get_or_create(name=getattr(settings, "EXPLORER_ACCESS_GROUP", "explorer_users"))

    for username in getattr(settings, "API_SERVICE_USERNAMES", []) or []:
        user = User.objects.filter(username=username).first()
        if user is not None:
            user.groups.add(writers)


class Migration(migrations.Migration):

    dependencies = [
        ("YSE_App", "0030_shared_db_column_defaults"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
