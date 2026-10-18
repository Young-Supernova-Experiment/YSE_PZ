"""TNS / Hermes reports send only what the sender may see and share (#691).

Manual reports: public data, plus restricted data the submitter can see and
may share (PI, deputy, staff). Automatic publishing: public data, plus
restricted data a PI opted in. Synthetic data only.
"""

from __future__ import annotations

import datetime
import json

from auditlog.models import LogEntry
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.feeds import hermes as hermes_feed
from YSE_App.models import AutopublishOptIn, SharingService, Transient, TransientPhotData
from YSE_App.services import autopublish_optin, data_sharing
from YSE_App.sharing import report_scope, tns
from YSE_App.sharing.report_scope import ReportScope
from YSE_App.sharing_views import report_dialog_context
from YSE_App.tests.fixtures_minimal import attach_synthetic_spectrum
from YSE_App.tests.test_data_pis import PiBase, _member

RESTRICTED_FLUX = "15.000"  # first point of the restricted set (earliest, so it is the discovery)
PUBLIC_FLUX = "18.000"  # first point of the public set


@override_settings(JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False)
class ReportBase(PiBase):
    def setUp(self):
        super().setUp()
        self.spec.pis.add(self.pi)
        self.phot.pis.add(self.pi)
        # the restricted set is the earliest, so a report that includes it uses it as the discovery
        long_ago = timezone.now() - datetime.timedelta(days=30)
        for i, point in enumerate(TransientPhotData.objects.filter(photometry=self.phot).order_by("pk")):
            point.obs_date = long_ago + datetime.timedelta(days=i)
            point.mag = 15.0 + 0.05 * i
            point.save()
        self.public_spec = attach_synthetic_spectrum(self.admin, self.transient, obs_group=self.obs_group,
                                                     instrument=self.instrument)
        self.pi_user.groups.add(self.owners)  # a PI sees the group's data like any member
        self.deputy = _member("rv_deputy", self.owners)
        data_sharing.add_delegate(self.pi_user, self.pi, self.deputy)
        self.staff = _member("rv_staff", staff=True)
        self.service = SharingService.objects.create(
            slug="rv-tns", name="RV TNS", kind=SharingService.KIND_TNS, tns_group_id="83", tns_group_name="YSE",
            config={"instrument_ids": {self.instrument.name: 172}}, created_by=self.admin, modified_by=self.admin)

    def as_candidate(self):
        """Discovery reports need a name that is not a TNS designation."""
        Transient.objects.filter(pk=self.transient.pk).update(name="rv_cand")
        self.transient.refresh_from_db()

    def flux_of_discovery(self, payload):
        group = payload["at_report"]["0"]["photometry"]["photometry_group"]
        return group["0"]["flux"]

    def scope_ids(self, user):
        scope = ReportScope.for_user(user)
        return scope.photometry_ids(self.transient), scope.spectrum_ids(self.transient)


class ScopeTests(ReportBase):
    def test_non_pi_group_member_and_outsider_get_public_only(self):
        for user in (self.member, self.outsider, self.requester):
            phot, spec = self.scope_ids(user)
            self.assertNotIn(self.phot.pk, phot, user.username)
            self.assertNotIn(self.spec.pk, spec, user.username)
            self.assertTrue(phot and spec)  # the public sets stay

    def test_pi_and_deputy_get_restricted_too(self):
        for user in (self.pi_user, self.deputy):
            phot, spec = self.scope_ids(user)
            self.assertIn(self.phot.pk, phot, user.username)
            self.assertIn(self.spec.pk, spec, user.username)
        # staff outside the group no longer see the data, so they cannot send it (#706)
        phot, spec = self.scope_ids(self.staff)
        self.assertNotIn(self.phot.pk, phot)
        self.assertNotIn(self.spec.pk, spec)
        self.staff.groups.add(self.owners)  # a staff member who sees it may send it as an administrator
        phot, spec = self.scope_ids(self.staff)
        self.assertIn(self.phot.pk, phot)

    def test_pi_must_also_see_the_data(self):
        self.pi_user.groups.clear()  # visible_q no longer shows the set
        phot, spec = self.scope_ids(self.pi_user)
        self.assertNotIn(self.phot.pk, phot)
        self.assertNotIn(self.spec.pk, spec)

    def test_omitted_counts_only_what_the_user_can_see(self):
        self.assertEqual(ReportScope.for_user(self.member).omitted(self.transient),
                         {"photometry_sets": 1, "spectra": 1})
        self.assertEqual(ReportScope.for_user(self.outsider).omitted(self.transient),
                         {"photometry_sets": 0, "spectra": 0})  # not theirs to see, so not even counted
        self.assertEqual(ReportScope.for_user(self.pi_user).omitted(self.transient),
                         {"photometry_sets": 0, "spectra": 0})

    def test_no_scope_means_public_only(self):
        self.as_candidate()
        report = tns.build_at_report(self.service, self.transient)
        self.assertEqual(self.flux_of_discovery(report), PUBLIC_FLUX)
        self.assertEqual(ReportScope.for_user(None).photometry_ids(self.transient),
                         ReportScope.public_only().photometry_ids(self.transient))


class ManualReportTests(ReportBase):
    def test_discovery_payload_by_role(self):
        self.as_candidate()
        expected = {self.member: PUBLIC_FLUX, self.outsider: PUBLIC_FLUX, self.pi_user: RESTRICTED_FLUX,
                    self.deputy: RESTRICTED_FLUX, self.staff: PUBLIC_FLUX}  # staff: only what they see (#706)
        for user, flux in expected.items():
            payload = tns.build_payload(self.service, self.transient, tns.KIND_DISCOVERY,
                                        scope=ReportScope.for_user(user))
            self.assertEqual(self.flux_of_discovery(payload), flux, user.username)

    def test_create_submission_uses_the_submitters_scope(self):
        self.as_candidate()
        sub = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.member, dispatch=False)
        self.assertEqual(self.flux_of_discovery(sub.payload), PUBLIC_FLUX)
        sub = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.pi_user, dispatch=False)
        self.assertEqual(self.flux_of_discovery(sub.payload), RESTRICTED_FLUX)

    def test_classification_refuses_a_spectrum_the_user_may_not_send(self):
        for user in (self.member, self.outsider, self.staff):  # staff outside the group: not theirs (#706)
            with self.assertRaises(tns.PayloadError):
                tns.build_classification_report(self.service, self.transient, spectrum=self.spec,
                                                classification="SN Ia", scope=ReportScope.for_user(user))
        for user in (self.pi_user, self.deputy):
            report = tns.build_classification_report(self.service, self.transient, spectrum=self.spec,
                                                     classification="SN Ia", scope=ReportScope.for_user(user))
            self.assertEqual(report["_yse"]["spectrum_id"], self.spec.pk)
        public = tns.build_classification_report(self.service, self.transient, spectrum=self.public_spec,
                                                 classification="SN Ia", scope=ReportScope.for_user(self.outsider))
        self.assertEqual(public["_yse"]["spectrum_id"], self.public_spec.pk)

    def test_upload_step_rechecks_the_spectrum(self):
        sub = tns.create_submission(self.service, self.transient, tns.KIND_CLASSIFICATION, self.pi_user,
                                    dispatch=False, spectrum=self.spec, classification="SN Ia")
        sub.created_by = self.member  # a payload for data its creator could not send is not uploaded
        sub.save()
        with self.assertRaises(Exception) as ctx:
            tns._prepare_report(sub, client=None)
        self.assertIn("not cleared", str(ctx.exception))

    def test_dialog_lists_only_sendable_spectra_and_counts_the_rest(self):
        self.service.groups.clear()
        for user, n_spectra, omitted in ((self.member, 1, 1), (self.pi_user, 2, None), (self.staff, 1, None)):
            ctx = report_dialog_context(user, self.transient)
            self.assertEqual(len(ctx["sharing_spectra"]), n_spectra, user.username)
            self.assertEqual((ctx["sharing_omitted"] or {}).get("spectra"), omitted and 1, user.username)
        ctx = report_dialog_context(self.member, self.transient)
        self.assertEqual(ctx["sharing_omitted"], {"photometry_sets": 1, "spectra": 1})
        self.assertNotIn(str(self.spec.pk), [str(s["id"]) for s in ctx["sharing_spectra"]])

    def _preview(self, user, **params):
        client = Client()
        client.force_login(user)
        data = {"service": self.service.pk}
        data.update(params)
        return client.get(reverse("sharing_report_preview", args=[self.transient.pk]), data)

    def test_preview_endpoint_payload_and_omitted_counts(self):
        self.as_candidate()
        body = self._preview(self.member).json()
        self.assertEqual(self.flux_of_discovery(body["payload"]), PUBLIC_FLUX)
        self.assertEqual(body["omitted"], {"photometry_sets": 1, "spectra": 1})
        self.assertTrue(body["omitted_reason"])
        self.assertNotIn(RESTRICTED_FLUX, json.dumps(body))
        body = self._preview(self.pi_user).json()
        self.assertEqual(self.flux_of_discovery(body["payload"]), RESTRICTED_FLUX)
        self.assertEqual(body["omitted"], {"photometry_sets": 0, "spectra": 0})
        self.assertEqual(body["omitted_reason"], "")

    def test_preview_endpoint_classification_spectrum_by_role(self):
        params = {"kind": tns.KIND_CLASSIFICATION, "spectrum": self.spec.pk, "classification": "SN Ia"}
        body = self._preview(self.member, **params).json()
        self.assertTrue(body["problems"])
        body = self._preview(self.deputy, **params).json()
        self.assertEqual(body["problems"], [])

    def test_submit_endpoint_queues_scoped_payload(self):
        self.as_candidate()
        client = Client()
        client.force_login(self.member)
        response = client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                               {"service": self.service.pk})
        self.assertEqual(response.status_code, 200, response.content)
        from YSE_App.models import SharingSubmission
        sub = SharingSubmission.objects.get(pk=response.json()["id"])
        self.assertEqual(self.flux_of_discovery(sub.payload), PUBLIC_FLUX)

    def test_hermes_message_by_role(self):
        for user, n_points in ((self.member, 3), (self.pi_user, 5), (self.staff, 3)):
            message = hermes_feed.build_message(self.transient, "t", scope=ReportScope.for_user(user))
            self.assertEqual(len(message["data"]["photometry"]), n_points, user.username)
        self.assertEqual(len(hermes_feed.build_message(self.transient, "t")["data"]["photometry"]), 3)


class AutopublishTests(ReportBase):
    def test_default_is_public_only(self):
        self.as_candidate()
        sub = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, None, dispatch=False)
        self.assertEqual(self.flux_of_discovery(sub.payload), PUBLIC_FLUX)
        self.assertNotIn(self.phot.pk, ReportScope.autopublish().photometry_ids(self.transient))
        self.assertNotIn(self.spec.pk, ReportScope.autopublish().spectrum_ids(self.transient))

    def test_opt_in_includes_the_dataset(self):
        self.as_candidate()
        autopublish_optin.set_opt_in(self.pi_user, self.phot, True)
        autopublish_optin.set_opt_in(self.deputy, self.spec, True)
        sub = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, None, dispatch=False)
        self.assertEqual(self.flux_of_discovery(sub.payload), RESTRICTED_FLUX)
        self.assertIn(self.spec.pk, ReportScope.autopublish().spectrum_ids(self.transient))
        autopublish_optin.set_opt_in(self.pi_user, self.phot, False)
        sub = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, None, dispatch=False)
        self.assertEqual(self.flux_of_discovery(sub.payload), PUBLIC_FLUX)

    def test_opt_in_does_not_widen_a_manual_report(self):
        autopublish_optin.set_opt_in(self.pi_user, self.phot, True)
        self.assertNotIn(self.phot.pk, ReportScope.for_user(self.member).photometry_ids(self.transient))

    def test_rule_picks_only_allowed_spectra(self):
        from YSE_App.sharing import autopublish
        self.transient.best_spectrum = self.spec
        self.transient.save()
        # the best spectrum is restricted, so the rule falls back to the public one
        self.assertEqual(self._rule_spectrum(autopublish), self.public_spec)
        autopublish_optin.set_opt_in(self.pi_user, self.spec, True)
        self.assertEqual(self._rule_spectrum(autopublish), self.spec)

    def _rule_spectrum(self, autopublish):
        captured = {}

        def fake_create(service, transient, kind, user, **options):
            captured.update(options)
            raise tns.PayloadError("stop")

        rule = type("Rule", (), {"matches": lambda s, t: True, "kind": tns.KIND_CLASSIFICATION, "coauthors": "",
                                 "remarks": "", "service": self.service, "name": "r"})()
        original = tns.create_submission
        tns.create_submission = fake_create
        try:
            autopublish.apply_rule(rule, self.transient, dispatch=False)
        finally:
            tns.create_submission = original
        return captured.get("spectrum")

    def test_hermes_publish_scope_for_rule_submission(self):
        class Sub:
            auto_publisher_id = 5
            created_by = self.pi_user

        self.assertEqual(ReportScope.for_submission(Sub).mode, report_scope.AUTOPUBLISH)
        Sub.auto_publisher_id = None
        self.assertEqual(ReportScope.for_submission(Sub).user, self.pi_user)


class OptInPermissionTests(ReportBase):
    def test_only_share_role_holders_can_set_it(self):
        for user in (self.member, self.outsider, self.requester):
            with self.assertRaises(PermissionDenied):
                autopublish_optin.set_opt_in(user, self.phot, True)
        self.assertFalse(AutopublishOptIn.objects.exists())
        for user in (self.pi_user, self.deputy, self.staff):
            self.assertTrue(autopublish_optin.set_opt_in(user, self.phot, True))
            self.assertTrue(autopublish_optin.is_opted_in(self.phot))
            self.assertFalse(autopublish_optin.set_opt_in(user, self.phot, False))
            self.assertFalse(autopublish_optin.is_opted_in(self.phot))

    def test_public_data_needs_no_opt_in(self):
        public_phot = self.transient.transientphotometry_set.exclude(pk=self.phot.pk).first()
        with self.assertRaises(ValidationError):
            autopublish_optin.set_opt_in(self.pi_user, public_phot, True)

    def test_changes_are_logged(self):
        before = LogEntry.objects.count()
        autopublish_optin.set_opt_in(self.pi_user, self.spec, True)
        autopublish_optin.set_opt_in(self.pi_user, self.spec, False)
        self.assertEqual(LogEntry.objects.count(), before + 2)

    def test_endpoint(self):
        url = reverse("sharing_autopublish_optin", args=["spectrum", self.spec.pk])
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.post(url, {"enabled": "1"}).status_code, 403)
        self.assertFalse(AutopublishOptIn.objects.exists())
        client.force_login(self.pi_user)
        self.assertEqual(client.get(url).status_code, 405)
        response = client.post(url, {"enabled": "1"})
        self.assertEqual(response.json()["autopublish"], True)
        self.assertTrue(autopublish_optin.is_opted_in(self.spec))
        self.assertEqual(client.post(url, {"enabled": "0"}).json()["autopublish"], False)
