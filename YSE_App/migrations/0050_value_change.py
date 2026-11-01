"""One timestamped change log for automated updates on transients and hosts (#737).

Creates one new table (``YSE_App_valuechange``); no existing table is touched, so code
from before this migration (the other stack on the shared ``YSE_test`` DB) is unaffected.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0049_redshift_provenance'),
    ]

    operations = [
        migrations.CreateModel(
            name='ValueChange',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category', models.CharField(choices=[('redshift', 'Redshift'), ('classification', 'Classification'),
                                                       ('host', 'Host link'), ('photo_z', 'Photo-z'),
                                                       ('override', 'Redshift override')], max_length=16)),
                ('field', models.CharField(max_length=64)),
                ('old_value', models.CharField(blank=True, max_length=255, null=True)),
                ('new_value', models.CharField(blank=True, max_length=255, null=True)),
                ('old_source', models.CharField(blank=True, default='', max_length=64)),
                ('source', models.CharField(blank=True, default='', help_text='Where the new value came from.',
                                            max_length=64)),
                ('service', models.CharField(help_text="The writer: an ExternalService slug, 'api' or 'manual'.",
                                             max_length=64)),
                ('restricted', models.BooleanField(default=False)),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('actor', models.ForeignKey(blank=True, help_text='Who made the change; empty for the system '
                                                                  '(crons, jobs, sweeps).',
                                            null=True, on_delete=django.db.models.deletion.SET_NULL,
                                            related_name='+', to=settings.AUTH_USER_MODEL)),
                ('host', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                           related_name='value_changes', to='YSE_App.host')),
                ('run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                          related_name='value_changes', to='YSE_App.externalservicerun')),
                ('transient', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                                related_name='value_changes', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('-created_at', '-pk'),
                'indexes': [models.Index(fields=['transient', 'created_at'], name='yse_valchg_transient_idx'),
                            models.Index(fields=['host', 'created_at'], name='yse_valchg_host_idx')],
            },
        ),
    ]
