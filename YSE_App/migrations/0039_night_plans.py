"""Night plans (#661): the NightPlan and PlannedBlock tables of the decision-trail draft.

* New table ``YSE_App_nightplan``: one version of a classical night's schedule.
* New table ``YSE_App_plannedblock``: one scheduled block of a plan version.

Safe on the shared YSE_test database: two new tables, no change to existing ones, so
``develop`` and ``experimental`` INSERTs keep working whichever code runs. No data changes.
"""

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0038_merge_develop_groups'),
    ]

    operations = [
        migrations.CreateModel(
            name='NightPlan',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('version', models.PositiveSmallIntegerField(help_text='1, 2, ... per night')),
                ('state', models.CharField(
                    choices=[('draft', 'Draft'), ('proposed', 'Proposed'), ('approved', 'Approved'),
                             ('executing', 'Executing'), ('closed', 'Closed'), ('superseded', 'Superseded')],
                    default='draft', max_length=16)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('solver', models.CharField(blank=True, help_text='e.g. "AstroScheduler 1.2.0"', max_length=64, null=True)),
                ('solver_inputs', YSE_App.models.fields.JSONTextField(
                    blank=True, help_text='Inputs and their hash, so the plan can be reproduced', null=True)),
                ('notes', models.TextField(blank=True, null=True)),
                ('approved_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name='approved_night_plans', to=settings.AUTH_USER_MODEL)),
                ('author', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='night_plans',
                    to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='nightplan_created_by',
                    to=settings.AUTH_USER_MODEL)),
                ('modified_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='nightplan_modified_by',
                    to=settings.AUTH_USER_MODEL)),
                ('night', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='plans',
                    to='YSE_App.classicalobservingdate')),
                ('parent', models.ForeignKey(
                    blank=True, help_text='The version this one revised', null=True,
                    on_delete=django.db.models.deletion.SET_NULL, related_name='revisions',
                    to='YSE_App.nightplan')),
            ],
            options={
                'ordering': ('night', 'version'),
            },
        ),
        migrations.CreateModel(
            name='PlannedBlock',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(
                    choices=[('science', 'Science'), ('standard', 'Standard'), ('telluric', 'Telluric'),
                             ('calibration', 'Calibration')],
                    max_length=16)),
                ('target_name', models.CharField(help_text='Name as shown at the telescope', max_length=64)),
                ('ra', models.FloatField(blank=True, help_text='degrees', null=True)),
                ('dec', models.FloatField(blank=True, help_text='degrees', null=True)),
                ('mag', models.FloatField(blank=True, help_text='Expected or estimated magnitude', null=True)),
                ('mag_band', models.CharField(blank=True, max_length=8, null=True)),
                ('start_ut', models.DateTimeField(blank=True, null=True)),
                ('end_ut', models.DateTimeField(blank=True, null=True)),
                ('settings', YSE_App.models.fields.JSONTextField(
                    blank=True, help_text='Per-block details: airmass, overheads, exposure source', null=True)),
                ('comments', models.TextField(blank=True, null=True)),
                ('order', models.PositiveSmallIntegerField(help_text='1, 2, ... within the plan')),
                ('requested_sequence', YSE_App.models.fields.JSONTextField(
                    blank=True, default=list, help_text='Planned exposures: [{channel, n, exptime_s}]')),
                ('time_with_overhead_s', models.PositiveIntegerField(blank=True, null=True)),
                ('setting_ut', models.DateTimeField(
                    blank=True, help_text='When the target sets below the limit', null=True)),
                ('wrap', models.CharField(blank=True, help_text='Azimuth wrap (Keck): N or S', max_length=8, null=True)),
                ('warnings', models.TextField(blank=True, null=True)),
                ('priority_at_plan', models.FloatField(
                    blank=True, help_text='Follow-up priority when planned (1-5)', null=True)),
                ('alternative_for', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='alternatives', to='YSE_App.plannedblock')),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='plannedblock_created_by',
                    to=settings.AUTH_USER_MODEL)),
                ('followup', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='planned_blocks', to='YSE_App.transientfollowup')),
                ('modified_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='plannedblock_modified_by',
                    to=settings.AUTH_USER_MODEL)),
                ('plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='blocks', to='YSE_App.nightplan')),
                ('transient', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='planned_blocks', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('plan', 'order'),
            },
        ),
        migrations.AddConstraint(
            model_name='nightplan',
            constraint=models.UniqueConstraint(fields=('night', 'version'), name='yse_nightplan_night_version'),
        ),
        migrations.AddConstraint(
            model_name='plannedblock',
            constraint=models.UniqueConstraint(fields=('plan', 'order'), name='yse_plannedblock_plan_order'),
        ),
    ]
