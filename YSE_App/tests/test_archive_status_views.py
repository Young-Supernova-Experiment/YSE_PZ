"""The HST/Chandra tab labels must not report an archive outage as "No HST".

On yse_experimental the 2026fov page showed "No HST" although MAST holds HST
images of it: ``get_hst_status`` ran its MAST lookup inside a
``with ThreadPoolExecutor`` block, whose exit waited for the worker even after
the 8 s timeout, so the response took as long as MAST did; the page's 10 s
XHR timeout then fired and the ``.fail`` handler wrote "No HST". When the
server-side timeout did fire it answered ``{"has_data": false, "timed_out":
true}``, which the page also rendered as "No HST", and cached it for an hour.
"""

import os
import time
from unittest import mock

from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from YSE_App import view_utils
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user


class _FakeHst:
    """Stand-in for common.mast_query.hstImages that finds five images."""

    def __init__(self, ra, dec, obj):
        self.Nimages = 0
        self.obstable = None

    def getObstable(self):
        self.Nimages = 5

    def getJPGurl(self):
        self.jpglist = []


class _BrokenHst(_FakeHst):
    def getObstable(self):
        raise ConnectionError("MAST unreachable")


class ArchiveStatusTimeoutTests(SimpleTestCase):
    def test_timeout_does_not_wait_for_the_worker(self):
        def slow():
            time.sleep(3)
            return 1

        started = time.monotonic()
        result = view_utils._archive_status_with_timeout(slow, timeout_seconds=0.2)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0, "the response must not wait for MAST to answer")

    def test_result_is_returned_when_the_lookup_is_fast(self):
        self.assertEqual(view_utils._archive_status_with_timeout(lambda: 7, timeout_seconds=2), 7)


class HstStatusViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("hst_status_user")
        cls.transient = create_minimal_transient(cls.user, name="2026hststatus")

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("get_hst_status", args=[self.transient.id])

    def test_answer_is_reported_and_cached_for_an_hour(self):
        with mock.patch("YSE_App.common.mast_query.hstImages", _FakeHst), mock.patch.object(
            view_utils.cache, "set", wraps=view_utils.cache.set
        ) as cache_set:
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"has_data": True, "count": 5})
        cache_set.assert_called_once()
        self.assertEqual(cache_set.call_args.kwargs["timeout"], view_utils.ARCHIVE_STATUS_CACHE_SECONDS)

    def test_timeout_is_reported_as_an_error_not_as_no_data(self):
        with mock.patch.object(view_utils, "_archive_status_with_timeout", return_value=None), mock.patch.object(
            view_utils.cache, "set", wraps=view_utils.cache.set
        ) as cache_set:
            payload = self.client.get(self.url).json()
        self.assertIsNone(payload["has_data"])
        self.assertEqual(payload["error"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertIn("open the tab to retry", payload["message"])
        self.assertEqual(
            cache_set.call_args.kwargs["timeout"], view_utils.ARCHIVE_STATUS_FAILURE_CACHE_SECONDS
        )

    def test_lookup_exception_is_reported_as_an_error(self):
        with mock.patch("YSE_App.common.mast_query.hstImages", _BrokenHst):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["has_data"])
        self.assertEqual(payload["error"], "lookup_failed")

    def test_chandra_timeout_is_reported_as_an_error(self):
        with mock.patch.object(view_utils, "_archive_status_with_timeout", return_value=None):
            payload = self.client.get(reverse("get_chandra_status", args=[self.transient.id])).json()
        self.assertIsNone(payload["has_data"])
        self.assertEqual(payload["error"], "timeout")

    def test_unknown_transient_is_404(self):
        self.assertEqual(self.client.get(reverse("get_hst_status", args=[987654321])).status_code, 404)


class HstImageViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("hst_image_user")
        cls.transient = create_minimal_transient(cls.user, name="2026hstimage")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_mast_failure_is_a_502_with_a_message(self):
        with mock.patch("YSE_App.common.mast_query.hstImages", _BrokenHst):
            response = self.client.get(reverse("get_hst_image", args=[self.transient.id]))
        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["error"], "lookup_failed")
        self.assertIn("MAST", payload["message"])
        self.assertEqual(payload["jpegurl"], [])

    def test_unknown_transient_is_404_not_a_name_error(self):
        self.assertEqual(self.client.get(reverse("get_hst_image", args=[987654321])).status_code, 404)


class DetailPageArchiveLabelJsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("hst_label_js_user")
        cls.transient = create_minimal_transient(cls.user, name="2026hstlabel")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _detail_html(self, defer):
        env = {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}
        with mock.patch.dict(os.environ, env):
            response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def test_deferred_page_distinguishes_lookup_failure_from_no_data(self):
        html = self._detail_html(defer=True)
        self.assertIn("HST (lookup failed)", html)
        self.assertIn("Chandra (lookup failed)", html)
        self.assertIn("json.error", html)
        self.assertIn("yse-hst-retry", html)
        self.assertIn(reverse("get_hst_status", args=[self.transient.id]), html)

    def test_inline_page_reports_a_failed_image_lookup(self):
        html = self._detail_html(defer=False)
        self.assertIn("HST (lookup failed)", html)
        self.assertIn(reverse("get_hst_image", args=[self.transient.id]), html)
