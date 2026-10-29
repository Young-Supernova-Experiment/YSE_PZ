"""Spectra stored as arrays instead of one row per pixel (#492, #891): the helper, the writer, the readers that go
through it, and the ``convert_spectra_to_arrays`` command. Synthetic data only; no external services."""

import io
import zipfile

import numpy as np
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.common import spectrum_arrays
from YSE_App.models import (
    HostSpecData,
    HostSpectrum,
    HostSpectrumArrays,
    TransientSpecData,
    TransientSpectrumArrays,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_spectrum,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

READER = "spectrum_arrays_user"


def _pid(record):
    """A pixel record's id: the serializers are hyperlinked, so it is the last part of ``url``."""
    return int(record["url"].rstrip("/").rsplit("/", 1)[-1])


def _rows(user, spectrum, n, *, err=True, start=4000.0, model=TransientSpecData):
    model.objects.bulk_create([
        model(spectrum=spectrum, wavelength=start + 10.0 * i, flux=1.0 + 0.1 * i,
              flux_err=(0.05 if err else None), **audit_fields(user))
        for i in range(n)
    ])


class PackBuildTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(READER)
        cls.transient = create_minimal_transient(cls.user, name="2026arr")
        cls.spectrum = attach_synthetic_spectrum(cls.user, cls.transient)

    def test_pack_unpack_round_trip(self):
        values = np.array([3500.125, 1e-17, -2.5, 0.0, 9.87654321e5])
        out = spectrum_arrays.unpack(spectrum_arrays.pack(values))
        np.testing.assert_array_equal(out, values)
        self.assertEqual(out.dtype, np.dtype('<f8'))

    def test_build_sorts_and_drops_non_finite(self):
        row = spectrum_arrays.build(self.spectrum, [5000.0, 4000.0, np.nan, 4500.0, 6000.0],
                                    [3.0, 1.0, 9.0, 2.0, np.inf], [0.3, None, 0.9, 0.2, 0.4])
        self.assertEqual(row.n_pixels, 3)
        self.assertEqual((row.wavelength_min, row.wavelength_max), (4000.0, 5000.0))
        np.testing.assert_array_equal(spectrum_arrays.unpack(row.wavelength), [4000.0, 4500.0, 5000.0])
        np.testing.assert_array_equal(spectrum_arrays.unpack(row.flux), [1.0, 2.0, 3.0])
        err = spectrum_arrays.unpack(row.flux_err)
        self.assertTrue(np.isnan(err[0]))
        np.testing.assert_array_equal(err[1:], [0.2, 0.3])

    def test_build_without_errors_and_empty(self):
        row = spectrum_arrays.build(self.spectrum, [1.0, 2.0], [1.0, 2.0], [None, None])
        self.assertIsNone(row.flux_err)
        with self.assertRaises(ValueError):
            spectrum_arrays.build(self.spectrum, [np.nan], [1.0])


class ArraysForTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(READER)
        cls.transient = create_minimal_transient(cls.user, name="2026ars")
        cls.array_spec = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.row_spec = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.empty_spec = attach_synthetic_spectrum(cls.user, cls.transient)
        spectrum_arrays.save(cls.array_spec, [6000.0, 5000.0, 7000.0], [2.0, 1.0, 3.0], [0.2, 0.1, None])
        _rows(cls.user, cls.row_spec, 4, err=False)

    def test_mixes_array_and_row_storage(self):
        out = spectrum_arrays.arrays_for([self.array_spec.id, self.row_spec.id, self.empty_spec.id])
        self.assertEqual(set(out), {self.array_spec.id, self.row_spec.id})
        w, f, e = out[self.array_spec.id]
        np.testing.assert_array_equal(w, [5000.0, 6000.0, 7000.0])
        np.testing.assert_array_equal(f, [1.0, 2.0, 3.0])
        np.testing.assert_array_equal(e[:2], [0.1, 0.2])
        self.assertTrue(np.isnan(e[2]))
        w, f, e = out[self.row_spec.id]
        np.testing.assert_array_equal(w, [4000.0, 4010.0, 4020.0, 4030.0])
        self.assertIsNone(e)

    def test_array_row_wins_over_rows(self):
        _rows(self.user, self.array_spec, 2)
        w, _f, _e = spectrum_arrays.arrays_for([self.array_spec.id])[self.array_spec.id]
        self.assertEqual(w.size, 3)

    def test_pixel_counts(self):
        counts = spectrum_arrays.pixel_counts([self.array_spec.id, self.row_spec.id, self.empty_spec.id])
        self.assertEqual(counts, {self.array_spec.id: 3, self.row_spec.id: 4})

    def test_discard(self):
        self.assertEqual(spectrum_arrays.discard([self.array_spec.id]), 1)
        self.assertFalse(spectrum_arrays.has_pixels(self.array_spec.id))
        self.assertTrue(spectrum_arrays.has_pixels(self.row_spec.id))

    def test_plot_points_for_array_only_spectrum(self):
        from YSE_App import view_utils

        points = view_utils._spec_points_by_spectrum([self.array_spec.id, self.row_spec.id, self.empty_spec.id])
        self.assertEqual(set(points), {self.array_spec.id, self.row_spec.id})
        np.testing.assert_array_equal(points[self.array_spec.id][0], [5000.0, 6000.0, 7000.0])
        spectra, n_without = view_utils._spectra_for_plot([self.array_spec, self.row_spec, self.empty_spec])
        self.assertEqual(len(spectra), 2)
        self.assertEqual(n_without, 1)

    def test_cache_token_changes_when_arrays_are_written(self):
        from YSE_App import view_utils

        before = view_utils._transient_spectrum_cache_token(self.transient.id)
        spectrum_arrays.save(self.empty_spec, [1.0, 2.0], [1.0, 1.0])
        self.assertNotEqual(before, view_utils._transient_spectrum_cache_token(self.transient.id))


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER])
class DownloadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(READER)
        cls.transient = create_minimal_transient(cls.user, name="2026art")
        cls.spec = attach_synthetic_spectrum(cls.user, cls.transient)
        spectrum_arrays.save(cls.spec, [5000.0, 4000.0], [2.0e-16, 1.0e-16], [None, 3.0e-18])

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_download_spectra_csv_from_arrays(self):
        r = self.client.get(reverse("download_spectra", args=[self.transient.slug]))
        self.assertEqual(r.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(len(archive.namelist()), 1)
        content = archive.read(archive.namelist()[0]).decode()
        body = content.split("wavelength,flux,fluxerr\n", 1)[1].strip().splitlines()
        self.assertEqual(body, ["4000.000,1.00000e-16,3.00000e-18", "5000.000,2.00000e-16,-9.90000e+01"])

    def test_download_data_json_from_arrays(self):
        r = self.client.get(reverse("download_data", args=[self.transient.slug]))
        self.assertEqual(r.status_code, 200)
        spectra = r.json()[self.transient.name]["spectra"]
        self.assertEqual(len(spectra), 1)
        data = spectra[0]["data"]
        self.assertEqual([d["fields"]["wavelength"] for d in data], [4000.0, 5000.0])
        self.assertEqual([d["fields"]["flux_err"] for d in data], [3.0e-18, None])
        self.assertEqual(data[0]["model"], "YSE_App.transientspecdata")
        self.assertEqual(data[0]["fields"]["spectrum"], self.spec.id)


class ConvertCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(READER)
        cls.transient = create_minimal_transient(cls.user, name="2026aru")
        cls.spec_a = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.spec_b = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.spec_empty = attach_synthetic_spectrum(cls.user, cls.transient)
        _rows(cls.user, cls.spec_a, 5)
        _rows(cls.user, cls.spec_b, 3, err=False)
        host = attach_synthetic_host(cls.user, cls.transient)
        cls.host_spec = HostSpectrum.objects.create(
            host=host, instrument=cls.spec_a.instrument, obs_group=cls.spec_a.obs_group,
            ra=host.ra, dec=host.dec, obs_date=cls.spec_a.obs_date, **audit_fields(cls.user))
        _rows(cls.user, cls.host_spec, 4, model=HostSpecData)

    def _call(self, *args):
        out = io.StringIO()
        call_command("convert_spectra_to_arrays", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        out = self._call("--delete-rows")
        self.assertIn("transient spectra to convert: 2", out)
        self.assertIn("host spectra to convert: 1", out)
        self.assertEqual(TransientSpectrumArrays.objects.count(), 0)
        self.assertEqual(HostSpectrumArrays.objects.count(), 0)
        self.assertEqual(TransientSpecData.objects.count(), 8)

    def test_apply_converts_and_is_idempotent(self):
        self._call("--apply", "--batch", "1")
        self.assertEqual(set(TransientSpectrumArrays.objects.values_list("spectrum_id", "n_pixels")),
                         {(self.spec_a.id, 5), (self.spec_b.id, 3)})
        self.assertEqual(HostSpectrumArrays.objects.get().n_pixels, 4)
        self.assertEqual(TransientSpecData.objects.count(), 8)  # rows are kept
        before = list(TransientSpectrumArrays.objects.order_by("spectrum_id").values_list("spectrum_id", "modified_date"))
        out = self._call("--apply")
        self.assertIn("transient spectra converted: 0", out)
        after = list(TransientSpectrumArrays.objects.order_by("spectrum_id").values_list("spectrum_id", "modified_date"))
        self.assertEqual(before, after)
        # what readers see is unchanged by the conversion
        w, f, e = spectrum_arrays.arrays_for([self.spec_a.id])[self.spec_a.id]
        np.testing.assert_array_equal(w, 4000.0 + 10.0 * np.arange(5))
        np.testing.assert_allclose(e, 0.05)
        self.assertIsNone(spectrum_arrays.arrays_for([self.spec_b.id])[self.spec_b.id][2])

    def test_selection_and_max(self):
        self._call("--apply", "--transient", "--max", "1")
        self.assertEqual(TransientSpectrumArrays.objects.count(), 1)
        self.assertEqual(HostSpectrumArrays.objects.count(), 0)
        self._call("--apply", "--host")
        self.assertEqual(TransientSpectrumArrays.objects.count(), 1)
        self.assertEqual(HostSpectrumArrays.objects.count(), 1)

    def test_delete_rows_only_verified(self):
        self._call("--apply", "--transient")
        # spec_b gains a row after conversion: its arrays no longer match, so its rows stay
        _rows(self.user, self.spec_b, 1, start=9000.0)
        self._call("--delete-rows")  # dry run deletes nothing
        self.assertEqual(TransientSpecData.objects.count(), 9)
        out = self._call("--apply", "--delete-rows", "--transient")
        self.assertIn("deleted: 5 rows of 1 spectra", out)
        self.assertFalse(TransientSpecData.objects.filter(spectrum=self.spec_a).exists())
        self.assertEqual(TransientSpecData.objects.filter(spectrum=self.spec_b).count(), 4)
        self.assertEqual(HostSpecData.objects.count(), 4)  # host spectra not selected
        self.assertEqual(spectrum_arrays.pixel_counts([self.spec_a.id])[self.spec_a.id], 5)


def _results(response):
    data = response.json()
    return data["results"] if isinstance(data, dict) and "results" in data else data


class ApiPixelTests(TestCase):
    """``/api/transientspecdata/`` and ``/api/hostspecdata/`` list array-stored pixels as per-pixel
    records (#492), paginated with the rows, only for spectra the user may see."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group

        cls.user = create_test_user("spectrum_arrays_api_user", is_staff=False)
        cls.other = create_test_user("spectrum_arrays_api_other", is_staff=False)
        mine, theirs = Group.objects.create(name="arr-api-mine"), Group.objects.create(name="arr-api-theirs")
        cls.user.groups.add(mine)
        cls.other.groups.add(theirs)
        cls.transient = create_minimal_transient(cls.user, name="2026arv")
        cls.array_spec = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.row_spec = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.both_spec = attach_synthetic_spectrum(cls.user, cls.transient)
        cls.hidden_spec = attach_synthetic_spectrum(cls.other, cls.transient)
        for spec in (cls.array_spec, cls.row_spec, cls.both_spec):
            spec.groups.add(mine)
        cls.hidden_spec.groups.add(theirs)
        spectrum_arrays.save(cls.array_spec, [4200.0, 4100.0, 4300.0], [2.0, 1.0, 3.0], [0.2, None, 0.3])
        spectrum_arrays.save(cls.hidden_spec, [5000.0, 5100.0], [1.0, 1.0])
        _rows(cls.user, cls.row_spec, 2)
        _rows(cls.user, cls.both_spec, 2, start=6000.0)
        spectrum_arrays.save(cls.both_spec, [6000.0, 6010.0], [1.0, 1.1], [0.05, 0.05])
        host = attach_synthetic_host(cls.user, cls.transient)
        cls.host_spec = HostSpectrum.objects.create(
            host=host, instrument=cls.array_spec.instrument, obs_group=cls.array_spec.obs_group,
            ra=host.ra, dec=host.dec, obs_date=cls.array_spec.obs_date, **audit_fields(cls.user))
        cls.host_spec.groups.add(mine)
        spectrum_arrays.save(cls.host_spec, [7000.0, 7100.0], [1.0, 2.0], host=True)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_filter_by_spectrum_gives_per_pixel_records(self):
        got = _results(self.client.get("/api/transientspecdata/", {"spectrum": self.array_spec.id}))
        self.assertEqual([p["wavelength"] for p in got], [4100.0, 4200.0, 4300.0])
        self.assertEqual([p["flux"] for p in got], [1.0, 2.0, 3.0])
        self.assertEqual([p["flux_err"] for p in got], [None, 0.2, 0.3])
        self.assertTrue(all(_pid(p) < 0 for p in got))
        self.assertEqual(len({_pid(p) for p in got}), 3)
        self.assertTrue(got[0]["spectrum"].rstrip("/").endswith("/%d" % self.array_spec.id))
        self.assertEqual(got[0]["pixel_mask"], 0)
        self.assertIsNone(got[0]["wavelength_err"])
        rows = _results(self.client.get("/api/transientspecdata/", {"spectrum": self.row_spec.id}))
        self.assertEqual(set(rows[0]), set(got[0]))  # same fields as a real row

    def test_mixed_list_paginates_without_duplicates_or_hidden_spectra(self):
        full = self.client.get("/api/transientspecdata/", {"transient": self.transient.id}).json()
        self.assertEqual(full["count"], 3 + 2 + 2)  # hidden spectrum's pixels are not counted
        pages = []
        for offset in range(0, 7, 2):
            pages += _results(self.client.get("/api/transientspecdata/",
                                              {"transient": self.transient.id, "limit": 2, "offset": offset}))
        self.assertEqual([_pid(p) for p in pages], [_pid(p) for p in full["results"]])
        self.assertEqual(len({_pid(p) for p in pages}), 7)
        # the converted spectrum with rows is listed once, from its rows (real ids)
        both = [p for p in pages if p["wavelength"] >= 6000.0]
        self.assertEqual(len(both), 2)
        self.assertTrue(all(_pid(p) > 0 for p in both))
        self.assertFalse(any(5000.0 <= p["wavelength"] < 5200.0 for p in pages))

    def test_retrieve_pseudo_id(self):
        from YSE_App.services.spectrum_pixel_api import pseudo_pixel_id

        r = self.client.get("/api/transientspecdata/%d/" % pseudo_pixel_id(self.array_spec.id, 1))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["wavelength"], 4200.0)
        hidden = self.client.get("/api/transientspecdata/%d/" % pseudo_pixel_id(self.hidden_spec.id, 0))
        self.assertEqual(hidden.status_code, 404)
        past_end = self.client.get("/api/transientspecdata/%d/" % pseudo_pixel_id(self.array_spec.id, 3))
        self.assertEqual(past_end.status_code, 404)
        patch = self.client.patch("/api/transientspecdata/%d/" % pseudo_pixel_id(self.array_spec.id, 1),
                                  {"flux": 9.0}, content_type="application/json")
        self.assertIn(patch.status_code, (403, 405))

    def test_other_user_sees_only_their_spectrum(self):
        self.client.force_login(self.other)
        got = _results(self.client.get("/api/transientspecdata/", {"transient": self.transient.id}))
        self.assertEqual([p["wavelength"] for p in got], [5000.0, 5100.0])

    def test_host_pixels(self):
        got = _results(self.client.get("/api/hostspecdata/", {"spectrum": self.host_spec.id}))
        self.assertEqual([p["wavelength"] for p in got], [7000.0, 7100.0])
        self.assertTrue(all(_pid(p) < 0 for p in got))


class WritePixelsTests(TestCase):
    """Every upload path writes through ``write_pixels`` (#492)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(READER)
        cls.transient = create_minimal_transient(cls.user, name="2026wpx")
        cls.spectrum = attach_synthetic_spectrum(cls.user, cls.transient)

    def test_writes_arrays_and_replaces_old_pixels(self):
        _rows(self.user, self.spectrum, 4)
        n = spectrum_arrays.write_pixels(self.spectrum, [(5000.0, 2.0, 0.2), (4990.0, 1.0, None)], user=self.user)
        self.assertEqual(n, 2)
        self.assertFalse(TransientSpecData.objects.filter(spectrum=self.spectrum).exists())
        wave, flux, err = spectrum_arrays.arrays_for([self.spectrum.id])[self.spectrum.id]
        self.assertEqual((list(wave), list(flux)), ([4990.0, 5000.0], [1.0, 2.0]))
        self.assertTrue(np.isnan(err[0]))
        self.assertEqual(err[1], 0.2)

    def test_two_columns_store_no_errors(self):
        spectrum_arrays.write_pixels(self.spectrum, [(4000.0, 1.0), (4010.0, 1.5)])
        self.assertIsNone(spectrum_arrays.arrays_for([self.spectrum.id])[self.spectrum.id][2])

    @override_settings(SPECTRUM_PIXEL_STORAGE='rows')
    def test_rollback_switch_writes_rows(self):
        spectrum_arrays.save(self.spectrum, [1.0], [1.0])
        spectrum_arrays.write_pixels(self.spectrum, [(4000.0, 1.0, 0.1), (4010.0, 1.5, 0.1)], user=self.user)
        self.assertFalse(TransientSpectrumArrays.objects.filter(spectrum=self.spectrum).exists())
        self.assertEqual(TransientSpecData.objects.filter(spectrum=self.spectrum).count(), 2)

    def test_empty_upload_writes_nothing(self):
        self.assertEqual(spectrum_arrays.write_pixels(self.spectrum, []), 0)
        self.assertEqual(spectrum_arrays.pixel_counts([self.spectrum.id]), {})
