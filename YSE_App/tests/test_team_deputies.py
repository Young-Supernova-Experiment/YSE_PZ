"""Team deputies, the per-team "accept membership requests" switch, and the paginated,
searchable member list on the team page.

All users, groups and data here are synthetic.
"""

from __future__ import annotations

import importlib

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from YSE_App.group_forms import GroupProfileForm
from YSE_App.models import GroupMembershipRequest, GroupProfile, Notification
from YSE_App.services import groups as group_svc
from YSE_App.services import membership

deputy_migration = importlib.import_module("YSE_App.migrations.0059_team_deputies_requests")


def make_user(username, **extra):
    return User.objects.create_user(username, password="x", **extra)


class TeamBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.team = Group.objects.create(name="td-team")
        self.owner = make_user("td_owner")
        self.deputy = make_user("td_deputy")
        self.member = make_user("td_member")
        self.outsider = make_user("td_outsider")
        for user in (self.owner, self.deputy, self.member):
            user.groups.add(self.team)
        profile = group_svc.profile_for(self.team)
        profile.owners.add(self.owner)
        profile.deputies.add(self.deputy)

    def detail(self, **params):
        return self.client.get(reverse("team_detail", args=[self.team.pk]), params)


# ---------------------------------------------------------------------------
# deputies


class DeputyPowerTests(TeamBase):
    def test_deputy_manages_like_an_owner(self):
        self.assertTrue(group_svc.can_manage(self.deputy, self.team))
        self.assertFalse(group_svc.can_appoint_deputies(self.deputy, self.team))
        self.client.force_login(self.deputy)
        page = self.detail()
        self.assertContains(page, 'id="yse-team-members"')
        self.assertContains(page, 'id="yse-team-member-add"')
        self.assertNotContains(page, "Make deputy")

    def test_deputy_approves_requests_and_is_notified_of_them(self):
        req = membership.create_request(self.outsider, self.team, "synthetic note")
        self.assertTrue(Notification.objects.filter(recipient=self.deputy, kind="team_request").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.owner, kind="team_request").exists())
        self.client.force_login(self.deputy)
        self.assertContains(self.detail(), 'id="yse-team-pending-requests"')
        self.client.post(reverse("team_request_decide", args=[self.team.pk, req.pk]), {"action": "approve"})
        self.assertTrue(self.outsider.groups.filter(pk=self.team.pk).exists())

    def test_deputy_adds_and_removes_plain_members(self):
        self.client.force_login(self.deputy)
        self.client.post(reverse("team_member_add", args=[self.team.pk]), {"identifier": "td_outsider"})
        self.assertTrue(self.outsider.groups.filter(pk=self.team.pk).exists())
        self.client.post(reverse("team_member_remove", args=[self.team.pk, self.member.pk]))
        self.assertFalse(self.member.groups.filter(pk=self.team.pk).exists())

    def test_deputy_cannot_appoint_or_remove_deputies(self):
        other = make_user("td_other_deputy")
        other.groups.add(self.team)
        group_svc.profile_for(self.team).deputies.add(other)
        self.client.force_login(self.deputy)
        resp = self.client.post(reverse("team_deputy", args=[self.team.pk, self.member.pk]), {"action": "appoint"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(group_svc.is_deputy(self.member, self.team))
        self.client.post(reverse("team_member_remove", args=[self.team.pk, other.pk]))
        self.assertTrue(other.groups.filter(pk=self.team.pk).exists())
        with self.assertRaises(group_svc.TeamError):
            group_svc.remove_member(self.deputy, self.team, other)

    def test_deputy_cannot_remove_an_owner(self):
        with self.assertRaises(group_svc.InvitationError):
            group_svc.remove_member(self.deputy, self.team, self.owner)

    def test_deputy_of_one_team_cannot_manage_another(self):
        other = Group.objects.create(name="td-other-team")
        self.deputy.groups.add(other)
        self.assertFalse(group_svc.can_manage(self.deputy, other))

    def test_team_list_shows_the_deputy_role(self):
        self.client.force_login(self.deputy)
        self.assertContains(self.client.get(reverse("team_list")), "Deputy")


class AppointDeputyTests(TeamBase):
    def test_owner_appoints_and_removes_a_deputy(self):
        self.client.force_login(self.owner)
        url = reverse("team_deputy", args=[self.team.pk, self.member.pk])
        resp = self.client.post(url, {"action": "appoint", "page": "2", "q": "td"})
        self.assertRedirects(resp, reverse("team_detail", args=[self.team.pk]) + "?page=2&q=td",
                             fetch_redirect_response=False)
        self.assertTrue(group_svc.is_deputy(self.member, self.team))
        self.assertTrue(group_svc.can_manage(self.member, self.team))
        self.assertTrue(Notification.objects.filter(recipient=self.member, kind="team_request").exists())
        self.client.post(url, {"action": "remove"})
        self.assertFalse(group_svc.is_deputy(self.member, self.team))
        self.assertFalse(group_svc.can_manage(self.member, self.team))

    def test_staff_appoint_deputies(self):
        staff = make_user("td_staff", is_staff=True)
        group_svc.set_deputy(staff, self.team, self.member, appoint=True)
        self.assertTrue(group_svc.is_deputy(self.member, self.team))

    def test_only_members_who_are_not_owners_can_be_deputies(self):
        with self.assertRaises(group_svc.TeamError):
            group_svc.set_deputy(self.owner, self.team, self.owner, appoint=True)
        with self.assertRaises(group_svc.TeamError):
            group_svc.set_deputy(self.owner, self.team, self.outsider, appoint=True)
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("team_deputy", args=[self.team.pk, self.outsider.pk]), {"action": "appoint"})
        self.assertEqual(resp.status_code, 404)

    def test_bad_action_is_rejected(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("team_deputy", args=[self.team.pk, self.member.pk]), {"action": "owner"})
        self.assertEqual(resp.status_code, 400)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("team_deputy", args=[self.team.pk, self.member.pk])).status_code, 405)

    def test_leaving_or_removal_ends_the_role(self):
        self.client.force_login(self.deputy)
        self.client.post(reverse("team_leave", args=[self.team.pk]))
        self.assertFalse(GroupProfile.objects.filter(group=self.team, deputies=self.deputy).exists())
        group_svc.set_deputy(self.owner, self.team, self.member, appoint=True)
        group_svc.remove_member(self.owner, self.team, self.member)
        self.assertFalse(GroupProfile.objects.filter(group=self.team, deputies=self.member).exists())

    def test_removal_elsewhere_ends_management(self):
        self.deputy.groups.remove(self.team)  # e.g. in the Django admin
        self.assertFalse(group_svc.can_manage(self.deputy, self.team))

    def test_system_groups_have_no_deputies(self):
        writers, _ = Group.objects.get_or_create(name="api_writers")
        staff = make_user("td_staff", is_staff=True)
        with self.assertRaises(PermissionError):
            group_svc.set_deputy(staff, writers, self.member, appoint=True)
        from YSE_App.admin import GroupProfileAdminForm
        form = GroupProfileAdminForm(
            data={"description": "", "is_active": "on", "is_system": "on", "deputies": [self.member.pk]},
            instance=group_svc.profile_for(writers))
        self.assertFalse(form.is_valid())


# ---------------------------------------------------------------------------
# accepting requests


@override_settings(SELF_SIGNUP_ENABLED=False)
class AcceptRequestsTests(TeamBase):
    def test_requests_work_without_self_signup(self):
        self.client.force_login(self.outsider)
        page = self.client.get(reverse("team_list"))
        self.assertContains(page, 'id="yse-team-requests"')
        self.client.post(reverse("team_request_create"), {"group": self.team.pk})
        self.assertTrue(GroupMembershipRequest.objects.filter(user=self.outsider, group=self.team).exists())

    def test_owner_or_deputy_switches_requests_off_and_on(self):
        self.client.force_login(self.deputy)
        self.assertContains(self.detail(), 'id="yse-team-accepts-requests"')
        url = reverse("team_update", args=[self.team.pk])
        self.client.post(url, {"description": "", "is_active": "on"})  # box unticked
        self.assertIs(GroupProfile.objects.get(group=self.team).accepts_requests, False)
        self.assertNotIn(self.team, membership.requestable_groups(self.outsider))
        with self.assertRaises(membership.MembershipRequestError):
            membership.create_request(self.outsider, self.team)
        self.client.post(url, {"description": "", "is_active": "on", "accepts_requests": "on"})
        self.assertIs(GroupProfile.objects.get(group=self.team).accepts_requests, True)
        self.assertIn(self.team, membership.requestable_groups(self.outsider))

    def test_unset_means_on_and_the_form_shows_it_ticked(self):
        profile = GroupProfile.objects.get(group=self.team)
        self.assertIsNone(profile.accepts_requests)
        self.assertTrue(GroupProfileForm(instance=profile).initial["accepts_requests"])

    def test_a_team_with_only_a_deputy_takes_requests(self):
        group_svc.profile_for(self.team).owners.clear()
        self.assertIn(self.team, membership.requestable_groups(self.outsider))
        group_svc.profile_for(self.team).deputies.clear()
        self.assertNotIn(self.team, membership.requestable_groups(self.outsider))

    def test_members_cannot_switch_it(self):
        self.client.force_login(self.member)
        resp = self.client.post(reverse("team_update", args=[self.team.pk]), {"description": "", "is_active": "on"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(GroupProfile.objects.get(group=self.team).accepts_requests)

    @override_settings(GROUP_REQUESTS_ENABLED=False)
    def test_site_switch_closes_requests(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(reverse("team_request_create"), {"group": self.team.pk}).status_code, 404)


# ---------------------------------------------------------------------------
# member list


@override_settings(GROUP_MEMBERS_PER_PAGE=50)
class MemberListTests(TeamBase):
    def setUp(self):
        super().setUp()
        User.objects.bulk_create([User(username="td_bulk_%03d" % i) for i in range(117)])
        # MySQL's bulk_create returns no primary keys: read the rows back.
        self.team.user_set.add(*User.objects.filter(username__startswith="td_bulk_"))  # 120 members in all
        self.client.force_login(self.owner)

    def test_paginated(self):
        page = self.detail()
        self.assertEqual(len(page.context["members"].rows), 50)
        self.assertEqual(page.context["members"].paginator.count, 120)
        self.assertContains(page, "Page 1 of 3")
        last = self.detail(page=3)
        self.assertEqual(len(last.context["members"].rows), 20)
        self.assertEqual(len(self.detail(page=99).context["members"].rows), 20)  # out of range: last page
        self.assertEqual(len(self.detail(page="x").context["members"].rows), 50)

    def test_search_by_username_or_name(self):
        self.member.first_name = "Zephyrine"
        self.member.save()
        page = self.detail(q="zephyr")
        self.assertEqual([r["user"] for r in page.context["members"].rows], [self.member])
        self.assertEqual(page.context["members"].paginator.count, 1)
        self.assertEqual(self.detail(q="td_bulk_00").context["members"].paginator.count, 10)
        self.assertContains(self.detail(q="nobody-here"), "No members match.")

    def test_roles_shown(self):
        for username, expected in (("td_owner", (True, False)), ("td_deputy", (False, True)),
                                   ("td_member", (False, False))):
            rows = self.detail(q=username).context["members"].rows
            self.assertEqual([(r["is_owner"], r["is_deputy"]) for r in rows], [expected], username)

    def test_remove_keeps_page_and_search(self):
        resp = self.client.post(reverse("team_member_remove", args=[self.team.pk, self.member.pk]),
                                {"page": "2", "q": "td"})
        self.assertRedirects(resp, reverse("team_detail", args=[self.team.pk]) + "?page=2&q=td",
                             fetch_redirect_response=False)

    def test_members_do_not_see_the_list(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.detail(), 'id="yse-team-members"')


class MigrationTests(TestCase):
    def test_new_column_is_nullable(self):
        """develop's code (no such column) INSERTs into the shared database."""
        self.assertTrue(GroupProfile._meta.get_field("accepts_requests").null)
        op = next(o for o in deputy_migration.Migration.operations if o.name == "accepts_requests")
        self.assertTrue(op.field.null)
