# Merge migration (develop backport, 2026-10-21): joins experimental's line
# (... 0051_merge_develop_consent) with develop's 0042_agreement_consent_wording (#766).
# No schema or data changes.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0051_merge_develop_consent'),
        ('YSE_App', '0042_agreement_consent_wording'),
    ]

    operations = []
