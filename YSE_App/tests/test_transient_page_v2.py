"""Transient page preview layout (#856, #851): only with the UI preview on. Synthetic data only."""

import os
import re
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.models import HostMorphology, HostSED, TransientTag
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_log,
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.js_syntax_utils import html_script_problems

V2_TABS = [
    "Summary",
    "Follow-up",
    "Photometry",
    "Spectra",
    "Observing",
    "Brokers",
    "Analysis &amp; Annotations",
    "Archives",
    "GW",
    "History",
]
CURRENT_TABS = [
    "Summary",
    "Follow-up",
    "Resources",
    "Detailed Photometry",
    "Analysis",
    "Annotations",
    "Brokers",
    "Spectra",
    "HST Data",
    "JWST Data",
    "Chandra Data",
    "GW Info",
    "History",
    "Comments (on Summary)",
]
_TAB_RE = re.compile(r'<a class="nav-link[^"]*" href="#\w+" data-toggle="tab"[^>]*>([^<]*)</a>')


class TransientPageV2Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("page_v2_user")
        cls.transient = create_minimal_transient(cls.user, name="2026pagevtwo")
        attach_synthetic_photometry(cls.user, cls.transient, n_points=6)
        attach_synthetic_spectrum(cls.user, cls.transient)
        attach_synthetic_log(cls.user, cls.transient, comment="Synthetic v2 comment.")
        host = attach_synthetic_host(cls.user, cls.transient)
        audit = audit_fields(cls.user)
        host.host_morphology = HostMorphology.objects.create(name="SynthSpiral", **audit)
        host.host_sed = HostSED.objects.create(log_mass=10.25, log_SFR=0.5, **audit)
        host.save()
        cls.transient.tags.add(TransientTag.objects.create(name="GW Candidate", **audit))

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("transient_detail", kwargs={"slug": self.transient.slug})

    def _get(self, preview, defer=True):
        if preview:
            self.client.cookies[PREVIEW_COOKIE] = "1"
        with mock.patch.dict(os.environ, {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    def test_preview_off_renders_the_current_page(self):
        response = self._get(preview=False)
        self.assertTemplateUsed(response, "YSE_App/transient_detail.html")
        self.assertTemplateUsed(response, "YSE_App/transient_detail/summary_tab.html")
        self.assertTemplateNotUsed(response, "YSE_App/transient_detail_v2.html")
        self.assertTemplateNotUsed(response, "YSE_App/transient_detail/summary_tab_v2.html")
        html = response.content.decode()
        self.assertEqual(_TAB_RE.findall(html), CURRENT_TABS)
        self.assertNotIn("yse-v2-", html)

    def test_preview_on_renders_ten_tabs_in_order_without_counts(self):
        for defer in (True, False):
            with self.subTest(defer=defer):
                response = self._get(preview=True, defer=defer)
                self.assertTemplateUsed(response, "YSE_App/transient_detail_v2.html")
                self.assertTemplateUsed(response, "YSE_App/transient_detail/summary_tab_v2.html")
                self.assertTemplateNotUsed(response, "YSE_App/transient_detail/summary_tab.html")
                html = response.content.decode()
                labels = _TAB_RE.findall(html)
                self.assertEqual(labels, V2_TABS)
                for label in labels:
                    self.assertIsNone(re.search(r"\d", label), label)
                self.assertEqual(html_script_problems(html), [])

    def test_archives_tab_starts_hidden_and_gw_only_for_candidates(self):
        html = self._get(preview=True).content.decode()
        self.assertIn('<li class="nav-item d-none" id="yse-v2-archives-nav">', html)
        self.transient.tags.clear()
        html = self._get(preview=True).content.decode()
        self.assertNotIn('href="#gw_tab"', html)
        self.assertEqual(len(_TAB_RE.findall(html)), 9)

    def test_summary_order_basic_info_comments_then_evidence(self):
        html = self._get(preview=True).content.decode()
        order = [
            'id="yse-v2-actionbar"',
            'id="yse-v2-basic-info"',
            'id="yse-comments-section"',
            'id="yse-v2-photometry"',
            'id="lcplot"',
            'id="yse-v2-spectra"',
            'id="specplot"',
            'id="yse-v2-host"',
            'id="yse-v2-more-details"',
        ]
        positions = [html.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Synthetic v2 comment.", html)
        # Everything else sits in "More details".
        more = html[html.index('id="yse-v2-more-details"'):]
        for marker in ('id="photstat-summary"', 'id="yse-interests-section"', 'id="add_tags_datalist"', ">NED</a>"):
            self.assertIn(marker, more)

    def test_evidence_shows_brightness_trend_and_host_properties(self):
        html = self._get(preview=True).content.decode()
        self.assertIn('id="yse-v2-latest"', html)
        self.assertIn('id="yse-v2-trend"', html)
        host = html[html.index('id="yse-v2-host"'):html.index('id="yse-v2-more-details"')]
        self.assertIn("SynthSpiral", host)
        self.assertIn("log M = 10.25", host)
        self.assertIn("log SFR = 0.50", host)
        self.assertIn("arcsec", host)
        self.assertIn('id="aladin-lite-div"', host)

    def test_one_status_menu_and_actions(self):
        html = self._get(preview=True).content.decode()
        self.assertEqual(html.count('id="transient_status_name"'), 1)
        self.assertNotIn("transientStatusChange btn", html)  # the four status buttons are gone
        self.assertIn("yse-v2-request-followup", html)
        self.assertIn('data-yse-hook="list-stepping"', html)
        self.assertIn(reverse("download_photometry", kwargs={"slug": self.transient.slug}), html)

    def test_bazin_is_the_default_fit_and_salt_an_option(self):
        html = self._get(preview=True).content.decode()
        self.assertLess(html.index('id="bazinplot"'), html.index('id="salt2plot"'))
        self.assertIn("$b.trigger('click')", html)

    def test_dates_are_utc(self):
        html = self._get(preview=True).content.decode()
        self.assertIn("Discovery (UTC)", html)
        self.assertIn("<th>Date (UTC)</th>", html)
