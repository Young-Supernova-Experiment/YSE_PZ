"""Bazin-extrapolated magnitude on ``TransientPhotStat`` (#271).

Four nullable columns on ``YSE_App_transientphotstat``; ``develop`` (same
shared database) never writes them, so its INSERTs are unaffected.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0032_api_writer_groups'),
    ]

    operations = [
        migrations.AddField(
            model_name='transientphotstat',
            name='extrapolated_mag',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='transientphotstat',
            name='extrapolated_band',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='YSE_App.photometricband'),
        ),
        migrations.AddField(
            model_name='transientphotstat',
            name='extrapolated_mjd',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='transientphotstat',
            name='extrapolated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
