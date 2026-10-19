"""Revoke an accepted data access request (#291): new status, nullable revoke fields.

Only nullable columns are added (online in MySQL, no table copy); existing rows are untouched, and
code from before this migration ignores the columns.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import YSE_App.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0047_point_program_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataaccessrequest',
            name='added_dataset_ids',
            field=YSE_App.models.fields.JSONTextField(
                blank=True, null=True,
                help_text='The subset of granted_dataset_ids the target group was newly added to (it was not '
                          'already on them); revoking removes the group only from these. Empty on older rows.'),
        ),
        migrations.AddField(
            model_name='dataaccessrequest',
            name='revoked_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='data_access_revocations', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='dataaccessrequest',
            name='revoked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='dataaccessrequest',
            name='revoke_direction',
            field=models.CharField(
                blank=True, null=True, max_length=10,
                choices=[('forward', 'Going forward (earlier data stay visible)'),
                         ('backwards', 'Backwards (earlier data hidden too)')]),
        ),
        migrations.AlterField(
            model_name='dataaccessrequest',
            name='status',
            field=models.CharField(
                db_index=True, default='pending', max_length=16,
                choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('declined', 'Declined'),
                         ('revoked', 'Revoked')]),
        ),
    ]
