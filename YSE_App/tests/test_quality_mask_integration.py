"""Phase 5 integration tests for the data quality bitmask (#36): the gaps.

``test_quality_mask_column.py`` (#80-#82), ``test_photometry_table_quality_mask.py``
(#85) and ``test_lightcurve_legend.py`` (#84) already cover the backfill, the
MySQL ``DEFAULT 0``, the add / remove / clear dual-write, the PhotometryService
and SpectraService readers and the UI. This module adds what they leave out,
in the style of ``test_sql_rewrites_equivalence.py``: each reader that moved
from ``exclude(data_quality__isnull=False)`` to
``quality_mask & LEGACY_EXCLUDE == 0`` returns the same answer as the old
anti-join on a mixed fixture (flagged, multiply flagged, unflagged, flagged
then unflagged):

* ``Transient.disc_mag`` / ``recent_mag`` / ``recent_magdate``;
* ``services.bazin.detections_by_transient``;
* ``table_utils.last_phot_summary`` (the limiting-magnitude lookup);
* the stored ``TransientPhotStat`` after every add / remove / clear / set,
  from the photometry side and from the DataQuality side.
"""

import datetime

from django.utils import timezone

from YSE_App.common.quality_mask import PhotFlag
from YSE_App.models import TransientPhotData, TransientPhotometry
from YSE_App.models.phot_stat_models import TransientPhotStat
from YSE_App.services import bazin, photstat
from YSE_App.table_utils import last_phot_summary
from YSE_App.tests import test_quality_mask_column as base
from YSE_App.tests.fixtures_minimal import audit_fields, create_instrument_stack


def _anti_join(transient):
    """The pre-#82 reader: every row with a DataQuality is dropped."""
    return TransientPhotData.objects.filter(photometry__transient=transient).exclude(
        data_quality__isnull=False)


class MixedPhotometryFixture(base.Fixture):
    """Points 0 (the discovery point), 2, 5 and 7 (the newest) flagged; 3 flagged then cleared."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.points[0].data_quality.add(cls.dqs['Bad'])
        cls.points[2].data_quality.add(cls.dqs['Quicklook'], cls.dqs['Masking'])
        cls.points[5].data_quality.add(cls.dqs['Suspect'])
        cls.points[7].data_quality.add(cls.dqs['Bad'])
        cls.points[3].data_quality.add(cls.dqs['Bad'])
        cls.points[3].data_quality.remove(cls.dqs['Bad'])


class TransientMagReaderEquivalenceTests(MixedPhotometryFixture):
    def test_disc_mag_skips_a_flagged_discovery_point(self):
        self.assertEqual(self.points[0].discovery_point, True)
        expected = _anti_join(self.transient).filter(discovery_point=1)
        self.assertFalse(expected.exists())
        self.assertIsNone(self.transient.disc_mag())
        self.points[0].data_quality.clear()
        self.assertEqual(self.transient.disc_mag(), self.points[0].mag)
        self.assertEqual(self.transient.disc_mag(), _anti_join(self.transient).get(discovery_point=1).mag)

    def test_recent_mag_and_date_match_the_anti_join(self):
        newest = _anti_join(self.transient).filter(mag__isnull=False).order_by('-obs_date').first()
        self.assertNotEqual(newest.pk, self.points[7].pk)
        self.assertEqual(self.transient.recent_mag(), '%.2f' % newest.mag)
        newest_any = _anti_join(self.transient).order_by('-obs_date').first()
        self.assertEqual(self.transient.recent_magdate(), newest_any.obs_date.strftime('%m/%d/%Y'))

    def test_everything_flagged_leaves_nothing(self):
        for p in self.points:
            p.data_quality.add(self.dqs['Bad'])
        self.assertFalse(_anti_join(self.transient).exists())
        self.assertIsNone(self.transient.disc_mag())
        self.assertIsNone(self.transient.recent_mag())
        self.assertIsNone(self.transient.recent_magdate())

    def test_not_published_alone_does_not_hide_a_point(self):
        """The readers use the legacy rule, not quality_mask == 0 (spec v7)."""
        newest = _anti_join(self.transient).filter(mag__isnull=False).order_by('-obs_date').first()
        TransientPhotData.objects.filter(pk=newest.pk).update(quality_mask=int(PhotFlag.NOT_PUBLISHED))
        self.assertEqual(self.transient.recent_mag(), '%.2f' % newest.mag)


class BazinReaderEquivalenceTests(MixedPhotometryFixture):
    def _rows(self):
        entry = bazin.detections_by_transient([self.transient.pk]).get(self.transient.pk)
        return entry, sorted(m for _, pts in entry['bands'].values() for m, _, _ in pts)

    def test_detections_match_the_anti_join(self):
        entry, mjds = self._rows()
        expected = sorted(
            bazin.datetime_to_mjd(d)
            for d in _anti_join(self.transient).filter(mag__isnull=False, mag_err__isnull=False)
            .values_list('obs_date', flat=True))
        self.assertEqual(len(mjds), len(expected))
        self.assertEqual(mjds, expected)
        self.assertTrue(entry['token'].startswith('%d:' % len(expected)))

    def test_flag_and_unflag_move_the_token(self):
        before, _ = self._rows()
        self.points[1].data_quality.add(self.dqs['Bad'])
        after, mjds = self._rows()
        self.assertEqual(len(mjds), len(_anti_join(self.transient)))
        self.assertNotEqual(before['token'], after['token'])
        self.points[1].data_quality.clear()
        _, mjds = self._rows()
        self.assertEqual(len(mjds), len(_anti_join(self.transient)))


class LastPhotSummaryEquivalenceTests(base.Fixture):
    """table_utils.last_phot_summary: the deepest limit among the newest non-detections."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        audit = audit_fields(cls.user)
        obs_group, instrument, band = create_instrument_stack(cls.user, obs_group_name='qm-limits')
        cls.limit_band = band
        photometry = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=instrument, obs_group=obs_group, **audit)
        when = timezone.now() + datetime.timedelta(days=1)  # newer than every detection
        # flux / err below the S/N cut: upper limits; the second is the deeper one
        cls.shallow = TransientPhotData.objects.create(
            photometry=photometry, band=band, obs_date=when, flux=1.0, flux_err=1.0,
            flux_zero_point=27.5, **audit)
        cls.deep = TransientPhotData.objects.create(
            photometry=photometry, band=band, obs_date=when, flux=0.1, flux_err=0.1,
            flux_zero_point=27.5, **audit)
        photstat.recompute(cls.transient.pk)

    def _limit(self):
        return last_phot_summary([self.transient.pk])[self.transient.pk]['limit']

    def _expected(self):
        best = None
        for row in _anti_join(self.transient).filter(mag__isnull=True):
            lim = photstat.limiting_mag(photstat.PhotPoint(
                mjd=0, band_id=None, flux=row.flux, flux_err=row.flux_err, flux_zero_point=row.flux_zero_point))
            if lim is not None and (best is None or lim > best):
                best = lim
        return best

    def test_deepest_limit_skips_flagged_rows(self):
        deep_lim, shallow_lim = self._limit(), None
        self.assertAlmostEqual(deep_lim, self._expected())
        self.deep.data_quality.add(self.dqs['Bad'])
        shallow_lim = self._limit()
        self.assertAlmostEqual(shallow_lim, self._expected())
        self.assertLess(shallow_lim, deep_lim)
        self.deep.data_quality.clear()
        self.assertAlmostEqual(self._limit(), deep_lim)

    def test_not_published_alone_keeps_the_limit(self):
        TransientPhotData.objects.filter(pk=self.deep.pk).update(quality_mask=int(PhotFlag.NOT_PUBLISHED))
        self.assertAlmostEqual(self._limit(), self._expected())


class PhotStatRefreshTests(base.Fixture):
    """The stored TransientPhotStat follows every kind of data_quality change."""

    def _stat(self):
        return TransientPhotStat.objects.get(transient=self.transient)

    def _unflagged(self):
        return _anti_join(self.transient).count()

    def test_photometry_side_add_remove_set_clear(self):
        p, q = self.points[2], self.points[4]
        photstat.recompute(self.transient.pk)
        self.assertEqual(self._stat().num_obs_global, 8)
        p.data_quality.add(self.dqs['Bad'])
        self.assertEqual(self._stat().num_obs_global, 7)
        q.data_quality.set([self.dqs['Quicklook']])
        self.assertEqual(self._stat().num_obs_global, 6)
        p.data_quality.remove(self.dqs['Bad'])
        self.assertEqual(self._stat().num_obs_global, 7)
        q.data_quality.clear()
        self.assertEqual(self._stat().num_obs_global, 8)
        self.assertEqual(self._stat().num_obs_global, self._unflagged())

    def test_dataquality_side_add_remove_clear(self):
        bad = self.dqs['Bad']
        photstat.recompute(self.transient.pk)
        bad.transientphotdata_set.add(*self.points[:3])
        self.assertEqual(self._stat().num_obs_global, 5)
        bad.transientphotdata_set.remove(self.points[0])
        self.assertEqual(self._stat().num_obs_global, 6)
        bad.transientphotdata_set.clear()
        self.assertEqual(self._stat().num_obs_global, 8)

    def test_flagged_newest_point_moves_the_last_detection(self):
        photstat.recompute(self.transient.pk)
        newest = self.points[7]
        self.assertAlmostEqual(self._stat().last_detected_mag, newest.mag)
        newest.data_quality.add(self.dqs['Bad'])
        self.assertAlmostEqual(self._stat().last_detected_mag, self.points[6].mag)
        newest.data_quality.clear()
        self.assertAlmostEqual(self._stat().last_detected_mag, newest.mag)
