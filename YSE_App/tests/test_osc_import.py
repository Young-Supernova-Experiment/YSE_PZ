"""Open Supernova Catalog import (#887-#889, #892-#895): parser, matching, writer, TNS check and commands.

Synthetic entries only; no network (TNS is a fake client).
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from YSE_App.catalog_import import instruments, matching, osc, osc_fetch, osc_tns
from YSE_App.catalog_import.osc_writer import EntryNotImported, Writer, preview_entry
from YSE_App.common import spectrum_arrays
from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.common.quality_mask import PhotFlag, SpecFlag
from YSE_App.models import (
    AlternateTransientNames,
    Instrument,
    Transient,
    TransientPhotData,
    TransientClass,
    TransientPhotometry,
    TransientSpecData,
    TransientSpectrum,
)
from YSE_App.models.catalog_models import (
    CatalogDataMatch,
    CatalogEntry,
    CatalogEntrySource,
    CatalogImportAttempt,
    CatalogInstrumentAlias,
    CatalogModelFit,
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

    def test_model_outputs_not_chains(self):
        self.assertEqual(self.plan['model_points'], 2)
        model = self.plan['models'][0]
        self.assertNotIn('realizations', model)
        self.assertEqual(model['n_realizations'], 2)
        self.assertEqual(model['parameter_summary']['texplosion']['median'], -5.0)
        curve = model['light_curve'][0]
        self.assertEqual((curve['band'], curve['time'], curve['median'], curve['n']),
                         ('g', [88000.0, 88001.0], [15.5, 15.4], [1, 1]))

    def test_refereed_distance_and_tns_names(self):
        refereed = {s['alias']: s['refereed'] for s in self.plan['sources']}
        self.assertEqual(refereed, {'1': True, '2': None, '3': True})
        self.assertFalse(osc.is_refereed('2099ATel.1234....1A'))
        t = self.plan['transient']
        self.assertAlmostEqual(t['distance_mpc'], 0.0312 * osc.SPEED_OF_LIGHT_KMS / osc.HUBBLE_CONSTANT)
        self.assertEqual(t['distance_basis'], 'redshift')
        self.assertEqual(t['iau_names'], ['2099zz'])
        close = osc.parse_entry(NAME, entry(lumdist=[{'value': '15.0', 'u_value': 'Mpc', 'source': '1'}]))
        self.assertEqual((close['transient']['distance_mpc'], close['transient']['distance_basis']), (15.0, 'lumdist'))

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
        self.assertLessEqual(set(osc.MODEL_FIELDS + osc.MODEL_JSON_FIELDS + osc.MODEL_OUTPUT_FIELDS),
                             names(CatalogModelFit))


class MatchingTests(SimpleTestCase):

    def test_bands(self):
        self.assertEqual(matching.normalize_band("r'"), 'r')
        self.assertEqual(matching.normalize_band('W1'), 'UVW1')
        self.assertNotEqual(matching.normalize_band('r'), matching.normalize_band('R'))

    def test_photometry(self):
        O = matching.Obs
        existing = [O('a', 100.000, 5, 'B', 15.0), O('b', 100.015, 5, 'B', 15.1), O('c', 100.0, 6, 'V', 15.0),
                    O('u', 200.0, 1, 'r', 18.0)]
        new = [O(1, 100.001, 5, 'B', 15.3),       # strong match, nearest is 'a'
               O(2, 100.014, 5, 'B', 14.0),       # strong: values may differ with reduction -> 'b'
               O(3, 100.0, 7, 'V', 15.0),         # another known instrument: no match
               O(4, 200.005, 1, 'r', 19.0),       # weak (Unknown), magnitudes too far apart
               O(5, 200.005, 1, 'r', 18.2),       # weak match
               O(6, 100.5, 5, 'B', 15.0)]         # too late
        found = {m.new.key: (m.existing.key, m.weak) for m in matching.match_photometry(new, existing, unknown_id=1)}
        self.assertEqual(found, {1: ('a', False), 2: ('b', False), 5: ('u', True)})

    def test_spectra_and_decisions(self):
        O = matching.Obs
        existing = [O('s', 100.0, 5, wavelength_min=3500, wavelength_max=9000)]
        found = matching.match_spectra([O(1, 100.3, 5, wavelength_min=8500, wavelength_max=10500),
                                        O(2, 100.3, 5, wavelength_min=4000, wavelength_max=9500)], existing)
        self.assertEqual([(m.new.key, m.existing.key) for m in found], [(2, 's')])
        self.assertEqual(matching.decide(None, True), matching.NEW)
        self.assertEqual(matching.decide(found[0], False), matching.KEPT_EXISTING)
        self.assertEqual(matching.decide(found[0], True), matching.SUPERSEDED_EXISTING)
        found[0].weak = True
        self.assertEqual(matching.decide(found[0], True), matching.REVIEW)




def _setup_world(test_class, username):
    test_class.user = create_test_user(username)
    ensure_transient_statuses(test_class.user)
    audit = audit_fields(test_class.user)
    _, instrument, _ = create_instrument_stack(test_class.user, obs_group_name=username)
    test_class.telescope = instrument.telescope
    test_class.unknown, _ = Instrument.objects.get_or_create(name='Unknown', defaults={'telescope': instrument.telescope,
                                                                                       **audit})
    test_class.nocam, _ = Instrument.objects.get_or_create(name='NoCam', defaults={'telescope': instrument.telescope,
                                                                                   **audit})
    for name in ('SN Ia', 'SN II'):
        TransientClass.objects.get_or_create(name=name, defaults=audit)


def _existing_point(user, transient, instrument, mjd, band='B', mag=15.25):
    from YSE_App.models import ObservationGroup, PhotometricBand

    audit = audit_fields(user)
    group, _ = ObservationGroup.objects.get_or_create(name='YSE-test', defaults=audit)
    header = TransientPhotometry.objects.create(transient=transient, instrument=instrument, obs_group=group, **audit)
    band_row, _ = PhotometricBand.objects.get_or_create(instrument=instrument, name=band, defaults=audit)
    return TransientPhotData.objects.create(photometry=header, band=band_row, obs_date=osc.mjd_to_datetime(mjd),
                                            mag=mag, mag_err=0.05, **audit)


class OSCWriterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _setup_world(cls, 'osc_import_test')

    def apply(self, data=None, name=NAME):
        plan = osc.parse_entry(name, data or entry(), origin='test/%s.json' % name)
        return Writer(self.user).apply_entry(plan)

    def test_new_transient_takes_everything_from_the_osc(self):
        result = self.apply()
        self.assertTrue(result['created'])
        t = Transient.objects.get(name='2099zz')
        self.assertEqual(t.status.name, 'Ignore')
        self.assertEqual(t.obs_group.name, 'OSC')
        self.assertEqual((t.redshift, t.redshift_source), (0.0312, 'OSC'))
        self.assertEqual(t.mw_ebv, 0.05)
        self.assertEqual(t.best_spec_class.name, 'SN Ia')
        self.assertEqual(t.disc_date.isoformat(), '2099-01-02T12:00:00+00:00')
        self.assertEqual(set(AlternateTransientNames.objects.filter(transient=t).values_list('name', flat=True)),
                         {NAME, 'EX99abc'})

        entry_row = CatalogEntry.objects.get(catalog='OSC', entry_name=NAME)
        self.assertTrue(entry_row.created_transient)
        self.assertEqual(entry_row.tns_status, CatalogEntry.TNS_PENDING)
        self.assertEqual(entry_row.import_values['iau_names'], ['2099zz'])
        self.assertEqual(entry_row.extra, {'futurekey': 'an unmapped top-level value'})
        self.assertEqual(CatalogEntrySource.objects.filter(entry=entry_row).count(), 3)
        self.assertTrue(CatalogSource.objects.get(bibcode='2099ApJ...1..1A').refereed)
        z = TransientCatalogQuantity.objects.get(transient=t, key='redshift', kind='heliocentric')
        self.assertEqual((z.value, z.e_value), ('0.0312', 0.0004))

        # About 134 Mpc: the OSC host is not linked; Prost/Blast decide (#893).
        self.assertIsNone(t.host)
        self.assertEqual(entry_row.host_check['status'], 'prost_blast_precedence')

        points = TransientPhotData.objects.filter(photometry__transient=t)
        self.assertEqual(points.count(), 5)
        self.assertEqual(TransientPhotDataCatalogInfo.objects.filter(phot_data__in=points).count(), 5)
        limit = points.get(catalog_info__upperlimit=True)
        self.assertIsNone(limit.mag_err)
        self.assertEqual((limit.flux, limit.flux_zero_point), (3.3, 23.9))
        self.assertEqual(limit.band.instrument.name, 'Unknown')
        self.assertEqual(points.filter(band__name='B').first().photometry.instrument, self.nocam)
        xray = points.get(catalog_info__energy_low=0.5)
        self.assertEqual(xray.catalog_info.extra, {'newphotkey': 'kept in extra'})
        self.assertEqual(points.get(catalog_info__includeshost=True).quality_mask, int(PhotFlag.NOT_DIFFERENCED))
        for header in TransientPhotometry.objects.filter(transient=t):
            self.assertEqual(list(header.groups.values_list('name', flat=True)), [PUBLIC_COLLABORATION_GROUP_NAME])
            self.assertTrue(TransientPhotometryCatalogInfo.objects.filter(photometry=header, entry=entry_row).exists())

        # Spectra are arrays, not pixel rows (#492).
        spectra = TransientSpectrum.objects.filter(transient=t)
        self.assertEqual(spectra.count(), 2)
        self.assertFalse(TransientSpecData.objects.filter(spectrum__in=spectra).exists())
        first = spectra.get(catalog_info__filename='sn2099zz-20990106.flm')
        w, f, e = spectrum_arrays.arrays_for([first.id])[first.id]
        self.assertEqual(list(w), [4000.0, 4005.0])
        self.assertIsNone(e)
        self.assertTrue(first.quality_mask & int(SpecFlag.DEREDSHIFTED))
        self.assertTrue(first.quality_mask & int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN))
        self.assertEqual(list(first.groups.values_list('name', flat=True)), [PUBLIC_COLLABORATION_GROUP_NAME])

        # Model outputs, not chains (#892).
        fit = CatalogModelFit.objects.get(entry=entry_row)
        self.assertEqual((fit.code, fit.n_realizations), ('MOSFiT', 2))
        self.assertEqual(fit.parameter_summary['texplosion']['median'], -5.0)
        self.assertEqual(fit.light_curve[0]['band'], 'g')

    def test_close_object_gets_the_osc_host(self):
        self.apply(entry(lumdist=[{'value': '15.0', 'u_value': 'Mpc', 'source': '1'}]))
        t = Transient.objects.get(name='2099zz')
        self.assertEqual(t.host.name, 'NGC 99999')
        self.assertEqual(CatalogEntry.objects.get().host_check['status'], 'link_osc_host')

    def test_existing_transient_only_gains_data(self):
        existing = create_minimal_transient(self.user, name='2099zz', ra=150.001, dec=-30.5, status_name='Watch')
        existing.redshift = 0.5
        existing.save()
        result = self.apply()
        self.assertFalse(result['created'])
        existing.refresh_from_db()
        self.assertEqual((existing.status.name, existing.ra, existing.redshift), ('Watch', 150.001, 0.5))
        self.assertIsNone(existing.mw_ebv)
        self.assertIsNone(existing.best_spec_class)
        self.assertIsNone(existing.host)
        entry_row = CatalogEntry.objects.get()
        self.assertFalse(entry_row.created_transient)
        self.assertIsNone(entry_row.tns_status)
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=existing).count(), 5)

    def test_duplicate_from_refereed_paper_supersedes_yse(self):
        existing = create_minimal_transient(self.user, name='2099zz', ra=150.0, dec=-30.5)
        ours = _existing_point(self.user, existing, self.nocam, 88000.505)
        result = self.apply()
        self.assertEqual(result['photometry'].get(matching.SUPERSEDED_EXISTING), 1)
        ours.refresh_from_db()
        self.assertTrue(ours.quality_mask & int(PhotFlag.REJECTED))
        match = CatalogDataMatch.objects.get(existing_phot_data=ours)
        self.assertEqual((match.decision, match.weak, match.previous_quality_mask), ('superseded_existing', False, 0))
        self.assertIsNotNone(match.catalog_phot_data)
        # Reverting restores YSE's point.
        call_command('import_osc', '--revert', NAME, '--apply', stdout=StringIO())
        ours.refresh_from_db()
        self.assertEqual(ours.quality_mask, 0)
        self.assertFalse(CatalogEntry.objects.exists())

    def test_duplicate_from_unrefereed_source_keeps_yse(self):
        existing = create_minimal_transient(self.user, name='2099zz', ra=150.0, dec=-30.5)
        ours = _existing_point(self.user, existing, self.nocam, 88000.505)
        data = entry()
        data['sources'][0] = {'name': '2099ATel.1234....1A', 'bibcode': '2099ATel.1234....1A', 'alias': '1'}
        result = self.apply(data)
        self.assertEqual(result['photometry'].get(matching.KEPT_EXISTING), 1)
        ours.refresh_from_db()
        self.assertEqual(ours.quality_mask, 0)
        match = CatalogDataMatch.objects.get(existing_phot_data=ours)
        self.assertIsNone(match.catalog_phot_data)
        self.assertEqual(match.catalog_record['mag'], 15.2)
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=existing).count(), 5)  # 4 OSC + ours

    def test_reimport_replaces_its_own_rows(self):
        self.apply()
        t = Transient.objects.get(name='2099zz')

        def state():
            return (TransientPhotData.objects.filter(photometry__transient=t).count(),
                    TransientSpectrum.objects.filter(transient=t).count(),
                    TransientCatalogQuantity.objects.filter(transient=t).count(),
                    AlternateTransientNames.objects.filter(transient=t).count(), CatalogSource.objects.count())

        before = state()
        result = self.apply()
        self.assertFalse(result['created'])
        self.assertEqual(result['matched_by'], 'imported before')
        self.assertEqual(before, state())
        entry_row = CatalogEntry.objects.get()
        self.assertTrue(entry_row.created_transient)  # carried over
        self.assertEqual(entry_row.tns_status, CatalogEntry.TNS_PENDING)
        self.assertFalse(CatalogDataMatch.objects.exists())  # its own earlier rows are not duplicates

    def test_match_by_alias_and_by_position(self):
        by_alias = create_minimal_transient(self.user, name='EX99abc', ra=1.0, dec=1.0)
        self.assertEqual(self.apply()['transient'], by_alias.name)
        CatalogEntry.objects.all().delete()
        other_plan = osc.parse_entry('SN2099yy', entry(alias=[], name='SN2099yy'))
        near = create_minimal_transient(self.user, name='nearby-candidate', ra=150.0 + 0.5 / 3600, dec=-30.5)
        self.assertEqual(preview_entry(other_plan)['transient'], near)
        self.assertIsNone(preview_entry(other_plan, match_radius=0)['transient'])

    def test_entry_without_position_or_match_is_refused(self):
        writer = Writer(self.user)
        with self.assertRaises(EntryNotImported):
            writer.apply_entry(osc.parse_entry(NAME, entry(ra=[], dec=[])))
        self.assertFalse(CatalogEntry.objects.exists())
        # The writer's caches were dropped with the rolled-back entry.
        self.assertTrue(writer.apply_entry(osc.parse_entry(NAME, entry()))['created'])

    def test_instrument_aliases(self):
        audit = audit_fields(self.user)
        ptf = Instrument.objects.create(name='CFH12k-test', telescope=self.telescope, **audit)
        ztf = Instrument.objects.create(name='ZTF-Cam-test', telescope=self.telescope, **audit)
        CatalogInstrumentAlias.objects.create(telescope='P48', instrument='', mjd_max=58194, yse_instrument=ptf, **audit)
        CatalogInstrumentAlias.objects.create(telescope='p48', instrument='', mjd_min=58194, yse_instrument=ztf, **audit)
        resolver = instruments.InstrumentResolver()
        self.assertEqual(resolver.resolve('P48', None, 56000), ptf)
        self.assertEqual(resolver.resolve('P48', None, 59000), ztf)
        self.assertEqual(resolver.resolve(None, 'Nowhere 1m - NoCam'), self.nocam)
        self.assertEqual(resolver.resolve('Elsewhere', 'Nothing'), self.unknown)
        lines = instruments.ensure_instruments(self.user, [{'telescope': 'Brand New Scope', 'instrument': 'BNS-Cam',
                                                            'observatory': 'New Site', 'aliases': [['BNS', '']]}])
        self.assertIn('create Brand New Scope / BNS-Cam (observatory New Site)', lines)
        self.assertFalse(Instrument.objects.filter(name='BNS-Cam').exists())  # dry run
        instruments.ensure_instruments(self.user, [{'telescope': 'Brand New Scope', 'instrument': 'BNS-Cam',
                                                    'observatory': 'New Site', 'aliases': [['BNS', '']]}], apply=True)
        self.assertEqual(instruments.InstrumentResolver().resolve('BNS', None).name, 'BNS-Cam')


class FakeTNS:
    def __init__(self, objects, cone=()):
        self.objects, self.cone, self.calls = objects, list(cone), []

    def get_object(self, name, photometry=False, spectra=False):
        self.calls.append(('get', name))
        return self.objects.get(name, {})

    def search(self, ra, dec, radius_arcsec=3.0, **extra):
        self.calls.append(('search', round(ra, 3), round(dec, 3)))
        return [{'objname': n} for n in self.cone]


class OSCTNSCheckTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _setup_world(cls, 'osc_tns_test')

    def setUp(self):
        # An object with no TNS name in the OSC, classified II there; TNS knows it as 2099aaa, an Ia.
        data = entry(alias=[{'value': 'EX99abc', 'source': '2'}], claimedtype=[{'value': 'II', 'source': '1'}])
        Writer(self.user).apply_entry(osc.parse_entry('EX99abc', data))
        self.transient = Transient.objects.get(name='EX99abc')
        self.reply = {'objname': '2099aaa', 'radeg': 150.0001, 'decdeg': -30.5001, 'object_type': {'name': 'SN Ia'}}

    def test_cone_search_hit_renames_and_takes_tns_values(self):
        client = FakeTNS({'2099aaa': self.reply}, cone=['2099aaa'])
        osc_tns.run(dry_run=True, client=client, interval=0)
        self.assertEqual(CatalogEntry.objects.get().tns_status, CatalogEntry.TNS_PENDING)  # dry run
        osc_tns.run(dry_run=False, client=client, interval=0, user=self.user)
        t = Transient.objects.get(pk=self.transient.pk)
        self.assertEqual((t.name, t.ra, t.dec, t.TNS_spec_class, t.best_spec_class.name),
                         ('2099aaa', 150.0001, -30.5001, 'SN Ia', 'SN Ia'))
        self.assertTrue(AlternateTransientNames.objects.filter(transient=t, name='EX99abc').exists())
        entry_row = CatalogEntry.objects.get()
        self.assertEqual((entry_row.tns_status, entry_row.tns_name), (CatalogEntry.TNS_UPDATED, '2099aaa'))
        self.assertEqual(osc_tns.pending_entries().count(), 0)

    def test_values_changed_by_hand_are_kept(self):
        self.transient.ra = 150.002
        self.transient.save()
        osc_tns.run(dry_run=False, client=FakeTNS({'2099aaa': self.reply}, cone=['2099aaa']), interval=0, user=self.user)
        t = Transient.objects.get(pk=self.transient.pk)
        self.assertEqual((t.name, t.ra), ('2099aaa', 150.002))

    def test_conflict_and_not_found(self):
        create_minimal_transient(self.user, name='2099aaa', ra=1.0, dec=1.0)
        osc_tns.run(dry_run=False, client=FakeTNS({'2099aaa': self.reply}, cone=['2099aaa']), interval=0, user=self.user)
        self.assertEqual(CatalogEntry.objects.get().tns_status, CatalogEntry.TNS_CONFLICT)
        self.assertEqual(Transient.objects.get(pk=self.transient.pk).name, 'EX99abc')

        CatalogEntry.objects.update(tns_status=CatalogEntry.TNS_PENDING)
        osc_tns.run(dry_run=False, client=FakeTNS({}, cone=[]), interval=0, user=self.user)
        self.assertEqual(CatalogEntry.objects.get().tns_status, CatalogEntry.TNS_NOT_FOUND)


class ImportOSCCommandTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _setup_world(cls, 'osc_import_cmd')

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
        self.assertFalse(CatalogImportAttempt.objects.exists())

    def test_apply_records_attempts_and_retries_failures(self):
        broken = os.path.join(self.tmp.name, 'SN2099yy.json')
        with open(broken, 'w') as handle:
            handle.write('{not json')
        out, err = StringIO(), StringIO()
        call_command('import_osc', self.tmp.name, '--apply', '--user', self.user.username, '--no-tns',
                     stdout=out, stderr=err)
        self.assertIn('created 2099zz', out.getvalue())
        self.assertIn('failures by stage: parse 1', out.getvalue())
        failed = CatalogImportAttempt.objects.get(entry_key='SN2099yy')
        self.assertEqual((failed.stage, failed.status, failed.attempts), ('parse', 'failed', 1))
        self.assertEqual(CatalogImportAttempt.objects.get(entry_key=NAME).status, 'ok')

        with open(broken, 'w') as handle:
            json.dump({'SN2099yy': entry(alias=[], name='SN2099yy', ra=[{'value': '11:00:00', 'source': '1'}])}, handle)
        call_command('import_osc', '--retry-failed', '--apply', '--user', self.user.username, '--no-tns',
                     stdout=StringIO(), stderr=StringIO())
        retried = CatalogImportAttempt.objects.get(entry_key='SN2099yy')
        self.assertEqual((retried.status, retried.attempts), ('ok', 2))
        self.assertTrue(CatalogEntry.objects.filter(entry_name='SN2099yy').exists())
