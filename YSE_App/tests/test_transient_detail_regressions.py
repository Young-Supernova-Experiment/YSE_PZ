"""Transient detail regressions found on the yse_test develop build (Sep 2026).

Reported against master: the Summary tab never deselected, tag drag/drop was
dead, "Apply Tag" hit Django's CSRF page, the spectra toolbar vanished from
the Summary tab and single spectra could no longer be plotted. Two root causes:
a stray ``});`` that made the detail page's main inline script a SyntaxError,
and the deferred shell rendering the Summary spectrum toolbar with no spectra.
"""

import os
from unittest import mock

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from YSE_App.models import TransientSpecData
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
        # Only spectra that have points get a per-spectrum plot link.
        for spec in self.spectra:
            TransientSpecData.objects.create(
                spectrum=spec, wavelength=4500.0, flux=1.0,
                created_by=self.user, modified_by=self.user,
            )
        response = self.client.get(self.spectra_tab_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn("Plot all on Summary tab", html)
        for spec in self.spectra:
            self.assertIn(f'class="specPlotChange" spec_plot-id="{spec.id}"', html)


class TransientSummaryTabRegressionTests(TestCase):
    """transient_summary_individual.html had the same li.active/a.active pair (#171)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("summary_tab_regression_user")
        cls.transient = create_minimal_transient(
            cls.user, name="2026summarytabfix", status_name="New"
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_summary_tab_marks_active_on_the_link_only(self):
        url = reverse("transient_summary", kwargs={"status_or_query_name": "New"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn(self.transient.name, html)
        self.assertIn(
            f'<li class="nav-item"><a class="nav-link active" href="#summary_tab_{self.transient.id}" '
            'data-toggle="tab">Summary</a></li>',
            html,
        )
        self.assertNotIn('class="nav-item active"', html)


def multiline_template_comments(root):
    """Return ``(relative_path, line_number)`` for every ``{#`` whose line lacks ``#}``.

    Django's ``{# ... #}`` comment is single-line only: a comment that spans lines
    is not recognised and its text is emitted into the page. Multi-line notes must
    use ``{% comment %} ... {% endcomment %}``.
    """
    problems = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in sorted(filenames):
            if not filename.endswith(".html"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, start=1):
                    if "{#" in line and "#}" not in line:
                        problems.append((os.path.relpath(path, root), lineno))
    return problems


class TemplateCommentRegressionTests(TestCase):
    """The spectra toolbar fragment opened with a three-line ``{# ... #}`` (#199).

    The Summary-tab spectrum pane showed the comment as literal text on both the
    deferred and inline detail page.
    """

    templates_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
    )

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("template_comment_user")
        cls.transient = create_minimal_transient(cls.user, name="2026templatecomment")
        cls.spectra = [attach_synthetic_spectrum(cls.user, cls.transient) for _ in range(2)]

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_no_multiline_django_comments_in_templates(self):
        self.assertTrue(os.path.isdir(self.templates_root), self.templates_root)
        problems = multiline_template_comments(self.templates_root)
        self.assertEqual(
            problems,
            [],
            "multi-line {# #} comments render as page text; use "
            "{% comment %}...{% endcomment %}: "
            + ", ".join(f"{path}:{lineno}" for path, lineno in problems),
        )

    def test_summary_spectra_tools_fragment_has_no_raw_comment(self):
        url = reverse("transient_detail_summary_spectra_tools_fragment", args=[self.transient.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertNotIn("{#", html)
        self.assertNotIn("#}", html)
        self.assertNotIn("Summary-tab spectrum toolbar", html)
        self.assertIn("Show single spectrum", html)

    def test_detail_page_has_no_raw_comment(self):
        url = reverse("transient_detail", kwargs={"slug": self.transient.slug})
        for defer in (True, False):
            with self.subTest(defer=defer):
                env = {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}
                with mock.patch.dict(os.environ, env):
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode("utf-8", errors="replace")
                self.assertNotIn("Summary-tab spectrum toolbar", html)


class SpectrumPlotEmptyStateTests(TestCase):
    """spectrumplot said "No spectrum data on file" for 2025aarm's 8 spectra (#208).

    The Summary-tab toolbar counts TransientSpectrum rows; the plot needs
    TransientSpecData points. The empty state must say which one is missing.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("specplot_empty_state_user")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _plot_html(self, transient):
        response = self.client.get(reverse("spectrumplot", args=[transient.id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def _toolbar_html(self, transient):
        url = reverse("transient_detail_summary_spectra_tools_fragment", args=[transient.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def test_no_spectra_says_no_spectrum_data(self):
        transient = create_minimal_transient(self.user, name="2026specplotnone")
        html = self._plot_html(transient)
        self.assertIn("No spectrum data on file for this transient.", html)
        self.assertNotIn("Download", self._toolbar_html(transient))

    def test_spectra_without_points_are_counted_not_denied(self):
        transient = create_minimal_transient(self.user, name="2026specplotnopoints")
        for _ in range(2):
            attach_synthetic_spectrum(self.user, transient)
        html = self._plot_html(transient)
        self.assertNotIn("No spectrum data on file", html)
        self.assertIn(
            "2 spectra on file, but none has wavelength/flux points to plot.", html
        )
        self.assertIn("Download 2 Spectra", self._toolbar_html(transient))

    def test_single_spectrum_without_points(self):
        transient = create_minimal_transient(self.user, name="2026specplotonenopoints")
        attach_synthetic_spectrum(self.user, transient)
        html = self._plot_html(transient)
        self.assertIn(
            "1 spectrum on file, but it has no wavelength/flux points to plot.", html
        )
        self.assertIn("Download 1 Spectrum", self._toolbar_html(transient))

    def test_spectrum_with_points_is_plotted(self):
        transient = create_minimal_transient(self.user, name="2026specplotpoints")
        spectrum = attach_synthetic_spectrum(self.user, transient)
        for i in range(20):
            TransientSpecData.objects.create(
                spectrum=spectrum,
                wavelength=4000 + 10 * i,
                flux=1.0 + 0.1 * i,
                created_by=self.user,
                modified_by=self.user,
            )
        html = self._plot_html(transient)
        self.assertNotIn("yse-plot-empty", html)
        self.assertNotIn("on file", html)
        self.assertIn("<script", html)


class SpectrumPlotSingleEmptyStateTests(TestCase):
    """spectrumplotsingle returned '' for a spectrum without points, so picking
    one from the dropdown blanked the pane with no explanation (#211)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("specplot_single_user")
        cls.transient = create_minimal_transient(cls.user, name="2026specsingle")
        cls.empty_spectrum = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.full_spectrum = attach_synthetic_spectrum(cls.user, cls.transient)
        for i in range(20):
            TransientSpecData.objects.create(
                spectrum=cls.full_spectrum,
                wavelength=4000 + 10 * i,
                flux=1.0 + 0.1 * i,
                created_by=cls.user,
                modified_by=cls.user,
            )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _single_html(self, spec_id):
        url = reverse("spectrumplotsingle", args=[self.transient.id, spec_id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def test_spectrum_without_points_says_so(self):
        html = self._single_html(self.empty_spectrum.id)
        self.assertIn("1 spectrum on file, but it has no wavelength/flux points to plot.", html)

    def test_unknown_spectrum_says_not_on_file(self):
        html = self._single_html(987654321)
        self.assertIn("yse-plot-empty", html)
        self.assertIn("not on file", html)

    def test_spectrum_with_points_is_plotted(self):
        html = self._single_html(self.full_spectrum.id)
        self.assertNotIn("yse-plot-empty", html)
        self.assertIn("<script", html)


class SpectraTabPointCountTests(TestCase):
    """The Spectra tab offered "Plot on Summary tab" for spectra that have no
    TransientSpecData points (2026fov on yse_experimental): show the count."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("spectra_tab_points_user")
        cls.transient = create_minimal_transient(cls.user, name="2026spectabpoints")
        cls.empty_spectrum = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.full_spectrum = attach_synthetic_spectrum(cls.user, cls.transient)
        for i in range(12):
            TransientSpecData.objects.create(
                spectrum=cls.full_spectrum,
                wavelength=5000 + 10 * i,
                flux=2.0,
                created_by=cls.user,
                modified_by=cls.user,
            )

    def test_tab_shows_point_counts_and_offers_plot_only_with_points(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("transient_detail_spectra_tab_fragment", args=[self.transient.id])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn("<th>Points</th>", html)
        self.assertIn("<td>12</td>", html)
        self.assertIn("<td>0</td>", html)
        self.assertIn(f'spec_plot-id="{self.full_spectrum.id}"', html)
        self.assertNotIn(f'spec_plot-id="{self.empty_spectrum.id}"', html)
        self.assertIn("No data points to plot", html)
        self.assertIn('spec_plot-id="all"', html)


class PlotEndpointAuthTests(TestCase):
    """The AJAX plot endpoints require a login like the pages that embed them (#210).

    ``transient_detail`` / ``transient_summary`` fetch these with ``$.get`` from a
    page that is itself ``@login_required``, so the session cookie is present and
    a logged-in user still gets 200; an anonymous request is redirected to
    ``LOGIN_URL`` instead of being served ungrouped data (or a debug traceback
    for an unknown id).
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("plot_auth_user")
        cls.transient = create_minimal_transient(cls.user, name="2026plotauth")
        cls.spectrum = attach_synthetic_spectrum(cls.user, cls.transient)
        for i in range(20):
            TransientSpecData.objects.create(
                spectrum=cls.spectrum,
                wavelength=4000 + 10 * i,
                flux=1.0 + 0.1 * i,
                created_by=cls.user,
                modified_by=cls.user,
            )

    def _plot_urls(self):
        t = self.transient.id
        return {
            "spectrumplot": reverse("spectrumplot", args=[t]),
            "spectrumplot_summary": reverse("spectrumplot_summary", args=[t]),
            "spectrumplotsingle": reverse("spectrumplotsingle", args=[t, self.spectrum.id]),
            "lightcurveplot_detail": reverse("lightcurveplot_detail", args=[t]),
            "lightcurveplot_flux": reverse("lightcurveplot_flux", args=[t]),
            "lightcurveplot_summary": reverse("lightcurveplot_summary", args=[t]),
            "salt2plot": reverse("salt2plot", args=[t, 0]),
            "salt2fluxplot": reverse("salt2fluxplot", args=[t, 0]),
            "bazinplot": reverse("bazinplot", args=[t, 0]),
        }

    def test_anonymous_requests_are_redirected_to_login(self):
        client = Client()
        for name, url in self._plot_urls().items():
            with self.subTest(view=name):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    response["Location"].startswith(settings.LOGIN_URL),
                    (name, response["Location"]),
                )

    def test_logged_in_requests_still_succeed(self):
        client = Client()
        client.force_login(self.user)
        for name, url in self._plot_urls().items():
            with self.subTest(view=name):
                self.assertEqual(client.get(url).status_code, 200, name)

    def test_unknown_transient_is_404_not_500_for_spectrum_plots(self):
        client = Client()
        client.force_login(self.user)
        for name, url in {
            "spectrumplot": reverse("spectrumplot", args=[999999]),
            "spectrumplot_summary": reverse("spectrumplot_summary", args=[999999]),
            "spectrumplotsingle": reverse("spectrumplotsingle", args=[999999, 1]),
        }.items():
            with self.subTest(view=name):
                self.assertEqual(client.get(url).status_code, 404, name)

    def test_anonymous_unknown_transient_is_still_a_redirect(self):
        """Auth runs before the lookup, so no traceback leaks for a bad id."""
        response = Client().get(reverse("spectrumplot", args=[999999]))
        self.assertEqual(response.status_code, 302)
