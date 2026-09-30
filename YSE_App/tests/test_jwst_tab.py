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


def _row(obs_id, instrument_name, filters, dataproduct_type='image', t_min=60200.0):
    """A MAST-shaped observation row for the mode-selection tests (#387)."""
    return {
        'obs_id': obs_id, 'instrument_name': instrument_name, 'filters': filters,
        't_min': t_min, 't_exptime': 1000.0, 'proposal_id': '4321', 'proposal_pi': 'Example, PI',
        'target_name': 'SN-HOST', 'dataproduct_type': dataproduct_type, 'calib_level': 3,
        'jpegURL': None, 'dataURL': f'mast:JWST/product/{obs_id}.fits', 'obsid': hash(obs_id) % 10000,
        'obs_collection': 'JWST',
    }


# What MAST really hands back for a well-observed field: NIRSpec and the MIRI
# spectroscopic modes come labelled ``dataproduct_type='image'`` too, so the
# product type alone does not separate images from spectra (#387).
MIXED_ROWS = [
    _row('jw04321-o001_nircam_f200w', 'NIRCAM/IMAGE', 'CLEAR;F200W', t_min=60201.0),
    _row('jw04321-o002_nirspec_msa', 'NIRSPEC/MSA', 'G140M;F100LP', t_min=60202.0),
    _row('jw04321-o003_nirspec_ifu', 'NIRSPEC/IFU', 'G235H;F170LP', t_min=60203.0),
    _row('jw04321-o004_nirspec_image', 'NIRSPEC/IMAGE', 'CLEAR;F110W', t_min=60204.0),
    _row('jw04321-o005_miri_f770w', 'MIRI/IMAGE', 'F770W', t_min=60205.0),
    _row('jw04321-o006_miri_mrs', 'MIRI/IFU', 'SHORT;MEDIUM;LONG', t_min=60206.0),
    _row('jw04321-o007_miri_lrs', 'MIRI/SLIT', 'P750L', t_min=60207.0),
    _row('jw04321-o008_miri_lrs_slitless', 'MIRI/SLITLESS', 'P750L', t_min=60208.0),
    _row('jw04321-o009_miri_image_p750l', 'MIRI/IMAGE', 'P750L', t_min=60209.0),
    _row('jw04321-o010_nircam_grism', 'NIRCAM/GRISM', 'GRISMR;F322W2', t_min=60210.0),
    _row('jw04321-o011_nircam_image_grism', 'NIRCAM/IMAGE', 'GRISMC;F444W', t_min=60211.0),
    _row('jw04321-o012_niriss_wfss', 'NIRISS/WFSS', 'GR150R;F200W', t_min=60212.0),
    _row('jw04321-o013_niriss_soss', 'NIRISS/SOSS', 'GR700XD;CLEAR', t_min=60213.0),
    _row('jw04321-o014_miri_targacq', 'MIRI/TARGACQ', 'F560W', t_min=60214.0),
    _row('jw04321-o015_nircam_spectrum', 'NIRCAM/IMAGE', 'CLEAR;F150W', 'spectrum', t_min=60215.0),
    _row('jw04321-o016_miri_coron', 'MIRI/CORON', 'F1140C', t_min=60216.0),
    _row('jw04321-o017_niriss_ami', 'NIRISS/AMI', 'NRM;F480M', t_min=60217.0),
    _row('jw04321-o018_new_mode', 'NIRCAM/NEWMODE', 'CLEAR;F200W', t_min=60218.0),
]
MIXED_IMAGE_OBS_IDS = ['jw04321-o001_nircam_f200w', 'jw04321-o005_miri_f770w',
                       'jw04321-o016_miri_coron', 'jw04321-o017_niriss_ami']


class _FakeJwst(mast_query.JwstImages):
    """Stand-in for common.mast_query.jwstObservations: the real images-only
    helper fed a recorded MAST answer holding one NIRCam image and one NIRSpec
    spectrum. Only the image may come out (#356, #387)."""

    answer = RECORDED_ROWS

    def query(self):
        return self.set_table(_table(self.answer))


class _MixedJwst(_FakeJwst):
    answer = MIXED_ROWS


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

    def test_query_asks_for_jwst_images_within_the_hst_radius(self):
        with mock.patch.object(mast_query.Observations, 'query_criteria',
                               return_value=_table(RECORDED_ROWS[:1])) as query:
            jwst = mast_query.jwstObservations(199.8674542, -13.7236833)
            rows = jwst.query()
        self.assertEqual(jwst.count, 1)
        kwargs = query.call_args.kwargs
        self.assertEqual(kwargs['obs_collection'], ['JWST'])
        self.assertEqual(kwargs['radius'], mast_query.instrument_defaults['radius'])
        self.assertEqual(kwargs['dataproduct_type'], ['image'], 'images only, no spectra (#356)')
        self.assertEqual(kwargs['instrument_name'], sorted(mast_query.JWST_IMAGING_MODES),
                         'MAST is asked for the imaging modes only (#387)')
        self.assertEqual(kwargs['intentType'], 'science')
        self.assertAlmostEqual(kwargs['coordinates'].ra.degree, 199.8674542)
        self.assertEqual([r['obs_id'] for r in rows], [RECORDED_ROWS[0]['obs_id']])

    def test_spectra_returned_by_mast_are_dropped(self):
        """Even if MAST hands back a spectrum row, the tab never lists it."""
        with mock.patch.object(mast_query.Observations, 'query_criteria',
                               return_value=_table(RECORDED_ROWS)):
            jwst = mast_query.jwstObservations(199.8674542, -13.7236833)
            rows = jwst.query()
        self.assertEqual(jwst.count, 1)
        self.assertEqual([r['dataproduct_type'] for r in rows], ['image'])
        self.assertEqual(len(jwst.obstable), 2, 'the raw MAST table is kept for inspection')

    def test_product_types_can_be_widened_explicitly(self):
        jwst = mast_query.MastObservations(10.0, 20.0, collections=['JWST'],
                                           product_types=('image', 'spectrum'))
        self.assertEqual(len(jwst.set_table(_table(RECORDED_ROWS))), 2)
        self.assertEqual(len(mast_query.MastObservations(10.0, 20.0, collections=['JWST'],
                                                         product_types=None).set_table(_table(RECORDED_ROWS))), 2)

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


class JwstImagingSelectionTests(SimpleTestCase):
    """is_jwst_image / JwstImages: images only, whatever MAST calls the product (#387)."""

    def test_allowlist_is_the_imaging_modes(self):
        self.assertEqual(mast_query.JWST_IMAGING_MODES, {
            'NIRCAM/IMAGE', 'NIRCAM/CORON', 'MIRI/IMAGE', 'MIRI/CORON', 'NIRISS/IMAGE', 'NIRISS/AMI'})
        self.assertFalse(any(m.startswith('NIRSPEC/') for m in mast_query.JWST_IMAGING_MODES))

    def test_nirspec_is_excluded_whatever_the_product_type(self):
        for mode in ('NIRSPEC/MSA', 'NIRSPEC/SLIT', 'NIRSPEC/IFU', 'NIRSPEC/IMAGE', 'NIRSPEC/TARGACQ'):
            for product in ('image', 'spectrum', 'cube', None):
                row = _row('x', mode, 'CLEAR;F110W', product)
                self.assertFalse(mast_query.is_jwst_image(row), (mode, product))

    def test_miri_keeps_imaging_and_coronagraphy_only(self):
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'MIRI/IMAGE', 'F770W')))
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'MIRI/CORON', 'F1550C')))
        for mode, filters in (('MIRI/IFU', 'SHORT'), ('MIRI/IFU', 'MEDIUM;LONG'), ('MIRI/SLIT', 'P750L'),
                              ('MIRI/SLITLESS', 'P750L'), ('MIRI/TARGACQ', 'F560W')):
            self.assertFalse(mast_query.is_jwst_image(_row('x', mode, filters)), mode)

    def test_spectroscopic_filters_exclude_a_row_even_in_an_imaging_mode(self):
        for filters in ('P750L', 'SHORT', 'MEDIUM', 'LONG', 'SHORT;MEDIUM;LONG', 'GRISMR;F322W2',
                        'GRISMC;F444W', 'GR150R;F200W', 'GR700XD;CLEAR', 'G140M;F100LP', 'G395H;F290LP',
                        'PRISM;CLEAR', 'clear;grismr'):
            self.assertTrue(mast_query.jwst_filters_are_spectroscopic(filters), filters)
            self.assertFalse(mast_query.is_jwst_image(_row('x', 'NIRCAM/IMAGE', filters)), filters)
            self.assertFalse(mast_query.is_jwst_image(_row('x', 'MIRI/IMAGE', filters)), filters)
        for filters in ('CLEAR;F150W', 'F770W', 'F1140C', 'MASK335R;F335M', 'NRM;F480M', 'F150W2;F162M',
                        'F200W', 'F2550W', None, ''):
            self.assertFalse(mast_query.jwst_filters_are_spectroscopic(filters), filters)
        # "LONG" is the MRS grating setting, not a substring rule: F444W or F200LP style names stay.
        self.assertFalse(mast_query.jwst_filters_are_spectroscopic('F200LP'))
        self.assertFalse(mast_query.jwst_filters_are_spectroscopic('F1000W;LONGNAME'), 'whole tokens only')

    def test_nircam_and_niriss_modes(self):
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'NIRCAM/IMAGE', 'CLEAR;F200W')))
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'NIRCAM/CORON', 'MASK335R;F335M')))
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'NIRISS/IMAGE', 'CLEAR;F200W')))
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'NIRISS/AMI', 'NRM;F480M')))
        for mode in ('NIRCAM/GRISM', 'NIRCAM/TARGACQ', 'NIRISS/WFSS', 'NIRISS/SOSS'):
            self.assertFalse(mast_query.is_jwst_image(_row('x', mode, 'CLEAR;F200W')), mode)

    def test_unknown_modes_and_non_image_products_are_out(self):
        self.assertFalse(mast_query.is_jwst_image(_row('x', 'NIRCAM/NEWMODE', 'CLEAR;F200W')))
        self.assertFalse(mast_query.is_jwst_image(_row('x', 'FGS/IMAGE', 'CLEAR')))
        self.assertFalse(mast_query.is_jwst_image(_row('x', None, 'CLEAR;F200W')))
        self.assertFalse(mast_query.is_jwst_image(_row('x', 'NIRCAM/IMAGE', 'CLEAR;F150W', 'spectrum')))
        self.assertFalse(mast_query.is_jwst_image(_row('x', 'NIRCAM/IMAGE', 'CLEAR;F150W', None)))
        # MAST casing is upper, but a differently cased answer is judged the same way.
        self.assertTrue(mast_query.is_jwst_image(_row('x', 'nircam/image', 'clear;f150w', 'Image')))

    def test_jwst_images_keeps_only_the_image_rows_of_a_mixed_answer(self):
        jwst = mast_query.JwstImages(199.8674542, -13.7236833)
        rows = jwst.set_table(_table(MIXED_ROWS))
        self.assertEqual([r['obs_id'] for r in rows], MIXED_IMAGE_OBS_IDS)
        self.assertEqual(jwst.count, 4)
        self.assertEqual({r['instrument_name'] for r in rows},
                         {'NIRCAM/IMAGE', 'MIRI/IMAGE', 'MIRI/CORON', 'NIRISS/AMI'})
        self.assertEqual(len(jwst.obstable), len(MIXED_ROWS), 'the raw MAST table is kept for inspection')
        self.assertIsInstance(mast_query.jwstObservations(1.0, 2.0), mast_query.JwstImages)

    def test_plain_mast_observations_still_keeps_every_image_typed_row(self):
        """The allowlist is a JWST rule; the generic helper only checks the product type."""
        generic = mast_query.MastObservations(1.0, 2.0, collections=['JWST'])
        self.assertEqual(len(generic.set_table(_table(MIXED_ROWS))), len(MIXED_ROWS) - 1)

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
        self.assertEqual(payload, {"has_data": True, "count": 1}, "the NIRSpec spectrum is not counted")
        cache_set.assert_called_once()
        self.assertEqual(cache_set.call_args.args[0], f"jwst_status_v2_{self.transient.id}")
        self.assertEqual(cache_set.call_args.kwargs["timeout"], view_utils.ARCHIVE_STATUS_CACHE_SECONDS)

    def test_cached_answer_skips_mast(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _FakeJwst):
            self.client.get(self.url)
        with mock.patch("YSE_App.common.mast_query.jwstObservations", side_effect=_BrokenJwst) as broken:
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"has_data": True, "count": 1})
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
        self.assertEqual(payload["count"], 1, "images only: the spectrum row is excluded (#356)")
        self.assertEqual(len(payload["rows"]), 1)
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
        self.assertEqual([r["product"] for r in payload["rows"]], ["image"])

    def test_successful_answer_is_cached_and_reused(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _FakeJwst):
            first = self.client.get(self.url).json()
        self.assertEqual(cache.get(f"jwst_observations_v2_{self.transient.id}"), first)
        with mock.patch("YSE_App.common.mast_query.jwstObservations", side_effect=_BrokenJwst) as broken:
            second = self.client.get(self.url)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first)
        broken.assert_not_called()

    def test_no_observations_is_an_empty_list(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _EmptyJwst):
            payload = self.client.get(self.url).json()
        self.assertEqual(payload, {"count": 0, "rows": []})

    def test_body_label_and_has_jwst_agree_on_a_mixed_mast_answer(self):
        """NIRSpec / MIRI spectroscopy never reaches the tab, its label or the flag (#387)."""
        status_url = reverse("get_jwst_status", args=[self.transient.id])
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _MixedJwst):
            body = self.client.get(self.url).json()
            status = self.client.get(status_url).json()
        self.assertEqual([r["obs_id"] for r in body["rows"]], MIXED_IMAGE_OBS_IDS)
        self.assertEqual([r["inst"] for r in body["rows"]],
                         ["NIRCAM/IMAGE", "MIRI/IMAGE", "MIRI/CORON", "NIRISS/AMI"])
        self.assertEqual(body["count"], 4)
        self.assertEqual(status, {"has_data": True, "count": 4})
        self.transient.refresh_from_db()
        self.assertIs(self.transient.has_jwst, True)
        listed = " ".join(r["inst"] + " " + (r["filters"] or "") for r in body["rows"])
        for word in ("NIRSPEC", "IFU", "SLIT", "GRISM", "WFSS", "SOSS", "P750L", "TARGACQ"):
            self.assertNotIn(word, listed)

    def test_spectroscopy_only_field_is_no_jwst(self):
        """A position with NIRSpec and MIRI MRS/LRS coverage but no image: No JWST, has_jwst False."""
        spectra_only = type("_SpectraOnly", (_FakeJwst,), {
            "answer": [r for r in MIXED_ROWS if r["obs_id"] not in MIXED_IMAGE_OBS_IDS]})
        status_url = reverse("get_jwst_status", args=[self.transient.id])
        with mock.patch("YSE_App.common.mast_query.jwstObservations", spectra_only):
            body = self.client.get(self.url).json()
            status = self.client.get(status_url).json()
        self.assertEqual(body, {"count": 0, "rows": []})
        self.assertEqual(status, {"has_data": False, "count": 0})
        self.transient.refresh_from_db()
        self.assertIs(self.transient.has_jwst, False)

    def test_mast_failure_is_a_502_with_a_message_and_is_not_cached(self):
        with mock.patch("YSE_App.common.mast_query.jwstObservations", _BrokenJwst):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["error"], "lookup_failed")
        self.assertIn("MAST", payload["message"])
        self.assertEqual(payload["rows"], [])
        self.assertIsNone(cache.get(f"jwst_observations_v2_{self.transient.id}"))

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
        self.assertIn("JWST Images for", html)
        self.assertNotIn("Filter / grating", html)
        self.assertIn("yse-jwst-retry", html)
        self.assertIn(reverse("get_jwst_observations", args=[self.transient.id]), html)
        # No counts on tab labels (#387): "JWST Data", "Annotations", never "(N)".
        self.assertNotIn("JWST Data (", html)
        self.assertNotIn("countLabel", html)
        self.assertNotIn("' (' + json.count", html)
        self.assertNotIn("Annotations (", html)
        self.assertNotIn("_tab_header').text('Annotations' +", html)
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
