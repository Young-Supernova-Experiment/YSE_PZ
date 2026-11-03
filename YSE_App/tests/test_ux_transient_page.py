"""Transient page UX (#812 spectra tab, #814 copy buttons, #816 analysis labels). Synthetic data only."""

import datetime
import zipfile
from io import BytesIO

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import TransientSpecData
from YSE_App.templatetags.analysis_extras import analysis_label
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_spectrum,
    create_minimal_transient,
    create_test_user,
)


class AnalysisLabelTests(SimpleTestCase):
    def test_known_keys_get_readable_labels_with_units(self):
        self.assertEqual(analysis_label("reduced_chi2"), "χ²/dof")
        self.assertEqual(analysis_label("mB"), "m_B (mag)")
        self.assertEqual(analysis_label("tau_rise"), "τ_rise (days)")

    def test_unknown_key_falls_back_to_key(self):
        self.assertEqual(analysis_label("some_new_key"), "some_new_key")


def _add_points(user, spectrum):
    for i in range(3):
        TransientSpecData.objects.create(
            spectrum=spectrum, wavelength=4000 + 10 * i, flux=1.0, created_by=user, modified_by=user,
        )


class SpectraTabTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("ux_transient_page_user")
        cls.transient = create_minimal_transient(cls.user, name="2026uxspec")
        cls.other = create_minimal_transient(cls.user, name="2026uxother")
        now = timezone.now()
        cls.spectra = []
        for days, phase in ((3, 4.5), (2, None), (1, -2.0)):
            spec = attach_synthetic_spectrum(cls.user, cls.transient)
            spec.obs_date = now - datetime.timedelta(days=days)
            spec.spec_phase = phase
            spec.save()
            _add_points(cls.user, spec)
            cls.spectra.append(spec)
        cls.other_spec = attach_synthetic_spectrum(cls.user, cls.other)
        _add_points(cls.user, cls.other_spec)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_tab_has_phase_column_checkboxes_and_download_selected(self):
        url = reverse("transient_detail_spectra_tab_fragment", args=[self.transient.id])
        html = self.client.get(url).content.decode()
        self.assertIn("Phase (days)", html)
        self.assertIn(">4.5</td>", html)
        self.assertIn(">-2.0</td>", html)
        self.assertIn(">—</td>", html)
        for spec in self.spectra:
            self.assertIn('class="yse-spec-select" value="%d"' % spec.id, html)
        self.assertIn('id="yse_spec_download_selected"', html)
        self.assertIn("DataTable(", html)

    def _zip_names(self, query=""):
        url = reverse("download_spectra", kwargs={"slug": self.transient.slug}) + query
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return zipfile.ZipFile(BytesIO(response.content)).namelist()

    def test_download_all_unchanged(self):
        self.assertEqual(len(self._zip_names()), 3)

    def test_download_selected_narrows_to_ids(self):
        ids = "%d,%d" % (self.spectra[0].id, self.spectra[2].id)
        self.assertEqual(len(self._zip_names("?ids=" + ids)), 2)

    def test_download_selected_never_widens(self):
        # A spectrum of another transient and junk ids are ignored.
        names = self._zip_names("?ids=%d,abc,%d" % (self.other_spec.id, self.spectra[1].id))
        self.assertEqual(len(names), 1)
        self.assertNotIn("2026uxother", " ".join(names))


class CopyButtonsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("ux_copy_user")
        cls.transient = create_minimal_transient(cls.user, name="2026uxcopy")

    def test_header_has_copy_link_and_coordinates(self):
        client = Client()
        client.force_login(self.user)
        html = client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug})).content.decode()
        self.assertIn(">Copy link</button>", html)
        self.assertIn(">Copy coordinates</button>", html)
        self.assertIn('data-copy-path="%s"' % reverse("transient_detail", kwargs={"slug": self.transient.slug}), html)
        coords = self.transient.CoordString()
        self.assertIn('data-copy-text="%s %s (%s %s deg)"' % (
            coords[0], coords[1], self.transient.RADecimalString(), self.transient.DecDecimalString()), html)
        self.assertIn('aria-live="polite" id="yse-copy-status"', html)
