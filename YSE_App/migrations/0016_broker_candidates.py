# Broker candidates and per-group broker filters (issues #277, #279; part of #276).

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import YSE_App.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0015_photstat_limit_bands'),
    ]

    operations = [
        migrations.CreateModel(
            name='BrokerFilter',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('broker', models.CharField(db_index=True, help_text='Provider slug, e.g. antares, fink, alerce.', max_length=32)),
                ('description', models.TextField(blank=True, default='')),
                ('query', YSE_App.models.fields.JSONTextField(blank=True, help_text='Broker-side query the poll runs (provider-specific keys, e.g. Fink classes / days).', null=True)),
                ('criteria', YSE_App.models.fields.JSONTextField(blank=True, help_text='Client-side cuts on the normalised alert (see YSE_App.brokers.filters.CRITERIA).', null=True)),
                ('enabled', models.BooleanField(db_index=True, default=True)),
                ('auto_save', models.BooleanField(default=False, help_text='Save passing alerts as transients at once instead of waiting for a scanner.')),
                ('save_status', models.CharField(default='New', help_text='TransientStatus for saved transients.', max_length=32)),
                ('save_obs_group', models.CharField(blank=True, default='', help_text="ObservationGroup name for saved transients (blank: the provider's default, e.g. ZTF).", max_length=64)),
                ('import_photometry', models.BooleanField(default=True, help_text='Pull the broker light curve when saving.')),
                ('max_alerts', models.PositiveIntegerField(default=200, help_text='Alerts fetched per poll.')),
                ('last_run_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('last_run_summary', models.CharField(blank=True, default='', editable=False, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='broker_filters_created', to=settings.AUTH_USER_MODEL)),
                ('group', models.ForeignKey(blank=True, help_text="Group whose scanners see this filter's candidates (blank: everyone).", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='broker_filters', to='auth.group')),
            ],
            options={
                'ordering': ('broker', 'name'),
                'unique_together': {('broker', 'name')},
            },
        ),
        migrations.CreateModel(
            name='Candidate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('broker', models.CharField(db_index=True, max_length=32)),
                ('alert_id', models.CharField(help_text='Broker object id (ZTF name, ANTARES locus id).', max_length=128)),
                ('latest_alert_id', models.CharField(blank=True, default='', help_text='Newest alert / candid seen.', max_length=128)),
                ('ra', models.FloatField()),
                ('dec', models.FloatField()),
                ('discovery_mjd', models.FloatField(blank=True, null=True)),
                ('last_mjd', models.FloatField(blank=True, db_index=True, null=True)),
                ('last_mag', models.FloatField(blank=True, null=True)),
                ('last_band', models.CharField(blank=True, default='', max_length=16)),
                ('rb', models.FloatField(blank=True, null=True)),
                ('classification', models.CharField(blank=True, default='', max_length=128)),
                ('n_alerts', models.PositiveIntegerField(default=1, help_text='Polls that saw this object.')),
                ('passed_filter_names', YSE_App.models.fields.JSONTextField(blank=True, null=True)),
                ('payload', YSE_App.models.fields.JSONTextField(blank=True, help_text='Normalised alert (BrokerAlert.to_dict).', null=True)),
                ('status', models.CharField(choices=[('new', 'New'), ('saved', 'Saved'), ('rejected', 'Rejected')], db_index=True, default='new', max_length=16)),
                ('status_changed_at', models.DateTimeField(blank=True, null=True)),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('first_seen', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_seen', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('filters', models.ManyToManyField(blank=True, related_name='candidates', to='YSE_App.BrokerFilter')),
                ('status_changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='candidates_reviewed', to=settings.AUTH_USER_MODEL)),
                ('transient', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='broker_candidates', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('-last_seen', '-id'),
                'unique_together': {('broker', 'alert_id')},
            },
        ),
        migrations.AddIndex(
            model_name='candidate',
            index=models.Index(fields=['status', 'last_seen'], name='yse_candidate_status_seen_idx'),
        ),
        migrations.AddIndex(
            model_name='candidate',
            index=models.Index(fields=['ra'], name='yse_candidate_ra_idx'),
        ),
        migrations.AddIndex(
            model_name='candidate',
            index=models.Index(fields=['dec'], name='yse_candidate_dec_idx'),
        ),
    ]
