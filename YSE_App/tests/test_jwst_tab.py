"""JWST tab on the transient detail page (#328, #329, #330).

The tab mirrors the HST one: ``get_jwst_status`` labels the tab from a cached
count with the shared archive-status timeout/error handling, and
``get_jwst_observations`` lists the MAST observations when the tab is opened.
MAST is never contacted here: ``common.mast_query.jwstObservations`` (and
``astroquery.mast.Observations.query_criteria`` for the helper itself) are
replaced with recorded answers.
"""

import os
from unittest import mock
from urllib.parse import unquote

import numpy as np
from astropy.table import MaskedColumn, Table
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from YSE_App import view_utils
from YSE_App.common import mast_query
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user
from YSE_App.tests.js_syntax_utils import bracket_imbalance, inline_script_bodies

RECORDED_ROWS = [
    {
        'obs_id': 'jw01234-o001_t001_nircam_clear-f150w', 'instrument_name': 'NIRCAM/IMAGE',
        'filters': 'CLEAR;F150W', 't_min': 60123.4567, 't_exptime': 1288.6, 'proposal_id': '1234',
        'proposal_pi': 'Example, PI', 'target_name': 'SN-HOST', 'dataproduct_type': 'image',
        'calib_level': 3, 'jpegURL': 'mast:JWST/product/jw01234-o001_t001_nircam_clear-f150w_i2d.jpg',
        'dataURL': 'mast:JWST/product/jw01234-o001_t001_nircam_clear-f150w_i2d.fits',
        'obsid': 1, 'obs_collection': 'JWST',
    },
    {
        'obs_id': 'jw01234-o002_s00001_nirspec_g140m-f100lp', 'instrument_name': 'NIRSPEC/MSA',
        'filters': 'G140M;F100LP', 't_min': 60130.1, 't_exptime': 2917.7, 'proposal_id': '1234',
        'proposal_pi': 'Example, PI', 'target_name': 'SN-HOST', 'dataproduct_type': 'spectrum',
        'calib_level': 3, 'jpegURL': None,
        'dataURL': 'mast:JWST/product/jw01234-o002_s00001_nirspec_g140m-f100lp_x1d.fits',
        'obsid': 2, 'obs_collection': 'JWST',
    },
]


class _FakeJwst:
    """Stand-in for common.mast_query.jwstObservations answering two rows."""

    answer = RECORDED_ROWS

    def __init__(self, ra, dec, radius=None):
        self.rows = []

    @property
    def count(self):
        return len(self.rows)

    def query(self):
        self.rows = [mast_query.MastObservations.rows_from_table(_table(self.answer))[i]
                     for i in range(len(self.answer))]
        return self.rows


class _EmptyJwst(_FakeJwst):
    answer = []


class _BrokenJwst(_FakeJwst):
    def query(self):
        raise ConnectionError("MAST unreachable")


def _table(rows):
    """An astropy table shaped like Observations.query_criteria's answer."""
    if not rows:
        return Table(names=mast_query.MastObservations.columns)
    table = Table(rows=[[r.get(c) for c in mast_query.MastObservations.columns] for r in rows],
                  names=mast_query.MastObservations.columns)
    # jpegURL is a masked string column in real answers; mask the None cell.
    jpeg = [r.get('jpegURL') or '' for r in rows]
    table['jpegURL'] = MaskedColumn(jpeg, mask=[not v for v in jpeg])
    return table


class MastObservationsHelperTests(SimpleTestCase):
    """common.mast_query.MastObservations with a recorded query_criteria answer."""

    def test_query_uses_the_jwst_collection_and_the_hst_radius(self):
        with mock.patch.object(mast_query.Observations, 'query_criteria',
                               return_value=_table(RECORDED_ROWS)) as query:
            jwst = mast_query.jwstObservations(199.8674542, -13.7236833)
            rows = jwst.query()
        self.assertEqual(jwst.count, 2)
        kwargs = query.call_args.kwargs
        self.assertEqual(kwargs['obs_collection'], ['JWST'])
        self.assertEqual(kwargs['radius'], mast_query.instrument_defaults['radius'])
        self.assertEqual(kwargs['dataproduct_type'], ['image', 'spectrum'])
        self.assertEqual(kwargs['intentType'], 'science')
        self.assertAlmostEqual(kwargs['coordinates'].ra.degree, 199.8674542)
        self.assertEqual([r['obs_id'] for r in rows], [r['obs_id'] for r in RECORDED_ROWS])

    def test_rows_carry_dates_and_links(self):
        rows = mast_query.MastObservations.rows_from_table(_table(RECORDED_ROWS))
        image, spectrum = rows
        self.assertEqual(image['obsdate'], '2023-06-28 10:57:38')
        self.assertEqual(image['instrument_name'], 'NIRCAM/IMAGE')
        self.assertEqual(image['proposal_id'], '1234')
        self.assertEqual(image['calib_level'], 3)
        self.assertIsInstance(image['t_exptime'], float)
        self.assertEqual(
            image['previewurl'],
            'https://mast.stsci.edu/api/v0.1/Download/file?uri='
            'mast%3AJWST%2Fproduct%2Fjw01234-o001_t001_nircam_clear-f150w_i2d.jpg',
        )
        self.assertTrue(image['dataurl'].endswith('_i2d.fits'))
        self.assertIn('Portal.html?searchQuery=', image['portalurl'])
        self.assertIn('"obs_id"', unquote(image['portalurl']))
        self.assertIn(image['obs_id'], unquote(image['portalurl']))
        self.assertIsNone(spectrum['previewurl'], 'a masked jpegURL is no preview')
        self.assertEqual(spectrum['dataproduct_type'], 'spectrum')

    def test_rows_are_sorted_by_start_time_and_masked_cells_are_none(self):
        table = _table(list(reversed(RECORDED_ROWS)))
        table['t_exptime'] = MaskedColumn([1.0, 2.0], mask=[True, False])
        rows = mast_query.MastObservations.rows_from_table(table)
        self.assertEqual([r['t_min'] for r in rows], [60123.4567, 60130.1])
        self.assertIsNone(rows[1]['t_exptime'])

    def test_empty_table_is_no_rows(self):
        self.assertEqual(mast_query.MastObservations.rows_from_table(_table([])), [])
        self.assertEqual(mast_query.MastObservations.rows_from_table(None), [])

    def test_http_urls_pass_through_mast_file_url(self):
        self.assertEqual(mast_query.mast_file_url('https://x/y.jpg'), 'https://x/y.jpg')
        self.assertIsNone(mast_query.mast_file_url('--'))
        self.assertIsNone(mast_query.mast_file_url(np.ma.masked))


class JwstStatusViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("jwst_status_user")
        cls.transient = create_minimal_transient(cls.user, name="2026jwststatus")

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("get_jwst_status", args=[self.transient.id])

    def test_answer_is_reported_and_cached_for_an_hour(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _FakeJwst), mock.patch.object(
            view_utils.cache, "set", wraps=view_utils.cache.set
        ) as cache_set:
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"has_data": True, "count": 2})
        cache_set.assert_called_once()
        self.assertEqual(cache_set.call_args.args[0], f"jwst_status_v1_{self.transient.id}")
        self.assertEqual(cache_set.call_args.kwargs["timeout"], view_utils.ARCHIVE_STATUS_CACHE_SECONDS)

    def test_cached_answer_skips_mast(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _FakeJwst):
            self.client.get(self.url)
        with mock.patch("YSE_App.common.mast_query.jwstObservations", side_effect=_BrokenJwst) as broken:
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"has_data": True, "count": 2})
        broken.assert_not_called()

    def test_no_data_is_reported_as_no_data(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _EmptyJwst):
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"has_data": False, "count": 0})

    def test_timeout_is_reported_as_an_error_not_as_no_data(self):
        with mock.patch.object(view_utils, "_archive_status_with_timeout", return_value=None), mock.patch.object(
            view_utils.cache, "set", wraps=view_utils.cache.set
        ) as cache_set:
            payload = self.client.get(self.url).json()
        self.assertIsNone(payload["has_data"])
        self.assertEqual(payload["error"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertIn("JWST", payload["message"])
        self.assertEqual(
            cache_set.call_args.kwargs["timeout"], view_utils.ARCHIVE_STATUS_FAILURE_CACHE_SECONDS
        )

    def test_lookup_exception_is_reported_as_an_error_and_cached_briefly(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _BrokenJwst), mock.patch.object(
            view_utils.cache, "set", wraps=view_utils.cache.set
        ) as cache_set:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["has_data"])
        self.assertEqual(payload["error"], "lookup_failed")
        self.assertEqual(
            cache_set.call_args.kwargs["timeout"], view_utils.ARCHIVE_STATUS_FAILURE_CACHE_SECONDS
        )

    def test_unknown_transient_is_404(self):
        self.assertEqual(self.client.get(reverse("get_jwst_status", args=[987654321])).status_code, 404)

    def test_login_is_required(self):
        response = Client().get(self.url)
        self.assertIn(response.status_code, (302, 403))


class JwstObservationsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("jwst_obs_user")
        cls.transient = create_minimal_transient(cls.user, name="2026jwstobs")

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("get_jwst_observations", args=[self.transient.id])

    def test_rows_carry_the_tab_columns(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _FakeJwst):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["rows"]), 2)
        image = payload["rows"][0]
        self.assertEqual(set(image), set(view_utils.JWST_OBSERVATION_FIELDS))
        self.assertEqual(image["inst"], "NIRCAM/IMAGE")
        self.assertEqual(image["filters"], "CLEAR;F150W")
        self.assertEqual(image["obsdate"], "2023-06-28 10:57:38")
        self.assertEqual(image["program"], "1234")
        self.assertEqual(image["target"], "SN-HOST")
        self.assertAlmostEqual(image["exptime"], 1288.6)
        self.assertEqual(image["product"], "image")
        self.assertIn("Download/file?uri=", image["previewurl"])
        self.assertIn("Download/file?uri=", image["dataurl"])
        self.assertIn("Portal.html", image["portalurl"])
        self.assertIsNone(payload["rows"][1]["previewurl"])

    def test_successful_answer_is_cached_and_reused(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _FakeJwst):
            first = self.client.get(self.url).json()
        self.assertEqual(cache.get(f"jwst_observations_v1_{self.transient.id}"), first)
        with mock.patch("YSE_App.common.mast_query.jwstObservations", side_effect=_BrokenJwst) as broken:
            second = self.client.get(self.url)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first)
        broken.assert_not_called()

    def test_no_observations_is_an_empty_list(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _EmptyJwst):
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"count": 0, "rows": []})

    def test_mast_failure_is_a_502_with_a_message_and_is_not_cached(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _BrokenJwst):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["error"], "lookup_failed")
        self.assertIn("MAST", payload["message"])
        self.assertEqual(payload["rows"], [])
        self.assertIsNone(cache.get(f"jwst_observations_v1_{self.transient.id}"))

    def test_timeout_is_a_504_with_a_message(self):
        with mock.patch.object(view_utils, "_archive_status_with_timeout", return_value=None):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 504)
        payload = response.json()
        self.assertEqual(payload["error"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["count"], 0)

    def test_unknown_transient_is_404(self):
        self.assertEqual(self.client.get(reverse("get_jwst_observations", args=[987654321])).status_code, 404)


class HstImageViewSharedHelperTests(TestCase):
    """get_hst_image now runs through the same helper: timeouts become 504s."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("hst_shared_user")
        cls.transient = create_minimal_transient(cls.user, name="2026hstshared")

    def test_timeout_is_a_504_with_the_empty_payload_shape(self):
        client = Client()
        client.force_login(self.user)
        with mock.patch.object(view_utils, "_archive_status_with_timeout", return_value=None):
            response = client.get(reverse("get_hst_image", args=[self.transient.id]))
        self.assertEqual(response.status_code, 504)
        payload = response.json()
        self.assertEqual(payload["error"], "timeout")
        for key in ("jpegurl", "fitsurl", "obsdate", "filters", "inst"):
            self.assertEqual(payload[key], [])


class DetailPageJwstTabTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("jwst_page_user")
        cls.transient = create_minimal_transient(cls.user, name="2026jwstpage")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _detail_html(self, defer):
        env = {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}
        with mock.patch.dict(os.environ, env):
            response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def _assert_tab_markup(self, html):
        self.assertIn('id="jwst_tab_header"', html)
        self.assertIn('id="jwst_tab"', html)
        self.assertIn('id="jwst_observation_list"', html)
        self.assertIn("JWST (lookup failed)", html)
        self.assertIn("No JWST", html)
        self.assertIn("yse-jwst-retry", html)
        self.assertIn(reverse("get_jwst_observations", args=[self.transient.id]), html)
        # The JWST tab sits right after the HST one.
        self.assertLess(html.index('id="hst_tab_header"'), html.index('id="jwst_tab_header"'))
        self.assertLess(html.index('id="jwst_tab_header"'), html.index('id="chandra_tab_header"'))
        for body in inline_script_bodies(html):
            self.assertEqual(bracket_imbalance(body), [])

    def test_deferred_page_labels_the_tab_from_the_status_endpoint(self):
        html = self._detail_html(defer=True)
        self._assert_tab_markup(html)
        self.assertIn(reverse("get_jwst_status", args=[self.transient.id]), html)
        self.assertIn("yseLoadJwstObservations", html)

    def test_inline_page_loads_the_observations_directly(self):
        html = self._detail_html(defer=False)
        self._assert_tab_markup(html)
        self.assertNotIn(reverse("get_jwst_status", args=[self.transient.id]), html)
        self.assertIn("loadJwstObservations();", html)
