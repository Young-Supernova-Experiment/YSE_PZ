"""Regression tests for transient tables on the dashboard.

Covers two regressions Ryan Foley found on yse_test (develop vs production):

* "last obs date format changed in tables": ``annotate_dashboard_transient_fields``
  puts a raw datetime on each row, so django-tables2 rendered it with Django's
  default datetime format instead of the MM/DD/YYYY string that
  ``Transient.recent_magdate()`` produced on production.
* "dashboard sorting only works on the new table": deferred status sections were
  fetched from ``/dashboard/section/<status>/`` without the page query string,
  so ``<prefix>sort`` / ``<prefix>page`` (django-tables2) and ``<prefix>-ex``
  (django-filter) never reached the fragment view.
"""

import datetime

from django.test import Client, TestCase
from django.utils import timezone

from YSE_App.models import Transient
from YSE_App.table_utils import (
    LastObsDateColumn,
    TransientTable,
    annotate_dashboard_transient_fields,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    create_minimal_transient,
    create_test_user,
    seed_dashboard_transients,
)


class LastObsDateColumnTests(TestCase):
    def test_render_formats_datetime_like_production(self):
        value = timezone.make_aware(datetime.datetime(2026, 9, 3, 21, 29, 24))
        self.assertEqual(LastObsDateColumn().render(value), '09/03/2026')

    def test_render_passes_preformatted_string_through(self):
        # Un-annotated querysets fall back to Transient.recent_magdate(),
        # which already returns MM/DD/YYYY.
        self.assertEqual(LastObsDateColumn().render('09/03/2026'), '09/03/2026')

    def test_dashboard_table_cell_matches_model_method_format(self):
        user = create_test_user(username='last_obs_date_user')
        transient = create_minimal_transient(user, name='last-obs-date-test')
        attach_synthetic_photometry(user, transient, n_points=3)

        table = TransientTable(
            annotate_dashboard_transient_fields(
                Transient.objects.filter(pk=transient.pk)
            )
        )
        cell = list(table.rows)[0].get_cell('recent_magdate')

        expected = transient.recent_magdate()
        self.assertRegex(expected, r'^\d{2}/\d{2}/\d{4}$')
        self.assertEqual(cell, expected)


class DashboardDeferredSectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('dash_sort_user')
        seed_dashboard_transients(cls.user, count_per_status=2)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_shell_forwards_query_string_to_deferred_sections(self):
        response = self.client.get('/dashboard/?followingsort=-ra_string')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('mdash-section-loading', body)
        self.assertIn('window.location.search', body)
        self.assertIn('sectionUrl', body)

    def test_shell_renders_filter_form_for_deferred_sections(self):
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Synchronous "New" section and a deferred one both keep their search box.
        self.assertIn('name="new-ex"', body)
        self.assertIn('name="following-ex"', body)

    def test_section_fragment_applies_sort_param(self):
        # seed_dashboard_transients: ra = 10.00 (…-0) and 10.01 (…-1).
        asc = self.client.get('/dashboard/section/following/?followingsort=ra_string')
        self.assertEqual(asc.status_code, 200)
        asc_body = asc.content.decode()
        self.assertLess(
            asc_body.index('perf-following-0'), asc_body.index('perf-following-1')
        )

        desc = self.client.get('/dashboard/section/following/?followingsort=-ra_string')
        self.assertEqual(desc.status_code, 200)
        desc_body = desc.content.decode()
        self.assertLess(
            desc_body.index('perf-following-1'), desc_body.index('perf-following-0')
        )
        # Header link toggles back to ascending using the section prefix.
        self.assertIn('followingsort=ra_string', desc_body)
