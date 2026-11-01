"""SALT and Bazin fit tables (#846; YSE_App/models/fit_models.py).

Two new tables only: no existing table, column or row changes. Their foreign keys to
``YSE_App_transient`` and ``YSE_App_externalservicerun`` (tables develop knows) get
database-level ``ON DELETE CASCADE`` in 0064 (``YSE_App/common/fk_on_delete.py``, as in 0062), so
develop's deletes of a transient or a run on the shared database still succeed. Nothing is
filled here: ``manage.py backfill_fit_tables --apply`` writes rows for older runs.
"""

import django.db.models.deletion
from django.db import migrations, models

import YSE_App.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0062_shared_db_fk_on_delete'),
    ]

    operations = [
        migrations.CreateModel(
            name='BazinFit',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fit_date', models.DateTimeField(blank=True, null=True)),
                ('is_subset', models.BooleanField(default=False)),
                ('selection', YSE_App.models.fields.JSONTextField(blank=True, null=True)),
                ('n_points', models.PositiveIntegerField(blank=True, null=True)),
                ('chi2', models.FloatField(blank=True, null=True)),
                ('dof', models.IntegerField(blank=True, null=True)),
                ('reduced_chi2', models.FloatField(blank=True, null=True)),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('band', models.CharField(max_length=64)),
                ('band_key', models.CharField(blank=True, default='', max_length=16)),
                ('is_reference_band', models.BooleanField(default=False)),
                ('method', models.CharField(blank=True, default='', max_length=32)),
                ('peak_mjd', models.FloatField(blank=True, null=True)),
                ('peak_mag', models.FloatField(blank=True, null=True)),
                ('t0', models.FloatField(blank=True, null=True)),
                ('t0_err', models.FloatField(blank=True, null=True)),
                ('tau_rise', models.FloatField(blank=True, null=True)),
                ('tau_rise_err', models.FloatField(blank=True, null=True)),
                ('tau_fall', models.FloatField(blank=True, null=True)),
                ('tau_fall_err', models.FloatField(blank=True, null=True)),
                ('mjd_min', models.FloatField(blank=True, null=True)),
                ('mjd_max', models.FloatField(blank=True, null=True)),
                ('last_non_detection_mjd', models.FloatField(blank=True, null=True)),
                ('peak_after_last_non_detection', models.BooleanField(blank=True, null=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bazin_fits',
                                          to='YSE_App.externalservicerun')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bazin_fits',
                                                to='YSE_App.transient')),
            ],
            options={
                'ordering': ('-fit_date', '-run_id', 'band'),
                'indexes': [models.Index(fields=['transient', 'fit_date'], name='yse_bazinfit_transient_idx')],
                'constraints': [models.UniqueConstraint(fields=('run', 'band'), name='yse_bazinfit_run_band_uniq')],
            },
        ),
        migrations.CreateModel(
            name='SaltFit',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fit_date', models.DateTimeField(blank=True, null=True)),
                ('is_subset', models.BooleanField(default=False)),
                ('selection', YSE_App.models.fields.JSONTextField(blank=True, null=True)),
                ('n_points', models.PositiveIntegerField(blank=True, null=True)),
                ('chi2', models.FloatField(blank=True, null=True)),
                ('dof', models.IntegerField(blank=True, null=True)),
                ('reduced_chi2', models.FloatField(blank=True, null=True)),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('model', models.CharField(max_length=32)),
                ('model_version', models.CharField(blank=True, default='', max_length=32)),
                ('t0', models.FloatField(blank=True, null=True)),
                ('t0_err', models.FloatField(blank=True, null=True)),
                ('x0', models.FloatField(blank=True, null=True)),
                ('x0_err', models.FloatField(blank=True, null=True)),
                ('x1', models.FloatField(blank=True, null=True)),
                ('x1_err', models.FloatField(blank=True, null=True)),
                ('c', models.FloatField(blank=True, null=True)),
                ('c_err', models.FloatField(blank=True, null=True)),
                ('m_b', models.FloatField(blank=True, null=True)),
                ('m_b_err', models.FloatField(blank=True, null=True)),
                ('z', models.FloatField(blank=True, null=True)),
                ('z_err', models.FloatField(blank=True, null=True)),
                ('z_source', models.CharField(blank=True, default='', max_length=16)),
                ('mwebv', models.FloatField(blank=True, null=True)),
                ('bands', YSE_App.models.fields.JSONTextField(blank=True, default=list)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='salt_fits',
                                          to='YSE_App.externalservicerun')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='salt_fits',
                                                to='YSE_App.transient')),
            ],
            options={
                'ordering': ('-fit_date', '-run_id'),
                'indexes': [models.Index(fields=['transient', 'fit_date'], name='yse_saltfit_transient_idx')],
                'constraints': [models.UniqueConstraint(fields=('run',), name='yse_saltfit_run_uniq')],
            },
        ),
    ]
