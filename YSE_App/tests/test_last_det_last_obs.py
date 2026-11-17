"""Transient tables: "Last Det." and "Last Obs." (#557).

* Last Det. is the stat row's last detection with its band (the light-curve
  plot's detection rule); a low-S/N forced point or a non-detection newer
  than it does not change it.
* Last Obs. is the date of the newest unflagged point of any kind; when that
  point is an upper limit (the plot's inverted triangle) the cell adds the
  limit and its band.
* Display and sort read the same TransientPhotStat columns.
* The cells for a page cost a fixed number of queries.

Synthetic transients only.
"""

import datetime

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from YSE_App.models import (
    DataQuality,
    PhotometricBand,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
)
from YSE_App.common.utc_dates import utc_day, utc_day_html
from YSE_App.services import phot_points
from YSE_App.services.photstat import recompute_many
from YSE_App.table_utils import (
    FieldTransientTable,
    LastDetColumn,
    LastObsColumn,
    NewTransientTable,
    TransientTable,
    YSETransientTable,
    annotate_dashboard_transient_fields,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


def _day(n):
    return (timezone.now() - datetime.timedelta(days=n)).replace(microsecond=0)


class LastDetLastObsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('ldlo_user')
        audit = audit_fields(cls.user)
        cls.obs_group, cls.instrument, cls.r = create_instrument_stack(cls.user, obs_group_name='ldlo')
        cls.g, _ = PhotometricBand.objects.get_or_create(
            name='g-ldlo', defaults={'instrument': cls.instrument, 'disp_color': '#00ff00',
                                     'disp_symbol': 'circle', **audit})
        cls.bad, _ = DataQuality.objects.get_or_create(name='Bad', defaults=audit)

        def transient(name, points):
            t = create_minimal_transient(cls.user, name=name)
            phot = TransientPhotometry.objects.create(
                transient=t, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
            for days, band, kw in points:
                flagged = kw.pop('flagged', False)
                pd = TransientPhotData.objects.create(
                    photometry=phot, band=band, obs_date=_day(days), discovery_point=False, **kw, **audit)
                if flagged:
                    pd.data_quality.add(cls.bad)
            return t

        # newest point is a detection
        cls.det = transient('2026ldlo-det', [
            (5, cls.r, dict(mag=19.0, mag_err=0.05)),
            (2, cls.g, dict(mag=18.5, mag_err=0.05)),
        ])
        # newest point is a non-detection: limit = -2.5 log10(10 + 3*20) + 27.5
        cls.lim = transient('2026ldlo-lim', [
            (6, cls.g, dict(mag=19.2, mag_err=0.05)),
            (1, cls.r, dict(flux=10.0, flux_err=20.0, flux_zero_point=27.5)),
        ])
        # newest point is a low-S/N forced magnitude with no usable limit: date only
        cls.noisy = transient('2026ldlo-noisy', [
            (4, cls.r, dict(mag=18.8, mag_err=0.05)),
            (1, cls.r, dict(mag=21.9, mag_err=0.8, flux=40.0, flux_err=10.0)),
        ])
        # the newest point is flagged: ignored entirely
        cls.flagged = transient('2026ldlo-flag', [
            (3, cls.g, dict(mag=18.1, mag_err=0.05)),
            (1, cls.r, dict(flux=1.0, flux_err=20.0, flagged=True)),
        ])
        # only non-detections
        cls.nodet = transient('2026ldlo-nodet', [
            (2, cls.r, dict(flux=0.0, flux_err=30.0)),
        ])
        cls.nostat = create_minimal_transient(cls.user, name='2026ldlo-none')
        cls.all = [cls.det, cls.lim, cls.noisy, cls.flagged, cls.nodet]
        recompute_many([t.pk for t in cls.all])
        cls.expected_limit = phot_points.limiting_mag(10.0, 20.0, 27.5)

    def _qs(self):
        return annotate_dashboard_transient_fields(
            Transient.objects.filter(name__startswith='2026ldlo').order_by('name'))

    def _cells(self, table_cls=TransientTable, qs=None, **kwargs):
        table = table_cls(self._qs() if qs is None else qs, **kwargs)
        return {row.record.name: (row.get_cell('recent_mag'), row.get_cell('recent_magdate'))
                for row in table.rows}

    @staticmethod
    def _date(days):
        return utc_day_html(_day(days))  # YYYYMMDD.ddd with the MJD on hover (#807)

    def test_column_labels(self):
        table = TransientTable(self._qs())
        self.assertEqual(table.columns['recent_mag'].header, 'Last Det.')
        self.assertEqual(table.columns['recent_magdate'].header, 'Last Obs. (UTC)')

    def test_newest_point_is_a_detection(self):
        self.assertEqual(self._cells()['2026ldlo-det'], ('18.50 g', self._date(2)))

    def test_newest_point_is_a_non_detection(self):
        last_det, last_obs = self._cells()['2026ldlo-lim']
        self.assertEqual(last_det, '19.20 g')
        self.assertEqual(last_obs, '%s (>%.2f r)' % (self._date(1), self.expected_limit))

    def test_noisy_forced_point_is_not_a_detection_or_a_limit(self):
        self.assertEqual(self._cells()['2026ldlo-noisy'], ('18.80 r', self._date(1)))

    def test_flagged_point_is_ignored(self):
        self.assertEqual(self._cells()['2026ldlo-flag'], ('18.10 g', self._date(3)))

    def test_only_non_detections(self):
        lim = phot_points.limiting_mag(0.0, 30.0, None)
        self.assertEqual(self._cells()['2026ldlo-nodet'], ('—', '%s (>%.2f r)' % (self._date(2), lim)))

    def test_no_stat_row(self):
        self.assertEqual(self._cells()['2026ldlo-none'], ('—', '—'))

    def test_values_match_the_stat_row(self):
        for t in self.all:
            stat = TransientPhotStat.objects.select_related('last_detected_band').get(transient=t)
            last_det, last_obs = self._cells()[t.name]
            self.assertIn('>%s</span>' % utc_day(stat.last_obs_date), last_obs)
            if stat.last_detected_mag is not None:
                self.assertTrue(last_det.startswith('%.2f ' % stat.last_detected_mag))

    def test_same_cells_in_every_transient_table(self):
        expected = self._cells()
        for table_cls in (FieldTransientTable, NewTransientTable, YSETransientTable):
            with self.subTest(table=table_cls.__name__):
                self.assertEqual(self._cells(table_cls), expected)

    def test_display_and_sort_agree(self):
        mag_of = {}
        for name, (last_det, _) in self._cells().items():
            mag_of[name] = None if last_det == '—' else float(last_det.split()[0])
        for descending in (False, True):
            with self.subTest(descending=descending):
                table = TransientTable(self._qs(), order_by='-recent_mag' if descending else 'recent_mag')
                shown = [mag_of[row.record.name] for row in table.rows]
                with_value = [m for m in shown if m is not None]
                self.assertEqual(with_value, sorted(with_value, reverse=descending))

    def test_query_count_is_flat_per_page(self):
        def count_for(names):
            qs = annotate_dashboard_transient_fields(Transient.objects.filter(name__in=names).order_by('name'))
            table = TransientTable(qs)
            rows = list(table.rows)
            with CaptureQueriesContext(connection) as ctx:
                for row in rows:
                    row.get_cell('recent_mag')
                    row.get_cell('recent_magdate')
            return len(ctx.captured_queries)

        few = count_for(['2026ldlo-det', '2026ldlo-lim'])
        many = count_for([t.name for t in self.all] + ['2026ldlo-none'])
        self.assertEqual(few, many)
        self.assertLessEqual(many, 2)

    def test_columns_are_orderable_and_keep_their_names(self):
        self.assertIsInstance(TransientTable.base_columns['recent_mag'], LastDetColumn)
        self.assertIsInstance(TransientTable.base_columns['recent_magdate'], LastObsColumn)
        fields = list(TransientTable.Meta.fields)
        self.assertEqual(fields.index('recent_magdate'), fields.index('recent_mag') + 1)
