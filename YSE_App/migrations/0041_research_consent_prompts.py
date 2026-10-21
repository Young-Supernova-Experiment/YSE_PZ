"""When the research consent form was offered to users who have not answered (reminders, #744).

One new table, ``YSE_App_researchconsentprompt``, so develop and experimental keep working on the
shared YSE_test database whichever code runs. Excluded from SQL Explorer
(``_PERSONAL_DATA_TABLES``). Named so it never collides with experimental's
``0041_observed_block_outcomes``; the backport adds a merge migration.

Backfill: users who accepted agreement v2 before this migration were already shown the form
once (PR #745), so each one without a consent record gets a prompt row dated at that acceptance.
Re-running is harmless (existing rows are kept).
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def backfill(apps, schema_editor):
    Prompt = apps.get_model("YSE_App", "ResearchConsentPrompt")
    Acceptance = apps.get_model("YSE_App", "AgreementAcceptance")
    Record = apps.get_model("YSE_App", "ResearchConsentRecord")
    answered = set(Record.objects.values_list("user_id", flat=True))
    have = set(Prompt.objects.values_list("user_id", flat=True))
    rows = []
    for user_id, accepted_at in Acceptance.objects.filter(agreement__version="2").values_list("user_id", "accepted_at"):
        if user_id in answered or user_id in have:
            continue
        have.add(user_id)
        rows.append(Prompt(user_id=user_id, first_prompted_at=accepted_at, last_prompted_at=accepted_at,
                           prompt_count=1, logins_since_first=0))
    Prompt.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0040_seed_research_consent'),
    ]

    operations = [
        migrations.CreateModel(
            name='ResearchConsentPrompt',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('first_prompted_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_prompted_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('prompt_count', models.PositiveIntegerField(default=1)),
                ('logins_since_first', models.PositiveIntegerField(default=0)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                              related_name='research_consent_prompt',
                                              to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'research consent prompt',
            },
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
