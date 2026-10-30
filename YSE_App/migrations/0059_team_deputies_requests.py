"""Team deputies and the per-team "accept membership requests" switch.

A new many-to-many table (``YSE_App_groupprofile_deputies``) and one nullable column,
``YSE_App_groupprofile.accepts_requests``. NULL means "accepts requests" (the behaviour
before this column), so existing rows are unchanged and INSERTs from code that does not
know the column (develop, on the shared YSE_test database) keep working.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0058_membership_request_held'),
    ]

    operations = [
        migrations.AddField(
            model_name='groupprofile',
            name='accepts_requests',
            field=models.BooleanField(blank=True, default=None, help_text='Signed-in users can ask to join; owners and deputies decide. Off: invitations and direct adds only.', null=True),
        ),
        migrations.AddField(
            model_name='groupprofile',
            name='deputies',
            field=models.ManyToManyField(blank=True, help_text='Members an owner appoints to manage the team: approve requests, add, invite and remove members. Only owners and staff appoint or remove deputies.', related_name='deputy_group_profiles', to=settings.AUTH_USER_MODEL),
        ),
    ]
