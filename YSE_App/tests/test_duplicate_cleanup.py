"""Duplicate clean-up of photometry and spectra (#890): matching, keep rules, apply, revert, resume.

Synthetic data only; no network.
"""

from __future__ import annotations

import datetime
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from YSE_App.catalog_import import duplicates, guard, matching
from YSE_App.common import spectrum_arrays
from YSE_App.common.quality_mask import PhotFlag, SpecFlag
from YSE_App.models import (
    DuplicateDataDecision,
    Instrument,
    ObservationGroup,
    PhotometricBand,
    TransientPhotData,
    TransientPhotometry,
    TransientSpecData,
    TransientSpectrum,
)
from YSE_App.models.catalog_models import CatalogEntry, TransientPhotDataCatalogInfo, TransientSpectrumCatalogInfo
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

PHOT_REJECTED = int(PhotFlag.REJECTED)
SPEC_REJECTED = int(SpecFlag.REJECTED)
EPOCH = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)


def _date(mjd):
    return EPOCH + datetime.timedelta(days=mjd)


class DuplicateMatchingTests(SimpleTestCase):

    def test_same_time_band_and_instrument_are_one_group(self):
        obs = [matching.Obs('a', 60000.5, 1, 'g', 18.0), matching.Obs('b', 60000.5002, 1, "g'", 18.1),
               matching.Obs('c', 60000.5, 1, 'r', 18.0), matching.Obs('d', 60000.5, 2, 'g', 18.0)]
        groups = matching.duplicate_photometry(obs, unknown_id=99)
        self.assertEqual([[o.key for o in g] for g in groups], [['a', 'b']])

    def test_exposures_minutes_apart_are_not_copies(self):
        obs = [matching.Obs(i, 60000.5 + i * 0.01, 1, 'o', 18.0) for i in range(4)]  # ATLAS-like quad
        self.assertEqual(matching.duplicate_photometry(obs), [])

    def test_no_chaining_through_neighbours(self):
        obs = [matching.Obs(i, 60000.5 + i * 0.0006, 1, 'g', 18.0) for i in range(3)]
        groups = matching.duplicate_photometry(obs)
        self.assertEqual([[o.key for o in g] for g in groups], [[0, 1]])

    def test_weak_match_needs_close_magnitudes(self):
        obs = [matching.Obs('a', 60000.5, 1, 'g', 18.0), matching.Obs('b', 60000.5, 99, 'g', 19.0)]
        self.assertEqual(matching.duplicate_photometry(obs, unknown_id=99), [])

    def test_spectra_need_overlapping_ranges(self):
        obs = [matching.Obs('a', 60000.5, 1, wavelength_min=3500, wavelength_max=5500),
               matching.Obs('b', 60000.51, 1, wavelength_min=3600, wavelength_max=5400),
               matching.Obs('c', 60000.5, 1, wavelength_min=5600, wavelength_max=9000)]
        groups = matching.duplicate_spectra(obs)
        self.assertEqual([[o.key for o in g] for g in groups], [['a', 'b']])


class DuplicateCleanupTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('dup_cleanup_user')
        cls.audit = audit_fields(cls.user)
        _, cls.instrument, _ = create_instrument_stack(cls.user, obs_group_name='dup-cleanup')
        Instrument.objects.get_or_create(name='Unknown', defaults={'telescope': cls.instrument.telescope, **cls.audit})
        cls.ours, _ = ObservationGroup.objects.get_or_create(name='dup-cleanup', defaults=cls.audit)
        cls.tns, _ = ObservationGroup.objects.get_or_create(name='TNS', defaults=cls.audit)
        cls.band, _ = PhotometricBand.objects.get_or_create(instrument=cls.instrument, name='g', defaults=cls.audit)

    def setUp(self):
        self.transient = create_minimal_transient(self.user, name='2099dup', obs_group_name='dup-cleanup')

    # ---- helpers ----------------------------------------------------------------------------

    def _point(self, mjd=60000.5, *, transient=None, group=None, reference=None, mag_err=0.05, created=None):
        header = TransientPhotometry.objects.create(transient=transient or self.transient, instrument=self.instrument,
                                                    obs_group=group or self.ours, reference=reference, **self.audit)
        point = TransientPhotData.objects.create(photometry=header, band=self.band, obs_date=_date(mjd), mag=18.0,
                                                 mag_err=mag_err, **self.audit)
        if created is not None:
            TransientPhotData.objects.filter(pk=point.pk).update(created_date=created)
        return point

    def _spectrum(self, mjd=60000.5, *, group=None, reference=None, errors=True, rows=False, created=None):
        spectrum = TransientSpectrum.objects.create(transient=self.transient, instrument=self.instrument,
                                                    obs_group=group or self.ours, reference=reference, ra=10.0,
                                                    dec=20.0, obs_date=_date(mjd), **self.audit)
        wave, flux, err = [4000.0, 5000.0, 6000.0], [1.0, 1.1, 1.2], [0.1, 0.1, 0.1]
        if rows:
            for w, f, e in zip(wave, flux, err):
                TransientSpecData.objects.create(spectrum=spectrum, wavelength=w, flux=f,
                                                 flux_err=e if errors else None, **self.audit)
        else:
            spectrum_arrays.save(spectrum, wave, flux, err if errors else None)
        if created is not None:
            TransientSpectrum.objects.filter(pk=spectrum.pk).update(created_date=created)
        return spectrum

    def _mask(self, row):
        return type(row).objects.values_list('quality_mask', flat=True).get(pk=row.pk)

    def _run(self, *args, apply=False):
        out = StringIO()
        extra = ['--apply', '--database', guard.database_name()] if apply else []
        call_command('clean_duplicate_data', *args, *extra, stdout=out)
        return out.getvalue()

    # ---- tests ------------------------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        a, b = self._point(), self._point()
        out = self._run('--transient', '2099dup')
        self.assertIn('Using database', out)
        self.assertIn('1 copies to flag', out)
        self.assertEqual((self._mask(a), self._mask(b)), (0, 0))
        self.assertFalse(DuplicateDataDecision.objects.exists())

    def test_apply_keeps_earliest_upload(self):
        now = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
        later = self._point(created=now)
        earlier = self._point(created=now - datetime.timedelta(days=3))
        self._run('--transient', '2099dup', apply=True)
        self.assertEqual(self._mask(earlier), 0)
        self.assertEqual(self._mask(later), PHOT_REJECTED)
        decision = DuplicateDataDecision.objects.get()
        self.assertEqual((decision.phot_data_id, decision.kept_phot_data_id, decision.reason, decision.previous_quality_mask),
                         (later.pk, earlier.pk, DuplicateDataDecision.LATER_UPLOAD, 0))

    def test_copy_with_errors_wins_over_earlier_without(self):
        now = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
        bare = self._point(mag_err=None, created=now - datetime.timedelta(days=3))
        with_err = self._point(created=now)
        self._run('--transient', '2099dup', apply=True)
        self.assertEqual((self._mask(with_err), self._mask(bare)), (0, PHOT_REJECTED))
        self.assertEqual(DuplicateDataDecision.objects.get().reason, DuplicateDataDecision.NO_ERRORS)

    def test_tns_round_trip_keeps_our_reduction(self):
        now = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
        tns_copy = self._point(group=self.tns, created=now - datetime.timedelta(days=3))
        by_reference = self._point(reference='TNS 2099dup report 1', created=now - datetime.timedelta(days=2))
        ours = self._point(mag_err=None, created=now)
        self._run('--transient', '2099dup', apply=True)
        self.assertEqual(self._mask(ours), 0)
        self.assertEqual((self._mask(tns_copy), self._mask(by_reference)), (PHOT_REJECTED, PHOT_REJECTED))
        self.assertEqual(set(DuplicateDataDecision.objects.values_list('reason', flat=True)),
                         {DuplicateDataDecision.TNS_COPY})

    def test_spectra_tns_round_trip_and_storage(self):
        tns_copy = self._spectrum(group=self.tns, reference='TNS 2099dup', rows=True)
        ours = self._spectrum(mjd=60000.52)
        separate = self._spectrum(mjd=60003.5)
        self._run('--transient', '2099dup', apply=True)
        self.assertEqual((self._mask(ours) & SPEC_REJECTED, self._mask(separate) & SPEC_REJECTED), (0, 0))
        self.assertTrue(self._mask(tns_copy) & SPEC_REJECTED)
        decision = DuplicateDataDecision.objects.get()
        self.assertEqual((decision.kind, decision.spectrum_id, decision.kept_spectrum_id, decision.reason),
                         (DuplicateDataDecision.SPECTRUM, tns_copy.pk, ours.pk, DuplicateDataDecision.TNS_COPY))

    def test_spectrum_with_errors_preferred(self):
        now = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
        bare = self._spectrum(errors=False, created=now - datetime.timedelta(days=1))
        with_err = self._spectrum(rows=True, created=now)
        flags = duplicates.spectrum_flags(self.transient.pk, duplicates.unknown_instrument_id())
        self.assertEqual([(f.copy.obs.key, f.kept.obs.key, f.reason) for f in flags],
                         [(bare.pk, with_err.pk, DuplicateDataDecision.NO_ERRORS)])

    def test_catalogue_rows_untouched(self):
        ours = self._point()
        catalogue_point = self._point()
        TransientPhotDataCatalogInfo.objects.create(phot_data=catalogue_point)
        entry = CatalogEntry.objects.create(transient=self.transient, catalog='OSC', entry_name='SN2099dup', **self.audit)
        spectrum = self._spectrum()
        catalogue_spectrum = self._spectrum()
        TransientSpectrumCatalogInfo.objects.create(spectrum=catalogue_spectrum, entry=entry, **self.audit)
        out = self._run('--transient', '2099dup', apply=True)
        self.assertIn('0 photometry points and 0 spectra flagged', out)
        for row in (ours, catalogue_point, spectrum, catalogue_spectrum):
            self.assertFalse(self._mask(row) & PHOT_REJECTED)
        self.assertFalse(DuplicateDataDecision.objects.exists())

    def test_revert_restores_mask(self):
        keep = self._point(created=datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))
        copy = self._point()
        TransientPhotData.objects.filter(pk=copy.pk).update(quality_mask=int(PhotFlag.SATURATED))
        self._run('--transient', '2099dup', apply=True)
        self.assertEqual(self._mask(copy), int(PhotFlag.SATURATED) | PHOT_REJECTED)
        self.assertIn('1 decisions would be reverted', self._run('--revert', '--transient', '2099dup'))
        self.assertEqual(self._mask(copy), int(PhotFlag.SATURATED) | PHOT_REJECTED)
        self.assertIn('Reverted 1 decisions', self._run('--revert', '--transient', '2099dup', apply=True))
        self.assertEqual((self._mask(copy), self._mask(keep)), (int(PhotFlag.SATURATED), 0))
        self.assertIsNotNone(DuplicateDataDecision.objects.get().reverted_date)

    def test_cleared_by_hand_is_not_flagged_again(self):
        self._point(), self._point()
        self._run('--transient', '2099dup', apply=True)
        copy = TransientPhotData.objects.get(pk=DuplicateDataDecision.objects.get().phot_data_id)
        TransientPhotData.objects.filter(pk=copy.pk).update(quality_mask=0)
        self._run('--transient', '2099dup', apply=True)
        self.assertEqual(self._mask(copy), 0)
        self.assertEqual(DuplicateDataDecision.objects.count(), 1)

    def test_max_and_after_resume(self):
        other = create_minimal_transient(self.user, name='2099dupb', obs_group_name='dup-cleanup')
        first, second = sorted([self.transient, other], key=lambda t: t.pk)
        self._point(transient=first), self._point(transient=first)
        self._point(transient=second), self._point(transient=second)
        start = first.pk - 1
        out = self._run('--after', str(start), '--max', '1', apply=True)
        self.assertIn('continue with --after %d' % first.pk, out)
        self.assertEqual(list(DuplicateDataDecision.objects.values_list('transient_id', flat=True)), [first.pk])
        self._run('--after', str(first.pk), '--max', '1', apply=True)
        self.assertEqual(sorted(DuplicateDataDecision.objects.values_list('transient_id', flat=True)),
                         sorted([first.pk, second.pk]))
        out = self._run('--after', str(start), apply=True)
        self.assertIn('0 photometry points and 0 spectra flagged', out)
        self.assertEqual(DuplicateDataDecision.objects.count(), 2)

    def test_apply_needs_database_name(self):
        self._point(), self._point()
        with self.assertRaises(CommandError):
            call_command('clean_duplicate_data', '--apply', stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command('clean_duplicate_data', '--apply', '--database', 'not-this-one', stdout=StringIO())
        self.assertFalse(DuplicateDataDecision.objects.exists())
