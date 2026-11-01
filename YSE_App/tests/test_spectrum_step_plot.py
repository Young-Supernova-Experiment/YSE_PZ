"""Spectrum plots draw a step per pixel (mode="center"), so individual pixels are visible."""

from django.core.cache import cache
from django.test import TestCase, override_settings

from YSE_App.models import TransientSpecData, TransientSpectrum
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user, create_transient_with_synthetic_data


@override_settings(DATA_SERVICE_READER_USERNAMES=["spectrum_step_user"])
class SpectrumStepPlotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("spectrum_step_user")
        cls.transient = create_transient_with_synthetic_data(
            cls.user, name="step-plot-sn", with_host=False, with_log=False, with_survey_field=False)
        cls.spectrum = TransientSpectrum.objects.get(transient=cls.transient)
        for i, wave in enumerate((5000.0, 5002.0, 5004.0, 5006.0, 5008.0)):
            TransientSpecData.objects.create(spectrum=cls.spectrum, wavelength=wave, flux=1.0 + 0.1 * i,
                                             **audit_fields(cls.user))

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def _assert_step(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        body = r.content.decode()
        self.assertIn('"Step"', body, url)
        self.assertIn('"center"', body, url)

    def test_detail_plot(self):
        self._assert_step("/spectrumplot/%d/" % self.transient.id)

    def test_summary_plot(self):
        self._assert_step("/spectrumplot_summary/%d/" % self.transient.id)

    def test_single_spectrum_plot(self):
        self._assert_step("/spectrumplotsingle/%d/%d/" % (self.transient.id, self.spectrum.id))
