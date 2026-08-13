"""TESS footprint lookup must not break transient create / CI."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from YSE_App.common.tess_obs import tess_obs


class TessObsSoftFailTests(SimpleTestCase):
    @patch("YSE_App.common.tess_obs.cache")
    @patch("YSE_App.common.tess_obs.requests.get")
    def test_http_403_returns_false(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_get.return_value = MagicMock(status_code=403, text="forbidden")
        self.assertFalse(tess_obs(150.0, 2.5, 2459000.5))
        mock_cache.set.assert_not_called()

    @patch("YSE_App.common.tess_obs.cache")
    @patch("YSE_App.common.tess_obs.requests.get")
    def test_request_exception_returns_false(self, mock_get, mock_cache):
        import requests

        mock_cache.get.return_value = None
        mock_get.side_effect = requests.Timeout("slow")
        self.assertFalse(tess_obs(150.0, 2.5, 2459000.5))
        mock_cache.set.assert_not_called()
