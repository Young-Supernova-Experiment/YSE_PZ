"""Per-scope photometry statistics (#607): ``YSE_App_transientphotstatscope``.

One row per transient with restricted photometry and visibility scope (the
public scope plus one per restricting collaboration group), with the same
statistic columns as ``YSE_App_transientphotstat``. Rows are written by the
photometry recompute and backfilled with ``manage.py backfill_photstat_scopes
--apply`` (runbook). ``TransientPhotStat`` moves its columns into an abstract
base class; no SQL change on that table.

Safe on the shared database: a new table only, no existing column changes.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0041_observed_block_outcomes'),
    ]

    operations = [
        migrations.CreateModel(
            name='TransientPhotStatScope',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('num_obs_global', models.IntegerField(default=0)),
                ('num_det_global', models.IntegerField(default=0)),
                ('num_limits_global', models.IntegerField(default=0)),
                ('last_obs_mjd', models.FloatField(blank=True, null=True)),
                ('last_obs_date', models.DateTimeField(blank=True, null=True)),
                ('first_detected_mjd', models.FloatField(blank=True, null=True)),
                ('first_detected_date', models.DateTimeField(blank=True, null=True)),
                ('first_detected_mag', models.FloatField(blank=True, null=True)),
                ('last_detected_mjd', models.FloatField(blank=True, null=True)),
                ('last_detected_date', models.DateTimeField(blank=True, null=True)),
                ('last_detected_mag', models.FloatField(blank=True, null=True)),
                ('peak_mjd', models.FloatField(blank=True, null=True)),
                ('peak_date', models.DateTimeField(blank=True, null=True)),
                ('peak_mag', models.FloatField(blank=True, null=True)),
                ('mean_mag', models.FloatField(blank=True, null=True)),
                ('faintest_mag', models.FloatField(blank=True, null=True)),
                ('deepest_limit', models.FloatField(blank=True, null=True)),
                ('deepest_limit_mjd', models.FloatField(blank=True, null=True)),
                ('last_non_detection_mjd', models.FloatField(blank=True, null=True)),
                ('time_to_non_detection', models.FloatField(blank=True, null=True)),
                ('rise_rate', models.FloatField(blank=True, null=True)),
                ('decay_rate', models.FloatField(blank=True, null=True)),
                ('per_band_json', models.TextField(blank=True, null=True)),
                ('phot_hash', models.CharField(blank=True, default='', max_length=40)),
                ('schema_version', models.IntegerField(default=0)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('scope_key', models.CharField(max_length=24)),
                ('deepest_limit_band', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='YSE_App.photometricband')),
                ('first_detected_band', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='YSE_App.photometricband')),
                ('group', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='auth.group')),
                ('last_detected_band', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='YSE_App.photometricband')),
                ('last_non_detection_band', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='YSE_App.photometricband')),
                ('peak_band', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='YSE_App.photometricband')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='photstat_scopes', to='YSE_App.transient')),
            ],
        ),
        migrations.AddConstraint(
            model_name='transientphotstatscope',
            constraint=models.UniqueConstraint(fields=('transient', 'scope_key'), name='yse_photstat_scope_unique'),
        ),
    ]
