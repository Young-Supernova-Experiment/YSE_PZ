"""Tables for data imported from external catalogues, first the Open Supernova Catalog (#887-#889, #892-#895).

New tables only (see YSE_App/models/catalog_models.py): no existing table or column changes,
so it is safe on the shared database. Nothing is imported by the migration; that is
``manage.py import_osc``.
"""

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _audit(model_name):
    """The BaseModel columns."""
    return [
        ('created_date', models.DateTimeField(auto_now_add=True)),
        ('modified_date', models.DateTimeField(auto_now=True)),
        ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                         related_name='%s_created_by' % model_name, to=settings.AUTH_USER_MODEL)),
        ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                          related_name='%s_modified_by' % model_name, to=settings.AUTH_USER_MODEL)),
    ]


def _id():
    return ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'))


def _float(name):
    return (name, models.FloatField(blank=True, null=True))


def _char(name, length):
    return (name, models.CharField(blank=True, max_length=length, null=True))


def _text(name):
    return (name, models.TextField(blank=True, null=True))


def _bool(name):
    return (name, models.BooleanField(blank=True, null=True))


def _json(name):
    return (name, YSE_App.models.fields.JSONTextField(blank=True, null=True))


TNS_STATUSES = ('pending', 'updated', 'not_found', 'conflict', 'skipped', 'error')
DECISIONS = ('kept_existing', 'superseded_existing', 'review')


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0056_spectrum_arrays'),
    ]

    operations = [
        migrations.CreateModel(
            name='CatalogSource',
            fields=[
                _id(),
                ('name', models.CharField(max_length=512)),
                ('bibcode', models.CharField(blank=True, db_index=True, max_length=32, null=True)),
                _char('doi', 255),
                _char('arxivid', 64),
                _char('url', 1024),
                _char('reference', 512),
                _text('acknowledgment'),
                _bool('secondary'),
                _bool('private'),
                _bool('refereed'),
                _json('extra'),
            ] + _audit('catalogsource'),
        ),
        migrations.CreateModel(
            name='CatalogEntry',
            fields=[
                _id(),
                ('catalog', models.CharField(max_length=32)),
                ('entry_name', models.CharField(max_length=128)),
                _char('schema', 512),
                _char('origin', 512),
                _char('origin_version', 64),
                _json('extra'),
                ('created_transient', models.BooleanField(default=False)),
                _json('import_values'),
                ('tns_status', models.CharField(blank=True, choices=[(s, s) for s in TNS_STATUSES], db_index=True,
                                                max_length=16, null=True)),
                _char('tns_name', 64),
                ('tns_checked', models.DateTimeField(blank=True, null=True)),
                _text('tns_note'),
                _json('host_check'),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='catalog_entries', to='YSE_App.transient')),
            ] + _audit('catalogentry'),
        ),
        migrations.CreateModel(
            name='CatalogEntrySource',
            fields=[
                _id(),
                ('alias', models.CharField(max_length=16)),
                ('entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name='entry_sources', to='YSE_App.catalogentry')),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name='entry_sources', to='YSE_App.catalogsource')),
            ],
        ),
        migrations.AddField(
            model_name='catalogentry',
            name='sources',
            field=models.ManyToManyField(blank=True, through='YSE_App.CatalogEntrySource', to='YSE_App.CatalogSource'),
        ),
        migrations.AddConstraint(
            model_name='catalogentry',
            constraint=models.UniqueConstraint(fields=('catalog', 'entry_name'), name='yse_catalog_entry_unique'),
        ),
        migrations.AddConstraint(
            model_name='catalogentrysource',
            constraint=models.UniqueConstraint(fields=('entry', 'alias'), name='yse_catalog_entry_source_alias'),
        ),
        migrations.CreateModel(
            name='CatalogModelFit',
            fields=[
                _id(),
                _char('alias', 16),
                _char('code', 128),
                _char('name', 256),
                _char('version', 64),
                _char('date', 32),
                _text('description'),
                _char('steps', 32),
                _json('convergence'),
                _json('score'),
                _json('setup'),
                ('n_realizations', models.PositiveIntegerField(blank=True, null=True)),
                _json('parameter_summary'),
                _json('light_curve'),
                _json('extra'),
                ('entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name='model_fits', to='YSE_App.catalogentry')),
                ('sources', models.ManyToManyField(blank=True, related_name='model_fits', to='YSE_App.CatalogSource')),
            ] + _audit('catalogmodelfit'),
        ),
        migrations.CreateModel(
            name='TransientCatalogQuantity',
            fields=[
                _id(),
                ('key', models.CharField(max_length=32)),
                _text('value'),
                _float('value_float'),
                _float('e_value'),
                _float('e_lower_value'),
                _float('e_upper_value'),
                _char('u_value', 64),
                _char('u_e_value', 64),
                _char('kind', 128),
                _bool('derived'),
                _bool('upperlimit'),
                _bool('lowerlimit'),
                _float('probability'),
                _text('description'),
                _json('correlations'),
                _char('realization', 16),
                _json('extra'),
                ('entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name='quantities', to='YSE_App.catalogentry')),
                ('model_fit', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                                related_name='quantities', to='YSE_App.catalogmodelfit')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='catalog_quantities', to='YSE_App.transient')),
                ('sources', models.ManyToManyField(blank=True, related_name='quantities', to='YSE_App.CatalogSource')),
            ] + _audit('transientcatalogquantity'),
            options={
                'indexes': [models.Index(fields=['transient', 'key'], name='yse_catalog_qty_key_idx')],
            },
        ),
        migrations.CreateModel(
            name='TransientPhotometryCatalogInfo',
            fields=[
                _id(),
                ('entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name='photometry_sets', to='YSE_App.catalogentry')),
                ('photometry', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                    related_name='catalog_info', to='YSE_App.transientphotometry')),
                ('sources', models.ManyToManyField(blank=True, related_name='photometry_sets', to='YSE_App.CatalogSource')),
            ] + _audit('transientphotometrycataloginfo'),
        ),
        migrations.CreateModel(
            name='TransientPhotDataCatalogInfo',
            fields=[
                ('phot_data', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                                                   related_name='catalog_info', serialize=False,
                                                   to='YSE_App.transientphotdata')),
                _float('time_start'), _float('time_end'), _float('e_time'), _float('e_lower_time'),
                _float('e_upper_time'), _char('u_time', 16),
                _char('band', 64), _char('bandset', 64), _char('system', 64), _char('telescope', 128),
                _char('instrument', 128), _char('observatory', 128), _char('survey', 128), _char('mode', 128),
                _text('observer'), _text('reducer'), _text('description'),
                _float('e_magnitude'), _float('e_lower_magnitude'), _float('e_upper_magnitude'), _float('zeropoint'),
                _bool('upperlimit'), _bool('lowerlimit'), _float('upperlimitsigma'),
                _float('countrate'), _float('e_countrate'), _float('e_lower_countrate'), _float('e_upper_countrate'),
                _char('u_countrate', 32),
                _float('flux'), _float('e_flux'), _float('e_lower_flux'), _float('e_upper_flux'), _char('u_flux', 32),
                _float('unabsorbedflux'), _float('e_unabsorbedflux'), _float('e_lower_unabsorbedflux'),
                _float('e_upper_unabsorbedflux'),
                _float('fluxdensity'), _float('e_fluxdensity'), _float('e_lower_fluxdensity'),
                _float('e_upper_fluxdensity'), _char('u_fluxdensity', 32),
                _float('luminosity'), _float('e_luminosity'), _float('e_lower_luminosity'),
                _float('e_upper_luminosity'), _char('u_luminosity', 32),
                _float('energy_low'), _float('energy_high'), _char('u_energy', 32),
                _float('frequency'), _char('u_frequency', 32), _float('wavelength'), _char('u_wavelength', 32),
                _float('photonindex'), _float('nhmw'), _float('exposuretime'), _char('u_exposuretime', 32),
                _float('offaxisangle'), _char('u_offaxisangle', 32), _float('extractionradius'),
                _char('u_extractionradius', 32), _float('airmass'),
                _bool('kcorrected'), _bool('scorrected'), _bool('mcorrected'), _bool('synthetic'), _bool('simulated'),
                _bool('restframe'), _bool('host'), _bool('includeshost'), _bool('hostnhcorr'),
                _json('extra'),
            ],
        ),
        migrations.CreateModel(
            name='TransientSpectrumCatalogInfo',
            fields=[
                _id(),
                _bool('time_missing'), _float('time_start'), _float('time_end'), _float('e_time'),
                _float('e_lower_time'), _float('e_upper_time'), _char('u_time', 16),
                _char('u_wavelengths', 64), _char('u_fluxes', 64), _char('u_errors', 64),
                _char('telescope', 128), _char('instrument', 128), _char('observatory', 128), _char('survey', 128),
                _text('observer'), _text('reducer'), _char('reduction', 128), _char('filename', 512),
                _float('airmass'), _float('snr'),
                _bool('deredshifted'), _bool('dereddened'), _bool('vacuumwavelengths'), _bool('host'),
                _bool('includeshost'),
                _json('exclude'),
                _char('realization', 16),
                _json('extra'),
                ('entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name='spectra', to='YSE_App.catalogentry')),
                ('model_fit', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                related_name='+', to='YSE_App.catalogmodelfit')),
                ('spectrum', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                  related_name='catalog_info', to='YSE_App.transientspectrum')),
                ('sources', models.ManyToManyField(blank=True, related_name='spectra', to='YSE_App.CatalogSource')),
            ] + _audit('transientspectrumcataloginfo'),
        ),
        migrations.CreateModel(
            name='CatalogDataMatch',
            fields=[
                _id(),
                ('decision', models.CharField(choices=[(d, d) for d in DECISIONS], max_length=24)),
                ('weak', models.BooleanField(default=False)),
                _float('time_difference'),
                ('previous_quality_mask', models.PositiveBigIntegerField(blank=True, null=True)),
                _json('catalog_record'),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('catalog_phot_data', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                                        related_name='+', to='YSE_App.transientphotdata')),
                ('catalog_spectrum', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                                       related_name='+', to='YSE_App.transientspectrum')),
                ('entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='matches',
                                            to='YSE_App.catalogentry')),
                ('existing_phot_data', models.ForeignKey(blank=True, null=True,
                                                         on_delete=django.db.models.deletion.CASCADE,
                                                         related_name='catalog_matches', to='YSE_App.transientphotdata')),
                ('existing_spectrum', models.ForeignKey(blank=True, null=True,
                                                        on_delete=django.db.models.deletion.CASCADE,
                                                        related_name='catalog_matches', to='YSE_App.transientspectrum')),
            ],
        ),
        migrations.CreateModel(
            name='CatalogImportAttempt',
            fields=[
                _id(),
                ('catalog', models.CharField(max_length=32)),
                ('entry_key', models.CharField(max_length=128)),
                _char('origin', 512),
                ('stage', models.CharField(choices=[('download', 'download'), ('parse', 'parse'), ('ingest', 'ingest')],
                                           max_length=16)),
                ('status', models.CharField(choices=[('ok', 'ok'), ('failed', 'failed')], db_index=True, max_length=8)),
                ('attempts', models.PositiveIntegerField(default=0)),
                _text('error'),
                ('first_attempt', models.DateTimeField(auto_now_add=True)),
                ('last_attempt', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name='catalogimportattempt',
            constraint=models.UniqueConstraint(fields=('catalog', 'entry_key'), name='yse_catalog_attempt_unique'),
        ),
        migrations.CreateModel(
            name='CatalogInstrumentAlias',
            fields=[
                _id(),
                ('catalog', models.CharField(default='OSC', max_length=32)),
                ('telescope', models.CharField(blank=True, default='', max_length=128)),
                ('instrument', models.CharField(blank=True, default='', max_length=128)),
                _float('mjd_min'),
                _float('mjd_max'),
                ('yse_instrument', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                     related_name='catalog_aliases', to='YSE_App.instrument')),
            ] + _audit('cataloginstrumentalias'),
            options={
                'indexes': [models.Index(fields=['catalog', 'telescope', 'instrument'],
                                         name='yse_catalog_inst_alias_idx')],
            },
        ),
    ]
