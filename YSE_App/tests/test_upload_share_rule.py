"""Upload helpers apply the PI-only sharing rule to group changes on existing restricted data (#647).

Synthetic data only.
"""

from __future__ import annotations

import datetime
import json

from django.contrib.auth.models import Group
from django.test import Client, override_settings

from YSE_App.data_utils import add_transient_spec_util
from YSE_App.models import TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing
from YSE_App.tests.deploy_checklist_helpers import CHECKLIST_PASSWORD, basic_auth_header
from YSE_App.tests.fixtures_minimal import audit_fields
from YSE_App.tests.test_data_pis import PiBase, _member

OBS_DATE = datetime.datetime(2026, 9, 2)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UploadShareRuleTests(PiBase):
    def setUp(self):
        super().setUp()
        self.spec.obs_date = OBS_DATE
        self.spec.save()
        TransientPhotometry.objects.exclude(pk=self.phot.pk).delete()  # one restricted photometry set
        self.spec.pis.add(self.pi)
        self.phot.pis.add(self.pi)
        self.deputy = _member("pi_deputy")
        data_sharing.add_delegate(self.pi_user, self.pi, self.deputy)
        self.staff = _member("svc_staff", staff=True)
        for user in (self.member, self.requester, self.pi_user, self.deputy, self.staff, self.outsider):
            user.set_password(CHECKLIST_PASSWORD)
            user.save()
        self.other = Group.objects.create(name="OTHERGROUP")
        self.requester.groups.add(self.other)

    def _post(self, user, path, payload):
        return Client().post(path, json.dumps(payload), content_type="application/json",
                             HTTP_AUTHORIZATION=basic_auth_header(user.username, CHECKLIST_PASSWORD))

    def _phot_payload(self, groups):
        return {
            "header": {"clobber": False, "mjdmatchmin": 0.01},
            "transient": {"name": self.transient.name, "ra": 10.0, "dec": 20.0, "status": "New"},
            "photheader": {"instrument": self.instrument.name, "obs_group": self.obs_group.name, "groups": groups},
        }

    def _spec_payload(self, groups):
        hd = {
            "instrument": self.instrument.name, "obs_group": self.obs_group.name, "groups": groups,
            "data_quality": None, "obs_date": "2026-09-02T00:00:00", "ra": 10.0, "dec": 20.0,
            "rlap": None, "redshift": None, "redshift_err": None, "redshift_quality": None,
            "spectrum_notes": "n", "spec_phase": None, "clobber": False,
        }
        return {"header": hd, "transient": {"name": self.transient.name}}

    def _group_names(self, dataset):
        return set(dataset.groups.values_list("name", flat=True))

    # views: photometry

    def test_non_pi_cannot_widen_restricted_photometry(self):
        response = self._post(self.requester, "/add_transient_phot/", self._phot_payload("REQGROUP,OTHERGROUP"))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["message"], "success")  # the upload itself still goes through
        self.assertEqual(body["group_changes_skipped"], ["OTHERGROUP", "REQGROUP"])
        self.assertEqual(self._group_names(self.phot), {"PIGROUP"})

    def test_group_member_without_share_role_cannot_widen(self):
        response = self._post(self.member, "/add_transient_phot/", self._phot_payload("REQGROUP"))
        self.assertEqual(response.json()["group_changes_skipped"], ["REQGROUP"])
        self.assertEqual(self._group_names(self.phot), {"PIGROUP"})

    def test_pi_deputy_and_staff_can_add_groups_to_photometry(self):
        for user, group in ((self.pi_user, "REQGROUP"), (self.deputy, "OTHERGROUP")):
            response = self._post(user, "/add_transient_phot/", self._phot_payload(group))
            self.assertEqual(response.status_code, 200, response.content)
            self.assertNotIn("group_changes_skipped", response.json())
        self.assertEqual(self._group_names(self.phot), {"PIGROUP", "REQGROUP", "OTHERGROUP"})
        self.phot.groups.remove(*Group.objects.filter(name__in=["REQGROUP", "OTHERGROUP"]))
        response = self._post(self.staff, "/add_transient_phot/", self._phot_payload("REQGROUP"))
        self.assertNotIn("group_changes_skipped", response.json())
        self.assertEqual(self._group_names(self.phot), {"PIGROUP", "REQGROUP"})

    def test_repeating_existing_groups_is_not_a_change(self):
        response = self._post(self.member, "/add_transient_phot/", self._phot_payload("PIGROUP"))
        self.assertNotIn("group_changes_skipped", response.json())

    # views: spectra

    def test_non_pi_cannot_widen_restricted_spectrum(self):
        response = self._post(self.requester, "/add_transient_spec/", self._spec_payload("REQGROUP"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["group_changes_skipped"], ["REQGROUP"])
        self.assertEqual(self._group_names(self.spec), {"PIGROUP"})

    def test_pi_deputy_and_staff_can_add_groups_to_spectrum(self):
        for user, group in ((self.pi_user, "REQGROUP"), (self.deputy, "OTHERGROUP")):
            self.assertNotIn("group_changes_skipped",
                             self._post(user, "/add_transient_spec/", self._spec_payload(group)).json())
        self.assertEqual(self._group_names(self.spec), {"PIGROUP", "REQGROUP", "OTHERGROUP"})

    # helpers

    def test_uploader_must_belong_to_groups_added_to_public_data(self):
        public = Group.objects.create(name="Public")
        self.spec.groups.set([public])
        allowed, skipped = data_sharing.groups_allowed_for_upload(self.requester, self.spec, [self.mine, self.owners])
        self.assertEqual(allowed, [self.mine])
        self.assertEqual(skipped, [self.owners])
        allowed, skipped = data_sharing.groups_allowed_for_upload(self.staff, self.spec, [self.mine, self.owners])
        self.assertEqual((len(allowed), skipped), (2, []))

    def test_new_or_ungrouped_datasets_are_unaffected(self):
        fresh = TransientSpectrum.objects.create(
            transient=self.transient, instrument=self.instrument, obs_group=self.obs_group, ra=1.0, dec=2.0,
            obs_date=datetime.datetime(2026, 9, 3), **audit_fields(self.admin))
        self.assertEqual(data_sharing.groups_allowed_for_upload(self.outsider, fresh, [self.mine]), ([self.mine], []))
        self.assertEqual(data_sharing.groups_allowed_for_upload(self.outsider, TransientSpectrum(), [self.mine]),
                         ([self.mine], []))

    # the util path used by the ingest crons and brokers

    def _spec_dict(self, groups, user_obs_date=OBS_DATE):
        return {"clobber": False, "0": {
            "instrument": self.instrument.name, "obs_group": self.obs_group.name, "obs_date": user_obs_date,
            "groups": groups, "specdata": {"0": {"wavelength": 5000.0, "flux": 1.0, "flux_err": 0.1}}}}

    def test_spec_util_service_account_still_adds_groups(self):
        response = add_transient_spec_util(self._spec_dict("REQGROUP"), self.transient, self.staff)
        self.assertNotIn("group_changes_skipped", json.loads(response.content))
        self.assertEqual(self._group_names(self.spec), {"PIGROUP", "REQGROUP"})

    def test_spec_util_non_pi_is_skipped_and_reported(self):
        response = add_transient_spec_util(self._spec_dict("REQGROUP"), self.transient, self.requester)
        body = json.loads(response.content)
        self.assertEqual(body["message"], "successfully added spec data")
        self.assertEqual(body["group_changes_skipped"], ["REQGROUP"])
        self.assertEqual(self._group_names(self.spec), {"PIGROUP"})
