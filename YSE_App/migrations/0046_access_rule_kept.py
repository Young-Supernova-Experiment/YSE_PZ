"""Access rules (#648, PR 6a): a ``kept`` capacity for rules written when sharing is turned off going forward.

Choices only (no SQL on MySQL): rows are written by the application.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0045_per_point_access_rules'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dataaccessrule',
            name='granted_as',
            field=models.CharField(
                choices=[('pi', 'PI'), ('program_deputy', 'Program deputy'),
                         ('group_deputy', "PI's data-sharing deputy"), ('staff', 'Administrator'),
                         ('kept', 'Kept after a sharing change')],
                max_length=16),
        ),
    ]
