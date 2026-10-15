# Merge migration (develop backport, 2026-10-15): joins experimental's line
# (... 0037_merge_develop_profile) with develop's 0033_group_profiles ...
# 0037_data_sharing_delegates (#440, #445-#448, #643).
# No schema or data changes.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0037_merge_develop_profile'),
        ('YSE_App', '0037_data_sharing_delegates'),
    ]

    operations = []
