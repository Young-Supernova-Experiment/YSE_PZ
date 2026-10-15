"""Group profiles, invitation links and user preferences (#445, #440).

New tables only (and their M2M tables), so it is safe on the shared YSE_test
database: no existing table or column changes. Named ``0033_group_profiles``
so it never collides with experimental's ``0033_photstat_extrapolated``.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0032_seed_user_agreement'),
    ]

    operations = [
        migrations.CreateModel(
            name='GroupProfile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True, help_text='Inactive groups are hidden from group pickers and default audiences. Their data, visibility and members are unchanged.')),
                ('is_system', models.BooleanField(default=False, help_text='Permission or site-wide group (for example Public, api_writers, explorer_users): no owners, no invitations, membership is managed by staff in the admin only.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('group', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='group_profile', to='auth.group')),
                ('owners', models.ManyToManyField(blank=True, help_text='Owners can invite people to the group, see its members and remove members. System groups cannot have owners.', related_name='owned_group_profiles', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'group profile',
                'ordering': ('group__name',),
            },
        ),
        migrations.CreateModel(
            name='GroupInvitation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token_hash', models.CharField(editable=False, max_length=64, unique=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('expires_at', models.DateTimeField()),
                ('max_uses', models.PositiveIntegerField(default=1)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invitations', to='auth.group')),
                ('revoked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'group invitation',
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.CreateModel(
            name='GroupInvitationUse',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('used_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('invitation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='uses', to='YSE_App.groupinvitation')),
                ('used_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'group invitation use',
                'ordering': ('-used_at', '-id'),
                'unique_together': {('invitation', 'used_by')},
            },
        ),
        migrations.CreateModel(
            name='UserPreference',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('comment_default_public', models.BooleanField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('comment_default_groups', models.ManyToManyField(blank=True, related_name='comment_default_for_preferences', to='auth.Group')),
                ('followup_default_groups', models.ManyToManyField(blank=True, related_name='followup_default_for_preferences', to='auth.Group')),
                ('hidden_groups', models.ManyToManyField(blank=True, help_text='Groups the user belongs to but does not want offered in pickers.', related_name='hidden_by_preferences', to='auth.Group')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='yse_preference', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'user preference',
            },
        ),
    ]
