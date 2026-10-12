"""quality_mask columns, dual-write, backfill and readers (#80, #81, #82, #480).

* the MySQL column DEFAULTs exist, so an INSERT that leaves the new columns
  out (``develop`` on the shared YSE_test database) still works;
* every DataQuality add / remove / clear, from either side, recomputes the
  mask; bits DataQuality does not manage are kept;
* WAVELENGTH_MEDIUM_UNKNOWN follows ``wavelength_medium`` on save;
* ``backfill_quality_mask`` reports in a dry run, writes only with --apply;
* the readers switched from ``exclude(data_quality__isnull=False)`` to
  ``quality_mask & LEGACY_EXCLUDE == 0`` (spec v7) return exactly the same rows;
  NOT_PUBLISHED and WAVELENGTH_MEDIUM_UNKNOWN never exclude a row.
"""

import datetime
import io
from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.db.models import Q
from django.test import TestCase
from django.utils import timezone

from YSE_App.common import quality_mask_sync
from YSE_App.common.quality_mask import (
    PHOT_LEGACY_EXCLUDE,
    SPEC_LEGACY_EXCLUDE,
    HostSpecFlag,
    PhotFlag,
    SpecFlag,
    data_quality_bit,
    data_quality_mask,
    merge_data_quality,
    is_legacy_flagged,
    legacy_good,
)
from YSE_App.data import PhotometryService, SpectraService
from YSE_App.models import (
    DataQuality,
    HostPhotData,
    HostPhotometry,
    HostSpecData,
    HostSpectrum,
    TransientPhotData,
    TransientSpecData,
    TransientSpectrum,
)
from YSE_App.services import photstat
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

REJECTED = int(PhotFlag.REJECTED)
OTHER = int(PhotFlag.OTHER)
MEDIUM = int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN)

# Every DataQuality name in the code and the docker seed data (docker/db_init).
SEED_NAMES = ('Bad', 'Masking', 'Bad Subtraction', 'Saturation', 'Crowding', 'Bad Calibration', 'Quicklook')


def _dq(user, name):
    return DataQuality.objects.get_or_create(name=name, defaults=audit_fields(user))[0]


def _mask(obj):
    return type(obj).objects.values_list('quality_mask', flat=True).get(pk=obj.pk)


class Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('qm_user')
        audit = audit_fields(cls.user)
        cls.dqs = {name: _dq(cls.user, name) for name in SEED_NAMES + ('Suspect',)}
        cls.transient = create_minimal_transient(cls.user, name='2026qm')
        cls.phot = attach_synthetic_photometry(cls.user, cls.transient, n_points=8)
        cls.points = list(TransientPhotData.objects.filter(photometry=cls.phot).order_by('obs_date'))
        cls.spectrum = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.spectrum2 = attach_synthetic_spectrum(cls.user, cls.transient)
        obs_group, instrument, band = create_instrument_stack(cls.user, obs_group_name='qm-host')
        cls.host = attach_synthetic_host(cls.user, cls.transient)
        cls.host_phot = HostPhotometry.objects.create(
            host=cls.host, instrument=instrument, obs_group=obs_group, **audit)
        cls.host_points = [
            HostPhotData.objects.create(photometry=cls.host_phot, band=band, mag=20.0 + i,
                                        obs_date=timezone.now() - datetime.timedelta(days=i), **audit)
            for i in range(3)
        ]
        cls.host_spectra = [
            HostSpectrum.objects.create(host=cls.host, instrument=instrument, obs_group=obs_group,
                                        ra=1.0, dec=2.0, obs_date=timezone.now(), **audit)
            for _ in range(2)
        ]


@skipUnless(connection.vendor == 'mysql', 'MySQL column defaults')
class ColumnDefaultTests(Fixture):
    COLUMNS = [
        ('YSE_App_transientphotdata', 'quality_mask', '0', 'NO'),
        ('YSE_App_hostphotdata', 'quality_mask', '0', 'NO'),
        ('YSE_App_transientspectrum', 'quality_mask', '0', 'NO'),
        ('YSE_App_hostspectrum', 'quality_mask', '0', 'NO'),
        ('YSE_App_transientspecdata', 'pixel_mask', '0', 'NO'),
        ('YSE_App_hostspecdata', 'pixel_mask', '0', 'NO'),
        ('YSE_App_transientspectrum', 'wavelength_medium', None, 'YES'),
        ('YSE_App_hostspectrum', 'wavelength_medium', None, 'YES'),
        ('YSE_App_transientspectrum', 'host_subtraction', None, 'YES'),
        ('YSE_App_hostphotdata', 'phot_type', None, 'YES'),
    ]

    def test_columns_have_mysql_defaults(self):
        with connection.cursor() as cursor:
            for table, column, default, nullable in self.COLUMNS:
                cursor.execute(
                    'SELECT COLUMN_DEFAULT, IS_NULLABLE, COLUMN_TYPE FROM information_schema.COLUMNS '
                    'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s',
                    [table, column])
                row = cursor.fetchone()
                self.assertIsNotNone(row, (table, column))
                self.assertEqual((row[0], row[1]), (default, nullable), (table, column))
                if column.endswith('_mask'):
                    self.assertIn('unsigned', row[2], (table, column))

    def test_insert_without_the_new_columns(self):
        """What develop's code does: the INSERT does not name the new columns."""
        now = timezone.now()
        u = self.user.pk
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO YSE_App_transientphotdata (created_date, modified_date, created_by_id, modified_by_id, '
                'band_id, obs_date, photometry_id, mag) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                [now, now, u, u, self.points[0].band_id, now, self.phot.pk, 19.0])
            phot_id = cursor.lastrowid
            cursor.execute(
                'INSERT INTO YSE_App_transientspecdata (created_date, modified_date, created_by_id, modified_by_id, '
                'spectrum_id, wavelength, flux) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                [now, now, u, u, self.spectrum.pk, 5000.0, 1.0])
            spec_data_id = cursor.lastrowid
            cursor.execute(
                'INSERT INTO YSE_App_transientspectrum (created_date, modified_date, created_by_id, modified_by_id, '
                'instrument_id, obs_group_id, `ra`, `dec`, obs_date, transient_id) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                [now, now, u, u, self.spectrum.instrument_id, self.spectrum.obs_group_id, 1.0, 2.0, now,
                 self.transient.pk])
            spec_id = cursor.lastrowid
        self.assertEqual(_mask(TransientPhotData(pk=phot_id)), 0)
        self.assertEqual(TransientSpecData.objects.get(pk=spec_data_id).pixel_mask, 0)
        spec = TransientSpectrum.objects.get(pk=spec_id)
        self.assertEqual((spec.quality_mask, spec.wavelength_medium, spec.host_subtraction), (0, None, None))
        # good data by both definitions
        self.assertTrue(TransientPhotData.objects.filter(legacy_good('TransientPhotData'), pk=phot_id).exists())


class DualWriteTests(Fixture):
    def test_forward_add_remove_clear(self):
        p = self.points[0]
        self.assertEqual(_mask(p), 0)
        p.data_quality.add(self.dqs['Bad'])
        self.assertEqual(_mask(p), REJECTED)
        self.assertEqual(p.quality_mask, REJECTED)  # the in-memory row follows
        p.data_quality.add(self.dqs['Quicklook'], self.dqs['Saturation'])
        self.assertEqual(_mask(p), REJECTED | int(PhotFlag.QUICKLOOK) | int(PhotFlag.SATURATED))
        p.data_quality.remove(self.dqs['Bad'], self.dqs['Saturation'])
        self.assertEqual(_mask(p), int(PhotFlag.QUICKLOOK))  # only "Bad" sets REJECTED
        p.data_quality.set([self.dqs['Crowding']])
        self.assertEqual(_mask(p), int(PhotFlag.BLENDED))
        p.data_quality.clear()
        self.assertEqual(_mask(p), 0)
        self.assertEqual(p.quality_mask, 0)

    def test_unmapped_name_is_other(self):
        p = self.points[1]
        p.data_quality.add(self.dqs['Suspect'])
        self.assertEqual(_mask(p), OTHER)

    def test_save_after_flagging_keeps_the_mask(self):
        """The clobber path in data_utils flags a row and then saves it."""
        p = self.points[2]
        p.data_quality.add(self.dqs['Bad'])
        p.mag = 18.5
        p.save()
        self.assertEqual(_mask(p), REJECTED)

    def test_reverse_side(self):
        bad = self.dqs['Bad']
        a, b, c = self.points[3:6]
        bad.transientphotdata_set.add(a, b)
        self.assertEqual([_mask(a), _mask(b), _mask(c)], [REJECTED, REJECTED, 0])
        bad.transientphotdata_set.remove(a)
        self.assertEqual([_mask(a), _mask(b)], [0, REJECTED])
        bad.transientphotdata_set.add(c)
        bad.transientphotdata_set.clear()
        self.assertEqual([_mask(a), _mask(b), _mask(c)], [0, 0, 0])
        self.dqs['Quicklook'].transientspectrum_set.add(self.spectrum)
        self.assertEqual(_mask(self.spectrum) & ~MEDIUM, int(SpecFlag.QUICKLOOK))

    def test_other_bits_are_kept(self):
        p = self.points[6]
        TransientPhotData.objects.filter(pk=p.pk).update(quality_mask=int(PhotFlag.NOT_PUBLISHED))
        p.data_quality.add(self.dqs['Masking'])
        self.assertEqual(_mask(p), int(PhotFlag.NOT_PUBLISHED) | int(PhotFlag.MASKED))
        p.data_quality.clear()
        self.assertEqual(_mask(p), int(PhotFlag.NOT_PUBLISHED))

    def test_host_photometry_and_spectra(self):
        hp = self.host_points[0]
        hp.data_quality.add(self.dqs['Bad Subtraction'])
        self.assertEqual(_mask(hp), int(PhotFlag.BAD_SUBTRACTION))
        hp.data_quality.set([self.dqs['Crowding']])
        self.assertEqual(_mask(hp), int(PhotFlag.BLENDED))
        hs = self.host_spectra[0]
        hs.data_quality.add(self.dqs['Bad Calibration'])
        self.assertEqual(_mask(hs) & ~MEDIUM, int(HostSpecFlag.NOT_FLUX_CALIBRATED))
        self.spectrum2.data_quality.add(self.dqs['Saturation'])  # no spectrum bit: OTHER
        self.assertEqual(_mask(self.spectrum2) & ~MEDIUM, OTHER)

    def test_photstat_sees_the_new_mask(self):
        p = self.points[7]
        before = photstat.points_by_transient([self.transient.pk])[self.transient.pk]
        self.assertFalse(any(pt.flagged for pt in before))
        p.data_quality.add(self.dqs['Bad'])
        after = photstat.points_by_transient([self.transient.pk])[self.transient.pk]
        self.assertEqual(sum(pt.flagged for pt in after), 1)

    def test_bulk_ingest_rows_get_their_mask(self):
        """_write_photdata_points bulk-inserts the through row, so it sets the mask itself."""
        self.assertEqual(data_quality_mask(['Bad'], 'TransientPhotData'), REJECTED)
        self.assertEqual(data_quality_mask([], 'TransientPhotData'), 0)


class WavelengthMediumTests(Fixture):
    def test_bit_follows_the_field_on_save(self):
        s = self.spectrum
        s.refresh_from_db()
        self.assertEqual(s.wavelength_medium, 'unknown')
        self.assertTrue(s.quality_mask & MEDIUM)
        s.wavelength_medium = 'air'
        s.save()
        self.assertFalse(_mask(s) & MEDIUM)
        s.wavelength_medium = None
        s.save(update_fields=['wavelength_medium'])
        self.assertTrue(_mask(s) & MEDIUM)
        s.wavelength_medium = 'vacuum'
        s.save(update_fields=['wavelength_medium'])
        self.assertFalse(_mask(s) & MEDIUM)

    def test_unknown_medium_is_still_good_data(self):
        self.assertTrue(TransientSpectrum.objects.filter(legacy_good('TransientSpectrum'), pk=self.spectrum.pk).exists())

    def test_not_published_does_not_exclude(self):
        p = self.points[0]
        TransientPhotData.objects.filter(pk=p.pk).update(quality_mask=int(PhotFlag.NOT_PUBLISHED))
        self.assertTrue(TransientPhotData.objects.filter(legacy_good('TransientPhotData'), pk=p.pk).exists())

    def test_flagging_keeps_the_medium_bit(self):
        self.spectrum.data_quality.add(self.dqs['Bad'])
        self.assertEqual(_mask(self.spectrum), REJECTED | MEDIUM)
        self.spectrum.data_quality.clear()
        self.assertEqual(_mask(self.spectrum), MEDIUM)

    def test_pixel_mask_default(self):
        d = TransientSpecData.objects.create(spectrum=self.spectrum, wavelength=5000.0, flux=1.0,
                                             **audit_fields(self.user))
        self.assertEqual(d.pixel_mask, 0)
        h = HostSpecData.objects.create(spectrum=self.host_spectra[0], wavelength=5000.0, flux=1.0,
                                        **audit_fields(self.user))
        self.assertEqual(h.pixel_mask, 0)


class BackfillTests(Fixture):
    def _flag(self):
        self.points[0].data_quality.add(self.dqs['Bad'])
        self.points[1].data_quality.add(self.dqs['Quicklook'], self.dqs['Suspect'])
        self.host_points[0].data_quality.add(self.dqs['Saturation'])
        self.spectrum.data_quality.add(self.dqs['Quicklook'])
        self.host_spectra[0].data_quality.add(self.dqs['Bad'])

    def _snapshot(self):
        return {m: dict(m.objects.values_list('pk', 'quality_mask'))
                for m in (TransientPhotData, HostPhotData, TransientSpectrum, HostSpectrum)}

    def _run(self, *args):
        out = io.StringIO()
        call_command('backfill_quality_mask', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_and_apply_writes(self):
        self._flag()
        expected = self._snapshot()
        # what a write that bypasses the signals (develop, raw SQL) leaves behind
        for m in (TransientPhotData, HostPhotData, TransientSpectrum, HostSpectrum):
            m.objects.update(quality_mask=0)
        stale = self._snapshot()

        out = self._run()
        self.assertIn('dry run', out)
        self.assertIn("DataQuality 'Quicklook'", out)
        self.assertIn("DataQuality 'Suspect' on 1 row(s) -> OTHER", out)
        self.assertEqual(self._snapshot(), stale)  # nothing written

        out = self._run('--apply', '--batch-size', '2')
        self.assertIn('updated', out)
        self.assertEqual(self._snapshot(), expected)

        out = self._run()
        self.assertIn('dry run: 0 row(s) would change', out)

    def test_model_option(self):
        self._flag()
        TransientPhotData.objects.update(quality_mask=0)
        HostSpectrum.objects.update(quality_mask=0)
        self._run('--apply', '--model', 'TransientPhotData')
        self.assertEqual(_mask(self.points[0]), REJECTED)
        self.assertEqual(_mask(self.host_spectra[0]), 0)

    def test_migration_backfill_helpers(self):
        self._flag()
        expected = self._snapshot()
        for m in (TransientPhotData, HostPhotData, TransientSpectrum, HostSpectrum):
            m.objects.update(quality_mask=0)
        for m in (TransientPhotData, HostPhotData, TransientSpectrum, HostSpectrum):
            quality_mask_sync.backfill_flagged(m)
        for m in (TransientSpectrum, HostSpectrum):
            quality_mask_sync.backfill_wavelength_medium(m)
        self.assertEqual(self._snapshot(), expected)


class MappingTests(TestCase):
    def test_seed_names(self):
        phot = {name: data_quality_bit(name, 'TransientPhotData') for name in SEED_NAMES}
        self.assertEqual(phot, {
            'Bad': REJECTED,
            'Masking': int(PhotFlag.MASKED),
            'Bad Subtraction': int(PhotFlag.BAD_SUBTRACTION),
            'Saturation': int(PhotFlag.SATURATED),
            'Crowding': int(PhotFlag.BLENDED),
            'Bad Calibration': int(PhotFlag.NOT_CALIBRATED),
            'Quicklook': int(PhotFlag.QUICKLOOK),
        })
        self.assertEqual(data_quality_bit('Bad Subtraction', 'HostPhotData'), int(PhotFlag.BAD_SUBTRACTION))
        self.assertEqual(data_quality_bit('Quicklook', 'TransientSpectrum'), int(SpecFlag.QUICKLOOK))
        self.assertEqual(data_quality_bit('Masking', 'TransientSpectrum'), OTHER)
        self.assertEqual(data_quality_bit('Bad Calibration', 'HostSpectrum'), int(HostSpecFlag.NOT_FLUX_CALIBRATED))
        self.assertEqual(data_quality_bit(' bad ', 'HostSpectrum'), REJECTED)
        self.assertEqual(data_quality_bit(None, 'HostSpectrum'), OTHER)

    def test_only_bad_is_rejected_but_every_name_is_legacy_flagged(self):
        for model in ('TransientPhotData', 'HostPhotData', 'TransientSpectrum', 'HostSpectrum'):
            for name in SEED_NAMES + ('anything else',):
                mask = data_quality_mask([name], model)
                self.assertEqual(bool(mask & REJECTED), name == 'Bad', (model, name))
                self.assertTrue(is_legacy_flagged(mask, model), (model, name))
            self.assertEqual(merge_data_quality(REJECTED | 1, [], model), 1)

    def test_legacy_exclude_leaves_non_legacy_bits_alone(self):
        self.assertEqual(PHOT_LEGACY_EXCLUDE, int(
            PhotFlag.REJECTED | PhotFlag.OTHER | PhotFlag.QUICKLOOK | PhotFlag.MASKED | PhotFlag.BAD_SUBTRACTION
            | PhotFlag.SATURATED | PhotFlag.BLENDED | PhotFlag.NOT_CALIBRATED))
        self.assertEqual(SPEC_LEGACY_EXCLUDE, int(
            SpecFlag.REJECTED | SpecFlag.OTHER | SpecFlag.QUICKLOOK | SpecFlag.NOT_FLUX_CALIBRATED))
        self.assertFalse(PHOT_LEGACY_EXCLUDE & int(PhotFlag.NOT_PUBLISHED))
        self.assertFalse(SPEC_LEGACY_EXCLUDE & (int(SpecFlag.NOT_PUBLISHED) | MEDIUM))


class ReaderEquivalenceTests(Fixture):
    """Same rows as the anti-join, over flagged, multiply flagged and unflagged rows."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.points[0].data_quality.add(cls.dqs['Bad'])
        cls.points[2].data_quality.add(cls.dqs['Quicklook'], cls.dqs['Masking'])
        cls.points[5].data_quality.add(cls.dqs['Suspect'])
        cls.points[7].data_quality.add(cls.dqs['Bad'])  # the newest point
        cls.host_points[1].data_quality.add(cls.dqs['Crowding'])
        cls.spectrum.data_quality.add(cls.dqs['Quicklook'])
        cls.host_spectra[1].data_quality.add(cls.dqs['Bad'])
        # a flag that was added and removed leaves a good row behind
        cls.points[3].data_quality.add(cls.dqs['Bad'])
        cls.points[3].data_quality.remove(cls.dqs['Bad'])

    def _ids(self, qs):
        return sorted(qs.values_list('pk', flat=True))

    def test_querysets_match_the_anti_join(self):
        for model in (TransientPhotData, HostPhotData, TransientSpectrum, HostSpectrum):
            old = self._ids(model.objects.exclude(data_quality__isnull=False))
            new = self._ids(model.objects.filter(legacy_good(model.__name__)))
            self.assertEqual(old, new, model.__name__)
            flagged = self._ids(model.objects.filter(data_quality__isnull=False).distinct())
            self.assertTrue(flagged, model.__name__)
            self.assertEqual(sorted(set(self._ids(model.objects.all())) - set(flagged)), new)

    def test_services_match(self):
        q = Q(photometry__transient=self.transient)
        self.assertEqual(self._ids(PhotometryService._GetTransientPhotData(False, q)),
                         self._ids(TransientPhotData.objects.exclude(data_quality__isnull=False).filter(q)))
        q = Q(photometry__host=self.host)
        self.assertEqual(self._ids(PhotometryService._GetHostPhotData(False, q)),
                         self._ids(HostPhotData.objects.exclude(data_quality__isnull=False).filter(q)))
        q = Q(transient=self.transient)
        self.assertEqual(self._ids(SpectraService._GetTransientSpectrum(False, q)),
                         self._ids(TransientSpectrum.objects.exclude(data_quality__isnull=False).filter(q)))
        q = Q(host=self.host)
        self.assertEqual(self._ids(SpectraService._GetHostSpectrum(False, q)),
                         self._ids(HostSpectrum.objects.exclude(data_quality__isnull=False).filter(q)))
        # includeBadData=True is untouched
        self.assertEqual(PhotometryService._GetTransientPhotData(True, Q(photometry=self.phot)).count(), 8)

    def test_recent_mag_skips_flagged_points(self):
        good = (TransientPhotData.objects.filter(photometry=self.phot).exclude(data_quality__isnull=False)
                .filter(mag__isnull=False).order_by('-obs_date').first())
        self.assertNotEqual(good.pk, self.points[7].pk)
        self.assertEqual(self.transient.recent_mag(), '%.2f' % good.mag)
        self.assertEqual(self.transient.recent_magdate(), good.obs_date.strftime('%m/%d/%Y'))

    def test_photstat_flags_match(self):
        points = photstat.points_by_transient([self.transient.pk])[self.transient.pk]
        flagged_ids = set(TransientPhotData.objects.filter(photometry=self.phot, data_quality__isnull=False)
                          .values_list('pk', flat=True))
        self.assertEqual(sum(p.flagged for p in points), len(flagged_ids))
