"""Team membership requests (#448).

One new table, ``YSE_App_groupmembershiprequest``. No existing table changes and no
data is written, so it is safe on the shared YSE_test database whichever branch's code
runs. Named ``0036_group_membership_requests`` so it never collides with an
experimental ``0036_*``.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0035_data_pis'),
    ]

    operations = [
        migrations.CreateModel(
            name='GroupMembershipRequest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message', models.TextField(blank=True, default='', help_text="Optional note to the team's owners.")),
                ('status', models.CharField(choices=[('pending', 'pending'), ('approved', 'approved'), ('denied', 'denied'), ('cancelled', 'cancelled')], db_index=True, default='pending', max_length=16)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('decided_at', models.DateTimeField(blank=True, null=True)),
                ('decided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='membership_requests', to='auth.group')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='group_membership_requests', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'group membership request',
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='groupmembershiprequest',
            index=models.Index(fields=['group', 'status'], name='yse_gmr_group_status'),
        ),
    ]
