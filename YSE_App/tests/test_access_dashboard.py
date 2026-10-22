"""Ryan's 2026-10-17 decisions (#648, PR 6a): restrict shared data, turn sharing off forward or backwards, dashboard.

Synthetic data only.
"""

import datetime

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import (
    AutopublishOptIn,
    DataAccessRule,
    DatasetGroupShare,
    GroupMembershipPeriod,
    PrincipalInvestigator,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.services import access_actions, access_pages, access_rules
from YSE_App.services.visibility import visible_photometry_ids_q
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

R = DataAccessRule
ON = override_settings(DATA_ACCESS_GRANTS_ENABLED=True)


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("ads648_staff", is_staff=True)
        cls.pi_user = create_test_user("ads648_pi", is_staff=False)
        cls.member = create_test_user("ads648_member", is_staff=False)
        cls.leaver = create_test_user("ads648_leaver", is_staff=False)
        cls.g, _ = Group.objects.get_or_create(name="ads648-g")
        cls.o, _ = Group.objects.get_or_create(name="ads648-o")
        cls.member.groups.add(cls.g)
        cls.leaver.groups.add(cls.g)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="ads648")
        cls.now = timezone.now()
        audit = audit_fields(cls.staff)
        cls.pi = PrincipalInvestigator.objects.create(
            name="ads648 PI", phone="0", email="ads648@example.com", institution="Synthetic", user=cls.pi_user, **audit)
        cls.transient = create_minimal_transient(cls.staff, name="YSEads648", ra=45.0, dec=-9.0)
        cls.phot = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        cls.phot.groups.add(cls.g, cls.o)
        cls.phot.pis.add(cls.pi)
        cls.points = [TransientPhotData.objects.create(
            photometry=cls.phot, band=cls.band, mag=19.0, mag_err=0.1,
            obs_date=cls.now - datetime.timedelta(days=d), **audit) for d in (1, 2)]
        cls.spec = TransientSpectrum.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, ra=1.0, dec=1.0,
            obs_date=cls.now - datetime.timedelta(days=1), **audit)
        cls.spec.groups.add(cls.g)
        cls.spec.pis.add(cls.pi)

    def setUp(self):
        cache.clear()

    def visible(self, user):
        user = type(user).objects.get(pk=user.pk)
        return set(TransientPhotData.objects.filter(visible_photometry_ids_q(user)).values_list("pk", flat=True))

    def pts(self):
        return {p.pk for p in self.points}


@ON
class GroupRemovalTests(_Fixture):
    def test_forward_by_default_keeps_earlier_points(self):
        kept = access_actions.remove_dataset_group(self.pi_user, self.phot, self.g)
        self.assertEqual(kept, 2)
        rules = R.objects.filter(photometry=self.phot, grantee_group=self.g)
        self.assertEqual(set(rules.values_list("effect", "granted_as")), {("allow", R.AS_KEPT)})
        share = DatasetGroupShare.objects.filter(transient_photometry=self.phot)
        self.assertFalse(share.filter(group=self.g).exists())
        self.assertEqual(self.visible(self.member) & self.pts(), self.pts())
        # a point added later is not shared with the group any more
        later = TransientPhotData.objects.create(photometry=self.phot, band=self.band, mag=20.0, mag_err=0.1,
                                                 obs_date=timezone.now(), **audit_fields(self.staff))
        self.assertNotIn(later.pk, self.visible(self.member))

    def test_backwards_removes_earlier_points_too(self):
        kept = access_actions.remove_dataset_group(self.pi_user, self.phot, self.g, backwards=True)
        self.assertEqual(kept, 0)
        self.assertFalse(R.objects.filter(photometry=self.phot).exists())
        self.assertFalse(self.visible(self.member) & self.pts())

    def test_plain_group_change_is_forward_and_last_group_makes_data_public(self):
        self.phot.groups.remove(self.o)  # as an upload or the API would
        self.assertEqual(R.objects.filter(photometry=self.phot, grantee_group=self.o).count(), 2)
        self.spec.groups.remove(self.g)  # its only group: the spectrum becomes public, nothing to keep
        self.assertFalse(R.objects.filter(transient_spectrum=self.spec).exists())

    def test_kept_rules_are_dated_when_shared_so_former_members_keep_them(self):
        self.leaver.groups.remove(self.g)
        left = GroupMembershipPeriod.objects.get(user=self.leaver, group=self.g).left_at
        self.assertEqual(self.visible(self.leaver) & self.pts(), self.pts())
        access_actions.remove_dataset_group(self.pi_user, self.phot, self.g)
        for rule in R.objects.filter(photometry=self.phot, grantee_group=self.g):
            self.assertLess(rule.updated_at, left)
        self.assertEqual(self.visible(self.leaver) & self.pts(), self.pts())

    def test_only_those_who_may_share(self):
        from rest_framework.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            access_actions.remove_dataset_group(self.member, self.phot, self.g)


@ON
class TurnOffTests(_Fixture):
    def test_restrict_shared_data_and_lift(self):
        result = access_actions.dataset_action(self.pi_user, self.phot, effect="deny", grantee_user=self.member)
        self.assertEqual(result.created, 2)
        self.assertFalse(self.visible(self.member) & self.pts())
        n = access_actions.remove_rules(self.pi_user, self.phot, effect="deny", grantee_user=self.member)
        self.assertEqual(n, 2)
        self.assertEqual(self.visible(self.member) & self.pts(), self.pts())

    def test_allow_off_forward_and_backwards(self):
        outsider = create_test_user("ads648_out", is_staff=False)
        access_actions.dataset_action(self.pi_user, self.phot, effect="allow", grantee_user=outsider)
        self.assertEqual(access_actions.remove_rules(self.pi_user, self.phot, effect="allow", grantee_user=outsider),
                         0)
        self.assertEqual(self.visible(outsider) & self.pts(), self.pts())
        self.assertEqual(access_actions.remove_rules(self.pi_user, self.phot, effect="allow", grantee_user=outsider,
                                                     backwards=True), 2)
        self.assertFalse(self.visible(outsider) & self.pts())

    def test_stop_forward_and_backwards(self):
        from YSE_App.models import Allocation

        alloc = Allocation.objects.create(
            name="ads648 A", telescope=self.instrument.telescope, instrument=self.instrument,
            principal_investigator=self.pi, start_date=self.now - datetime.timedelta(days=30),
            end_date=self.now + datetime.timedelta(days=30), **audit_fields(self.staff))
        TransientPhotometry.objects.filter(pk=self.phot.pk).update(allocation=alloc)
        outsider = create_test_user("ads648_out2", is_staff=False)
        grant, _ = access_actions.program_action(self.pi_user, alloc, effect="allow", grantee_user=outsider)
        self.assertEqual(R.objects.filter(standing_grant=grant).count(), 2)
        self.assertEqual(access_actions.stop_standing_grant(self.pi_user, grant), 0)
        self.assertEqual(R.objects.filter(standing_grant=grant).count(), 2)
        self.assertEqual(access_actions.stop_standing_grant(self.pi_user, grant, backwards=True), 2)
        grant.refresh_from_db()
        self.assertEqual(grant.stopped_at, grant.created_at)  # covers nothing any more
        access_actions.apply_standing_grants_to_set(self.phot, linked=True)
        self.assertFalse(R.objects.filter(standing_grant=grant).exists())

    def test_public_data_cannot_be_restricted(self):
        from rest_framework.exceptions import ValidationError

        public = TransientPhotometry.objects.create(
            transient=self.transient, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.staff))
        public.pis.add(self.pi)
        with self.assertRaises(ValidationError):
            access_actions.dataset_action(self.pi_user, public, effect="deny", grantee_group=self.g)


class DashboardOffTests(_Fixture):
    def test_hidden_when_off(self):
        client = Client()
        client.force_login(self.pi_user)
        self.assertEqual(client.get(reverse("access_dashboard")).status_code, 404)


@ON
class DashboardTests(_Fixture):
    def test_page_and_actions(self):
        client = Client()
        client.force_login(self.pi_user)
        url = reverse("access_dashboard")
        resp = client.get(url)
        self.assertContains(resp, "yse-dashboard-datasets")
        self.assertContains(resp, self.transient.name)
        self.assertContains(resp, "backwards (also earlier data)")
        resp = client.post(url, {"action": "autopublish", "kind": "photometry", "dataset_id": self.phot.pk,
                                 "enabled": "1"}, follow=True)
        self.assertContains(resp, "Automatic publishing allowed")
        self.assertTrue(AutopublishOptIn.objects.filter(photometry=self.phot).exists())
        resp = client.post(url, {"action": "remove_group", "kind": "photometry", "dataset_id": self.phot.pk,
                                 "group_id": self.o.pk, "direction": "backward"}, follow=True)
        self.assertContains(resp, "including data taken earlier")
        self.assertFalse(R.objects.filter(photometry=self.phot, grantee_group=self.o).exists())

    def test_staff_get_no_opt_in_toggles(self):
        rows = access_pages.dashboard_datasets(self.staff)
        self.assertTrue(all(not r["can_opt_in"] for r in rows))
        rows = access_pages.dashboard_datasets(self.pi_user)
        self.assertTrue(rows and all(r["can_opt_in"] for r in rows))

    def test_api(self):
        client = Client()
        client.force_login(self.pi_user)
        resp = client.post(reverse("api-access-dataset-group-remove", args=["photometry", self.phot.pk]),
                           {"group": self.g.name}, content_type="application/json")
        self.assertEqual(resp.json()["kept"], 2)
        resp = client.post(reverse("api-access-dataset-autopublish", args=["spectrum", self.spec.pk]),
                           {"enabled": True}, content_type="application/json")
        self.assertTrue(resp.json()["opted_in"])
        self.assertTrue(access_rules.enabled())
