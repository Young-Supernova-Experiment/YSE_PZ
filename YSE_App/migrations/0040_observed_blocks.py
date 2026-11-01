"""Observed blocks (#666): the ObservedBlock and OutcomeReason tables of the decision-trail draft.

* New table ``YSE_App_outcomereason``, seeded with the draft's reason codes.
* New table ``YSE_App_observedblock``: one target's outcome on one night.

Safe on the shared YSE_test database: two new tables and rows in one of them, no change to
existing tables. The seed is idempotent (get_or_create by code) and skipped on a database
without users (the service seeds on first use there); its reverse does nothing.
"""

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

# code, label, category, applies_to
REASONS = [
    ("weather", "Weather", "weather", ["block", "event"]),
    ("instrument", "Instrument problem", "instrument", ["block", "event"]),
    ("telescope", "Telescope problem", "telescope", ["block", "event"]),
    ("ran_out_of_time", "Ran out of time", "time", ["block", "consideration", "event"]),
    ("deprioritized", "Deprioritized", "priority", ["block", "consideration", "event"]),
    ("low_sn", "Low S/N", "data", ["block", "event"]),
    ("wrong_object", "Wrong object", "target", ["block", "event"]),
    ("not_visible", "Not visible / not found", "target", ["block", "consideration", "event"]),
    ("too_faint", "Too faint", "target", ["block", "consideration", "event"]),
    ("classified_elsewhere", "Classified elsewhere", "priority", ["block", "consideration", "event"]),
    ("superseded", "Superseded", "priority", ["block", "consideration", "event"]),
    ("other", "Other", "other", ["block", "consideration", "event"]),
]


def seed_reasons(apps, schema_editor):
    OutcomeReason = apps.get_model("YSE_App", "OutcomeReason")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    actor = User.objects.filter(is_superuser=True).order_by("id").first() or User.objects.order_by("id").first()
    if actor is None:
        # A fresh database without users (e.g. the test database): the audit fields need a
        # user, so services.observations.ensure_reasons() seeds on first use instead.
        return
    for code, label, category, applies_to in REASONS:
        OutcomeReason.objects.get_or_create(code=code, defaults={
            "label": label, "category": category, "applies_to": applies_to, "active": True,
            "created_by": actor, "modified_by": actor,
        })


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0039_night_plans'),
    ]

    operations = [
        migrations.CreateModel(
            name='OutcomeReason',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('code', models.SlugField(help_text='Stable code', max_length=32, unique=True)),
                ('label', models.CharField(help_text='Display text', max_length=64)),
                ('category', models.CharField(
                    choices=[('weather', 'Weather'), ('instrument', 'Instrument'), ('telescope', 'Telescope'),
                             ('time', 'Time'), ('priority', 'Priority'), ('target', 'Target'), ('data', 'Data'),
                             ('other', 'Other')],
                    max_length=16)),
                ('applies_to', YSE_App.models.fields.JSONTextField(
                    blank=True, default=list, help_text='Where it may be used: subset of [block, consideration, event]')),
                ('active', models.BooleanField(default=True, help_text='Retired reasons stay for history')),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='outcomereason_created_by',
                    to=settings.AUTH_USER_MODEL)),
                ('modified_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='outcomereason_modified_by',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('label',),
            },
        ),
        migrations.CreateModel(
            name='ObservedBlock',
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
                ('settings', YSE_App.models.fields.JSONTextField(blank=True, help_text='Per-block details', null=True)),
                ('comments', models.TextField(blank=True, null=True)),
                ('outcome', models.CharField(
                    choices=[('success', 'Success'), ('partial', 'Partial'), ('failed', 'Failed'),
                             ('skipped', 'Skipped')],
                    max_length=8)),
                ('achieved_sn', models.FloatField(blank=True, null=True)),
                ('seeing', models.FloatField(blank=True, help_text='arcsec', null=True)),
                ('source', models.CharField(
                    choices=[('tonight', 'Tonight page'), ('slack', 'Slack'), ('astroscheduler', 'AstroScheduler'),
                             ('import', 'Import'), ('admin', 'Admin')],
                    max_length=16)),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='observedblock_created_by',
                    to=settings.AUTH_USER_MODEL)),
                ('facility_request', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='observed_blocks', to='YSE_App.facilityrequest')),
                ('followup', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='observed_blocks', to='YSE_App.transientfollowup')),
                ('modified_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='observedblock_modified_by',
                    to=settings.AUTH_USER_MODEL)),
                ('night', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='observed_blocks',
                    to='YSE_App.classicalobservingdate')),
                ('planned_block', models.ForeignKey(
                    blank=True, help_text='The plan entry it executed; empty = added at the telescope', null=True,
                    on_delete=django.db.models.deletion.SET_NULL, related_name='observed_blocks',
                    to='YSE_App.plannedblock')),
                ('reason', models.ForeignKey(
                    blank=True, help_text='Required unless the outcome is success', null=True,
                    on_delete=django.db.models.deletion.PROTECT, related_name='observed_blocks',
                    to='YSE_App.outcomereason')),
                ('recorded_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='recorded_observed_blocks',
                    to=settings.AUTH_USER_MODEL)),
                ('transient', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='observed_blocks', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('night', 'end_ut', 'id'),
            },
        ),
        migrations.RunPython(seed_reasons, migrations.RunPython.noop),
    ]
