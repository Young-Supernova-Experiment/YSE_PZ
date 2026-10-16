"""Observed blocks (#670): add the ``punted`` outcome.

Choices only: no SQL change (``outcome`` stays varchar(8)). Safe on the shared database.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0040_observed_blocks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='observedblock',
            name='outcome',
            field=models.CharField(
                choices=[('success', 'Success'), ('partial', 'Partial'), ('failed', 'Failed'),
                         ('punted', 'Punted'), ('skipped', 'Skipped')],
                max_length=8),
        ),
    ]
