"""astropy never downloads IERS tables in the app (504s on observing nights, 2026-10-22)."""

import socket
import types
from unittest import mock

from django.test import SimpleTestCase

from YSE_App import apps


def _fake_iers(**conf):
    """Stand-in ``astropy.utils.iers`` (CI runs astropy 5.2; Ziggy runs 5.0, which has no switch)."""
    class IERS:
        def _check_interpolate_indices(self, *args):
            raise ValueError("out of range")

    class IERS_B(IERS):
        def _check_interpolate_indices(self, *args):
            raise ValueError("out of range")

    class IERSWarning(UserWarning):
        pass

    settings = dict(auto_download=True, auto_max_age=30.0)
    settings.update(conf)
    return types.SimpleNamespace(conf=types.SimpleNamespace(**settings), IERS=IERS, IERS_B=IERS_B,
                                 IERSWarning=IERSWarning)


class AstropyOfflineTests(SimpleTestCase):
    def test_app_startup_turns_downloads_off(self):
        from astropy.utils import iers

        self.assertFalse(iers.conf.auto_download)
        if hasattr(iers.conf, "iers_degraded_accuracy"):
            self.assertEqual(iers.conf.iers_degraded_accuracy, "ignore")

    def test_twilight_for_a_recent_night_opens_no_connection(self):
        """A night past the bundled IERS table computes without touching the network."""
        from django.core.cache import cache

        from YSE_App.services.night_astro import twilight_times

        site = mock.Mock(latitude=37.3414, longitude=-121.6429, elevation=1283.0)
        site.name = "Shane"
        site.pk = 999001
        site.id = 999001

        def _no_network(*args, **kwargs):
            raise AssertionError("astropy tried to open a network connection")

        cache.clear()
        with mock.patch.object(socket, "create_connection", side_effect=_no_network):
            night = twilight_times(site, "2026-10-09 00:00:00", which="previous")
        self.assertIn("sunset", night)
        self.assertIn("sunrise", night)

    def test_astropy_without_the_switch_drops_the_range_check(self):
        """astropy 5.0: future observing nights must not raise converting UTC to UT1."""
        fake = _fake_iers()
        with mock.patch("astropy.utils.iers", fake, create=True), mock.patch("warnings.filterwarnings"):
            apps.astropy_offline()
        self.assertFalse(fake.conf.auto_download)
        self.assertIsNone(fake.conf.auto_max_age)
        self.assertIsNone(fake.IERS()._check_interpolate_indices(1, 2, 3))
        self.assertIsNone(fake.IERS_B()._check_interpolate_indices(1, 2, 3))

    def test_astropy_with_the_switch_keeps_its_range_check(self):
        fake = _fake_iers(iers_degraded_accuracy="error")
        with mock.patch("astropy.utils.iers", fake, create=True), mock.patch("warnings.filterwarnings"):
            apps.astropy_offline()
        self.assertEqual(fake.conf.iers_degraded_accuracy, "ignore")
        with self.assertRaises(ValueError):
            fake.IERS()._check_interpolate_indices(1, 2, 3)

    def test_far_future_night_converts_to_ut1(self):
        from astropy.time import Time

        Time("2045-10-14T00:00:00", scale="utc").ut1.iso  # must not raise
