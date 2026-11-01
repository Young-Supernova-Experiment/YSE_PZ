"""Revoke an accepted data access request (#291): forward by default, backwards on request.

Synthetic data only.
"""

import datetime

from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.models import DataAccessRequest, Notification, TransientPhotData, TransientPhotometry
from YSE_App.services import data_access as dar
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user
from YSE_App.tests.test_access_dashboard import _Fixture

ON = override_settings(DATA_ACCESS_GRANTS_ENABLED=True)


class _RevokeBase(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.requester = create_test_user("rv291_requester", is_staff=False)
        cls.t_group, _ = Group.objects.get_or_create(name="rv291-team")
        cls.requester.groups.add(cls.t_group)
        audit = audit_fields(cls.staff)
        # a dataset restricted to the owner group only (the one the request covers)
        cls.secret = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        cls.secret.groups.add(cls.o)
        cls.secret.pis.add(cls.pi)
        cls.secret_points = [TransientPhotData.objects.create(
            photometry=cls.secret, band=cls.band, mag=18.0, mag_err=0.1,
            obs_date=cls.now - datetime.timedelta(days=d), **audit) for d in (3, 4)]
        # public data (no groups) on the same object
        cls.public = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        cls.public_point = TransientPhotData.objects.create(
            photometry=cls.public, band=cls.band, mag=17.0, mag_err=0.1, obs_date=cls.now, **audit)

    def secret_ids(self):
        return {p.pk for p in self.secret_points}

    def accepted(self):
        req = dar.request_access(self.requester, self.transient, "photometry", self.o, target_group=self.t_group,
                                 notify_owners=False)
        dar.decide(req, self.pi_user, True, notify_requester=False)
        req.refresh_from_db()
        return req


@ON
class RevokeTests(_RevokeBase):
    def test_acceptance_records_what_it_added(self):
        req = self.accepted()
        both = {self.phot.pk, self.secret.pk}  # every restricted set the owner group holds on the object
        self.assertEqual(set(req.granted_dataset_ids), both)
        self.assertEqual(set(req.added_dataset_ids), both)
        self.assertEqual(self.visible(self.requester) & self.secret_ids(), self.secret_ids())

    def test_forward_keeps_earlier_data_and_hides_later(self):
        req = self.accepted()
        dar.revoke(req, self.pi_user)
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_REVOKED)
        self.assertEqual(req.revoked_by, self.pi_user)
        self.assertIsNotNone(req.revoked_at)
        self.assertEqual(req.revoke_direction, "forward")
        self.assertFalse(self.secret.groups.filter(pk=self.t_group.pk).exists())
        self.assertEqual(self.visible(self.requester) & self.secret_ids(), self.secret_ids())
        later = TransientPhotData.objects.create(photometry=self.secret, band=self.band, mag=19.5, mag_err=0.1,
                                                 obs_date=timezone.now(), **audit_fields(self.staff))
        self.assertNotIn(later.pk, self.visible(self.requester))

    def test_backwards_hides_everything(self):
        req = self.accepted()
        dar.revoke(req, self.pi_user, backwards=True)
        req.refresh_from_db()
        self.assertEqual(req.revoke_direction, "backwards")
        self.assertFalse(self.visible(self.requester) & self.secret_ids())

    def test_public_data_is_unaffected(self):
        req = self.accepted()
        self.assertIn(self.public_point.pk, self.visible(self.requester))
        dar.revoke(req, self.pi_user, backwards=True)
        self.assertIn(self.public_point.pk, self.visible(self.requester))
        self.assertTrue(self.public.groups.count() == 0)

    def test_group_already_on_a_dataset_is_not_removed(self):
        self.secret.groups.add(self.t_group)  # shared before the request through another route
        req = self.accepted()
        self.assertEqual(req.added_dataset_ids, [self.phot.pk])
        dar.revoke(req, self.pi_user, backwards=True)
        self.assertTrue(self.secret.groups.filter(pk=self.t_group.pk).exists())

    def test_non_decider_is_refused(self):
        req = self.accepted()
        for user in (self.member, self.requester):
            with self.assertRaises(PermissionDenied):
                dar.revoke(req, user)
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_ACCEPTED)
        self.assertTrue(self.secret.groups.filter(pk=self.t_group.pk).exists())

    def test_staff_may_revoke_as_administrator(self):
        req = self.accepted()
        dar.revoke(req, self.staff, backwards=True)
        req.refresh_from_db()
        self.assertEqual(req.revoked_by, self.staff)
        self.assertFalse(self.visible(self.requester) & self.secret_ids())

    def test_only_accepted_requests_can_be_revoked(self):
        req = dar.request_access(self.requester, self.transient, "photometry", self.o, target_group=self.t_group,
                                 notify_owners=False)
        with self.assertRaises(ValidationError):
            dar.revoke(req, self.pi_user)
        dar.decide(req, self.pi_user, True, notify_requester=False)
        dar.revoke(req, self.pi_user)
        with self.assertRaises(ValidationError):
            dar.revoke(req, self.pi_user)

    def test_requester_is_notified_in_app(self):
        req = self.accepted()
        dar.revoke(req, self.pi_user)
        note = Notification.objects.filter(recipient=self.requester, kind="data_access", subject__startswith="Data access revoked")
        self.assertTrue(note.exists())

    def test_can_revoke_matches_deciders(self):
        req = self.accepted()
        self.assertTrue(dar.can_revoke(self.pi_user, req))
        self.assertTrue(dar.can_revoke(self.staff, req))
        self.assertFalse(dar.can_revoke(self.member, req))


@ON
class RevokeLegacyTests(_RevokeBase):
    """Requests accepted before added_dataset_ids existed: only datasets with a share record from the acceptance on."""

    def legacy(self):
        req = self.accepted()
        req.added_dataset_ids = None
        req.save()
        return DataAccessRequest.objects.get(pk=req.pk)

    def test_record_from_acceptance_is_removed(self):
        from YSE_App.models import DatasetGroupShare

        req = self.legacy()
        self.assertTrue(DatasetGroupShare.objects.filter(
            transient_photometry=self.secret, group=self.t_group, shared_at__gte=req.decided_at).exists())
        dar.revoke(req, self.pi_user)
        self.assertFalse(self.secret.groups.filter(pk=self.t_group.pk).exists())
        self.assertFalse(self.phot.groups.filter(pk=self.t_group.pk).exists())
        self.assertEqual(req.revoke_skipped, 0)

    def test_no_record_or_earlier_record_is_skipped(self):
        from YSE_App.models import DatasetGroupShare

        req = self.legacy()
        # secret: shared before tracking (no record); phot: an earlier record
        DatasetGroupShare.objects.filter(transient_photometry=self.secret, group=self.t_group).delete()
        DatasetGroupShare.objects.filter(transient_photometry=self.phot, group=self.t_group).update(
            shared_at=req.decided_at - datetime.timedelta(days=1))
        dar.revoke(req, self.pi_user, backwards=True)
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_REVOKED)
        self.assertEqual(req.revoke_skipped, 2)
        self.assertTrue(self.secret.groups.filter(pk=self.t_group.pk).exists())
        self.assertTrue(self.phot.groups.filter(pk=self.t_group.pk).exists())

    def test_skipped_count_in_api_response(self):
        from YSE_App.models import DatasetGroupShare

        req = self.legacy()
        DatasetGroupShare.objects.filter(transient_photometry=self.secret, group=self.t_group).delete()
        client = Client()
        client.force_login(self.pi_user)
        resp = client.post("/api/dataaccessrequests/%d/revoke/" % req.pk, {}, content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["revoke_skipped"], 1)


@ON
class RevokePagesTests(_RevokeBase):
    def test_inbox_form_forward_default_and_backwards(self):
        req = self.accepted()
        client = Client()
        client.force_login(self.pi_user)
        page = client.get(reverse("data_access_requests") + "?box=decided")
        self.assertContains(page, reverse("data_access_revoke", args=[req.pk]))
        resp = client.post(reverse("data_access_revoke", args=[req.pk]), {"direction": "backward"})
        self.assertEqual(resp.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.revoke_direction, "backwards")

    def test_post_by_non_decider_changes_nothing(self):
        req = self.accepted()
        client = Client()
        client.force_login(self.member)
        client.post(reverse("data_access_revoke", args=[req.pk]), {})
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_ACCEPTED)

    def test_dashboard_lists_and_revokes(self):
        req = self.accepted()
        client = Client()
        client.force_login(self.pi_user)
        self.assertContains(client.get(reverse("access_dashboard")), 'data-request-id="%d"' % req.pk)
        client.post(reverse("data_access_revoke", args=[req.pk]), {"direction": "forward", "next": reverse("access_dashboard")})
        req.refresh_from_db()
        self.assertEqual(req.revoke_direction, "forward")
        self.assertNotContains(client.get(reverse("access_dashboard")), 'data-request-id="%d"' % req.pk)

    def test_api_action(self):
        req = self.accepted()
        client = Client()
        client.force_login(self.pi_user)
        url = "/api/dataaccessrequests/%d/revoke/" % req.pk
        bad = client.post(url, {"direction": "sideways"}, content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        ok = client.post(url, {"direction": "backwards"}, content_type="application/json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()["status"], "revoked")
        self.assertEqual(ok.json()["revoke_direction"], "backwards")

    def test_api_refuses_non_decider(self):
        req = self.accepted()
        client = Client()
        client.force_login(self.requester)
        resp = client.post("/api/dataaccessrequests/%d/revoke/" % req.pk, {}, content_type="application/json")
        self.assertIn(resp.status_code, (403, 404))
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_ACCEPTED)


@override_settings(DATA_ACCESS_GRANTS_ENABLED=False)
class RevokeFlagOffTests(_RevokeBase):
    """Flag off: rules are recorded but not read, so removing the group hides the data at once (as it always did)."""

    def test_revoke_works_and_hides_data(self):
        req = self.accepted()
        self.assertEqual(self.visible(self.requester) & self.secret_ids(), self.secret_ids())
        dar.revoke(req, self.pi_user)
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_REVOKED)
        self.assertFalse(self.visible(self.requester) & self.secret_ids())
        self.assertIn(self.public_point.pk, self.visible(self.requester))
