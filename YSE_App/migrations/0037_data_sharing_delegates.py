"""PI-designated deputies who may share private data (#447).

* New table ``YSE_App_datasharingdelegate`` (``DataSharingDelegate``): a PI's
  deputy, optionally scoped to one group's data.
* ``DataAccessRequest.decided_as``: a nullable column recording whether the
  decider acted as a PI, a deputy or an administrator (the logged staff
  override).
* ``DataAccessRequest.owner_group``: help text only (no SQL).

Safe on the shared YSE_test database: one new table and one nullable column, so
``develop`` and ``experimental`` INSERTs keep working whichever code runs. No
data is changed. Named ``0037_data_sharing_delegates`` so it never collides
with an experimental ``0037_*``.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0036_group_membership_requests'),
    ]

    operations = [
        migrations.CreateModel(
            name='DataSharingDelegate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                    to=settings.AUTH_USER_MODEL)),
                ('delegate', models.ForeignKey(
                    help_text="The deputy: may decide data-access requests for this PI's private data.",
                    on_delete=django.db.models.deletion.CASCADE, related_name='data_sharing_delegations',
                    to=settings.AUTH_USER_MODEL)),
                ('group', models.ForeignKey(
                    blank=True, help_text="Only the PI's data restricted to this group; empty = all of the PI's data.",
                    null=True, on_delete=django.db.models.deletion.CASCADE, related_name='data_sharing_delegates',
                    to='auth.group')),
                ('pi', models.ForeignKey(
                    help_text='The PI on whose behalf the deputy shares.',
                    on_delete=django.db.models.deletion.CASCADE, related_name='sharing_delegates',
                    to='YSE_App.principalinvestigator')),
            ],
            options={
                'ordering': ('pi__name', 'delegate__username', 'id'),
            },
        ),
        migrations.AddConstraint(
            model_name='datasharingdelegate',
            constraint=models.UniqueConstraint(fields=('pi', 'delegate', 'group'), name='yse_datasharingdelegate_unique'),
        ),
        migrations.AddField(
            model_name='dataaccessrequest',
            name='decided_as',
            field=models.CharField(
                blank=True, choices=[('pi', 'PI'), ('deputy', "PI's deputy"), ('staff', 'Administrator override')],
                max_length=8, null=True),
        ),
        migrations.AlterField(
            model_name='dataaccessrequest',
            name='owner_group',
            field=models.ForeignKey(
                help_text="Group the restricted data is restricted to; the data's PIs and their deputies decide.",
                on_delete=django.db.models.deletion.CASCADE, related_name='data_access_requests_owned',
                to='auth.group'),
        ),
    ]
