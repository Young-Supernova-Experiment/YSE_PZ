"""REST API updates record the requesting user as ``modified_by`` (#886).

Synthetic users and rows only.
"""

import base64
import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from YSE_App.models import (
    Allocation,
    GWCandidate,
    GWCandidateImage,
    SurveyField,
    SurveyFieldMSB,
    SurveyObservation,
    TaskStatus,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    ensure_transient_statuses,
)

PASSWORD = "modified-by-test-pass-1"


def _user(username, **kw):
    user = User.objects.create(username=username, email=f"{username}@example.com", **kw)
    user.set_password(PASSWORD)
    user.save()
    return user


class ModifiedByBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.creator = _user("mb_creator", is_staff=True)
        cls.editor = _user("mb_editor", is_staff=True)
        cls.statuses = ensure_transient_statuses(cls.creator)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.creator, obs_group_name="mb-og")
        # a TNS-style synthetic name so the object is listed for any signed-in user
        cls.transient = create_minimal_transient(cls.creator, name="2026mbz", obs_group_name="mb-og")

    def api(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def assert_modified_by(self, obj, user):
        obj.refresh_from_db()
        self.assertEqual(obj.modified_by_id, user.id)
        self.assertNotEqual(obj.created_by_id, obj.modified_by_id)


class TransientModifiedByTests(ModifiedByBase):
    def status_url(self, name):
        return "http://testserver/api/transientstatuses/%d/" % self.statuses[name].pk

    def test_status_patch_records_requesting_user(self):
        resp = self.api(self.editor).patch("/api/transients/%d/" % self.transient.pk,
                                           {"status": self.status_url("Interesting")}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.status.name, "Interesting")
        self.assert_modified_by(self.transient, self.editor)

    def test_basic_auth_machine_client(self):
        token = base64.b64encode(f"{self.editor.username}:{PASSWORD}".encode()).decode()
        resp = APIClient().patch("/api/transients/%d/" % self.transient.pk,
                                 {"status": self.status_url("Watch")}, format="json",
                                 HTTP_AUTHORIZATION=f"Basic {token}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.transient, self.editor)

    def test_token_auth_machine_client(self):
        token = Token.objects.create(user=self.editor)
        resp = APIClient().patch("/api/transients/%d/" % self.transient.pk,
                                 {"status": self.status_url("Following")}, format="json",
                                 HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.transient, self.editor)


class AllocationModifiedByTests(ModifiedByBase):
    def test_patch_records_requesting_user(self):
        now = timezone.now()
        allocation = Allocation.objects.create(
            name="mb allocation", telescope=self.instrument.telescope, facility="generic", hours_allocated=5.0,
            start_date=now - datetime.timedelta(days=1), end_date=now + datetime.timedelta(days=30),
            **audit_fields(self.creator))
        resp = self.api(self.editor).patch("/api/allocations/%d/" % allocation.pk,
                                           {"name": "mb allocation renamed"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        allocation.refresh_from_db()
        self.assertEqual(allocation.name, "mb allocation renamed")
        self.assert_modified_by(allocation, self.editor)


class SurveyModifiedByTests(ModifiedByBase):
    def setUp(self):
        audit = audit_fields(self.creator)
        self.field = SurveyField.objects.create(
            field_id="mb-field", obs_group=self.obs_group, instrument=self.instrument,
            ra_cen=10.0, dec_cen=20.0, width_deg=3.4, height_deg=3.4, **audit)
        status, _ = TaskStatus.objects.get_or_create(name="Requested", defaults=audit)
        self.observation = SurveyObservation.objects.create(
            survey_field=self.field, status=status, exposure_time=30.0, photometric_band=self.band, **audit)
        self.msb = SurveyFieldMSB.objects.create(obs_group=self.obs_group, name="mb-msb", **audit)

    def test_survey_field_patch(self):
        resp = self.api(self.editor).patch("/api/surveyfields/%d/" % self.field.pk, {"cadence": 3.0}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.field, self.editor)

    def test_survey_field_msb_patch(self):
        resp = self.api(self.editor).patch("/api/surveyfieldmsbs/%d/" % self.msb.pk, {"active": True}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.msb, self.editor)

    def test_survey_observation_patch(self):
        resp = self.api(self.editor).patch("/api/surveyobservations/%d/" % self.observation.pk,
                                           {"obs_mjd": 60001.5}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.observation, self.editor)


class GWModifiedByTests(ModifiedByBase):
    def setUp(self):
        audit = audit_fields(self.creator)
        self.candidate = GWCandidate.objects.create(
            field_name="mb-gw-field", candidate_id="mb-cand-1", name="mb-gw", transient=self.transient, **audit)
        self.image = GWCandidateImage.objects.create(
            gw_candidate=self.candidate, image_filename="mb-synthetic.fits", image_filter=self.band, **audit)

    def test_gw_candidate_patch(self):
        resp = self.api(self.editor).patch("/api/gwcandidates/%d/" % self.candidate.pk,
                                           {"websniff_url": "https://example.com/mb"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.candidate, self.editor)

    def test_gw_candidate_image_patch(self):
        resp = self.api(self.editor).patch("/api/gwcandidateimages/%d/" % self.image.pk,
                                           {"dophot_class": 1}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_modified_by(self.image, self.editor)
