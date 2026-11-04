"""Stored analysis fits and TNS reports never carry data hidden from the viewer.

A fit is computed from its creator's data access, so a YSE member's fit of a
transient with YSE-only photometry is YSE data: other viewers see it only when
none of the transient's data are hidden from them. A TNS report (preview or
submit) is built only from data the reporting user may see, and only staff,
``YSE`` members or members of the service's groups may report.

All users and transients are synthetic; nothing talks to TNS.
"""

import datetime
import json
from unittest import mock

from django.contrib.auth.models import Group
from django.db.models import F
from django.test import Client, TestCase
from django.urls import reverse

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import SharingService, SharingSubmission, TransientPhotData
from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.services import analysis_services as svc
from YSE_App.services import fit_status
from YSE_App.services.visibility import YSE_COLLABORATION_GROUP_NAME, YSE_OBSERVATION_GROUP_NAME
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

FIT_MARKER = "synthfitmarker"


def _result_dict(value):
    """The API may serialize ``result`` as an object or as a JSON string."""
    if isinstance(value, str):
        return json.loads(value)
    return value


class PrivacyBase(TestCase):
    def setUp(self):
        self.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        self.yse, _ = Group.objects.get_or_create(name=YSE_COLLABORATION_GROUP_NAME)
        self.other_team, _ = Group.objects.get_or_create(name="fitpriv-other-team")

        self.member = create_test_user("fitpriv_yse_member", is_staff=False)
        self.member.groups.add(self.yse, self.public)
        self.outsider = create_test_user("fitpriv_public_only", is_staff=False)
        self.outsider.groups.add(self.public)
        self.admin = create_test_user("fitpriv_admin", is_staff=True)

        self.transient = create_minimal_transient(self.admin, name="fitpriv01")
        self.open_stack = create_instrument_stack(self.admin, obs_group_name="fitpriv-open-survey")
        self.yse_stack = create_instrument_stack(self.admin, obs_group_name=YSE_OBSERVATION_GROUP_NAME)
        self.team_stack = create_instrument_stack(self.admin, obs_group_name="fitpriv-team-survey")

    def _phot(self, stack, *groups, mag=None, shift_days=0):
        obs_group, instrument, band = stack
        phot = attach_synthetic_photometry(
            self.admin, self.transient, n_points=2, obs_group=obs_group, instrument=instrument, band=band)
        phot.groups.add(*groups)
        points = TransientPhotData.objects.filter(photometry=phot)
        if mag is not None:
            points.update(mag=mag)
        if shift_days:
            points.update(obs_date=F("obs_date") - datetime.timedelta(days=shift_days))
        return phot


class StoredFitPrivacyTests(PrivacyBase):
    def setUp(self):
        super().setUp()
        self._phot(self.open_stack)  # the outsider may see the transient
        self._phot(self.yse_stack, self.yse)  # and some photometry is YSE-only
        profile = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=self.admin, timeout_seconds=0)
        self.run = svc.start_analysis(profile, self.transient, self.member, {}, dispatch=False)
        self.run.mark_succeeded({"model": FIT_MARKER, "t0": 60000.0, "z": 0.03, "x1": 0.5, "c": 0.01})

    def _api_rows(self, user):
        client = Client()
        client.force_login(user)
        response = client.get("/api/analysisruns/?format=json&transient=%d" % self.transient.id)
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        return rows["results"] if isinstance(rows, dict) else rows

    def test_fit_helper(self):
        self.assertEqual(fit_status.stored_salt_fit(self.transient, self.member).pk, self.run.pk)
        self.assertIsNone(fit_status.stored_salt_fit(self.transient, self.outsider))
        self.assertTrue(fit_status.run_visible_to_user(self.run, self.member))
        self.assertFalse(fit_status.run_visible_to_user(self.run, self.outsider))
        # cached plot paths drop the creator exception, but a member still sees it (no hidden data)
        self.assertEqual(fit_status.stored_salt_fit(self.transient, self.member, allow_creator=False).pk, self.run.pk)

    def test_creator_exception_is_not_used_for_cached_plots(self):
        hidden_from_member = Group.objects.create(name="fitpriv-hidden-from-member")
        self._phot(self.team_stack, hidden_from_member)
        self.assertEqual(fit_status.stored_salt_fit(self.transient, self.member).pk, self.run.pk)
        self.assertIsNone(fit_status.stored_salt_fit(self.transient, self.member, allow_creator=False))

    def test_fit_visible_once_nothing_is_hidden(self):
        self.outsider.groups.add(self.yse)
        self.assertEqual(fit_status.stored_salt_fit(self.transient, self.outsider).pk, self.run.pk)

    def test_analysisruns_api(self):
        member_rows = self._api_rows(self.member)
        self.assertEqual([r["uuid"] for r in member_rows], [str(self.run.uuid)])
        self.assertEqual(_result_dict(member_rows[0]["result"])["model"], FIT_MARKER)
        outsider_rows = self._api_rows(self.outsider)
        self.assertEqual(outsider_rows, [])

    def test_fragment_and_status(self):
        url = reverse("transient_detail_salt_fit_fragment", args=[self.transient.id])
        client = Client()
        client.force_login(self.member)
        self.assertIn(FIT_MARKER, client.get(url).content.decode())
        client.force_login(self.outsider)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(FIT_MARKER, response.content.decode())
        status_url = reverse("analysis_run_status", args=[self.run.uuid])
        self.assertEqual(client.get(status_url).status_code, 403)
        client.force_login(self.member)
        self.assertEqual(client.get(status_url).status_code, 200)


@mock.patch("YSE_App.sharing.tns.requests.post", side_effect=AssertionError("no TNS calls in tests"))
@mock.patch("YSE_App.sharing.tns.requests.get", side_effect=AssertionError("no TNS calls in tests"))
class TNSReportPrivacyTests(PrivacyBase):
    def setUp(self):
        super().setUp()
        self._phot(self.open_stack, mag=18.25)
        self._phot(self.yse_stack, self.yse, mag=17.5, shift_days=1)
        # another team's points, earliest of all: they would be the discovery point if they leaked
        self._phot(self.team_stack, self.other_team, mag=12.5, shift_days=2)
        inst_ids = {stack[1].name: 172 for stack in (self.open_stack, self.yse_stack, self.team_stack)}
        self.service = SharingService.objects.create(
            slug="fitpriv-tns", name="Synthetic TNS", kind=SharingService.KIND_TNS, tns_group_id="83",
            tns_group_name="Synthetic", testing=True,
            config={"instrument_ids": inst_ids, "max_photometry_points": 50},
            created_by=self.admin, modified_by=self.admin)
        self.client = Client()

    def _preview(self, user, **extra):
        self.client.force_login(user)
        data = {"service": self.service.pk, "kind": "discovery"}
        data.update(extra)
        return self.client.post(reverse("sharing_report_preview", args=[self.transient.pk]), data)

    def test_non_yse_user_is_refused(self, *_mocks):
        self.assertEqual(self._preview(self.outsider).status_code, 403)
        response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                    {"service": self.service.pk, "kind": "discovery"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SharingSubmission.objects.exists())

    def test_member_preview_has_visible_points_only(self, *_mocks):
        response = self._preview(self.member)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["problems"], [])
        group = data["payload"]["at_report"]["0"]["photometry"]["photometry_group"]
        fluxes = sorted(entry["flux"] for entry in group.values())
        self.assertEqual(fluxes, ["17.500", "17.500", "18.250", "18.250"])
        self.assertNotIn("12.500", response.content.decode())

    def test_member_submit_has_visible_points_only(self, *_mocks):
        self.client.force_login(self.member)
        with mock.patch("YSE_App.sharing.tns.dispatch_submission"):
            response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                        {"service": self.service.pk, "kind": "discovery"})
        self.assertEqual(response.status_code, 200, response.content)
        submission = SharingSubmission.objects.get(pk=response.json()["id"])
        group = submission.payload["at_report"]["0"]["photometry"]["photometry_group"]
        self.assertEqual(len(group), 4)
        self.assertNotIn("12.500", str(submission.payload))

    def test_hidden_spectrum_id_is_refused(self, *_mocks):
        team_obs_group, team_instrument, _band = self.team_stack
        hidden = attach_synthetic_spectrum(self.admin, self.transient, obs_group=team_obs_group,
                                           instrument=team_instrument)
        hidden.groups.add(self.other_team)
        yse_obs_group, yse_instrument, _band = self.yse_stack
        visible = attach_synthetic_spectrum(self.admin, self.transient, obs_group=yse_obs_group,
                                            instrument=yse_instrument)
        visible.groups.add(self.yse)
        self.assertEqual(self._preview(self.member, kind="classification", spectrum=hidden.pk).status_code, 404)
        self.assertEqual(self._preview(self.member, kind="classification", spectrum=visible.pk).status_code, 200)
        self.client.force_login(self.member)
        with mock.patch("YSE_App.sharing.tns.dispatch_submission"):
            response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                        {"service": self.service.pk, "kind": "classification", "spectrum": hidden.pk})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SharingSubmission.objects.exists())

    def test_submission_api_payload_only_for_reporters(self, *_mocks):
        SharingSubmission.objects.create(
            service=self.service, transient=self.transient, kind="discovery", payload={"synthetic": 1},
            created_by=self.admin, modified_by=self.admin)
        for user, payload in ((self.outsider, None), (self.member, {"synthetic": 1})):
            self.client.force_login(user)
            rows = self.client.get("/api/sharingsubmissions/?format=json").json()
            rows = rows.get("results", rows) if isinstance(rows, dict) else rows
            self.assertEqual([r["payload"] for r in rows], [payload], user.username)
