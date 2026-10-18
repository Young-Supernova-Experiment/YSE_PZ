"""Data reported to TNS become and stay public (#708). Synthetic data only; TNS is mocked."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Group
from rest_framework.exceptions import ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.jobs import run_pass
from YSE_App.models import TransientPhotData, TransientPhotometry, TransientSpectrum
from YSE_App.services import access_actions, data_sharing, tns_public
from YSE_App.sharing import tns
from YSE_App.tests import test_access_rules_engine as engine
from YSE_App.tests import test_sharing_services as ss


def _group_names(dataset):
    return set(dataset.groups.values_list("name", flat=True))


class ReportedDataBecomePublicTests(ss.SharingBase):
    def setUp(self):
        super().setUp()
        self.private = Group.objects.create(name="tnspub-private")
        self.header = self.transient.transientphotometry_set.first()
        self.header.groups.set([self.private])
        self.spectrum = self.transient.transientspectrum_set.first()
        self.spectrum.groups.set([self.private])
        self.point_ids = set(TransientPhotData.objects.filter(photometry=self.header).values_list("pk", flat=True))
        self.service.testing = False
        self.service.config = dict(self.service.config, max_photometry_points=50)
        self.service.save()

    def report(self, kind=tns.KIND_DISCOVERY, reply=None, **options):
        router = ss.TNSRouter(reply=reply or ss.accepted_reply(
            prefix="SN" if kind == tns.KIND_CLASSIFICATION else "AT",
            kind="classification_report" if kind == tns.KIND_CLASSIFICATION else "at_report"))
        with mock.patch("YSE_App.sharing.tns.requests.post", router):
            submission = tns.create_submission(self.service, self.transient, kind, self.staff, **options)
            run_pass()
        submission.refresh_from_db()
        return submission

    def test_payload_records_the_points_it_sends_and_strips_them(self):
        payload = tns.build_at_report(self.service, self.transient, scope=tns.ReportScope.for_user(self.staff))
        self.assertEqual(set(payload["_yse"]["photometry_point_ids"]), self.point_ids)
        self.assertNotIn("_yse", tns.strip_private(payload))

    def test_nothing_changes_before_tns_confirms(self):
        tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        self.assertEqual(_group_names(self.header), {"tnspub-private"})
        rejected = self.report(reply=ss.rejected_reply("duplicate"))
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(_group_names(self.header), {"tnspub-private"})

    def test_accepted_discovery_makes_the_sent_set_public(self):
        submission = self.report()
        self.assertEqual(submission.status, "accepted")
        self.header.refresh_from_db()
        self.assertEqual(_group_names(self.header), {PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertTrue(data_sharing.is_public_dataset(self.header))
        self.assertEqual(TransientPhotometry.objects.filter(transient__pk=self.transient.pk).count(), 1)
        self.assertEqual(_group_names(self.spectrum), {"tnspub-private"})  # not sent

    def test_partly_sent_set_is_split(self):
        self.service.config = dict(self.service.config, max_photometry_points=2)
        self.service.save()
        payload = tns.build_at_report(self.service, self.transient, scope=tns.ReportScope.for_user(self.staff))
        sent = set(payload["_yse"]["photometry_point_ids"])
        self.assertTrue(sent and sent < self.point_ids)
        self.report()
        self.header.refresh_from_db()
        self.assertEqual(_group_names(self.header), {"tnspub-private"})
        self.assertEqual(set(TransientPhotData.objects.filter(photometry=self.header).values_list("pk", flat=True)),
                         self.point_ids - sent)
        copy = TransientPhotometry.objects.get(transient=self.transient, groups__name=PUBLIC_COLLABORATION_GROUP_NAME)
        self.assertNotEqual(copy.pk, self.header.pk)
        self.assertEqual(_group_names(copy), {PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertEqual(set(TransientPhotData.objects.filter(photometry=copy).values_list("pk", flat=True)), sent)
        self.assertEqual((copy.instrument_id, copy.obs_group_id), (self.header.instrument_id, self.header.obs_group_id))

    def test_service_function_is_idempotent_and_logged(self):
        with self.assertLogs("YSE_App.services.tns_public", level="INFO") as logs:
            first = tns_public.mark_reported_public(self.transient, photometry_point_ids=self.point_ids,
                                                    spectrum_ids=[self.spectrum.pk])
        self.assertEqual((first["sets_public"], first["sets_split"], first["spectra"]), (1, 0, 1))
        self.assertTrue(any("now public" in line for line in logs.output))
        again = tns_public.mark_reported_public(self.transient, photometry_point_ids=self.point_ids,
                                                spectrum_ids=[self.spectrum.pk])
        self.assertEqual(again["sets_public"], 0)
        self.assertEqual(TransientPhotometry.objects.filter(transient=self.transient).count(), 1)
        self.assertEqual(_group_names(self.spectrum), {PUBLIC_COLLABORATION_GROUP_NAME})

    def test_already_public_set_just_gets_the_public_group(self):
        self.header.groups.clear()
        tns_public.mark_reported_public(self.transient, photometry_point_ids=list(self.point_ids)[:1])
        self.assertEqual(_group_names(self.header), {PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertEqual(TransientPhotometry.objects.filter(transient=self.transient).count(), 1)

    def test_other_transients_and_unsent_data_are_untouched(self):
        other = ss.create_minimal_transient(self.staff, name="yse_other_001")
        result = tns_public.mark_reported_public(other, photometry_point_ids=self.point_ids,
                                                 spectrum_ids=[self.spectrum.pk])
        self.assertEqual(result["points"] + result["spectra"], 0)
        self.assertEqual(_group_names(self.header), {"tnspub-private"})

    def test_accepted_classification_makes_the_spectrum_public(self):
        self.transient.name = "2026abc"
        self.transient.save()
        self.report(kind=tns.KIND_CLASSIFICATION, spectrum=self.spectrum, classification="SN Ia")
        self.assertEqual(_group_names(self.spectrum), {PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertEqual(_group_names(self.header), {"tnspub-private"})  # no photometry was sent

    def test_sandbox_report_does_not_publish(self):
        self.service.testing = True
        self.service.save()
        self.assertEqual(self.report().status, "accepted")
        self.assertEqual(_group_names(self.header), {"tnspub-private"})


class TnsDataStaysVisibleTests(engine._Fixture):
    """A deny rule or a group removal cannot hide data once it is public (the engine never hides public data)."""

    def setUp(self):
        super().setUp()
        points = [self.points[("g", 0)].pk, self.points[("g", 1)].pk]
        tns_public.mark_reported_public(self.transient, photometry_point_ids=points, spectrum_ids=[self.specs["g"].pk])

    @engine.ON
    def test_deny_rules_and_group_removal_leave_it_visible(self):
        for user in (self.member, self.member2):
            self.assertTrue(self.all_points("g") <= self.points_of(user))
        for point in (self.points[("g", 0)], self.points[("g", 1)]):
            self.rule(point, engine.DENY, user=self.member)
            self.rule(point, engine.DENY, group=self.g)
        self.rule(self.specs["g"], engine.DENY, user=self.member)
        self.member.groups.remove(self.g)
        for user in (self.member, self.member2, self.outsider):
            self.assertTrue(self.all_points("g") <= self.points_of(user), user.username)
            self.assertIn(self.sets["g"].pk, self.sets_of(user))
            self.assertIn(self.specs["g"].pk, self.specs_of(user))

    @engine.ON
    def test_the_rule_actions_refuse_or_skip_it(self):
        with self.assertRaises(ValidationError):
            access_actions.dataset_action(self.staff, self.sets["g"], effect=engine.DENY, grantee_group=self.g)
        result = access_actions.write_rules(points=list(self.all_points("g")), effect=engine.DENY,
                                            grantee_group=self.g, granted_by=self.staff,
                                            granted_as=engine.R.AS_STAFF)
        self.assertEqual(result.created, 0)
        self.assertFalse(engine.R.objects.filter(transient_phot_data__photometry=self.sets["g"]).exists())
        self.assertTrue(data_sharing.is_public_dataset(TransientSpectrum.objects.get(pk=self.specs["g"].pk)))
