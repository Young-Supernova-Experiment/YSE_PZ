# Merge migration (develop backport, 2026-10-20): joins experimental's line
# (... 0050_value_change) with develop's 0038_user_agreement_v2 ...
# 0041_research_consent_prompts (#743, #744, #746).
# No schema or data changes.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0050_value_change'),
        ('YSE_App', '0041_research_consent_prompts'),
    ]

    operations = []
