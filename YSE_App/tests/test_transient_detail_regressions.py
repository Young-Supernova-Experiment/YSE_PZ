"""Transient detail regressions found on the yse_test develop build (Sep 2026).

Reported against master: the Summary tab never deselected, tag drag/drop was
dead, "Apply Tag" hit Django's CSRF page, the spectra toolbar vanished from
the Summary tab and single spectra could no longer be plotted. Two root causes:
a stray ``});`` that made the detail page's main inline script a SyntaxError,
and the deferred shell rendering the Summary spectrum toolbar with no spectra.
"""

import os
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_spectrum,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.js_syntax_utils import bracket_imbalance, html_script_problems


class JsSyntaxUtilsTests(TestCase):
    def test_balanced_block_passes(self):
        js = "$(function() { var s = '})'; /* } */ // (\n  $('#x').on('click', function(){});\n});"
        self.assertEqual(bracket_imbalance(js), [])

    def test_stray_closer_is_reported(self):
        js = "$(function() {\n  foo();\n});\n\n      });\n"
        problems = bracket_imbalance(js)
        self.assertTrue(problems, "expected the stray '});' to be flagged")
        self.assertIn("unexpected '}'", problems[0])


class TransientDetailRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("detail_regression_user")
        cls.transient = create_minimal_transient(cls.user, name="2026detailfix")
        cls.spectra = [attach_synthetic_spectrum(cls.user, cls.transient) for _ in range(2)]

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.detail_url = reverse("transient_detail", kwargs={"slug": self.transient.slug})
        self.tools_url = reverse(
            "transient_detail_summary_spectra_tools_fragment", args=[self.transient.id]
        )
        self.spectra_tab_url = reverse(
            "transient_detail_spectra_tab_fragment", args=[self.transient.id]
        )

    def _detail_html(self, defer):
        env = {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}
        with mock.patch.dict(os.environ, env):
            response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def test_summary_tab_marks_active_on_the_link_only(self):
        """Bootstrap 4 moves `active` between the <a>s; a stale li.active kept Summary lit."""
        html = self._detail_html(defer=True)
        self.assertIn(
            '<li class="nav-item"><a class="nav-link active" href="#summary_tab" '
            'data-toggle="tab">Summary</a></li>',
            html,
        )
        self.assertNotIn('class="nav-item active"', html)

    def test_inline_scripts_are_structurally_balanced(self):
        """A stray `});` used to make the whole tags/CSRF/spectra script a SyntaxError."""
        for defer in (True, False):
            with self.subTest(defer=defer):
                html = self._detail_html(defer=defer)
                self.assertEqual(html_script_problems(html), [])

    def test_tag_and_csrf_wiring_present(self):
        html = self._detail_html(defer=True)
        self.assertIn('id="add_tags_datalist"', html)
        self.assertIn("$('#add_tags_datalist').on('submit'", html)
        self.assertIn('$("#associated-events").droppable(', html)
        self.assertIn("$.ajaxSetup(", html)
        self.assertIn('xhr.setRequestHeader("X-CSRFToken", csrftoken)', html)
        self.assertIn("$(document).on('click', '.specPlotChange'", html)

    def test_deferred_shell_loads_summary_spectra_toolbar_fragment(self):
        html = self._detail_html(defer=True)
        self.assertIn('id="summary_spectra_tools"', html)
        self.assertIn(self.tools_url, html)
        self.assertIn("'#summary_spectra_tools'", html)

    def test_non_deferred_summary_tab_renders_spectra_toolbar_inline(self):
        html = self._detail_html(defer=False)
        self.assertIn('id="summary_spectra_tools"', html)
        self.assertIn("Download 2 Spectra", html)
        for spec in self.spectra:
            self.assertIn(f'spec_plot-id="{spec.id}"', html)

    def test_summary_spectra_tools_fragment_lists_each_spectrum(self):
        response = self.client.get(self.tools_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn("Show single spectrum", html)
        self.assertIn("Download 2 Spectra", html)
        self.assertIn(reverse("download_spectra", kwargs={"slug": self.transient.slug}), html)
        self.assertIn('class="dropdown-item specPlotChange" spec_plot-id="all"', html)
        for spec in self.spectra:
            self.assertIn(f'class="dropdown-item specPlotChange" spec_plot-id="{spec.id}"', html)

    def test_spectra_tab_fragment_offers_per_spectrum_plot(self):
        response = self.client.get(self.spectra_tab_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn("Plot all on Summary tab", html)
        for spec in self.spectra:
            self.assertIn(f'class="specPlotChange" spec_plot-id="{spec.id}"', html)
