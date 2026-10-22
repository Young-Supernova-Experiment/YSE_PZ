"""astropy never downloads IERS tables in the app (504s on observing nights, 2026-10-22)."""

import socket
from unittest import mock

from django.test import SimpleTestCase


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
