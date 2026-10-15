# Feed sources: Hermes / SCiMMA, Einstein Probe and JPL Scout (#280: #281, #282, #283).

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0022_instrument_logs_weather'),
    ]

    operations = [
        migrations.CreateModel(
            name='FeedSource',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('slug', models.SlugField(max_length=64, unique=True)),
                ('kind', models.CharField(choices=[('hermes', 'Hermes / SCiMMA topic'), ('ep', 'Einstein Probe alerts'), ('scout', 'JPL Scout NEO sweep')], db_index=True, max_length=16)),
                ('topic', models.CharField(blank=True, default='', help_text='Hermes: the topic to read (e.g. hermes.test, tns.new-objects). Other kinds ignore it.', max_length=128)),
                ('description', models.TextField(blank=True, default='')),
                ('enabled', models.BooleanField(db_index=True, default=True)),
                ('config', YSE_App.models.fields.JSONTextField(blank=True, default=dict, help_text='Per-kind JSON options (docs/feeds-*.md): criteria, auto_save, save_status, save_obs_group, match_radius_arcsec, import_photometry, max_per_run, comment; hermes: since_hours, kinds, classify; ep: url, topic, max_error_arcsec, annotate; scout: min_neo_score, max_vmag, max_unc_arcmin, neofixer.')),
                ('last_polled', models.DateTimeField(blank=True, editable=False, null=True)),
                ('last_summary', models.CharField(blank=True, default='', editable=False, max_length=255)),
                ('last_error', models.TextField(blank=True, default='', editable=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='feed_sources_created', to=settings.AUTH_USER_MODEL)),
                ('credential', models.ForeignKey(blank=True, help_text='Hermes: JSON secret with hermes_token (or token). EP / Scout: optional.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='feed_sources', to='YSE_App.encryptedcredential')),
            ],
            options={
                'verbose_name': 'feed source',
                'ordering': ('kind', 'name'),
            },
        ),
    ]
