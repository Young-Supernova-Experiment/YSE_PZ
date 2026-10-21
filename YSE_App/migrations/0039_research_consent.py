"""Voluntary research consent form and per-user consent records (#744).

* ``YSE_App_researchconsentform``: versions of the consent form; ``effective_date`` is go-live.
* ``YSE_App_researchconsentrecord``: every consent, decline and withdrawal, with its time
  and the form version.

Two new tables only, so develop and experimental keep working on the shared YSE_test
database whichever code runs. Both are excluded from SQL Explorer (``_PERSONAL_DATA_TABLES``).
Named so they never collide with experimental's 0038-0050; the backport adds a merge migration.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0038_user_agreement_v2'),
    ]

    operations = [
        migrations.CreateModel(
            name='ResearchConsentForm',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.CharField(max_length=32, unique=True)),
                ('title', models.CharField(max_length=200)),
                ('body', models.TextField(help_text='HTML shown on /research-consent/.')),
                ('effective_date', models.DateField(default=django.utils.timezone.now,
                                                    help_text='Go-live date of this form.')),
                ('is_current', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'research consent form',
                'ordering': ('-effective_date', '-id'),
            },
        ),
        migrations.CreateModel(
            name='ResearchConsentRecord',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('decision', models.CharField(choices=[('consented', 'Consented'), ('declined', 'Declined'),
                                                       ('withdrawn', 'Withdrawn')], max_length=16)),
                ('decided_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('form', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='records',
                                           to='YSE_App.researchconsentform')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name='research_consent_records',
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'research consent record',
                'ordering': ('-decided_at', '-id'),
            },
        ),
        migrations.AddIndex(
            model_name='researchconsentrecord',
            index=models.Index(fields=['user', 'decided_at'], name='yse_rconsent_user_time'),
        ),
    ]
