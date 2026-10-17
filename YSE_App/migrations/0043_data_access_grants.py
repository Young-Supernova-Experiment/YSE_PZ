"""Read-time access grants and program deputies (#648, PR 1): schema only.

* New table ``YSE_App_dataaccessgrant``: one allow / deny rule for one group or
  user on a program (allocation), a night of a program, one dataset, or one
  night of a photometry set; soft revoke (``revoked_at``), no expiry.
* New table ``YSE_App_allocationdelegate``: program deputies.
* New nullable ``allocation_id`` FK column (indexed) on
  ``YSE_App_transientphotometry`` and ``YSE_App_transientspectrum``.

Safe on the shared database: two new tables and two nullable columns, no data
written. Nothing reads them until ``DATA_ACCESS_GRANTS_ENABLED`` is on.
"""

from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q
import django.db.models.deletion

_NO_WINDOW = Q(night_start__isnull=True, night_end__isnull=True, classical_night__isnull=True)
_WINDOW = Q(night_start__isnull=False, night_end__isnull=False)
_NO_DATASET = Q(transient_photometry__isnull=True, transient_spectrum__isnull=True)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0042_transient_photstat_scope'),
    ]

    operations = [
        migrations.AddField(
            model_name='transientphotometry',
            name='allocation',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='transient_photometry', to='YSE_App.allocation',
                help_text='Program (allocation) these data were taken under, if known.'),
        ),
        migrations.AddField(
            model_name='transientspectrum',
            name='allocation',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='transient_spectra', to='YSE_App.allocation',
                help_text='Program (allocation) these data were taken under, if known.'),
        ),
        migrations.CreateModel(
            name='AllocationDelegate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('modified_at', models.DateTimeField(auto_now=True)),
                ('added_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                    to=settings.AUTH_USER_MODEL)),
                ('allocation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='delegates', to='YSE_App.allocation')),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='allocation_delegations',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('allocation_id', 'user__username', 'id'),
            },
        ),
        migrations.AddConstraint(
            model_name='allocationdelegate',
            constraint=models.UniqueConstraint(fields=['allocation', 'user'], name='yse_allocationdelegate_unique'),
        ),
        migrations.CreateModel(
            name='DataAccessGrant',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('effect', models.CharField(choices=[('allow', 'Allow'), ('deny', 'Restrict')], max_length=8)),
                ('level', models.CharField(
                    choices=[('program', 'Program'), ('night', 'Night of a program'), ('dataset', 'Dataset'),
                             ('points', 'Photometry points of one night')],
                    db_index=True, max_length=16,
                    help_text='Which target is set (checked against the target fields).')),
                ('night_start', models.DateTimeField(
                    blank=True, null=True, help_text='Night and points levels: UT start of the night (inclusive).')),
                ('night_end', models.DateTimeField(
                    blank=True, null=True, help_text='Night and points levels: UT end of the night (exclusive).')),
                ('granted_as', models.CharField(
                    choices=[('pi', 'PI'), ('program_deputy', 'Program deputy'),
                             ('group_deputy', "PI's data-sharing deputy"), ('staff', 'Administrator')],
                    max_length=16)),
                ('note', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('revoked_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('revoke_retroactive', models.BooleanField(
                    default=False,
                    help_text='Revoked rows only: True = the rule no longer applies to any data; False = it still '
                              'applies to data whose obs date is before the revocation (forward revocation).')),
                ('allocation', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='access_grants', to='YSE_App.allocation',
                    help_text='Program and night levels: the program (allocation).')),
                ('classical_night', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='access_grants', to='YSE_App.classicalobservingdate',
                    help_text='Optional: the classical night this window is (display and links only).')),
                ('transient_photometry', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='access_grants', to='YSE_App.transientphotometry',
                    help_text='Dataset level: one photometry set. Points level: the set whose points in the window '
                              'are covered.')),
                ('transient_spectrum', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='access_grants', to='YSE_App.transientspectrum',
                    help_text='Dataset level: one spectrum.')),
                ('grantee_group', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='data_access_grants', to='auth.group')),
                ('grantee_user', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='data_access_grants', to=settings.AUTH_USER_MODEL)),
                ('granted_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='data_access_grants_made', to=settings.AUTH_USER_MODEL)),
                ('revoked_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='data_access_grants_revoked', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='dataaccessgrant',
            index=models.Index(fields=['grantee_user', 'revoked_at'], name='yse_dag_user_revoked'),
        ),
        migrations.AddIndex(
            model_name='dataaccessgrant',
            index=models.Index(fields=['grantee_group', 'revoked_at'], name='yse_dag_group_revoked'),
        ),
        migrations.AddConstraint(
            model_name='dataaccessgrant',
            constraint=models.CheckConstraint(
                name='yse_dag_one_grantee',
                check=(Q(grantee_group__isnull=False, grantee_user__isnull=True)
                       | Q(grantee_group__isnull=True, grantee_user__isnull=False)),
            ),
        ),
        migrations.AddConstraint(
            model_name='dataaccessgrant',
            constraint=models.CheckConstraint(
                name='yse_dag_one_target',
                check=(
                    (Q(level='program', allocation__isnull=False) & _NO_WINDOW & _NO_DATASET)
                    | (Q(level='night', allocation__isnull=False) & _WINDOW & _NO_DATASET)
                    | (Q(level='dataset', allocation__isnull=True) & _NO_WINDOW
                       & (Q(transient_photometry__isnull=False, transient_spectrum__isnull=True)
                          | Q(transient_photometry__isnull=True, transient_spectrum__isnull=False)))
                    | (Q(level='points', allocation__isnull=True) & _WINDOW
                       & Q(transient_photometry__isnull=False, transient_spectrum__isnull=True))
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name='dataaccessgrant',
            constraint=models.CheckConstraint(
                name='yse_dag_window_order',
                check=Q(night_start__isnull=True) | Q(night_end__isnull=True) | Q(night_end__gt=F('night_start')),
            ),
        ),
        migrations.AddConstraint(
            model_name='dataaccessgrant',
            constraint=models.CheckConstraint(name='yse_dag_effect', check=Q(effect__in=('allow', 'deny'))),
        ),
    ]
