# Allocations and facility requests (#303, #298): Allocation + FacilityRequest tables.

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0015_photstat_limit_bands'),
    ]

    operations = [
        migrations.CreateModel(
            name='Allocation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(help_text="Label, e.g. 'LCO 2026B YSE spectroscopy'.", max_length=128)),
                ('proposal_id', models.CharField(blank=True, default='', help_text='Program / proposal identifier at the facility (e.g. an LCO proposal code).', max_length=64)),
                ('hours_allocated', models.FloatField(default=0.0)),
                ('hours_used', models.FloatField(default=0.0)),
                ('start_date', models.DateTimeField(help_text='Semester start.')),
                ('end_date', models.DateTimeField(help_text='Semester end.')),
                ('facility', models.CharField(blank=True, default='', help_text='Facility API slug (see YSE_App.facilities); blank = manual allocation, no API.', max_length=32)),
                ('endpoint_url', models.URLField(blank=True, default='', help_text='Where requests are sent when the facility needs an endpoint (GENERIC API mode).', max_length=500)),
                ('default_request_params', YSE_App.models.fields.JSONTextField(blank=True, default=dict, help_text="JSON merged under every request's parameters.")),
                ('is_active', models.BooleanField(default=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocation_created_by', to=settings.AUTH_USER_MODEL)),
                ('credential', models.ForeignKey(blank=True, help_text='Encrypted credentials the facility API uses (#264).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='allocations', to='YSE_App.encryptedcredential')),
                ('groups', models.ManyToManyField(blank=True, help_text='Groups whose members may submit requests; empty = every authenticated user.', related_name='allocations', to='auth.Group')),
                ('instrument', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='allocations', to='YSE_App.instrument')),
                ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocation_modified_by', to=settings.AUTH_USER_MODEL)),
                ('principal_investigator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='allocations', to='YSE_App.principalinvestigator')),
                ('service', models.OneToOneField(blank=True, editable=False, help_text="External service that records this allocation's runs (created on demand).", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='allocation', to='YSE_App.externalservice')),
                ('telescope', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='allocations', to='YSE_App.telescope')),
            ],
            options={
                'ordering': ('-end_date', 'name'),
            },
        ),
        migrations.CreateModel(
            name='FacilityRequest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('payload', YSE_App.models.fields.JSONTextField(blank=True, default=dict, help_text='Validated request parameters.')),
                ('state', models.CharField(choices=[('draft', 'Draft'), ('queued', 'Queued'), ('submitted', 'Submitted'), ('accepted', 'Accepted'), ('running', 'Running'), ('complete', 'Complete'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], db_index=True, default='draft', max_length=12)),
                ('state_detail', models.TextField(blank=True, default='')),
                ('external_id', models.CharField(blank=True, default='', max_length=255)),
                ('external_url', models.URLField(blank=True, default='', max_length=1000)),
                ('submitted_at', models.DateTimeField(blank=True, null=True)),
                ('last_polled', models.DateTimeField(blank=True, null=True)),
                ('hours_charged', models.FloatField(default=0.0, help_text='Hours this request costs the allocation; charged once when it completes.')),
                ('charged_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('log', YSE_App.models.fields.JSONTextField(blank=True, default=list, help_text='Chronological list of {at, event, detail}.')),
                ('allocation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='requests', to='YSE_App.allocation')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='facilityrequest_created_by', to=settings.AUTH_USER_MODEL)),
                ('followup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='facility_requests', to='YSE_App.transientfollowup')),
                ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='facilityrequest_modified_by', to=settings.AUTH_USER_MODEL)),
                ('run', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='facility_request', to='YSE_App.externalservicerun')),
                ('submitted_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='facility_requests', to=settings.AUTH_USER_MODEL)),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='facility_requests', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('-created_date',),
            },
        ),
        migrations.AddIndex(
            model_name='facilityrequest',
            index=models.Index(fields=['allocation', 'state'], name='yse_facreq_alloc_state_idx'),
        ),
        migrations.AddIndex(
            model_name='facilityrequest',
            index=models.Index(fields=['transient', 'state'], name='yse_facreq_transient_state_idx'),
        ),
    ]
