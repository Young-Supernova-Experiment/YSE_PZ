"""Optional ``reference`` on the upload routes (#646). Synthetic data only."""

from __future__ import annotations

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.data_utils import clean_upload_reference
from YSE_App.models import TransientPhotometry, TransientSpectrum
from YSE_App.tests.deploy_checklist_helpers import (
    CHECKLIST_PASSWORD,
    basic_auth_header,
    create_instrument,
    create_telescope,
    ensure_observation_group,
)
from YSE_App.tests.fixtures_minimal import (
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

BIBCODE = "2020ApJ...900....1X"  # 19 characters


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UploadReferenceTests(TestCase):
    def setUp(self):
        self.user = create_test_user("ref_uploader", is_staff=True)
        self.user.set_password(CHECKLIST_PASSWORD)
        self.user.save()
        self.transient = create_minimal_transient(self.user, name="2026ref", obs_group_name="ref-group")
        self.obs_group, self.instrument, self.band = create_instrument_stack(self.user, obs_group_name="ref-group")
        telescope = create_telescope(self.user, "RefUploadTel")
        self.ucsc = ensure_observation_group(self.user, "UCSC")
        self.kast, _ = create_instrument(self.user, telescope, "KAST")
        self.client = Client()
        self.client.force_login(self.user)
        self.auth = basic_auth_header(self.user.username, CHECKLIST_PASSWORD)

    def _post_json(self, path, payload):
        return Client().post(path, json.dumps(payload), content_type="application/json",
                             HTTP_AUTHORIZATION=self.auth)

    def phot_payload(self, reference=None):
        ph = {"instrument": self.instrument.name, "obs_group": self.obs_group.name, "groups": ""}
        if reference is not None:
            ph["reference"] = reference
        return {
            "header": {"clobber": False, "mjdmatchmin": 0.01},
            "transient": {"name": self.transient.name, "ra": 10.0, "dec": 20.0, "status": "New"},
            "photheader": ph,
        }

    def spec_payload(self, reference=None, notes="ref spec", clobber=False):
        hd = {
            "instrument": self.instrument.name, "obs_group": self.obs_group.name, "groups": "",
            "data_quality": None, "obs_date": "2026-09-02T00:00:00", "ra": 10.0, "dec": 20.0,
            "rlap": None, "redshift": None, "redshift_err": None, "redshift_quality": None,
            "spectrum_notes": notes, "spec_phase": None, "clobber": clobber,
        }
        if reference is not None:
            hd["reference"] = reference
        return {"header": hd, "transient": {"name": self.transient.name},
                "0": {"wavelength": 5000.0, "flux": 1.0, "flux_err": 0.1}}

    def test_clean_reference(self):
        self.assertIsNone(clean_upload_reference(None))
        self.assertIsNone(clean_upload_reference("   "))
        self.assertEqual(clean_upload_reference(" " + BIBCODE + " "), BIBCODE)
        self.assertEqual(clean_upload_reference("TNS 2026 AT2026abc"), "TNS 2026 AT2026abc")

    def test_phot_upload_stores_reference_and_keeps_it(self):
        self.assertEqual(self._post_json("/add_transient_phot/", self.phot_payload(BIBCODE)).status_code, 200)
        phot = TransientPhotometry.objects.get(transient=self.transient, instrument=self.instrument)
        self.assertEqual(phot.reference, BIBCODE)
        self._post_json("/add_transient_phot/", self.phot_payload("other text"))
        phot.refresh_from_db()
        self.assertEqual(phot.reference, BIBCODE, "an existing reference is not overwritten")

    def test_phot_upload_without_reference_is_null(self):
        self.assertEqual(self._post_json("/add_transient_phot/", self.phot_payload()).status_code, 200)
        phot = TransientPhotometry.objects.get(transient=self.transient, instrument=self.instrument)
        self.assertIsNone(phot.reference)

    def test_spec_upload_stores_reference_and_keeps_it(self):
        response = self._post_json("/add_transient_spec/", self.spec_payload("not a bibcode, free text"))
        self.assertEqual(response.status_code, 200, response.content)
        spec = TransientSpectrum.objects.get(transient=self.transient, spectrum_notes="ref spec")
        self.assertEqual(spec.reference, "not a bibcode, free text")
        self._post_json("/add_transient_spec/", self.spec_payload(BIBCODE, clobber=True))
        spec.refresh_from_db()
        self.assertEqual(spec.reference, "not a bibcode, free text")

    def test_spec_upload_without_reference_is_null(self):
        self.assertEqual(self._post_json("/add_transient_spec/", self.spec_payload()).status_code, 200)
        spec = TransientSpectrum.objects.get(transient=self.transient, spectrum_notes="ref spec")
        self.assertIsNone(spec.reference)

    def _form_payload(self, reference=None):
        payload = {
            "transient": self.transient.id, "ra": self.transient.ra, "dec": self.transient.dec,
            "spec_phase": "", "obs_date": "2026-09-01T03:00", "obs_group": self.ucsc.id,
            "instrument": self.kast.id, "data_quality": "",
            "filename": SimpleUploadedFile("spec.txt", b"4000 1.0 0.1\n4010 1.1 0.1\n"),
        }
        if reference is not None:
            payload["reference"] = reference
        return payload

    def test_spectrum_form_stores_reference(self):
        # the form is on the transient page (#898)
        response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertContains(response, 'name="reference"')
        response = self.client.post(reverse("upload_spectrum"), self._form_payload(BIBCODE))
        self.assertEqual(response.status_code, 302, getattr(response, "content", b""))
        spec = TransientSpectrum.objects.get(transient=self.transient, instrument=self.kast)
        self.assertEqual(spec.reference, BIBCODE)

    def test_spectrum_form_without_reference_is_null(self):
        response = self.client.post(reverse("upload_spectrum"), self._form_payload())
        self.assertEqual(response.status_code, 302, getattr(response, "content", b""))
        spec = TransientSpectrum.objects.get(transient=self.transient, instrument=self.kast)
        self.assertIsNone(spec.reference)
