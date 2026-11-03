"""Spectrum pixels as arrays (#492): one row per spectrum instead of one per pixel.

New tables only (``YSE_App_transientspectrumarrays``, ``YSE_App_hostspectrumarrays``); readers use
them when present and the per-pixel rows otherwise, so it is safe on the shared database.
``manage.py convert_spectra_to_arrays`` converts existing spectra (dry run by default).
"""

from django.db import migrations, models
import django.db.models.deletion


def _arrays_fields(to):
    return [
        ('spectrum', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                                          related_name='arrays', serialize=False, to=to)),
        ('wavelength', models.BinaryField()),
        ('flux', models.BinaryField()),
        ('flux_err', models.BinaryField(blank=True, null=True)),
        ('n_pixels', models.PositiveIntegerField()),
        ('wavelength_min', models.FloatField()),
        ('wavelength_max', models.FloatField()),
        ('modified_date', models.DateTimeField(auto_now=True)),
    ]


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0055_mentorship_owner_status'),
    ]

    operations = [
        migrations.CreateModel(name='TransientSpectrumArrays', fields=_arrays_fields('YSE_App.transientspectrum'),
                               options={'abstract': False}),
        migrations.CreateModel(name='HostSpectrumArrays', fields=_arrays_fields('YSE_App.hostspectrum'),
                               options={'abstract': False}),
    ]
