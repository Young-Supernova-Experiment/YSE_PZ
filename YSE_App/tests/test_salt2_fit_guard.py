"""SALT2/SALT3 fit guard for the light-curve plot endpoints (#189).

``salt2plot/<id>/1/`` and ``salt2fluxplot/<id>/1/`` used to call
``sncosmo.fit_lc`` with no error handling, so a failed model download, a band
missing from ``bandpassdict`` or too few points returned HTTP 500 and the plot
never rendered. The fit is now wrapped: the endpoint logs the error, annotates
the plot with a short "SALT fit unavailable" note and returns the light curve
without the fit.

``sncosmo`` is mocked so the tests never download a SALT model.
"""

from unittest import mock

from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from YSE_App import view_utils
from YSE_App.models import Instrument, Observatory, PhotometricBand, Telescope
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)


class Salt2FitGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("salt2_guard_user")
        cls.transient = create_minimal_transient(cls.user, name="2026saltguard")
        audit = audit_fields(cls.user)
        observatory = Observatory.objects.create(
            name="SaltGuardObs", utc_offset=0, tz_name="UTC", **audit
        )
        telescope = Telescope.objects.create(
            name="SaltGuardTel", observatory=observatory,
            latitude=0.0, longitude=0.0, elevation=0.0, **audit,
        )
        # str(band) == "Band: GPC1 - r" is a key of bandpassdict, so every
        # endpoint reaches the fit branch with real points.
        instrument = Instrument.objects.create(name="GPC1", telescope=telescope, **audit)
        band = PhotometricBand.objects.create(
            name="r", instrument=instrument,
            disp_color="#ff0000", disp_symbol="circle", **audit,
        )
        cls.photometry = attach_synthetic_photometry(
            cls.user, cls.transient,
            obs_group=cls.transient.obs_group, instrument=instrument, band=band,
            n_points=8,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.detail_fit_url = reverse("salt2plot", args=[self.transient.id, 1])
        self.detail_nofit_url = reverse("salt2plot", args=[self.transient.id, 0])
        self.flux_fit_url = reverse("salt2fluxplot", args=[self.transient.id, 1])
        self.flux_nofit_url = reverse("salt2fluxplot", args=[self.transient.id, 0])
        # The plot stack is imported lazily; load it so the patches below hit
        # the module view_utils actually calls into.
        view_utils._load_heavy_plot_stack()

    def _patched_sncosmo(self, *, fit_lc=None, model=None):
        """Patch sncosmo.Model / sncosmo.fit_lc as seen by view_utils."""
        if model is None:
            model = mock.MagicMock(name="Model", return_value=mock.MagicMock(name="model"))
        if fit_lc is None:
            fit_lc = mock.MagicMock(name="fit_lc", side_effect=RuntimeError("synthetic fit failure"))
        return mock.patch.multiple(view_utils.sncosmo, Model=model, fit_lc=fit_lc)

    def _assert_plot_without_fit(self, response):
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8", errors="replace")
        self.assertGreater(len(body), 100)
        self.assertIn("plot", body.lower())
        self.assertIn(view_utils.SALT_FIT_UNAVAILABLE_TEXT, body)
        return body

    def test_salt2plot_returns_plot_when_fit_raises(self):
        fit_lc = mock.MagicMock(side_effect=RuntimeError("synthetic fit failure"))
        with self._patched_sncosmo(fit_lc=fit_lc), \
                self.assertLogs("YSE_App.view_utils", level="WARNING") as logs:
            response = self.client.get(self.detail_fit_url)
        self._assert_plot_without_fit(response)
        self.assertTrue(fit_lc.called, "the fit branch should have been exercised")
        self.assertTrue(any("SALT fit failed" in line for line in logs.output), logs.output)

    def test_salt2fluxplot_returns_plot_when_fit_raises(self):
        fit_lc = mock.MagicMock(side_effect=ValueError("too few points"))
        with self._patched_sncosmo(fit_lc=fit_lc), \
                self.assertLogs("YSE_App.view_utils", level="WARNING"):
            response = self.client.get(self.flux_fit_url)
        self._assert_plot_without_fit(response)
        self.assertTrue(fit_lc.called, "the fit branch should have been exercised")

    def test_salt2plot_returns_plot_when_model_download_fails(self):
        """sncosmo.Model(source=...) fetching the SALT model is guarded too."""
        model = mock.MagicMock(side_effect=OSError("could not download salt2 model"))
        fit_lc = mock.MagicMock()
        with self._patched_sncosmo(model=model, fit_lc=fit_lc), \
                self.assertLogs("YSE_App.view_utils", level="WARNING"):
            response = self.client.get(self.detail_fit_url)
        self._assert_plot_without_fit(response)
        self.assertTrue(model.called)
        self.assertFalse(fit_lc.called)

    def test_lightcurveplot_summary_with_salt2_is_guarded(self):
        """Not URL-reachable with salt2=True, but the same unguarded fit lived here."""
        request = RequestFactory().get("/lightcurveplot_summary/%d/" % self.transient.id)
        request.user = self.user
        fit_lc = mock.MagicMock(side_effect=RuntimeError("synthetic fit failure"))
        with self._patched_sncosmo(fit_lc=fit_lc), \
                self.assertLogs("YSE_App.view_utils", level="WARNING"):
            response = view_utils.lightcurveplot_summary(request, self.transient.id, salt2=True)
        self._assert_plot_without_fit(response)

    def test_salt2fit_disabled_never_touches_sncosmo(self):
        """Control: with salt2fit=0 the plot renders and no fit or note is produced."""
        fit_lc = mock.MagicMock()
        model = mock.MagicMock()
        with self._patched_sncosmo(model=model, fit_lc=fit_lc):
            for url in (self.detail_nofit_url, self.flux_nofit_url):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertGreater(len(response.content), 100)
                    self.assertNotIn(
                        view_utils.SALT_FIT_UNAVAILABLE_TEXT.encode(), response.content
                    )
        self.assertFalse(model.called)
        self.assertFalse(fit_lc.called)
