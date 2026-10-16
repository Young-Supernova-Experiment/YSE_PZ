"""Observed blocks (#670, #672): the ``observed`` ("Data taken") and ``punted`` outcomes.

Choices and help text only: no SQL change (``outcome`` stays varchar(8)). Safe on the shared
database.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0040_observed_blocks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='observedblock',
            name='outcome',
            field=models.CharField(
                choices=[('success', 'Success'), ('observed', 'Data taken'), ('partial', 'Partial'),
                         ('failed', 'Failed'), ('punted', 'Punted'), ('skipped', 'Skipped')],
                max_length=8),
        ),
        migrations.AlterField(
            model_name='observedblock',
            name='reason',
            field=models.ForeignKey(
                blank=True, help_text='Required for failed, punted and skipped', null=True,
                on_delete=django.db.models.deletion.PROTECT, related_name='observed_blocks',
                to='YSE_App.outcomereason'),
        ),
    ]
