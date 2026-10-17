"""Per-point access rules, standing grants and access history (#648, PR 1b): schema only.

Replaces PR 1's four-level ``DataAccessGrant`` table (always empty: nothing
wrote to it and the flag is off) with Ryan's per-point design (2026-10-17):

* ``YSE_App_dataaccessrule``: one allow / deny rule per (photometry point or
  spectrum, grantee);
* ``YSE_App_standingaccessgrant``: program or night actions applied to points
  that arrive later;
* ``YSE_App_groupmembershipperiod``: dated group membership, seeded here from
  the current memberships with ``joined_at`` unknown (since before tracking);
* ``YSE_App_datasetgroupshare``: when a group was added to a dataset's own
  groups (no row: before tracking);
* ``YSE_App_accessrulesversion``: a single-row counter (row 1, version 0).

Safe on the shared database: drops one empty table that only experimental
code knows, creates five new tables and writes rows only into them.
``AllocationDelegate`` and the datasets' ``allocation`` columns are unchanged.
"""

from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q
import django.db.models.deletion

# "At most one" in the database (exactly one is checked by ``clean``): on MySQL, Django's
# cascade sets a nullable CASCADE foreign key to NULL just before deleting the row, so
# a deleted group, user, point or dataset must not trip the check.
_ONE_GRANTEE = ~Q(grantee_group__isnull=False, grantee_user__isnull=False)
_EFFECT = [('allow', 'Allow'), ('deny', 'Restrict')]
_AS = [('pi', 'PI'), ('program_deputy', 'Program deputy'), ('group_deputy', "PI's data-sharing deputy"),
       ('staff', 'Administrator')]

BATCH = 1000


def seed(apps, schema_editor):
    AccessRulesVersion = apps.get_model('YSE_App', 'AccessRulesVersion')
    GroupMembershipPeriod = apps.get_model('YSE_App', 'GroupMembershipPeriod')
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    db = schema_editor.connection.alias
    AccessRulesVersion.objects.using(db).get_or_create(pk=1, defaults={'version': 0})
    if GroupMembershipPeriod.objects.using(db).exists():
        return
    through = User.groups.through
    rows = []
    for user_id, group_id in through.objects.using(db).values_list('user_id', 'group_id').order_by('id').iterator():
        rows.append(GroupMembershipPeriod(user_id=user_id, group_id=group_id, joined_at=None, left_at=None))
        if len(rows) >= BATCH:
            GroupMembershipPeriod.objects.using(db).bulk_create(rows)
            rows = []
    if rows:
        GroupMembershipPeriod.objects.using(db).bulk_create(rows)


def unseed(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0043_data_access_grants'),
    ]

    operations = [
        migrations.DeleteModel(name='DataAccessGrant'),
        migrations.CreateModel(
            name='StandingAccessGrant',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('effect', models.CharField(choices=_EFFECT, max_length=8)),
                ('night_start', models.DateTimeField(
                    blank=True, null=True, help_text='One night only: UT start of the night (inclusive).')),
                ('night_end', models.DateTimeField(
                    blank=True, null=True, help_text='One night only: UT end of the night (exclusive).')),
                ('created_as', models.CharField(choices=_AS, max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('stopped_at', models.DateTimeField(
                    blank=True, db_index=True, null=True,
                    help_text='When it stopped applying to new points; rules already written stay.')),
                ('note', models.TextField(blank=True, default='')),
                ('allocation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='standing_access_grants',
                    to='YSE_App.allocation', help_text='The program (allocation) whose data this applies to.')),
                ('classical_night', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='standing_access_grants', to='YSE_App.classicalobservingdate',
                    help_text='Optional: the classical night this window is (display and links only).')),
                ('grantee_group', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='standing_access_grants', to='auth.group')),
                ('grantee_user', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='standing_access_grants', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='standing_access_grants_made', to=settings.AUTH_USER_MODEL)),
                ('stopped_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='standing_access_grants_stopped', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='standingaccessgrant',
            index=models.Index(fields=['allocation', 'stopped_at'], name='yse_sag_alloc_stopped'),
        ),
        migrations.AddConstraint(
            model_name='standingaccessgrant',
            constraint=models.CheckConstraint(name='yse_sag_one_grantee', check=_ONE_GRANTEE),
        ),
        migrations.AddConstraint(
            model_name='standingaccessgrant',
            constraint=models.CheckConstraint(
                name='yse_sag_window',
                check=(Q(night_start__isnull=True, night_end__isnull=True, classical_night__isnull=True)
                       | Q(night_start__isnull=False, night_end__isnull=False, night_end__gt=F('night_start'))),
            ),
        ),
        migrations.AddConstraint(
            model_name='standingaccessgrant',
            constraint=models.CheckConstraint(name='yse_sag_effect', check=Q(effect__in=('allow', 'deny'))),
        ),
        migrations.CreateModel(
            name='DataAccessRule',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('effect', models.CharField(choices=_EFFECT, max_length=8)),
                ('granted_as', models.CharField(choices=_AS, max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(
                    auto_now=True, help_text='Last change: for an allow, when the point was shared with the grantee.')),
                ('transient_phot_data', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='access_rules', to='YSE_App.transientphotdata',
                    help_text='The photometry point this rule is for.')),
                ('transient_spectrum', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='access_rules', to='YSE_App.transientspectrum',
                    help_text='The spectrum this rule is for.')),
                ('photometry', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='access_rules', to='YSE_App.transientphotometry',
                    help_text="The point's photometry set (copied from the point).")),
                ('transient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='access_rules',
                    to='YSE_App.transient', help_text="The target's transient (copied from the point or spectrum).")),
                ('grantee_group', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='data_access_rules', to='auth.group')),
                ('grantee_user', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='data_access_rules', to=settings.AUTH_USER_MODEL)),
                ('granted_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='data_access_rules_made', to=settings.AUTH_USER_MODEL)),
                ('standing_grant', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rules',
                    to='YSE_App.standingaccessgrant',
                    help_text='The standing grant that wrote this rule at ingest, if any.')),
            ],
            options={
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='dataaccessrule',
            index=models.Index(fields=['grantee_user', 'effect'], name='yse_dar_user_effect'),
        ),
        migrations.AddIndex(
            model_name='dataaccessrule',
            index=models.Index(fields=['grantee_group', 'effect'], name='yse_dar_group_effect'),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.CheckConstraint(name='yse_dar_one_grantee', check=_ONE_GRANTEE),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.CheckConstraint(
                name='yse_dar_one_target',
                check=~Q(transient_phot_data__isnull=False, transient_spectrum__isnull=False),
            ),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.CheckConstraint(name='yse_dar_effect', check=Q(effect__in=('allow', 'deny'))),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.UniqueConstraint(fields=['transient_phot_data', 'grantee_group'],
                                               name='yse_dar_point_group'),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.UniqueConstraint(fields=['transient_phot_data', 'grantee_user'],
                                               name='yse_dar_point_user'),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.UniqueConstraint(fields=['transient_spectrum', 'grantee_group'],
                                               name='yse_dar_spec_group'),
        ),
        migrations.AddConstraint(
            model_name='dataaccessrule',
            constraint=models.UniqueConstraint(fields=['transient_spectrum', 'grantee_user'],
                                               name='yse_dar_spec_user'),
        ),
        migrations.CreateModel(
            name='GroupMembershipPeriod',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('joined_at', models.DateTimeField(
                    blank=True, null=True, help_text='Null: a member since before tracking began.')),
                ('left_at', models.DateTimeField(blank=True, null=True, help_text='Null: still a member.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('group', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='membership_periods',
                    to='auth.group')),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='group_membership_periods',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('user_id', 'group_id', 'id'),
            },
        ),
        migrations.AddIndex(
            model_name='groupmembershipperiod',
            index=models.Index(fields=['user', 'group', 'left_at'], name='yse_gmp_user_group_left'),
        ),
        migrations.AddIndex(
            model_name='groupmembershipperiod',
            index=models.Index(fields=['group', 'left_at'], name='yse_gmp_group_left'),
        ),
        migrations.AddConstraint(
            model_name='groupmembershipperiod',
            constraint=models.CheckConstraint(
                name='yse_gmp_order',
                check=Q(joined_at__isnull=True) | Q(left_at__isnull=True) | Q(left_at__gte=F('joined_at')),
            ),
        ),
        migrations.CreateModel(
            name='DatasetGroupShare',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('shared_at', models.DateTimeField()),
                ('group', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='dataset_shares', to='auth.group')),
                ('transient_photometry', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='group_shares', to='YSE_App.transientphotometry')),
                ('transient_spectrum', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='group_shares', to='YSE_App.transientspectrum')),
            ],
            options={
                'ordering': ('-shared_at', '-id'),
            },
        ),
        migrations.AddConstraint(
            model_name='datasetgroupshare',
            constraint=models.CheckConstraint(
                name='yse_dgs_one_dataset',
                check=~Q(transient_photometry__isnull=False, transient_spectrum__isnull=False),
            ),
        ),
        migrations.AddConstraint(
            model_name='datasetgroupshare',
            constraint=models.UniqueConstraint(fields=['transient_photometry', 'group'], name='yse_dgs_phot_group'),
        ),
        migrations.AddConstraint(
            model_name='datasetgroupshare',
            constraint=models.UniqueConstraint(fields=['transient_spectrum', 'group'], name='yse_dgs_spec_group'),
        ),
        migrations.CreateModel(
            name='AccessRulesVersion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.BigIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RunPython(seed, unseed),
    ]
