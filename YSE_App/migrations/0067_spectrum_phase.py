"""Computed spectral phase (#848; YSE_App/models/spectral_phase_models.py).

One new table only: no existing table, column or row changes. Its foreign keys to
``YSE_App_transientspectrum`` and ``YSE_App_transient`` (tables develop knows) get database-level
``ON DELETE CASCADE`` in 0068 (``YSE_App/common/fk_on_delete.py``), so develop's deletes of a
spectrum or a transient on the shared database still succeed. Nothing is filled here:
``manage.py backfill_spectral_phases --apply`` writes rows for existing spectra.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0066_selection_fk_on_delete'),
    ]

    operations = [
        migrations.CreateModel(
            name='SpectrumPhase',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reference', models.CharField(choices=[('bazin_rest', 'Bazin peak, rest frame'),
                                                        ('bazin_obs', 'Bazin peak, observer frame'),
                                                        ('salt_t0', 'SALT t0, rest frame'),
                                                        ('discovery', 'Days since discovery')], max_length=16)),
                ('is_default', models.BooleanField(default=False)),
                ('phase', models.FloatField()),
                ('frame', models.CharField(choices=[('rest', 'rest frame'), ('observer', 'observer frame')],
                                           max_length=8)),
                ('redshift', models.FloatField(blank=True, null=True)),
                ('reference_mjd', models.FloatField()),
                ('spectrum_mjd', models.FloatField()),
                ('computed_date', models.DateTimeField(auto_now=True)),
                ('spectrum', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='phases',
                                               to='YSE_App.transientspectrum')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='spectrum_phases', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('spectrum_id', 'reference'),
                'indexes': [models.Index(fields=['reference', 'phase'], name='yse_specphase_ref_phase_idx'),
                            models.Index(fields=['is_default', 'phase'], name='yse_specphase_default_idx'),
                            models.Index(fields=['transient', 'reference'], name='yse_specphase_transient_idx')],
                'constraints': [models.UniqueConstraint(fields=('spectrum', 'reference'),
                                                        name='yse_specphase_spec_ref_uniq')],
            },
        ),
    ]
