"""One date format (#807), one status display map (#831), redshift source kind (#832).

Synthetic values only.
"""

import datetime

from django.template import Context, Template
from django.test import SimpleTestCase

from YSE_App.common.status_display import TRANSIENT_STATUS_DISPLAY, status_definition, status_label
from YSE_App.common.utc_dates import as_utc_datetime, mjd_string, utc_day, utc_day_html, utc_night
from YSE_App.services.redshift_precedence import BestRedshift

UTC = datetime.timezone.utc


class UtcDayTests(SimpleTestCase):

    def test_aware_datetime(self):
        value = datetime.datetime(2026, 10, 24, 8, 41, 17, tzinfo=UTC)
        self.assertEqual(utc_day(value), '20261024.362')
        self.assertEqual(mjd_string(value), '61337.362')

    def test_naive_datetime_is_utc(self):
        self.assertEqual(utc_day(datetime.datetime(2026, 10, 24, 8, 41, 17)), '20261024.362')

    def test_other_time_zone_is_converted_to_utc(self):
        pdt = datetime.timezone(datetime.timedelta(hours=-7))
        # 2026-10-23 22:00 PDT is 2026-10-24 05:00 UTC
        self.assertEqual(utc_day(datetime.datetime(2026, 10, 23, 22, 0, tzinfo=pdt)), '20261024.208')

    def test_date_only_values_have_no_fraction_or_tooltip(self):
        self.assertEqual(utc_day(datetime.date(2026, 10, 24)), '20261024')
        self.assertEqual(utc_day_html(datetime.date(2026, 10, 24)), '20261024')
        self.assertEqual(utc_day('2026-10-24'), '20261024')
        self.assertEqual(utc_day_html(' 2026-10-24 '), '20261024')
        self.assertEqual(mjd_string(datetime.date(2026, 10, 24)), '61337.000')

    def test_midnight_datetime_keeps_the_fraction(self):
        value = datetime.datetime(2026, 10, 24, tzinfo=UTC)
        self.assertEqual(utc_day(value), '20261024.000')

    def test_utc_night(self):
        self.assertEqual(utc_night(datetime.datetime(2026, 10, 24, tzinfo=UTC)), '20261024')
        self.assertEqual(utc_night(datetime.datetime(2026, 10, 24, 23, 59, 59, tzinfo=UTC)), '20261024')
        self.assertEqual(utc_night(datetime.date(2026, 10, 24)), '20261024')
        self.assertEqual(utc_night('2026-10-24'), '20261024')
        pdt = datetime.timezone(datetime.timedelta(hours=-7))
        self.assertEqual(utc_night(datetime.datetime(2026, 10, 23, 22, 0, tzinfo=pdt)), '20261024')
        for value in (None, '', 'not a date', 42):
            self.assertEqual(utc_night(value), '')

    def test_none_and_junk(self):
        for value in (None, '', 'not a date', 42):
            self.assertEqual(utc_day(value), '')
            self.assertEqual(mjd_string(value), '')
            self.assertEqual(utc_day_html(value), '')
        self.assertIsNone(as_utc_datetime(None))

    def test_iso_strings(self):
        self.assertEqual(utc_day('2026-10-24T12:00:00Z'), '20261024.500')
        self.assertEqual(utc_day('2026-10-24 06:00'), '20261024.250')

    def test_rounding_up_rolls_over_to_the_next_day(self):
        value = datetime.datetime(2026, 10, 24, 23, 59, 59, tzinfo=UTC)
        self.assertEqual(utc_day(value), '20261025.000')
        self.assertEqual(mjd_string(value), '61338.000')

    def test_day_and_mjd_agree(self):
        start = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        for minutes in range(0, 3 * 24 * 60, 37):
            value = start + datetime.timedelta(minutes=minutes)
            day, frac = utc_day(value).split('.')
            mjd_day, mjd_frac = mjd_string(value).split('.')
            self.assertEqual(frac, mjd_frac)
            expected = (datetime.date(1858, 11, 17) + datetime.timedelta(days=int(mjd_day))).strftime('%Y%m%d')
            self.assertEqual(day, expected)

    def test_html_has_the_mjd_tooltip(self):
        value = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        self.assertEqual(utc_day_html(value),
                         '<span class="yse-utcdate" title="MJD 61337.500">20261024.500</span>')


class TemplateFilterTests(SimpleTestCase):

    def render(self, source, **context):
        return Template(source).render(Context(context))

    def test_utcdate_is_a_builtin(self):
        value = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        self.assertEqual(self.render('{{ v|utcdate }}', v=value),
                         '<span class="yse-utcdate" title="MJD 61337.500">20261024.500</span>')
        self.assertEqual(self.render('{{ v|utcdate_text }}', v=value), '20261024.500')

    def test_utcnight_is_a_builtin(self):
        value = datetime.datetime(2026, 10, 24, tzinfo=UTC)
        self.assertEqual(self.render('{{ v|utcnight }}', v=value), '20261024')
        self.assertEqual(self.render('{{ v|utcnight|default:"-" }}', v=None), '-')

    def test_utcdate_of_a_date_is_plain(self):
        self.assertEqual(self.render('{{ v|utcdate }}', v=datetime.date(2026, 10, 24)), '20261024')

    def test_none_falls_through_to_default(self):
        self.assertEqual(self.render('{{ v|utcdate|default:"-" }}', v=None), '-')
        self.assertEqual(self.render('{{ v|utcdate_text }}', v=None), '')

    def test_status_filters(self):
        self.assertEqual(self.render('{{ s|status_label }}', s='FollowupRequested'), 'Follow-up requested')
        self.assertEqual(self.render('{{ s|status_title }}', s='NeedsTemplate'),
                         status_definition('NeedsTemplate'))


class StatusDisplayTests(SimpleTestCase):

    def test_every_status_has_a_label_and_definition(self):
        for name, (label, definition) in TRANSIENT_STATUS_DISPLAY.items():
            self.assertTrue(label and definition, name)
            self.assertEqual(status_label(name), label)

    def test_lookup_ignores_case_and_accepts_labels_and_objects(self):
        class Status:
            name = 'FollowupFinished'

        self.assertEqual(status_label(Status()), 'Follow-up finished')
        self.assertEqual(status_label('followuprequested'), 'Follow-up requested')
        self.assertEqual(status_definition('follow-up requested'), status_definition('FollowupRequested'))

    def test_unknown_names_show_unchanged(self):
        self.assertEqual(status_label('SomethingElse'), 'SomethingElse')
        self.assertEqual(status_definition('SomethingElse'), '')
        self.assertEqual(status_label(None), '')


class RedshiftKindTests(SimpleTestCase):

    def test_kinds(self):
        self.assertEqual(BestRedshift(0.05, source='NED', origin='transient').kind, 'spec')
        self.assertEqual(BestRedshift(0.05, source='NED', origin='host').kind, 'host')
        self.assertEqual(BestRedshift(0.05, source='photo-z', origin='host photo-z').kind, 'phot')
        self.assertEqual(BestRedshift(0.05, source='Legacy photo-z', origin='transient').kind, 'phot')
        self.assertEqual(BestRedshift(None).kind, '')
        self.assertEqual(BestRedshift(None).kind_title, '')


class MagSystemLabelTests(SimpleTestCase):
    """Magnitude headers never claim a system that is not stored (#832)."""

    def test_no_hard_coded_ab_header(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / 'templates' / 'YSE_App'
        for name in ('transient_detail_photometry_tables.html', 'transient_detail/summary_tab.html'):
            text = (root / name).read_text()
            self.assertNotIn('Mag (AB)', text, name)
            self.assertIn('mag_sys', text, name)
