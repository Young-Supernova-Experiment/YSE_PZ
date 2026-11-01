# Merge migration (develop backport, 2026-10-12): joins experimental's line
# (0031_spatial_time_indexes ... 0036_quality_mask) with develop's
# 0031_researcher_profile / 0032_seed_user_agreement (#627-#630).
# No schema or data changes.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0036_quality_mask'),
        ('YSE_App', '0032_seed_user_agreement'),
    ]

    operations = []
