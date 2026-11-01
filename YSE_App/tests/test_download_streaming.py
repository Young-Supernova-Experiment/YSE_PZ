"""Streamed per-object downloads (#929): ``download_data`` and ``download_photometry``.

The streamed bodies must be byte-for-byte what the old in-memory views returned, for every kind of
reader (session login, Basic auth, a user with a per-point deny rule). The old view bodies are kept
below as reference functions and compared against the new responses. Synthetic data only.
"""

import base64
import datetime
import secrets
from unittest import mock

import numpy as np
from django.contrib.auth.models import Group
from django.core import serializers
from django.core.cache import cache
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.common import spectrum_arrays
from YSE_App.common.utilities import date_to_mjd
from YSE_App.data import PhotometryService, SpectraService
from YSE_App.models import (
    DataAccessRule,
    DataQuality,
    Host,
    TransientPhotData,
    TransientPhotometry,
    TransientSpecData,
    TransientSpectrumArrays,
    Transient,
)
from YSE_App.services import download_stream
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

READER = "dls929_reader"


# --- reference: the view bodies before #929, unchanged apart from taking ``user`` directly ---------

def _legacy_finite_or_none(value):
    value = float(value)
    return value if np.isfinite(value) else None


def _legacy_array_pixels_as_specdata(spectrum_id, arrays):
    wave, flux, err = arrays
    out = []
    for i in range(wave.size):
        out.append({
            'model': 'YSE_App.transientspecdata',
            'pk': None,
            'fields': {
                'spectrum': spectrum_id,
                'wavelength': float(wave[i]),
                'flux': float(flux[i]),
                'wavelength_err': None,
                'flux_err': _legacy_finite_or_none(err[i]) if err is not None else None,
                'pixel_mask': 0,
            },
        })
    return out


def legacy_download_data(user, slug):
    import json

    transient = Transient.objects.filter(slug=slug)
    data = {transient[0].name: {'transient': {}, 'host': {}, 'photometry': {}, 'spectra': {}}}
    data[transient[0].name]['transient'] = json.loads(serializers.serialize("json", transient, use_natural_foreign_keys=True))

    if transient[0].host:
        host = Host.objects.filter(id=transient[0].host.id)
        data[transient[0].name]['host'] = json.loads(serializers.serialize("json", host, use_natural_foreign_keys=True))

    authorized_phot = PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, transient[0].id)
    if len(authorized_phot):
        data[transient[0].name]['photometry'] = json.loads(serializers.serialize("json", authorized_phot, use_natural_foreign_keys=True))
        for p, pd in zip(authorized_phot, range(len(data[transient[0].name]['photometry']))):
            photdata = PhotometryService.GetAuthorizedTransientPhotData_ByPhotometry(user, p.id, includeBadData=True)
            data[transient[0].name]['photometry'][pd]['data'] = json.loads(serializers.serialize("json", photdata, use_natural_foreign_keys=True))

    authorized_spectra = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(user, transient[0].id, includeBadData=True)
    if len(authorized_spectra):
        data[transient[0].name]['spectra'] = json.loads(serializers.serialize("json", authorized_spectra, use_natural_foreign_keys=True))
        array_ids = set(TransientSpectrumArrays.objects.filter(
            spectrum_id__in=[s.id for s in authorized_spectra]).values_list('spectrum_id', flat=True))
        stored_arrays = spectrum_arrays.arrays_for(array_ids)
        for s, sd in zip(authorized_spectra, range(len(data[transient[0].name]['spectra']))):
            if s.id in stored_arrays:
                data[transient[0].name]['spectra'][sd]['data'] = _legacy_array_pixels_as_specdata(s.id, stored_arrays[s.id])
                continue
            specdata = SpectraService.GetAuthorizedTransientSpecData_BySpectrum(user, s.id, includeBadData=True)
            if specdata:
                data[transient[0].name]['spectra'][sd]['data'] = \
                    json.loads(serializers.serialize("json", specdata, use_natural_foreign_keys=True))

    return JsonResponse(data).content


def legacy_download_photometry(user, slug):
    import json

    from YSE_App.views import snana_header_comment_lines

    content = ""
    transient = Transient.objects.filter(slug=slug)
    data = {transient[0].name: {'transient': {}, 'host': {}, 'photometry': {}, 'spectra': {}}}
    data[transient[0].name]['transient'] = json.loads(serializers.serialize("json", transient, use_natural_foreign_keys=True))
    content += snana_header_comment_lines(data[transient[0].name]['transient'][0]['fields'])
    content += "\n"
    content += "VARLIST:  MJD        FLT  FLUXCAL   FLUXCALERR    MAG     MAGERR     MAGSYS   TELESCOPE    INSTRUMENT   DQ\n"
    linefmt = "OBS:      %.3f  %s  %.3f  %.3f  %.3f  %.3f  %s  %s  %s  %s\n"

    authorized_phot = PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, transient[0].id)
    if len(authorized_phot):
        data[transient[0].name]['photometry'] = json.loads(serializers.serialize("json", authorized_phot, use_natural_foreign_keys=True))
        for p, pd in zip(authorized_phot, range(len(data[transient[0].name]['photometry']))):
            telescope = data[transient[0].name]['photometry'][pd]['fields']['instrument'].split(' - ')[0]
            instrument = data[transient[0].name]['photometry'][pd]['fields']['instrument'].split(' - ')[1]
            telescope = telescope.replace(' ', '_')
            instrument = instrument.replace(' ', '_')
            photdata = PhotometryService.GetAuthorizedTransientPhotData_ByPhotometry(user, p.id, includeBadData=True).order_by('obs_date')
            data[transient[0].name]['photometry'][pd]['data'] = json.loads(serializers.serialize("json", photdata, use_natural_foreign_keys=True))
            for d in data[transient[0].name]['photometry'][pd]['data']:
                mjd = date_to_mjd(d['fields']['obs_date'])
                if not len(d['fields']['data_quality']):
                    data_quality = None
                else:
                    data_quality = ','.join(d['fields']['data_quality'])
                f = d['fields']
                if f['flux'] and f['flux_zero_point'] and not f['mag']:
                    flux_err = f['flux_err'] if f['flux_err'] else 0
                    flux = f['flux'] * 10 ** (0.4 * (f['flux_zero_point'] - 27.5))
                    flux_err = flux_err * 10 ** (0.4 * (f['flux_zero_point'] - 27.5))
                    mag = -2.5 * np.log10(flux) + 27.5
                    mag_err = 2.5 / np.log(10) * flux_err / flux
                    content += linefmt % (mjd, f['band'].split(' - ')[1], flux, flux_err, mag, mag_err, f['mag_sys'], telescope, instrument, data_quality)
                elif not f['flux'] and f['mag']:
                    mag_err = f['mag_err'] if f['mag_err'] else 0
                    flux = 10 ** (-0.4 * (f['mag'] - 27.5))
                    flux_err = 0.4 * np.log(10) * flux * mag_err
                    content += linefmt % (mjd, f['band'].split(' - ')[1], flux, flux_err, f['mag'], mag_err, f['mag_sys'], telescope, instrument, data_quality)
                elif f['flux'] and f['flux_zero_point'] and f['mag']:
                    flux_err = f['flux_err'] if f['flux_err'] else 0
                    mag_err = f['mag_err'] if f['mag_err'] else 0
                    flux = f['flux'] * 10 ** (0.4 * (f['flux_zero_point'] - 27.5))
                    flux_err = flux_err * 10 ** (0.4 * (f['flux_zero_point'] - 27.5))
                    content += linefmt % (mjd, f['band'].split(' - ')[1], flux, flux_err, f['mag'], mag_err, f['mag_sys'], telescope, instrument, data_quality)
                elif f['flux'] and f['mag']:
                    mag_err = f['mag_err'] if f['mag_err'] else 0
                    flux = 10 ** (-0.4 * (f['mag'] - 27.5))
                    flux_err = 0.4 * np.log(10) * flux * mag_err
                    content += linefmt % (mjd, f['band'].split(' - ')[1], flux, flux_err, f['mag'], mag_err, f['mag_sys'], telescope, instrument, data_quality)
                else:
                    continue
    content += "# END: \n"
    return content.encode()


# --- fixture ------------------------------------------------------------------------------------

@override_settings(DATA_SERVICE_READER_USERNAMES=[READER], DATA_ACCESS_GRANTS_ENABLED=True)
class DownloadStreamingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = create_test_user(READER, is_staff=False)
        cls.password = secrets.token_hex(12)  # generated per run, never a stored credential
        cls.reader.set_password(cls.password)
        cls.reader.save()
        cls.member = create_test_user("dls929_member", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name="dls929-g")
        cls.member.groups.add(cls.group)
        cls.reader.groups.add(cls.group)
        audit = audit_fields(cls.reader)

        cls.transient = create_minimal_transient(cls.reader, name="2026dls", ra=12.5, dec=-30.25)
        attach_synthetic_host(cls.reader, cls.transient, name="hôte-dls929")  # non-ASCII: JSON escapes match
        flag = DataQuality.objects.create(name="dls929-flag", **audit)
        now = timezone.now()

        cls.sets = []
        cls.points = []
        for k, kind in enumerate(("a", "b")):
            obs_group, instrument, band = create_instrument_stack(cls.reader, obs_group_name="dls929-%s" % kind)
            header = TransientPhotometry.objects.create(
                transient=cls.transient, instrument=instrument, obs_group=obs_group, **audit)
            header.groups.add(cls.group)
            cls.sets.append(header)
            # every SNANA branch: mag only, flux+ZP, flux+ZP+mag, flux+mag without ZP, nothing, mag without error
            rows = [
                dict(mag=18.25, mag_err=0.05),
                dict(flux=1234.5, flux_err=12.5, flux_zero_point=27.5),
                dict(flux=987.0, flux_err=None, flux_zero_point=25.0, mag=19.1, mag_err=0.07),
                dict(flux=500.0, mag=20.2, mag_err=None),
                dict(),
                dict(mag=21.0),
            ]
            for i, values in enumerate(rows):
                point = TransientPhotData.objects.create(
                    photometry=header, band=band,
                    obs_date=now - datetime.timedelta(days=10 * k + (len(rows) - i), hours=i),
                    **values, **audit)
                cls.points.append(point)
            cls.points[-6].data_quality.add(flag)

        # one point hidden from the member only (per-point rule)
        cls.hidden = cls.points[1]
        DataAccessRule.objects.create(transient_phot_data=cls.hidden, grantee_user=cls.member,
                                      effect="deny", granted_as="pi")

        # spectra: rows (one pixel without error), arrays, and one with no pixels (no "data" key)
        cls.row_spec = attach_synthetic_spectrum(cls.reader, cls.transient)
        TransientSpecData.objects.bulk_create([
            TransientSpecData(spectrum=cls.row_spec, wavelength=4000.0 + 2.5 * i, flux=1.0e-16 * (1 + i),
                              flux_err=(None if i == 1 else 2.0e-18), **audit)
            for i in range(4)
        ])
        cls.array_spec = attach_synthetic_spectrum(cls.reader, cls.transient)
        spectrum_arrays.save(cls.array_spec, [5000.0, 4500.0, 4750.0], [3.0e-16, 1.0e-16, 2.0e-16],
                             [None, 1.5e-18, 2.5e-18])
        cls.empty_spec = attach_synthetic_spectrum(cls.reader, cls.transient)
        for s in (cls.row_spec, cls.array_spec, cls.empty_spec):
            s.groups.add(cls.group)

    def setUp(self):
        cache.clear()

    def _fresh(self, user):
        return type(user).objects.get(pk=user.pk)

    def _get(self, name, user=None, basic=False):
        client = Client()
        headers = {}
        if basic:
            cred = base64.b64encode(("%s:%s" % (READER, self.password)).encode()).decode()
            headers["HTTP_AUTHORIZATION"] = "Basic " + cred
        else:
            client.force_login(user)
        response = client.get(reverse(name, kwargs={"slug": self.transient.slug}), **headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        return response, b"".join(response.streaming_content)

    # --- identity with the old output -------------------------------------------------------------

    def test_download_data_matches_old_output(self):
        for label, user, basic in (("session", self.reader, False), ("basic", self.reader, True),
                                   ("per-point", self.member, False)):
            with self.subTest(reader=label):
                expected = legacy_download_data(self._fresh(user), self.transient.slug)
                cache.clear()
                response, body = self._get("download_data", user, basic)
                self.assertEqual(body, expected)
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(response["Content-Disposition"], "attachment; filename=%s_data.json" % self.transient.slug)

    def test_download_photometry_matches_old_output(self):
        for label, user, basic in (("session", self.reader, False), ("basic", self.reader, True),
                                   ("per-point", self.member, False)):
            with self.subTest(reader=label):
                expected = legacy_download_photometry(self._fresh(user), self.transient.slug)
                cache.clear()
                response, body = self._get("download_photometry", user, basic)
                self.assertEqual(body, expected)
                self.assertEqual(response["Content-Type"], "text/plain")
                self.assertEqual(response["Content-Disposition"],
                                 "attachment; filename=%s_data.snana.txt" % self.transient.slug)

    def test_small_batches_and_chunks_give_the_same_bytes(self):
        expected_json = legacy_download_data(self._fresh(self.reader), self.transient.slug)
        expected_snana = legacy_download_photometry(self._fresh(self.reader), self.transient.slug)
        cache.clear()
        with mock.patch.object(download_stream, "ROW_BATCH", 2), mock.patch.object(download_stream, "FLUSH_CHARS", 64):
            response, body = self._get("download_data", self.reader)
            self.assertEqual(body, expected_json)
            client = Client()
            client.force_login(self.reader)
            chunks = list(client.get(reverse("download_data", kwargs={"slug": self.transient.slug})).streaming_content)
            self.assertGreater(len(chunks), 5)
            self.assertEqual(self._get("download_photometry", self.reader)[1], expected_snana)

    # --- the fixture really exercises what it claims ------------------------------------------------

    def test_fixture_covers_access_and_storage_cases(self):
        import json

        reader_body = json.loads(self._get("download_data", self.reader)[1])[self.transient.name]
        member_body = json.loads(self._get("download_data", self.member)[1])[self.transient.name]
        n_points = lambda body: sum(len(p["data"]) for p in body["photometry"])  # noqa: E731
        self.assertEqual(n_points(reader_body), 12)
        self.assertEqual(n_points(member_body), 11)
        member_pks = {d["pk"] for p in member_body["photometry"] for d in p["data"]}
        self.assertNotIn(self.hidden.pk, member_pks)
        spectra = {s["pk"]: s for s in reader_body["spectra"]}
        self.assertEqual(len(spectra[self.row_spec.pk]["data"]), 4)
        self.assertEqual([d["fields"]["wavelength"] for d in spectra[self.array_spec.pk]["data"]],
                         [4500.0, 4750.0, 5000.0])
        self.assertNotIn("data", spectra[self.empty_spec.pk])
        self.assertIn("\\u00f4", self._get("download_data", self.reader)[1].decode())

        snana_reader = self._get("download_photometry", self.reader)[1]
        snana_member = self._get("download_photometry", self.member)[1]
        # 5 usable points per set (the empty one is skipped); the member loses the hidden one
        self.assertEqual(snana_reader.count(b"\nOBS:"), 10)
        self.assertEqual(snana_member.count(b"\nOBS:"), 9)
        self.assertIn(b"dls929-flag", snana_reader)

    def test_object_without_visible_data(self):
        other = create_minimal_transient(self.reader, name="2026dlt")
        for name in ("download_data", "download_photometry"):
            with self.subTest(view=name):
                client = Client()
                client.force_login(self.member)
                response = client.get(reverse(name, kwargs={"slug": other.slug}))
                body = b"".join(response.streaming_content)
                legacy = legacy_download_data if name == "download_data" else legacy_download_photometry
                self.assertEqual(body, legacy(self._fresh(self.member), other.slug))
