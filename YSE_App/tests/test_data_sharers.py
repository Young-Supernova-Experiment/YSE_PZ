"""Who may share private data (#447): PIs and their designated deputies only; staff as a logged override.

Synthetic data only (users, groups, PIs and allocations are made up here).
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.models import (
    Allocation,
    ClassicalResource,
    DataAccessRequest,
    DataSharingDelegate,
    FollowupStatus,
    Notification,
    TransientFollowup,
    TransientSpectrum,
)
from YSE_App.services import data_access as dar
from YSE_App.services import data_pis
from YSE_App.services import data_sharing
from YSE_App.services import groups as group_svc
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_test_user,
)
from YSE_App.tests.test_data_pis import PiBase, _member, _pi


class SharerBase(PiBase):
    def _request(self, kind="spectrum", **kw):
        kw.setdefault("notify_owners", False)
        return dar.request_access(self.requester, self.transient, kind, self.owners, **kw)

    def _followup_with_allocation_pi(self, pi, *, allocation=True):
        now = timezone.now()
        alloc = None
        if allocation:
            alloc = Allocation.objects.create(
                name="Synthetic allocation", telescope=self.instrument.telescope, instrument=self.instrument,
                principal_investigator=pi, start_date=now - datetime.timedelta(days=30),
                end_date=now + datetime.timedelta(days=30), **audit_fields(self.admin),
            )
        resource = ClassicalResource.objects.create(
            telescope=self.instrument.telescope, begin_date_valid=now - datetime.timedelta(days=1),
            end_date_valid=now + datetime.timedelta(days=30), allocation=alloc,
            principal_investigator=None if allocation else pi, **audit_fields(self.admin),
        )
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.admin))
        return TransientFollowup.objects.create(
            transient=self.transient, status=status, valid_start=now, valid_stop=now + datetime.timedelta(days=3),
            classical_resource=resource, **audit_fields(self.admin),
        )


class DeciderRuleTests(SharerBase):
    def test_member_who_is_not_a_pi_cannot_decide(self):
        self.spec.pis.add(self.pi)
        req = self._request()
        self.assertFalse(dar.user_can_decide(self.member, req))
        self.assertIsNone(dar.decider_role(self.member, req))
        self.assertEqual(list(dar.requests_to_decide(self.member)), [])
        self.assertEqual(dar.pending_count_for(self.member), 0)
        with self.assertRaises(PermissionDenied):
            dar.decide(req, self.member, True)
        self.assertNotIn(self.member, dar.deciders_for(req))

    def test_group_owner_without_pi_status_cannot_decide(self):
        self.spec.pis.add(self.pi)
        owner = _member("pi_group_owner", self.owners)
        group_svc.profile_for(self.owners).owners.add(owner)
        req = self._request()
        self.assertFalse(dar.user_can_decide(owner, req))
        self.assertEqual(list(dar.requests_to_decide(owner)), [])
        with self.assertRaises(PermissionDenied):
            dar.decide(req, owner, True)

    def test_owner_who_is_the_pi_shares_as_the_pi(self):
        owner = _member("pi_owner_and_pi", self.owners)
        group_svc.profile_for(self.owners).owners.add(owner)
        self.spec.pis.add(_pi(self.admin, "Synthetic PI Owner", user=owner))
        req = self._request()
        self.assertEqual(dar.decider_role(owner, req), "pi")
        dar.decide(req, owner, True)
        req.refresh_from_db()
        self.assertEqual(req.decided_as, DataAccessRequest.DECIDED_AS_PI)
        self.assertEqual(req.granted_dataset_ids, [self.spec.pk])

    def test_deputy_can_decide(self):
        self.spec.pis.add(self.pi)
        deputy = _member("pi_deputy")
        data_sharing.add_delegate(self.pi_user, self.pi, deputy)
        req = self._request()
        self.assertEqual(dar.deciders_for(req), [self.pi_user, deputy])
        self.assertEqual(dar.designated_sharers_for(req, [self.spec]), [deputy])
        self.assertEqual(list(dar.requests_to_decide(deputy)), [req])
        self.assertEqual(dar.pending_count_for(deputy), 1)
        dar.decide(req, deputy, True, note="shared for the PI")
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_ACCEPTED)
        self.assertEqual(req.decided_as, DataAccessRequest.DECIDED_AS_DEPUTY)
        self.assertTrue(self.spec.groups.filter(pk=self.mine.pk).exists())

    def test_deputy_scoped_to_a_group(self):
        self.spec.pis.add(self.pi)
        other = Group.objects.create(name="OTHERGROUP")
        scoped_out = _member("pi_deputy_other")
        scoped_in = _member("pi_deputy_in")
        data_sharing.add_delegate(self.pi_user, self.pi, scoped_out, other)
        data_sharing.add_delegate(self.pi_user, self.pi, scoped_in, self.owners)
        req = self._request()
        self.assertFalse(dar.user_can_decide(scoped_out, req))
        self.assertEqual(list(dar.requests_to_decide(scoped_out)), [])
        self.assertTrue(dar.user_can_decide(scoped_in, req))
        self.assertEqual(list(dar.requests_to_decide(scoped_in)), [req])
        # a deputy of the spectrum's PI is not a decider for photometry led by someone else
        self.phot.pis.add(self.pi2)
        phot_req = self._request("photometry")
        self.assertFalse(dar.user_can_decide(scoped_in, phot_req))

    def test_accept_shares_only_the_deciders_datasets(self):
        spec2 = attach_synthetic_spectrum(self.admin, self.transient, obs_group=self.obs_group,
                                          instrument=self.instrument)
        spec2.groups.add(self.owners)
        self.spec.pis.add(self.pi)
        spec2.pis.add(self.pi2)
        req = self._request()
        self.assertEqual(set(u.pk for u in dar.deciders_for(req)), {self.pi_user.pk, self.pi_user2.pk})
        dar.decide(req, self.pi_user, True)
        req.refresh_from_db()
        self.assertEqual(req.granted_dataset_ids, [self.spec.pk])
        self.assertFalse(spec2.groups.filter(pk=self.mine.pk).exists(), "the other PI's spectrum is not shared")

    def test_staff_override_is_recorded_and_logged(self):
        self.spec.pis.add(self.pi)
        req = self._request()
        self.assertEqual(dar.decider_role(self.admin, req), "staff")
        with self.assertLogs("YSE_App.data_access", level="WARNING") as logs:
            dar.decide(req, self.admin, True)
        self.assertIn("administrator override", logs.output[0])
        req.refresh_from_db()
        self.assertEqual(req.decided_as, DataAccessRequest.DECIDED_AS_STAFF)
        self.assertTrue(req.is_staff_override)
        self.assertEqual(req.granted_dataset_ids, [self.spec.pk])

    def test_pi_decision_is_not_logged_as_override(self):
        self.spec.pis.add(self.pi)
        req = self._request()
        dar.decide(req, self.pi_user, False)
        req.refresh_from_db()
        self.assertEqual(req.decided_as, DataAccessRequest.DECIDED_AS_PI)


class NoPiTests(SharerBase):
    def test_without_pi_only_superusers_are_notified(self):
        staff_only = create_test_user("pi_staff_only", is_staff=True, is_superuser=False)
        inactive_super = create_test_user("pi_inactive_super", is_staff=True, is_superuser=True)
        inactive_super.is_active = False
        inactive_super.save()
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners)
        self.assertTrue(dar.user_can_decide(staff_only, req))  # staff may still decide
        notes = Notification.objects.filter(kind="data_access")
        self.assertEqual(set(notes.values_list("recipient__username", flat=True)), {"pi_admin"})

    def test_without_pi_only_staff_decide_and_are_notified(self):
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners)
        self.assertEqual(dar.deciders_for(req), [])
        self.assertFalse(dar.user_can_decide(self.member, req))
        self.assertTrue(dar.user_can_decide(self.admin, req))
        notes = Notification.objects.filter(kind="data_access")
        self.assertEqual(set(notes.values_list("recipient__username", flat=True)), {"pi_admin"})
        self.assertIn("No PI assigned", notes.first().text)

    def test_inbox_notes_no_pi_for_staff_and_hint_says_so(self):
        req = self._request()
        c = Client()
        c.force_login(self.admin)
        response = c.get(reverse("data_access_requests"))
        self.assertContains(response, 'data-request-id="%d"' % req.pk)
        self.assertContains(response, "No PI assigned: only administrators can decide.")
        c = Client()
        c.force_login(self.requester)
        DataAccessRequest.objects.all().delete()
        html = c.get(reverse("transient_detail_spectra_tab_fragment",
                             kwargs={"transient_id": self.transient.pk})).content.decode()
        self.assertIn("no PI assigned", html)
        self.assertIn("only an administrator can accept or decline", html)

    def test_member_sees_nothing_to_decide(self):
        self._request()
        c = Client()
        c.force_login(self.member)
        response = c.get(reverse("data_access_requests"))
        self.assertContains(response, "Nothing to decide")


class AllocationFallbackTests(SharerBase):
    def test_allocation_pi_is_the_fallback(self):
        self.spec.followup = self._followup_with_allocation_pi(self.pi2)
        self.spec.save()
        self.assertEqual(data_pis.pis_for(self.spec), [self.pi2])
        self.assertEqual(data_pis.pi_users_for(self.spec), [self.pi_user2])
        req = self._request()
        self.assertTrue(dar.user_can_decide(self.pi_user2, req))
        self.assertEqual(list(dar.requests_to_decide(self.pi_user2)), [req])
        self.assertFalse(dar.user_can_decide(self.pi_user, req))
        # the hint shows the fallback PI's name
        rows = dar.hidden_summary(self.transient, self.requester, kinds=("spectrum",))
        self.assertEqual(rows[0]["pis"], ["Synthetic PI Two"])

    def test_resource_pi_without_allocation(self):
        self.spec.followup = self._followup_with_allocation_pi(self.pi2, allocation=False)
        self.spec.save()
        self.assertEqual(data_pis.pis_for(self.spec), [self.pi2])
        req = self._request()
        self.assertEqual(list(dar.requests_to_decide(self.pi_user2)), [req])

    def test_explicit_pi_wins_over_the_allocation(self):
        self.spec.followup = self._followup_with_allocation_pi(self.pi2)
        self.spec.save()
        self.spec.pis.add(self.pi)
        self.assertEqual(data_pis.pis_for(self.spec), [self.pi])
        req = self._request()
        self.assertFalse(dar.user_can_decide(self.pi_user2, req))
        self.assertEqual(list(dar.requests_to_decide(self.pi_user2)), [])
        self.assertTrue(dar.user_can_decide(self.pi_user, req))

    def test_deputy_of_the_allocation_pi(self):
        self.spec.followup = self._followup_with_allocation_pi(self.pi2)
        self.spec.save()
        deputy = _member("alloc_deputy")
        data_sharing.add_delegate(self.pi_user2, self.pi2, deputy)
        req = self._request()
        self.assertEqual(list(dar.requests_to_decide(deputy)), [req])
        self.assertEqual(dar.decider_role(deputy, req), "deputy")


class PublicDataTests(SharerBase):
    def test_public_data_has_no_request_option(self):
        # only public data on this transient: no groups, or only the Public group
        TransientSpectrum.objects.filter(pk=self.spec.pk).delete()
        self.phot.groups.clear()
        public = Group.objects.get(name="Public")
        tns_like = attach_synthetic_spectrum(self.admin, self.transient, obs_group=self.obs_group,
                                             instrument=self.instrument)
        tns_like.groups.add(public)
        self.assertTrue(data_sharing.is_public_dataset(self.phot))
        self.assertTrue(data_sharing.is_public_dataset(tns_like))
        self.assertEqual(dar.hidden_summary(self.transient, self.requester), [])
        for kind in ("spectrum", "photometry"):
            with self.assertRaises(ValidationError):
                dar.request_access(self.requester, self.transient, kind, public)
        c = Client()
        c.force_login(self.requester)
        for name in ("transient_detail_spectra_tab_fragment", "transient_detail_photometry_fragment"):
            html = c.get(reverse(name, kwargs={"transient_id": self.transient.pk})).content.decode()
            self.assertNotIn("yse-data-access-hint", html)
            self.assertNotIn("yse-data-access-request-btn", html)

    def test_restricted_data_is_not_public(self):
        self.assertFalse(data_sharing.is_public_dataset(self.spec))


class GroupChangeTests(SharerBase):
    def test_changing_groups_of_restricted_data_needs_sharing_authority(self):
        self.spec.pis.add(self.pi)
        new = [self.owners, self.mine]
        with self.assertRaises(PermissionDenied):
            data_sharing.check_group_change(self.member, self.spec, new)
        with self.assertRaises(PermissionDenied):
            data_sharing.check_group_change(self.member, self.spec, [])  # un-restricting is sharing too
        data_sharing.check_group_change(self.member, self.spec, [self.owners])  # unchanged
        data_sharing.check_group_change(self.pi_user, self.spec, new)
        data_sharing.check_group_change(self.admin, self.spec, new)
        public_phot = attach_synthetic_photometry(self.admin, self.transient, obs_group=self.obs_group,
                                                  instrument=self.instrument, band=self.band, n_points=1)
        public_phot.groups.clear()
        data_sharing.check_group_change(self.member, public_phot, [self.owners])


class DeputyManagementTests(SharerBase):
    def test_service_rules(self):
        deputy = _member("mgmt_deputy")
        with self.assertRaises(PermissionDenied):
            data_sharing.add_delegate(self.member, self.pi, deputy)
        with self.assertRaises(PermissionDenied):
            data_sharing.add_delegate(self.pi_user2, self.pi, deputy)
        with self.assertRaises(ValidationError):
            data_sharing.add_delegate(self.pi_user, self.pi, self.pi_user)
        with self.assertRaises(ValidationError):
            data_sharing.add_delegate(self.pi_user, self.pi, deputy, Group.objects.get(name="Public"))
        row = data_sharing.add_delegate(self.pi_user, self.pi, deputy)
        self.assertEqual(row.created_by, self.pi_user)
        self.assertEqual(data_sharing.add_delegate(self.pi_user, self.pi, deputy), row, "idempotent")
        staff_row = data_sharing.add_delegate(self.admin, self.pi_unlinked, deputy)
        self.assertEqual(set(data_sharing.designations_of(deputy)), {(self.pi.pk, None), (self.pi_unlinked.pk, None)})
        with self.assertRaises(PermissionDenied):
            data_sharing.remove_delegate(self.member, row)
        data_sharing.remove_delegate(self.pi_user, row)
        data_sharing.remove_delegate(self.admin, staff_row)
        self.assertFalse(DataSharingDelegate.objects.exists())

    def test_page_add_and_remove(self):
        deputy = _member("page_deputy")
        c = Client()
        c.force_login(self.pi_user)
        page = reverse("data_sharing_deputies")
        response = c.get(page)
        self.assertContains(response, "Synthetic PI One")
        self.assertNotContains(response, "Synthetic PI Two")
        response = c.post(page, {"action": "add", "pi": self.pi.pk, "username": "page_deputy",
                                 "group": self.owners.pk, "note": "observing run"}, follow=True)
        self.assertContains(response, "page_deputy may now share Synthetic PI One")
        row = DataSharingDelegate.objects.get()
        self.assertEqual((row.delegate, row.group, row.note), (deputy, self.owners, "observing run"))
        response = c.post(page, {"action": "add", "pi": self.pi.pk, "username": "no-such-user"}, follow=True)
        self.assertContains(response, "No active account with that username.")
        # another PI's account cannot add deputies for this PI
        other = Client()
        other.force_login(self.pi_user2)
        response = other.post(page, {"action": "add", "pi": self.pi.pk, "username": "pi_member"}, follow=True)
        self.assertContains(response, "Not changed")
        response = other.post(page, {"action": "remove", "delegate_id": row.pk}, follow=True)
        self.assertContains(response, "Not changed")
        # the deputy sees the designation
        d = Client()
        d.force_login(deputy)
        self.assertContains(d.get(page), "You are a deputy for")
        response = c.post(page, {"action": "remove", "delegate_id": row.pk}, follow=True)
        self.assertContains(response, "no longer shares")
        self.assertFalse(DataSharingDelegate.objects.exists())

    def test_page_for_a_user_without_pi_record_and_anonymous(self):
        c = Client()
        c.force_login(self.member)
        self.assertContains(c.get(reverse("data_sharing_deputies")), "not linked to a PI record")
        response = Client().get(reverse("data_sharing_deputies"))
        self.assertEqual(response.status_code, 302)

    def test_admin_pages_load(self):
        row = data_sharing.add_delegate(self.admin, self.pi, _member("admin_deputy"))
        c = Client()
        c.force_login(self.admin)
        for url in (reverse("admin:YSE_App_datasharingdelegate_changelist"),
                    reverse("admin:YSE_App_datasharingdelegate_change", args=[row.pk]),
                    reverse("admin:YSE_App_principalinvestigator_change", args=[self.pi.pk])):
            self.assertEqual(c.get(url).status_code, 200, url)


class MigrationSafetyTests(TestCase):
    def test_decided_as_column_is_nullable(self):
        with connection.cursor() as cursor:
            desc = connection.introspection.get_table_description(cursor, DataAccessRequest._meta.db_table)
        by_name = {col.name: col for col in desc}
        self.assertTrue(by_name["decided_as"].null_ok)
