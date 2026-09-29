# Instrument logs (#310) and per-telescope weather / SkyCam fields (#311); umbrella #309.

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0019_interests_data_access'),
    ]

    operations = [
        migrations.AddField(
            model_name='telescope',
            name='weather_url',
            field=models.URLField(blank=True, default='', help_text='JSON endpoint for current conditions (Open-Meteo or OpenWeatherMap shape); {lat}, {lon} and {elevation} placeholders are filled from this telescope.', max_length=500),
        ),
        migrations.AddField(
            model_name='telescope',
            name='weather_link',
            field=models.URLField(blank=True, default='', help_text="The site's own weather page, linked from the widget.", max_length=500),
        ),
        migrations.AddField(
            model_name='telescope',
            name='skycam_url',
            field=models.URLField(blank=True, default='', help_text='All-sky camera image URL; the widget reloads it periodically.', max_length=500),
        ),
        migrations.AddField(
            model_name='telescope',
            name='weather',
            field=YSE_App.models.fields.JSONTextField(blank=True, default=dict, help_text='Cached last weather snapshot (written by the fetcher).'),
        ),
        migrations.AddField(
            model_name='telescope',
            name='weather_fetched_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name='InstrumentLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('start', models.DateTimeField(db_index=True, help_text='Start of the period the log covers (UTC).')),
                ('end', models.DateTimeField(blank=True, help_text='End of the period (UTC); blank = a point in time.', null=True)),
                ('message', models.TextField(blank=True, default='', help_text="Free-text summary; the entries live in 'log'.")),
                ('log', YSE_App.models.fields.JSONTextField(blank=True, default=dict, help_text='Structured entries: {"logs": [{"timestamp", "message", "level"}]}.')),
                ('source', models.CharField(choices=[('manual', 'Entered by hand'), ('api', 'Pulled from the facility API')], db_index=True, default='manual', max_length=8)),
                ('source_name', models.CharField(blank=True, default='', help_text='Where an API pull came from (facility slug or endpoint host).', max_length=64)),
                ('fingerprint', models.CharField(blank=True, db_index=True, default='', editable=False, max_length=64)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='instrumentlog_created_by', to=settings.AUTH_USER_MODEL)),
                ('instrument', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='logs', to='YSE_App.instrument')),
                ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='instrumentlog_modified_by', to=settings.AUTH_USER_MODEL)),
                ('run', models.ForeignKey(blank=True, help_text='The facility-API pull that produced this log.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='instrument_logs', to='YSE_App.externalservicerun')),
            ],
            options={
                'ordering': ('-start', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='instrumentlog',
            index=models.Index(fields=['instrument', 'start'], name='yse_instlog_instr_start_idx'),
        ),
    ]
