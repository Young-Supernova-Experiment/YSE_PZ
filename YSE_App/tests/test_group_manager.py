"""Team creation (throttled), owners adding members directly, leaving a team, and team
requests on the signup form (held until onboarding is done).

All users, groups, addresses and data here are synthetic.
"""

from __future__ import annotations

import datetime
import importlib

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import onboarding
from YSE_App.models import GroupMembershipRequest, GroupProfile, Notification
from YSE_App.services import groups as group_svc
from YSE_App.services import membership
from YSE_App.services import research_crypto
from YSE_App.tests.test_self_signup import GATE_ON, SIGNUP_ON, VALID_ORCID, signup_post

held_migration = importlib.import_module("YSE_App.migrations.0058_membership_request_held")

PROFILE_POST = {"full_name": "Synthetic Newcomer", "institution": "Example University", "department": "Physics",
                "career_level": "tenured", "orcid": VALID_ORCID}


def make_user(username, **extra):
    return User.objects.create_user(username, password="x", **extra)


class CacheClearMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


# ---------------------------------------------------------------------------
# creating teams


class TeamCreateTests(CacheClearMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("gm_creator")
        self.client.force_login(self.user)

    def test_any_user_creates_a_team_and_owns_it(self):
        resp = self.client.post(reverse("team_create"), {"name": "  gm  Team   One ", "description": "synthetic"})
        group = Group.objects.get(name="gm Team One")
        self.assertRedirects(resp, reverse("team_detail", args=[group.pk]), fetch_redirect_response=False)
        profile = GroupProfile.objects.get(group=group)
        self.assertEqual(list(profile.owners.all()), [self.user])
        self.assertEqual(profile.description, "synthetic")
        self.assertFalse(profile.is_system)
        self.assertTrue(self.user.groups.filter(pk=group.pk).exists())
        self.assertTrue(group_svc.can_manage(self.user, group))
        self.assertContains(self.client.get(reverse("team_list")), "gm Team One")

    def test_create_form_on_team_list(self):
        self.assertContains(self.client.get(reverse("team_list")), 'id="yse-team-create"')

    def test_names_must_be_new_plain_and_not_system(self):
        Group.objects.create(name="gm-existing")
        Group.objects.get_or_create(name="Public")
        for name in ("gm-EXISTING", "public", "api_writers", "x", "<script>", "-dash-first", "a" * 81):
            with self.assertRaises(group_svc.TeamError, msg=name):
                group_svc.create_team(self.user, name)
        self.assertEqual(GroupProfile.objects.filter(owners=self.user).count(), 0)

    @override_settings(GROUP_CREATES_PER_USER_PER_DAY=2, GROUP_CREATE_MAX_OWNED=0)
    def test_daily_limit_per_user(self):
        group_svc.create_team(self.user, "gm-daily-1")
        group_svc.create_team(self.user, "gm-daily-2")
        with self.assertRaises(group_svc.TeamError):
            group_svc.create_team(self.user, "gm-daily-3")
        self.client.post(reverse("team_create"), {"name": "gm-daily-4"})
        self.assertFalse(Group.objects.filter(name="gm-daily-4").exists())
        self.assertContains(self.client.get(reverse("team_list")), "too many teams today")

    @override_settings(GROUP_CREATES_PER_USER_PER_DAY=0, GROUP_CREATE_MAX_OWNED=2)
    def test_owned_team_cap_counts_active_teams(self):
        first = group_svc.create_team(self.user, "gm-owned-1")
        group_svc.create_team(self.user, "gm-owned-2")
        with self.assertRaises(group_svc.TeamError):
            group_svc.create_team(self.user, "gm-owned-3")
        GroupProfile.objects.filter(group=first).update(is_active=False)
        group_svc.create_team(self.user, "gm-owned-3")

    @override_settings(GROUP_CREATES_PER_HOUR=1, GROUP_CREATES_PER_USER_PER_DAY=0, GROUP_CREATE_MAX_OWNED=0)
    def test_site_wide_hourly_limit_applies_to_staff_too(self):
        staff = make_user("gm_staff", is_staff=True)
        group_svc.create_team(self.user, "gm-site-1")
        with self.assertRaises(group_svc.TeamError):
            group_svc.create_team(staff, "gm-site-2")

    @override_settings(GROUP_CREATES_PER_USER_PER_DAY=1, GROUP_CREATE_MAX_OWNED=1)
    def test_staff_skip_per_user_limits(self):
        staff = make_user("gm_staff", is_staff=True)
        group_svc.create_team(staff, "gm-staff-1")
        group_svc.create_team(staff, "gm-staff-2")

    @override_settings(GROUP_CREATE_ENABLED=False)
    def test_turned_off(self):
        self.assertEqual(self.client.post(reverse("team_create"), {"name": "gm-off"}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("team_list")), 'id="yse-team-create"')
        self.assertFalse(Group.objects.filter(name="gm-off").exists())

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse("team_create")).status_code, 405)

    @override_settings(**GATE_ON)
    def test_user_who_has_not_onboarded_cannot_create(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        resp = self.client.post(reverse("team_create"), {"name": "gm-gated"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Group.objects.filter(name="gm-gated").exists())


# ---------------------------------------------------------------------------
# adding members directly, leaving


class MemberAddTests(CacheClearMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("gm_owner")
        self.co_owner = make_user("gm_co_owner")
        self.target = make_user("gm_target", email="target@example.com")
        self.outsider = make_user("gm_outsider")
        self.team = group_svc.create_team(self.owner, "gm-add-team")
        group_svc.profile_for(self.team).owners.add(self.co_owner)
        self.url = reverse("team_member_add", args=[self.team.pk])

    def test_owner_adds_by_username_and_the_person_is_notified(self):
        self.client.force_login(self.owner)
        resp = self.client.post(self.url, {"identifier": "GM_TARGET"})
        self.assertRedirects(resp, reverse("team_detail", args=[self.team.pk]), fetch_redirect_response=False)
        self.assertTrue(self.target.groups.filter(pk=self.team.pk).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.target, kind="team_request").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.co_owner).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())

    def test_owner_adds_by_email(self):
        group_svc.add_member(self.owner, self.team, "Target@Example.com")
        self.assertTrue(self.target.groups.filter(pk=self.team.pk).exists())

    def test_shared_email_needs_the_username(self):
        make_user("gm_target_twin", email="target@example.com")
        with self.assertRaises(group_svc.TeamError):
            group_svc.add_member(self.owner, self.team, "target@example.com")

    def test_unknown_inactive_and_existing_members_are_refused(self):
        make_user("gm_inactive", is_active=False)
        for identifier in ("gm_nobody", "gm_inactive", "gm_owner"):
            with self.assertRaises(group_svc.TeamError, msg=identifier):
                group_svc.add_member(self.owner, self.team, identifier)

    def test_pending_request_is_marked_approved(self):
        req = GroupMembershipRequest.objects.create(user=self.target, group=self.team)
        group_svc.add_member(self.owner, self.team, "gm_target")
        req.refresh_from_db()
        self.assertEqual((req.status, req.decided_by), ("approved", self.owner))

    def test_non_owners_cannot_add(self):
        self.outsider.groups.add(self.team)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.url, {"identifier": "gm_target"}).status_code, 403)
        self.assertFalse(self.target.groups.filter(pk=self.team.pk).exists())

    def test_system_groups_cannot_be_added_to(self):
        writers, _ = Group.objects.get_or_create(name="api_writers")
        with self.assertRaises(PermissionError):
            group_svc.add_member(make_user("gm_staff", is_staff=True), writers, "gm_target")

    @override_settings(GROUP_ADDS_PER_OWNER_PER_DAY=1)
    def test_daily_add_limit(self):
        group_svc.add_member(self.owner, self.team, "gm_target")
        with self.assertRaises(group_svc.TeamError):
            group_svc.add_member(self.owner, self.team, "gm_outsider")

    @override_settings(**GATE_ON)
    def test_person_who_has_not_onboarded_needs_an_invitation(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        if onboarding.current_agreement_id() is None:
            self.skipTest("no current User Agreement in this database")
        with self.assertRaises(group_svc.TeamError):
            group_svc.add_member(self.owner, self.team, "gm_target")
        self.assertFalse(self.target.groups.filter(pk=self.team.pk).exists())

    def test_add_form_shown_to_owners_only(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("team_detail", args=[self.team.pk])), 'id="yse-team-member-add"')
        self.outsider.groups.add(self.team)
        self.client.force_login(self.outsider)
        page = self.client.get(reverse("team_detail", args=[self.team.pk]))
        self.assertNotContains(page, 'id="yse-team-member-add"')
        self.assertContains(page, 'id="yse-team-leave"')

    def test_member_leaves_and_owners_are_told(self):
        self.target.groups.add(self.team)
        self.client.force_login(self.target)
        resp = self.client.post(reverse("team_leave", args=[self.team.pk]))
        self.assertRedirects(resp, reverse("team_list"), fetch_redirect_response=False)
        self.assertFalse(self.target.groups.filter(pk=self.team.pk).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())

    def test_owner_cannot_leave(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("team_leave", args=[self.team.pk]))
        self.assertTrue(self.owner.groups.filter(pk=self.team.pk).exists())

    def test_public_cannot_be_left(self):
        public, _ = Group.objects.get_or_create(name="Public")
        self.target.groups.add(public)
        with self.assertRaises(group_svc.TeamError):
            group_svc.leave_team(self.target, public)


# ---------------------------------------------------------------------------
# team requests on the signup form


@override_settings(**SIGNUP_ON)
class SignupTeamRequestTests(CacheClearMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("gm_su_owner")
        self.team = group_svc.create_team(self.owner, "gm-su-alpha")
        self.team2 = group_svc.create_team(self.owner, "gm-su-beta")
        self.ownerless = Group.objects.create(name="gm-su-ownerless")
        self.writers, _ = Group.objects.get_or_create(name="api_writers")

    def test_form_offers_requestable_teams_only(self):
        page = self.client.get(reverse("signup"))
        self.assertContains(page, 'id="yse-signup-teams"')
        self.assertContains(page, "gm-su-alpha")
        self.assertNotContains(page, "gm-su-ownerless")
        self.assertNotContains(page, "api_writers")

    def test_no_team_section_without_requestable_teams(self):
        GroupProfile.objects.update(is_active=False)
        self.assertNotContains(self.client.get(reverse("signup")), 'id="yse-signup-teams"')

    def test_signup_without_a_team(self):
        signup_post(self.client)
        self.assertTrue(User.objects.filter(username="ss_newcomer").exists())
        self.assertFalse(GroupMembershipRequest.objects.exists())

    def test_signup_with_teams_opens_requests(self):
        # The test runner turns the gate off, so the requests are released at once.
        signup_post(self.client, teams=[self.team.pk, self.team2.pk], team_message="synthetic note")
        user = User.objects.get(username="ss_newcomer")
        reqs = GroupMembershipRequest.objects.filter(user=user).order_by("group__name")
        self.assertEqual([(r.group, r.status, r.message, bool(r.held)) for r in reqs],
                         [(self.team, "pending", "synthetic note", False),
                          (self.team2, "pending", "synthetic note", False)])
        self.assertEqual(Notification.objects.filter(recipient=self.owner, kind="team_request").count(), 2)
        self.assertFalse(user.groups.filter(pk=self.team.pk).exists())

    def test_unrequestable_choices_are_rejected(self):
        resp = signup_post(self.client, teams=[self.ownerless.pk])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="ss_newcomer").exists())

    @override_settings(GROUP_SIGNUP_MAX_REQUESTS=1)
    def test_at_most_n_teams(self):
        resp = signup_post(self.client, teams=[self.team.pk, self.team2.pk])
        self.assertContains(resp, "Choose at most 1 teams")
        self.assertFalse(User.objects.filter(username="ss_newcomer").exists())


@override_settings(**SIGNUP_ON, **GATE_ON)
class HeldSignupRequestTests(CacheClearMixin, TestCase):
    def setUp(self):
        super().setUp()
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        self.owner = make_user("gm_held_owner")
        self.team = group_svc.create_team(self.owner, "gm-held-team")

    def _agree(self):
        self.client.post(reverse("agreement"), {"agree": "on"})
        self.client.post(reverse("research_consent"), {"action": "decline"})

    def _signup(self):
        signup_post(self.client, teams=[self.team.pk])
        user = User.objects.get(username="ss_newcomer")
        return user, GroupMembershipRequest.objects.get(user=user)

    def test_held_until_onboarding_then_released_to_owners(self):
        user, req = self._signup()
        self.assertTrue(req.held)
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())
        self.assertEqual(list(membership.pending_for_group(self.team)), [])
        with self.assertRaises(membership.MembershipRequestError):
            membership.decide(self.owner, req, approve=True)
        self.assertFalse(user.groups.filter(pk=self.team.pk).exists())

        self._agree()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("researcher_profile_edit"), PROFILE_POST)
        req.refresh_from_db()
        self.assertFalse(req.held)
        self.assertTrue(Notification.objects.filter(recipient=self.owner, kind="team_request").exists())
        self.assertEqual(list(membership.pending_for_group(self.team)), [req])
        membership.decide(self.owner, req, approve=True)
        self.assertTrue(user.groups.filter(pk=self.team.pk).exists())

    def test_team_list_releases_once(self):
        user, req = self._signup()
        self._agree()
        self.client.post(reverse("researcher_profile_edit"), PROFILE_POST)  # on_commit not run in TestCase
        self.client.get(reverse("team_list"))
        self.client.get(reverse("team_list"))
        req.refresh_from_db()
        self.assertFalse(req.held)
        self.assertEqual(Notification.objects.filter(recipient=self.owner, kind="team_request").count(), 1)

    def test_never_released_requests_are_purged(self):
        _user, req = self._signup()
        GroupMembershipRequest.objects.filter(pk=req.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=200))
        fresh = GroupMembershipRequest.objects.create(user=self.owner, group=Group.objects.create(name="gm-other"),
                                                      held=True)
        self.assertEqual(membership.purge_requests(90), 1)
        self.assertFalse(GroupMembershipRequest.objects.filter(pk=req.pk).exists())
        self.assertTrue(GroupMembershipRequest.objects.filter(pk=fresh.pk).exists())


class HeldMigrationTests(TestCase):
    def test_column_is_nullable(self):
        """develop's code (no such column) INSERTs into the shared database."""
        field = GroupMembershipRequest._meta.get_field("held")
        self.assertTrue(field.null)
        op = held_migration.Migration.operations[0]
        self.assertTrue(op.field.null)
