"""Group profiles, invitation links, system groups and user preferences (#445, #440).

All users, groups and data here are synthetic.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
from urllib.parse import parse_qs, urlsplit

from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import onboarding
from YSE_App.forms import TransientCommentForm, TransientFollowupForm
from YSE_App.models import GroupInvitation, GroupInvitationUse, GroupProfile, Log, UserPreference
from YSE_App.services import audience
from YSE_App.services import data_access
from YSE_App.services import groups as group_svc
from YSE_App.services import interests
from YSE_App.services import research_crypto
from YSE_App.services.comments import create_transient_comment
from YSE_App.services.visibility import filter_transient_comments_for_user, object_visible_to_user
from YSE_App.tests.fixtures_minimal import attach_synthetic_photometry, create_minimal_transient

seed_migration = importlib.import_module("YSE_App.migrations.0034_seed_group_profiles")

GATE_ON = dict(ONBOARDING_GATE_ENABLED=True, ONBOARDING_EMAIL_REQUIRED=False, RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")
VALID_ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (fictitious researcher)


def make_user(username, **extra):
    return User.objects.create_user(username, password="x", **extra)


class GroupTestBase(TestCase):
    def setUp(self):
        self.team = Group.objects.create(name="gp-team-alpha")
        self.other_team = Group.objects.create(name="gp-team-beta")
        self.owner = make_user("gp_owner")
        self.member = make_user("gp_member")
        self.outsider = make_user("gp_outsider")
        self.staff = make_user("gp_staff", is_staff=True)
        self.owner.groups.add(self.team)
        self.member.groups.add(self.team)
        group_svc.profile_for(self.team).owners.add(self.owner)


class ProfileCreationTests(GroupTestBase):
    def test_new_group_gets_an_active_profile_without_owners(self):
        group = Group.objects.create(name="gp-team-new")
        profile = GroupProfile.objects.get(group=group)
        self.assertTrue(profile.is_active)
        self.assertFalse(profile.is_system)
        self.assertEqual(profile.owners.count(), 0)

    def test_named_system_groups_are_marked_on_create(self):
        for name in ("api_writers", "explorer_users", "Public"):
            group, _ = Group.objects.get_or_create(name=name)
            self.assertTrue(group_svc.profile_for(group).is_system, name)
            self.assertTrue(group_svc.is_system_group(group), name)

    @override_settings(YSE_SYSTEM_GROUP_NAMES=("gp-custom-system",))
    def test_system_group_list_is_a_setting(self):
        group = Group.objects.create(name="gp-custom-system")
        self.assertTrue(group_svc.is_system_group(group))

    def test_seed_migration_creates_profiles_without_owners_and_marks_system_groups(self):
        writers, _ = Group.objects.get_or_create(name="api_writers")
        perm_group = Group.objects.create(name="gp-permission-group")
        perm_group.permissions.add(Permission.objects.filter(codename="add_transient").first())
        GroupProfile.objects.filter(group__in=[writers, perm_group, self.other_team]).delete()

        seed_migration.seed(django_apps, None)
        seed_migration.seed(django_apps, None)  # idempotent (shared YSE_test database)

        self.assertTrue(GroupProfile.objects.get(group=writers).is_system)
        self.assertTrue(GroupProfile.objects.get(group=perm_group).is_system)
        plain = GroupProfile.objects.get(group=self.other_team)
        self.assertFalse(plain.is_system)
        self.assertTrue(plain.is_active)
        self.assertEqual(GroupProfile.objects.filter(group=self.other_team).count(), 1)
        self.assertEqual(GroupProfile.owners.through.objects.exclude(groupprofile__group=self.team).count(), 0)

    def test_ensure_command_fills_gaps(self):
        GroupProfile.objects.filter(group=self.other_team).delete()
        created, _marked = group_svc.ensure_all_profiles()
        self.assertGreaterEqual(created, 1)
        self.assertTrue(GroupProfile.objects.filter(group=self.other_team).exists())


class InvitationTokenTests(GroupTestBase):
    def test_token_is_random_and_only_its_hash_is_stored(self):
        inv, token = group_svc.create_invitation(self.owner, self.team)
        inv2, token2 = group_svc.create_invitation(self.owner, self.team)
        self.assertNotEqual(token, token2)
        self.assertGreaterEqual(len(token), 40)
        inv.refresh_from_db()
        self.assertEqual(inv.token_hash, hashlib.sha256(token.encode()).hexdigest())
        for field in GroupInvitation._meta.concrete_fields:
            self.assertNotEqual(str(getattr(inv, field.attname)), token, field.name)
        self.assertIsNone(group_svc.find_invitation(inv.token_hash))  # the hash is not a token
        self.assertEqual(group_svc.find_invitation(token), inv)

    def test_default_is_single_use_with_an_expiry(self):
        inv, token = group_svc.create_invitation(self.owner, self.team)
        self.assertEqual(inv.max_uses, 1)
        self.assertAlmostEqual((inv.expires_at - timezone.now()).days, 13, delta=1)
        first, second = make_user("gp_new_1"), make_user("gp_new_2")
        group, joined = group_svc.accept_invitation(first, group_svc.hash_token(token))
        self.assertTrue(joined)
        self.assertTrue(first.groups.filter(pk=self.team.pk).exists())
        with self.assertRaises(group_svc.InvitationError):
            group_svc.accept_invitation(second, group_svc.hash_token(token))
        self.assertFalse(second.groups.filter(pk=self.team.pk).exists())

    def test_max_uses(self):
        _inv, token = group_svc.create_invitation(self.owner, self.team, max_uses=2)
        users = [make_user("gp_multi_%d" % i) for i in range(3)]
        group_svc.accept_invitation(users[0], group_svc.hash_token(token))
        group_svc.accept_invitation(users[1], group_svc.hash_token(token))
        with self.assertRaises(group_svc.InvitationError):
            group_svc.accept_invitation(users[2], group_svc.hash_token(token))
        self.assertEqual(GroupInvitationUse.objects.count(), 2)

    def test_expired_and_revoked_links_are_refused(self):
        inv, token = group_svc.create_invitation(self.owner, self.team, max_uses=5)
        inv.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        inv.save()
        with self.assertRaises(group_svc.InvitationError):
            group_svc.accept_invitation(self.outsider, group_svc.hash_token(token))
        inv2, token2 = group_svc.create_invitation(self.owner, self.team, max_uses=5)
        group_svc.revoke_invitation(self.owner, inv2)
        with self.assertRaises(group_svc.InvitationError):
            group_svc.accept_invitation(self.outsider, group_svc.hash_token(token2))
        self.assertFalse(self.outsider.groups.filter(pk=self.team.pk).exists())

    def test_a_user_cannot_reuse_a_link_after_leaving(self):
        _inv, token = group_svc.create_invitation(self.owner, self.team, max_uses=5)
        group_svc.accept_invitation(self.outsider, group_svc.hash_token(token))
        self.outsider.groups.remove(self.team)
        with self.assertRaises(group_svc.InvitationError):
            group_svc.accept_invitation(self.outsider, group_svc.hash_token(token))

    def test_existing_member_uses_no_slot(self):
        inv, token = group_svc.create_invitation(self.owner, self.team)
        _group, joined = group_svc.accept_invitation(self.member, group_svc.hash_token(token))
        self.assertFalse(joined)
        self.assertEqual(inv.uses.count(), 0)

    def test_expiry_and_use_limits_are_bounded(self):
        with self.assertRaises(group_svc.InvitationError):
            group_svc.create_invitation(self.owner, self.team, days=10000)
        with self.assertRaises(group_svc.InvitationError):
            group_svc.create_invitation(self.owner, self.team, max_uses=0)

    def test_purge_keeps_open_links(self):
        open_inv, _ = group_svc.create_invitation(self.owner, self.team)
        old, _ = group_svc.create_invitation(self.owner, self.team)
        GroupInvitation.objects.filter(pk=old.pk).update(expires_at=timezone.now() - datetime.timedelta(days=200))
        self.assertEqual(group_svc.purge_invitations(90, dry_run=True), 1)
        self.assertEqual(group_svc.purge_invitations(90), 1)
        self.assertTrue(GroupInvitation.objects.filter(pk=open_inv.pk).exists())
        self.assertFalse(GroupInvitation.objects.filter(pk=old.pk).exists())


class OwnerPermissionTests(GroupTestBase):
    def test_only_owners_and_staff_create_invitations(self):
        with self.assertRaises(PermissionError):
            group_svc.create_invitation(self.member, self.team)
        with self.assertRaises(PermissionError):
            group_svc.create_invitation(self.outsider, self.team)
        group_svc.create_invitation(self.staff, self.team)
        group_svc.create_invitation(self.owner, self.team)

    def test_owner_sees_members_and_invites_on_the_team_page(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("team_detail", args=[self.team.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "gp_member")
        resp = self.client.post(reverse("team_invite_create", args=[self.team.pk]), {"days": 7, "max_uses": 1})
        self.assertRedirects(resp, reverse("team_detail", args=[self.team.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse("team_detail", args=[self.team.pk]))
        link = page.context["new_link"]
        self.assertIn("/teams/join/", link)
        token = link.rstrip("/").rsplit("/", 1)[-1]
        self.assertEqual(GroupInvitation.objects.get().token_hash, group_svc.hash_token(token))
        self.assertEqual(page["Referrer-Policy"], "no-referrer")
        # Shown once only.
        self.assertIsNone(self.client.get(reverse("team_detail", args=[self.team.pk])).context["new_link"])

    def test_owner_removes_a_member_but_not_an_owner(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("team_member_remove", args=[self.team.pk, self.member.pk]))
        self.assertFalse(self.member.groups.filter(pk=self.team.pk).exists())
        self.client.post(reverse("team_member_remove", args=[self.team.pk, self.owner.pk]))
        self.assertTrue(self.owner.groups.filter(pk=self.team.pk).exists())

    def test_member_cannot_manage_and_outsider_cannot_see(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("team_detail", args=[self.team.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Create link")
        self.assertEqual(self.client.post(reverse("team_invite_create", args=[self.team.pk]),
                                          {"days": 7, "max_uses": 1}).status_code, 403)
        self.assertEqual(self.client.post(reverse("team_member_remove", args=[self.team.pk, self.owner.pk])).status_code,
                         403)
        self.assertFalse(GroupInvitation.objects.exists())
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("team_detail", args=[self.team.pk])).status_code, 404)

    def test_owner_of_one_team_cannot_manage_another(self):
        self.owner.groups.add(self.other_team)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("team_invite_create", args=[self.other_team.pk]),
                                          {"days": 7, "max_uses": 1}).status_code, 403)

    def test_management_actions_are_post_only(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("team_invite_create", args=[self.team.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("team_member_remove", args=[self.team.pk, self.member.pk])).status_code,
                         405)

    def test_owner_can_mark_the_team_inactive(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("team_update", args=[self.team.pk]), {"description": "Synthetic team"})
        profile = GroupProfile.objects.get(group=self.team)
        self.assertFalse(profile.is_active)
        self.assertEqual(profile.description, "Synthetic team")

    def test_invalid_or_missing_invitation_shows_a_message(self):
        self.client.force_login(self.outsider)
        resp = self.client.get(reverse("group_invite_link", args=["x" * 43]))
        self.assertRedirects(resp, reverse("group_invite_accept"), fetch_redirect_response=False)
        page = self.client.get(reverse("group_invite_accept"))
        self.assertContains(page, "not valid")
        self.client.post(reverse("group_invite_accept"))
        self.assertFalse(self.outsider.groups.filter(pk=self.team.pk).exists())

    def test_logged_in_user_joins_through_the_link(self):
        _inv, token = group_svc.create_invitation(self.owner, self.team)
        self.client.force_login(self.outsider)
        self.client.get(reverse("group_invite_link", args=[token]))
        page = self.client.get(reverse("group_invite_accept"))
        self.assertContains(page, "Join gp-team-alpha")
        self.assertFalse(self.outsider.groups.filter(pk=self.team.pk).exists())
        self.client.post(reverse("group_invite_accept"))
        self.assertTrue(self.outsider.groups.filter(pk=self.team.pk).exists())

    def test_team_list(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("team_list"))
        self.assertContains(resp, "gp-team-alpha")
        self.assertNotContains(resp, "gp-team-beta")
        self.assertNotContains(resp, ">Public<")


class SystemGroupTests(GroupTestBase):
    def setUp(self):
        super().setUp()
        self.writers, _ = Group.objects.get_or_create(name="api_writers")
        self.public, _ = Group.objects.get_or_create(name="Public")
        self.staff.groups.add(self.writers)

    def test_no_invitations_even_for_staff(self):
        for group in (self.writers, self.public):
            with self.assertRaises(group_svc.InvitationError):
                group_svc.create_invitation(self.staff, group)
            self.assertFalse(group_svc.can_manage(self.staff, group))

    def test_a_link_cannot_be_used_once_its_group_becomes_system(self):
        _inv, token = group_svc.create_invitation(self.owner, self.team)
        GroupProfile.objects.filter(group=self.team).update(is_system=True)
        with self.assertRaises(group_svc.InvitationError):
            group_svc.accept_invitation(self.outsider, group_svc.hash_token(token))

    def test_system_groups_have_no_team_page(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("team_detail", args=[self.writers.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("team_invite_create", args=[self.writers.pk]),
                                          {"days": 7, "max_uses": 1}).status_code, 404)

    def test_admin_refuses_owners_on_system_groups(self):
        from YSE_App.admin import GroupProfileAdminForm
        profile = group_svc.profile_for(self.writers)
        form = GroupProfileAdminForm(
            data={"description": "", "is_active": "on", "is_system": "on", "owners": [self.owner.pk]},
            instance=profile)
        self.assertFalse(form.is_valid())
        ok = GroupProfileAdminForm(
            data={"description": "", "is_active": "on", "owners": [self.owner.pk]},
            instance=group_svc.profile_for(self.other_team))
        self.assertTrue(ok.is_valid(), ok.errors)

    def test_permission_groups_and_api_writers_are_not_requestable_targets(self):
        self.member.groups.add(self.writers)
        self.assertNotIn(self.writers, data_access.requestable_groups(self.member))
        self.assertNotIn(self.writers, data_access.eligible_target_groups(self.member))
        self.assertIn(self.team, data_access.requestable_groups(self.member))


class PickerTests(GroupTestBase):
    def setUp(self):
        super().setUp()
        self.writers, _ = Group.objects.get_or_create(name="api_writers")
        self.public, _ = Group.objects.get_or_create(name="Public")
        self.old_team = Group.objects.create(name="gp-team-old")
        self.member.groups.add(self.writers, self.public, self.old_team, self.other_team)
        GroupProfile.objects.filter(group=self.old_team).update(is_active=False)
        self.transient = create_minimal_transient(self.staff, name="2026gpick")

    def _comment_choice_names(self, user):
        form = TransientCommentForm(user=user, transient_id=self.transient.id)
        return [label for _value, label in form.fields["audience_groups"].choices]

    def test_inactive_and_system_groups_hidden_from_comment_picker(self):
        names = self._comment_choice_names(self.member)
        self.assertIn("gp-team-alpha", names)
        self.assertIn("Public", names)  # kept: YSE_PICKER_VISIBLE_SYSTEM_GROUPS
        self.assertNotIn("api_writers", names)
        self.assertNotIn("gp-team-old", names)

    def test_followup_picker_folds_inactive_groups_and_drops_system_ones(self):
        choices = audience.build_followup_audience_choices(self.member, None)
        by_name = {c["group"].name: c for c in choices}
        self.assertNotIn("api_writers", by_name)
        self.assertTrue(by_name["gp-team-old"]["hidden"])
        self.assertFalse(by_name["gp-team-old"]["checked"])
        self.assertTrue(by_name["gp-team-alpha"]["checked"])
        self.assertFalse(by_name["gp-team-alpha"]["hidden"])
        form = TransientFollowupForm(user=self.member, transient_id=self.transient.id)
        self.assertIn(self.old_team.pk, [c["group"].pk for c in form.followup_audience_more])
        self.assertNotIn(self.old_team.pk, form.fields["audience_groups"].initial)

    def test_interest_and_access_pickers(self):
        names = [g.name for g in interests.selectable_groups(self.member)]
        self.assertNotIn("gp-team-old", names)
        self.assertNotIn("api_writers", names)
        targets = [g.name for g in data_access.requestable_groups(self.member)]
        self.assertNotIn("gp-team-old", targets)
        # Still a valid target when chosen explicitly: the picker only narrows what is offered.
        self.assertIn(self.old_team, data_access.eligible_target_groups(self.member))

    def test_picker_never_empty_because_of_inactive_flags(self):
        loner = make_user("gp_loner")
        loner.groups.add(self.old_team)
        self.assertIn(self.old_team, group_svc.picker_groups(loner, loner.groups.exclude(name="Public")))

    def test_inactive_group_keeps_data_and_access(self):
        phot = attach_synthetic_photometry(self.staff, self.transient, n_points=2)
        phot.groups.add(self.old_team)
        log = create_transient_comment(transient=self.transient, comment="synthetic old-team note",
                                       user=self.member, notify=False, is_public=False,
                                       audience_groups=[self.old_team])
        viewer = make_user("gp_old_viewer")
        viewer.groups.add(self.old_team)
        GroupProfile.objects.filter(group=self.old_team).update(is_active=True)
        before = (object_visible_to_user(viewer, phot),
                  filter_transient_comments_for_user(Log.objects.filter(pk=log.pk), viewer).exists())
        GroupProfile.objects.filter(group=self.old_team).update(is_active=False)
        after = (object_visible_to_user(viewer, phot),
                 filter_transient_comments_for_user(Log.objects.filter(pk=log.pk), viewer).exists())
        self.assertEqual(before, (True, True))
        self.assertEqual(after, before)
        self.assertTrue(viewer.groups.filter(pk=self.old_team.pk).exists())
        self.assertEqual(list(Log.objects.get(pk=log.pk).groups.all()), [self.old_team])


class UserPreferenceTests(GroupTestBase):
    def setUp(self):
        super().setUp()
        self.member.groups.add(self.other_team)
        self.transient = create_minimal_transient(self.staff, name="2026gpref")

    def test_no_preference_means_everything_checked_as_before(self):
        self.assertIsNone(group_svc.preferences_for(self.member))
        self.assertFalse(UserPreference.objects.filter(user=self.member).exists())
        form = TransientCommentForm(user=self.member, transient_id=self.transient.id)
        offered = {value for value, _label in form.fields["audience_groups"].choices}
        self.assertEqual(set(form.fields["audience_groups"].initial), offered)
        self.assertFalse(form.initial_is_public)
        choices = audience.build_followup_audience_choices(self.member, None)
        self.assertTrue(all(c["checked"] for c in choices if c["enabled"]))

    def test_saved_defaults_preselect_and_narrow_the_server_default(self):
        pref = UserPreference.objects.create(user=self.member, comment_default_public=True)
        pref.comment_default_groups.add(self.team)
        pref.followup_default_groups.add(self.other_team)
        pref.hidden_groups.add(Group.objects.get(name="Public"))
        group_svc.forget_preferences(self.member)

        form = TransientCommentForm(user=self.member, transient_id=self.transient.id)
        self.assertEqual(form.fields["audience_groups"].initial, [self.team.pk])
        self.assertTrue(form.initial_is_public)
        labels = [label for _v, label in form.fields["audience_groups"].choices]
        self.assertIn("Public", labels)  # Public cannot be hidden (#440 follow-up)

        is_public, groups = audience.resolve_comment_audience(self.member, self.transient.id, is_public=False)
        self.assertFalse(is_public)
        self.assertEqual(groups, [self.team])

        checked = [c["group"] for c in audience.build_followup_audience_choices(self.member, None) if c["checked"]]
        self.assertEqual(checked, [self.other_team])

    def test_a_default_for_a_group_the_user_left_is_ignored(self):
        pref = UserPreference.objects.create(user=self.member)
        pref.comment_default_groups.add(self.other_team)
        self.member.groups.remove(self.other_team)
        group_svc.forget_preferences(self.member)
        form = TransientCommentForm(user=self.member, transient_id=self.transient.id)
        offered = {value for value, _label in form.fields["audience_groups"].choices}
        self.assertEqual(set(form.fields["audience_groups"].initial), offered)
        self.assertNotIn(self.other_team.pk, offered)

    def test_settings_page_saves_preferences(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("user_settings"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "gp-team-alpha")
        self.assertNotContains(resp, "api_writers")
        resp = self.client.post(reverse("user_settings"), {
            "hidden_groups": [self.other_team.pk], "comment_default_groups": [self.team.pk],
            "followup_default_groups": [], "comment_default_public": "on",
        })
        self.assertRedirects(resp, reverse("user_settings"), fetch_redirect_response=False)
        pref = UserPreference.objects.get(user=self.member)
        self.assertEqual(list(pref.hidden_groups.all()), [self.other_team])
        self.assertEqual(list(pref.comment_default_groups.all()), [self.team])
        self.assertTrue(pref.comment_default_public)

    def test_settings_page_refuses_groups_the_user_is_not_in(self):
        self.client.force_login(self.outsider)
        resp = self.client.post(reverse("user_settings"), {"comment_default_groups": [self.team.pk]})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(UserPreference.objects.filter(user=self.outsider).exists())


@override_settings(**GATE_ON)
class InviteGateFlowTests(GroupTestBase):
    """A brand-new user: login -> agreement -> profile -> confirm -> member."""

    def setUp(self):
        super().setUp()
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        _inv, self.token = group_svc.create_invitation(self.owner, self.team)
        self.link = reverse("group_invite_link", args=[self.token])
        self.newcomer = make_user("gp_newcomer")

    def test_signed_out_visitor_goes_to_login_and_back_to_the_link(self):
        resp = self.client.get(self.link)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(resp["Location"]).query).get("next"), [self.link])
        self.assertFalse(GroupInvitationUse.objects.exists())

    def test_new_user_accepts_agreement_and_profile_before_joining(self):
        self.client.force_login(self.newcomer)
        resp = self.client.get(self.link)
        self.assertRedirects(resp, reverse("group_invite_accept"), fetch_redirect_response=False)

        resp = self.client.get(reverse("group_invite_accept"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("agreement")))
        self.assertNotIn(self.token, resp["Location"])  # the token never travels in ?next=
        self.assertFalse(self.newcomer.groups.filter(pk=self.team.pk).exists())

        resp = self.client.post(reverse("agreement"), {"agree": "on", "next": reverse("group_invite_accept")})
        self.assertTrue(resp["Location"].startswith(reverse("research_consent")))  # offered once (#744)
        resp = self.client.post(reverse("research_consent"), {"action": "decline",
                                                              "next": reverse("group_invite_accept")})
        # Declining goes on to the incomplete profile (#766), carrying the invite page as ``next``.
        self.assertTrue(resp["Location"].startswith(reverse("researcher_profile_edit")))
        resp = self.client.get(reverse("group_invite_accept"))
        self.assertTrue(resp["Location"].startswith(reverse("researcher_profile_edit")))

        resp = self.client.post(reverse("researcher_profile_edit"), {
            "first_name": "Synthetic", "last_name": "Person", "full_name": "Synthetic Newcomer", "institution": "Example University", "department": "Physics",
            "career_level": "tenured", "orcid": VALID_ORCID, "next": reverse("group_invite_accept"),
        })
        self.assertRedirects(resp, reverse("group_invite_accept"), fetch_redirect_response=False)

        page = self.client.get(reverse("group_invite_accept"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "gp-team-alpha")
        self.assertFalse(self.newcomer.groups.filter(pk=self.team.pk).exists())

        resp = self.client.post(reverse("group_invite_accept"))
        self.assertRedirects(resp, reverse("team_detail", args=[self.team.pk]), fetch_redirect_response=False)
        self.assertTrue(self.newcomer.groups.filter(pk=self.team.pk).exists())
        self.assertEqual(GroupInvitationUse.objects.get().used_by, self.newcomer)

    def test_post_before_onboarding_does_not_join(self):
        """The gate leaves POSTs alone, so the accept view must not be reachable un-onboarded via the gate."""
        self.client.force_login(self.newcomer)
        self.client.get(self.link)
        # A POST is not redirected by the gate; the view itself must still refuse.
        resp = self.client.post(reverse("group_invite_accept"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("agreement")))
        self.assertFalse(self.newcomer.groups.filter(pk=self.team.pk).exists())
