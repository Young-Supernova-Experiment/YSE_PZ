# Stored galactic coordinates on Transient (gal_l / gal_b, gal_b indexed) with a one-statement
# backfill, and indexes on the remaining searchable TransientPhotStat columns (#286).

from django.db import migrations, models

from YSE_App.common.galactic import backfill_galactic_coords


def fill_galactic_coords(apps, schema_editor):
    backfill_galactic_coords(apps.get_model('YSE_App', 'Transient'), all_rows=True)


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0026_facility_queue_accounting'),
    ]

    operations = [
        migrations.AddField(
            model_name='transient',
            name='gal_b',
            field=models.FloatField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='transient',
            name='gal_l',
            field=models.FloatField(blank=True, editable=False, null=True),
        ),
        migrations.AddIndex(
            model_name='transient',
            index=models.Index(fields=['gal_b'], name='yse_transient_gal_b_idx'),
        ),
        migrations.AddIndex(
            model_name='transientphotstat',
            index=models.Index(fields=['first_detected_date'], name='yse_photstat_first_det_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='transientphotstat',
            index=models.Index(fields=['last_detected_date'], name='yse_photstat_last_det_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='transientphotstat',
            index=models.Index(fields=['rise_rate'], name='yse_photstat_rise_rate_idx'),
        ),
        migrations.AddIndex(
            model_name='transientphotstat',
            index=models.Index(fields=['decay_rate'], name='yse_photstat_decay_rate_idx'),
        ),
        migrations.AddIndex(
            model_name='transientphotstat',
            index=models.Index(fields=['deepest_limit'], name='yse_photstat_deep_limit_idx'),
        ),
        migrations.RunPython(fill_galactic_coords, migrations.RunPython.noop),
    ]
