"""Open Supernova Catalog import: parser, writer and command, on synthetic entries (no network)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from YSE_App.catalog_import import osc, osc_fetch
from YSE_App.catalog_import.osc_writer import EntryNotImported, Writer, preview_entry
from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.common.quality_mask import PhotFlag, SpecFlag
from YSE_App.models import (
    AlternateTransientNames,
    Instrument,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientSpecData,
    TransientSpectrum,
)
from YSE_App.models.change_log_models import ValueChange
from YSE_App.models.catalog_models import (
    CatalogEntry,
    CatalogEntrySource,
    CatalogModelFit,
    CatalogModelRealization,
    CatalogSource,
    TransientCatalogQuantity,
    TransientPhotDataCatalogInfo,
    TransientPhotometryCatalogInfo,
    TransientSpectrumCatalogInfo,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)

NAME = 'SN2099zz'

ENTRY = {
    'schema': 'https://github.com/astrocatalogs/supernovae/blob/abc1234/SCHEMA.md',
    'name': NAME,
    'sources': [
        {'name': '2099ApJ...1..1A', 'bibcode': '2099ApJ...1..1A', 'reference': 'Author et al. (2099)', 'alias': '1'},
        {'name': 'Example survey', 'url': 'https://example.invalid/survey', 'alias': '2', 'secondary': True},
        {'name': 'Model paper', 'bibcode': '2099MNRAS.2..2B', 'alias': '3'},
    ],
    'alias': [
        {'value': NAME, 'source': '1'},
        {'value': '<a id="EX99abc">EX99abc</a>', 'source': '2'},
    ],
    'ra': [{'value': '10:00:00.00', 'u_value': 'hours', 'source': '1,2'}],
    'dec': [{'value': '-30:30:00.0', 'u_value': 'degrees', 'source': '1,2'}],
    'discoverdate': [{'value': '2099/01/02.5', 'source': '2'}],
    'redshift': [
        {'value': '0.0300', 'kind': 'cmb', 'source': '1'},
        {'value': '0.0312', 'e_value': '0.0004', 'kind': 'heliocentric', 'source': '1'},
    ],
    'claimedtype': [{'value': 'Candidate', 'source': '2'}, {'value': 'Ia', 'source': '1'}],
    'ebv': [{'value': '0.05', 'e_value': '0.001', 'derived': True, 'source': '1'}],
    'maxabsmag': [{'value': '-20.1', 'derived': True, 'source': '1'}],
    'maxvisualabsmag': [{'value': '-19.3', 'derived': True, 'source': '1'}],
    'maxvisualband': [{'value': 'V', 'derived': True, 'source': '1'}],
    'maxvisualdate': [{'value': '2099/01/20', 'derived': True, 'source': '1'}],
    'host': [{'value': 'NGC 99999', 'source': '1'}],
    'hostra': [{'value': '10:00:01.00', 'u_value': 'hours', 'source': '1'}],
    'hostdec': [{'value': '-30:30:05.0', 'u_value': 'degrees', 'source': '1'}],
    'errors': [{'value': '2099ApJ...1..1A', 'kind': 'bibcode', 'extra': 'dec'}],
    'futurekey': 'an unmapped top-level value',
    'photometry': [
        {'time': '88000.5', 'u_time': 'MJD', 'band': 'B', 'magnitude': '15.20', 'e_magnitude': '0.03',
         'system': 'Vega', 'telescope': 'Nowhere 1m', 'instrument': 'NoCam', 'source': '1'},
        {'time': '88001.5', 'u_time': 'MJD', 'band': 'B', 'magnitude': '15.10', 'e_lower_magnitude': '0.02',
         'e_upper_magnitude': '0.04', 'telescope': 'Nowhere 1m', 'instrument': 'NoCam', 'source': '1',
         'includeshost': True},
        {'time': '87990.5', 'u_time': 'MJD', 'band': "r'", 'magnitude': '22.5', 'e_magnitude': '0.4',
         'fluxdensity': '3.3', 'e_fluxdensity': '1.2', 'u_fluxdensity': 'µJy', 'zeropoint': '23.9',
         'upperlimit': True, 'upperlimitsigma': '3.0', 'source': '2'},
        {'time': ['88010', '88020'], 'u_time': 'MJD', 'energy': ['0.5', '8.0'], 'u_energy': 'keV',
         'flux': '1.5e-14', 'u_flux': 'ergs/s/cm^2', 'telescope': 'Chandra', 'source': '1',
         'newphotkey': 'kept in extra'},
        {'time': '88030', 'u_time': 'MJD', 'frequency': '8.5', 'u_frequency': 'GHz', 'fluxdensity': '120',
         'e_fluxdensity': '20', 'u_fluxdensity': 'µJy', 'source': '1'},
        {'time': '88000', 'u_time': 'MJD', 'band': 'g', 'magnitude': '15.5', 'model': '1', 'realization': '1',
         'source': '3'},
        {'time': '88001', 'u_time': 'MJD', 'band': 'g', 'magnitude': '15.4', 'model': '1', 'realization': '2',
         'source': '3'},
    ],
    'spectra': [
        {'time': '88005', 'u_time': 'MJD', 'u_wavelengths': 'nm', 'u_fluxes': 'Uncalibrated',
         'data': [['400.0', '1.0'], ['400.5', '1.1'], ['401.0', 'nan']], 'instrument': 'NoSpec',
         'telescope': 'Nowhere 1m', 'deredshifted': True, 'exclude': {'above': '9000'}, 'source': '1',
         'filename': 'sn2099zz-20990106.flm'},
        {'u_wavelengths': 'Angstrom', 'u_fluxes': 'erg/s/cm^2/Angstrom', 'u_errors': 'erg/s/cm^2/Angstrom',
         'data': [['5000', '2e-16', '1e-17'], ['5005', '2.1e-16', '1e-17']], 'vacuumwavelengths': True,
         'source': '2'},
    ],
    'models': [
        {'alias': '1', 'code': 'MOSFiT', 'name': 'default', 'version': '1.0', 'source': '3',
         'score': {'kind': 'WAIC', 'value': '10.0'},
         'realizations': [{'alias': '1', 'parameters': {'texplosion': {'value': -5.0}}}, {'alias': '2'}]},
    ],
}


def entry(**changes):
    out = copy.deepcopy(ENTRY)
    out.update(changes)
    return out


class OSCParserTests(SimpleTestCase):

    def setUp(self):
        self.plan = osc.parse_entry(NAME, entry())

    def test_transient_fields(self):
        t = self.plan['transient']
        self.assertEqual(t['name'], '2099zz')
        self.assertAlmostEqual(t['ra'], 150.0)
        self.assertAlmostEqual(t['dec'], -30.5)
        self.assertEqual(t['disc_date'].isoformat(), '2099-01-02T12:00:00+00:00')
        self.assertEqual(t['disc_date_precision'], 'day')
        self.assertEqual((t['redshift'], t['redshift_err'], t['redshift_kind']), (0.0312, 0.0004, 'heliocentric'))
        self.assertEqual(t['spec_class'], 'SN Ia')
        self.assertEqual(t['mw_ebv'], 0.05)
        # The visual-band peak, not the brightest point in any band.
        self.assertEqual((t['abs_mag_peak'], t['abs_mag_peak_band']), (-19.3, 'V'))
        self.assertEqual(t['aliases'], [NAME, 'EX99abc'])
        self.assertEqual(t['host']['name'], 'NGC 99999')
        self.assertAlmostEqual(t['host']['ra'], 150.0 + 15.0 / 3600.0)

    def test_everything_is_kept(self):
        keys = {q['key'] for q in self.plan['quantities']}
        self.assertTrue({'alias', 'ra', 'dec', 'redshift', 'claimedtype', 'errors', 'maxabsmag'} <= keys)
        self.assertEqual(self.plan['extra'], {'futurekey': 'an unmapped top-level value'})
        cmb = [q for q in self.plan['quantities'] if q['key'] == 'redshift' and q['kind'] == 'cmb'][0]
        self.assertEqual((cmb['value'], cmb['value_float']), ('0.0300', 0.03))
        error = [q for q in self.plan['quantities'] if q['key'] == 'errors'][0]
        self.assertEqual(error['extra'], {'extra': 'dec'})
        self.assertEqual(len(self.plan['sources']), 3)
        self.assertEqual(self.plan['warnings'], [
            'spectrum 0 (sn2099zz-20990106.flm): 1 non-numeric pixels dropped',
            'spectrum 1 (no filename): no time; discovery/peak date used',
        ])

    def test_photometry(self):
        points = [p for s in self.plan['photometry_sets'] for p in s['points']]
        self.assertEqual(len(points), 5)
        kinds = sorted(osc.point_kind(p) for p in points)
        self.assertEqual(kinds, ['limit', 'magnitude', 'magnitude', 'radio', 'xray'])
        mag, asym = [p for p in points if p['band'] == 'B']
        self.assertEqual((mag['mag'], mag['mag_err'], mag['mag_sys']), (15.2, 0.03, 'Vega'))
        self.assertAlmostEqual(asym['mag_err'], 0.03)
        self.assertEqual(asym['quality_flags'], ['NOT_DIFFERENCED'])
        limit = [p for p in points if p['info']['upperlimit']][0]
        # A limit carries no mag_err (the light curve would count it as a detection) but its flux pair.
        self.assertIsNone(limit['mag_err'])
        self.assertEqual(limit['info']['e_magnitude'], 0.4)
        self.assertEqual((limit['flux'], limit['flux_err'], limit['flux_zero_point']), (3.3, 1.2, 23.9))
        xray = [p for p in points if osc.point_kind(p) == 'xray'][0]
        self.assertEqual((xray['mjd'], xray['info']['time_start'], xray['info']['time_end']), (88015.0, 88010.0, 88020.0))
        self.assertEqual((xray['info']['energy_low'], xray['info']['energy_high']), (0.5, 8.0))
        self.assertEqual(xray['band'], '0.5-8 keV')
        self.assertIsNone(xray['mag'])
        self.assertEqual(xray['info']['extra'], {'newphotkey': 'kept in extra'})
        radio = [p for p in points if osc.point_kind(p) == 'radio'][0]
        self.assertEqual(radio['band'], '8.5 GHz')

    def test_model_photometry_goes_with_the_model(self):
        self.assertEqual(self.plan['model_points'], 2)
        model = self.plan['models'][0]
        self.assertEqual(list(model['realizations']), ['1', '2'])
        self.assertEqual(model['realizations']['1']['photometry'][0]['magnitude'], '15.5')
        self.assertNotIn('model', model['realizations']['1']['photometry'][0])

    def test_spectra(self):
        first, second = self.plan['spectra']
        self.assertEqual([round(w, 3) for w, f, e in first['pixels']], [4000.0, 4005.0])
        self.assertEqual(sorted(first['quality_flags']), ['DEREDSHIFTED', 'NOT_FLUX_CALIBRATED'])
        self.assertEqual(first['info']['exclude'], {'above': '9000'})
        # No time: the discovery date stands in, flagged.
        self.assertTrue(second['info']['time_missing'])
        discovered = osc.parse_osc_date('2099/01/02.5')[0]
        self.assertAlmostEqual(second['mjd'], (discovered - osc.MJD_EPOCH).total_seconds() / 86400.0)
        self.assertEqual(second['wavelength_medium'], 'vacuum')
        self.assertEqual(second['pixels'][0], (5000.0, 2e-16, 1e-17))

    def test_helpers(self):
        self.assertEqual(osc.yse_name('SN1987A'), '1987A')
        self.assertEqual(osc.yse_name('ASASSN-13av'), 'ASASSN-13av')
        self.assertEqual(osc.yse_class_name('IIn'), 'SN IIn')
        self.assertEqual(osc.yse_class_name('Ic BL'), 'SN Ic-BL')
        self.assertEqual(osc.yse_class_name('TDE'), 'TDE')
        self.assertIsNone(osc.yse_class_name('Candidate'))
        self.assertIsNone(osc.yse_class_name('II?'))
        self.assertEqual(osc.parse_osc_date('1987/02')[1], 'month')
        self.assertEqual(osc.parse_osc_date('0185/12/07'), (None, None))
        self.assertAlmostEqual(osc.sexagesimal_to_degrees('-00:30:00', 'degrees', False), -0.5)
        self.assertAlmostEqual(osc.sexagesimal_to_degrees('150.5', 'degrees', True), 150.5)
        self.assertEqual(osc.time_to_mjd('2450000.5', 'JD'), 50000.0)
        self.assertEqual(osc_fetch.entry_key('<a id="LSQ12fhs">LSQ12fhs<_a>.json'), 'LSQ12fhs')
        self.assertEqual(osc_fetch.lookup({'SN2011fe': ('r', 'p')}, '2011fe'), ('r', 'p'))

    def test_plan_fields_match_the_models(self):
        def names(model):
            return {f.name for f in model._meta.get_fields()}

        self.assertLessEqual(set(osc.PHOT_INFO_FIELDS), names(TransientPhotDataCatalogInfo))
        self.assertLessEqual(set(osc.SPEC_INFO_FIELDS), names(TransientSpectrumCatalogInfo))
        self.assertLessEqual(set(osc.QUANTITY_FLOAT_FIELDS + osc.QUANTITY_TEXT_FIELDS + osc.QUANTITY_BOOL_FIELDS),
                             names(TransientCatalogQuantity))
        self.assertLessEqual(set(osc.SOURCE_FIELDS), names(CatalogSource))
        self.assertLessEqual(set(osc.MODEL_FIELDS + osc.MODEL_JSON_FIELDS), names(CatalogModelFit))


class OSCWriterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('osc_import_test')
        ensure_transient_statuses(cls.user)
        _, instrument, _ = create_instrument_stack(cls.user, obs_group_name='osc-test')
        Instrument.objects.get_or_create(name='Unknown', defaults={'telescope': instrument.telescope,
                                                                   **audit_fields(cls.user)})

    def apply(self, data=None):
        plan = osc.parse_entry(NAME, data or entry(), origin='test/%s.json' % NAME)
        return Writer(self.user).apply_entry(plan)

    def test_new_transient_and_all_data(self):
        result = self.apply()
        self.assertTrue(result['created'])
        t = Transient.objects.get(name='2099zz')
        self.assertEqual(t.status.name, 'Ignore')
        self.assertEqual(t.obs_group.name, 'OSC')
        self.assertEqual((t.redshift, t.redshift_source), (0.0312, 'OSC'))
        self.assertEqual(t.mw_ebv, 0.05)
        self.assertEqual(t.disc_date.isoformat(), '2099-01-02T12:00:00+00:00')
        self.assertEqual(t.host.name, 'NGC 99999')
        self.assertEqual(set(AlternateTransientNames.objects.filter(transient=t).values_list('name', flat=True)),
                         {NAME, 'EX99abc'})

        entry_row = CatalogEntry.objects.get(catalog='OSC', entry_name=NAME)
        self.assertEqual(entry_row.extra, {'futurekey': 'an unmapped top-level value'})
        self.assertEqual(CatalogEntrySource.objects.filter(entry=entry_row).count(), 3)
        z = TransientCatalogQuantity.objects.get(transient=t, key='redshift', kind='heliocentric')
        self.assertEqual((z.value, z.e_value), ('0.0312', 0.0004))
        self.assertEqual(list(z.sources.values_list('bibcode', flat=True)), ['2099ApJ...1..1A'])

        points = TransientPhotData.objects.filter(photometry__transient=t)
        self.assertEqual(points.count(), 5)
        self.assertEqual(TransientPhotDataCatalogInfo.objects.filter(phot_data__in=points).count(), 5)
        limit = points.get(catalog_info__upperlimit=True)
        self.assertIsNone(limit.mag_err)
        self.assertEqual((limit.flux, limit.flux_zero_point), (3.3, 23.9))
        self.assertEqual(limit.band.instrument.name, 'Unknown')
        self.assertEqual(limit.band.name, "r'")
        xray = points.get(catalog_info__energy_low=0.5)
        self.assertEqual((xray.catalog_info.flux, xray.catalog_info.u_flux), (1.5e-14, 'ergs/s/cm^2'))
        self.assertEqual(xray.catalog_info.extra, {'newphotkey': 'kept in extra'})
        self.assertEqual(points.get(catalog_info__includeshost=True).quality_mask, int(PhotFlag.NOT_DIFFERENCED))
        for header in TransientPhotometry.objects.filter(transient=t):
            self.assertEqual(list(header.groups.values_list('name', flat=True)), [PUBLIC_COLLABORATION_GROUP_NAME])
            self.assertEqual(header.obs_group.name, 'OSC')
            self.assertTrue(header.reference.startswith('OSC'))
            self.assertTrue(TransientPhotometryCatalogInfo.objects.filter(photometry=header, entry=entry_row).exists())

        spectra = TransientSpectrum.objects.filter(transient=t).order_by('obs_date')
        self.assertEqual(spectra.count(), 2)
        first = spectra.get(catalog_info__filename='sn2099zz-20990106.flm')
        self.assertEqual(TransientSpecData.objects.filter(spectrum=first).count(), 2)
        self.assertTrue(first.quality_mask & int(SpecFlag.DEREDSHIFTED))
        self.assertTrue(first.quality_mask & int(SpecFlag.NOT_FLUX_CALIBRATED))
        self.assertTrue(first.quality_mask & int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN))
        self.assertEqual(first.catalog_info.exclude, {'above': '9000'})
        self.assertEqual(list(first.groups.values_list('name', flat=True)), [PUBLIC_COLLABORATION_GROUP_NAME])
        self.assertTrue(spectra.get(catalog_info__time_missing=True).catalog_info.vacuumwavelengths)

        fit = CatalogModelFit.objects.get(entry=entry_row)
        self.assertEqual((fit.code, fit.score), ('MOSFiT', {'kind': 'WAIC', 'value': '10.0'}))
        real = CatalogModelRealization.objects.get(model_fit=fit, alias='1')
        self.assertEqual(real.parameters, {'texplosion': {'value': -5.0}})
        self.assertEqual(len(real.photometry), 1)

    def test_reimport_replaces_its_own_rows(self):
        self.apply()
        t = Transient.objects.get(name='2099zz')
        before = (TransientPhotData.objects.filter(photometry__transient=t).count(),
                  TransientSpectrum.objects.filter(transient=t).count(),
                  TransientCatalogQuantity.objects.filter(transient=t).count(),
                  AlternateTransientNames.objects.filter(transient=t).count(),
                  CatalogSource.objects.count())
        result = self.apply()
        self.assertFalse(result['created'])
        self.assertEqual(result['matched_by'], 'imported before')
        after = (TransientPhotData.objects.filter(photometry__transient=t).count(),
                 TransientSpectrum.objects.filter(transient=t).count(),
                 TransientCatalogQuantity.objects.filter(transient=t).count(),
                 AlternateTransientNames.objects.filter(transient=t).count(),
                 CatalogSource.objects.count())
        self.assertEqual(before, after)
        self.assertEqual(CatalogEntry.objects.count(), 1)

    def test_existing_transient_keeps_its_values(self):
        existing = create_minimal_transient(self.user, name='2099zz', ra=150.0, dec=-30.5, status_name='Watch')
        existing.redshift, existing.redshift_source = 0.5, 'NED'
        existing.save()
        result = self.apply()
        self.assertFalse(result['created'])
        existing.refresh_from_db()
        self.assertEqual(existing.status.name, 'Watch')
        # Redshift precedence: NED outranks another catalogue.
        self.assertEqual((existing.redshift, existing.redshift_source), (0.5, 'NED'))
        self.assertEqual(existing.mw_ebv, 0.05)  # was empty
        self.assertEqual(Transient.objects.filter(name='2099zz').count(), 1)

    def test_redshift_without_source_is_replaced(self):
        existing = create_minimal_transient(self.user, name='2099zz', ra=150.0, dec=-30.5)
        existing.redshift = 0.5
        existing.save()
        self.apply()
        existing.refresh_from_db()
        self.assertEqual((existing.redshift, existing.redshift_source), (0.0312, 'OSC'))
        self.assertTrue(ValueChange.objects.filter(transient=existing, field='redshift', service='osc-import').exists())

    def test_match_by_alias_and_by_position(self):
        by_alias = create_minimal_transient(self.user, name='EX99abc', ra=1.0, dec=1.0)
        self.assertEqual(self.apply()['transient'], by_alias.name)
        CatalogEntry.objects.all().delete()
        other = entry(alias=[], name='SN2099yy')
        other_plan = osc.parse_entry('SN2099yy', other)
        near = create_minimal_transient(self.user, name='nearby-candidate', ra=150.0 + 0.5 / 3600, dec=-30.5)
        self.assertEqual(preview_entry(other_plan)['transient'], near)
        self.assertIsNone(preview_entry(other_plan, match_radius=0)['transient'])

    def test_failed_entry_does_not_poison_the_next(self):
        writer = Writer(self.user)
        bad = osc.parse_entry(NAME, entry(ra=[], dec=[]))
        bad['photometry_sets'] = []  # fail after the shared rows are looked up
        writer.obs_group()
        with self.assertRaises(EntryNotImported):
            writer.apply_entry(bad)
        result = writer.apply_entry(osc.parse_entry(NAME, entry()))
        self.assertTrue(result['created'])

    def test_entry_without_position_or_match_is_refused(self):
        data = entry(ra=[], dec=[])
        with self.assertRaises(EntryNotImported):
            self.apply(data)
        self.assertFalse(Transient.objects.filter(name='2099zz').exists())
        self.assertFalse(CatalogEntry.objects.exists())


class ImportOSCCommandTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('osc_import_cmd')
        ensure_transient_statuses(cls.user)
        _, instrument, _ = create_instrument_stack(cls.user, obs_group_name='osc-cmd')
        Instrument.objects.get_or_create(name='Unknown', defaults={'telescope': instrument.telescope,
                                                                   **audit_fields(cls.user)})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, NAME + '.json')
        with open(self.path, 'w') as handle:
            json.dump({NAME: ENTRY}, handle)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command('import_osc', self.tmp.name, stdout=out)
        self.assertIn('would create 2099zz', out.getvalue())
        self.assertIn('Dry run: 1 entries', out.getvalue())
        self.assertFalse(Transient.objects.filter(name='2099zz').exists())
        self.assertFalse(CatalogEntry.objects.exists())

    def test_apply(self):
        out = StringIO()
        call_command('import_osc', self.path, '--apply', '--user', self.user.username, stdout=out)
        self.assertIn('created 2099zz', out.getvalue())
        self.assertTrue(CatalogEntry.objects.filter(entry_name=NAME).exists())
