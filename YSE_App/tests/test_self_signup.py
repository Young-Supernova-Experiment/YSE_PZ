"""Self-service signup, email verification and team membership requests (#448);
Public always offered in pickers (#440 follow-up); PI contact fields (#643).

All users, groups, addresses and data here are synthetic.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core import mail, signing
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from YSE_App import onboarding
from YSE_App.forms import TransientCommentForm
from YSE_App.group_forms import UserPreferenceForm
from YSE_App.models import GroupMembershipRequest, GroupProfile, Notification, PrincipalInvestigator, UserPreference
from YSE_App.services import audience
from YSE_App.services import groups as group_svc
from YSE_App.services import membership
from YSE_App.services import research_crypto
from YSE_App.signup_forms import _TS_SALT
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient

STRONG_PASSWORD = "synthetic-Passphrase-9431"
VALID_ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (fictitious researcher)

SIGNUP_ON = dict(
    SELF_SIGNUP_ENABLED=True, SELF_SIGNUP_EMAIL_VERIFICATION=True, SELF_SIGNUP_MIN_FILL_SECONDS=0,
    SELF_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR=10, SELF_SIGNUP_ACCOUNTS_PER_HOUR=30,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
GATE_ON = dict(ONBOARDING_GATE_ENABLED=True, RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")


def make_user(username, **extra):
    return User.objects.create_user(username, password="x", **extra)


def _old_ts(seconds=120):
    return signing.dumps(int(time.time()) - seconds, salt=_TS_SALT)


def signup_post(client, username="ss_newcomer", email="newcomer@example.com", **extra):
    data = {"username": username, "email": email, "password1": STRONG_PASSWORD, "password2": STRONG_PASSWORD,
            "website": "", "form_ts": _old_ts()}
    data.update(extra)
    return client.post(reverse("signup"), data)


def _link_from_outbox():
    body = mail.outbox[-1].body
    return re.search(r"https?://\S+/signup/verify/\S+/", body).group(0)


class CacheClearMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


# ---------------------------------------------------------------------------
# signup


class SignupDisabledTests(CacheClearMixin, TestCase):
    def test_flag_off_by_default(self):
        self.assertFalse(settings.SELF_SIGNUP_ENABLED)

    @override_settings(SELF_SIGNUP_ENABLED=False)
    def test_pages_are_404_and_login_has_no_link(self):
        self.assertEqual(self.client.get(reverse("signup")).status_code, 404)
        self.assertEqual(signup_post(self.client).status_code, 404)
        self.assertFalse(User.objects.filter(username="ss_newcomer").exists())
        self.assertEqual(self.client.get(reverse("signup_verify_email", args=["x" * 40])).status_code, 404)
        page = self.client.get(reverse("auth_login"))
        self.assertNotContains(page, 'id="yse-signup-link"')
        self.assertContains(page, reverse("password_reset"))

    @override_settings(SELF_SIGNUP_ENABLED=False)
    def test_requests_closed_when_off(self):
        team = Group.objects.create(name="ss-closed-team")
        owner = make_user("ss_closed_owner")
        group_svc.profile_for(team).owners.add(owner)
        user = make_user("ss_closed_user")
        self.client.force_login(user)
        resp = self.client.post(reverse("team_request_create"), {"group": team.pk})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(GroupMembershipRequest.objects.exists())


@override_settings(**SIGNUP_ON)
class SignupTests(CacheClearMixin, TestCase):
    def test_signup_page_is_public_and_linked_from_login(self):
        resp = self.client.get(reverse("signup"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="website"')  # honeypot present
        self.assertContains(self.client.get(reverse("auth_login")), 'id="yse-signup-link"')

    def test_views_are_marked_exempt_for_the_gate_and_the_login_middleware(self):
        for name, args in (("signup", []), ("signup_verify_email", ["x" * 40])):
            func = resolve(reverse(name, args=args)).func
            self.assertTrue(getattr(func, "onboarding_exempt", False), name)
            self.assertTrue(getattr(func, "login_exempt", False), name)
            self.assertIn(name, onboarding.EXEMPT_URL_NAMES)

    def test_signup_creates_an_active_non_staff_account_in_public_and_signs_in(self):
        resp = signup_post(self.client)
        self.assertRedirects(resp, reverse("team_list"), fetch_redirect_response=False)
        user = User.objects.get(username="ss_newcomer")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(user.check_password(STRONG_PASSWORD))
        self.assertTrue(user.groups.filter(name="Public").exists())
        self.assertEqual(list(user.groups.exclude(name="Public")), [])
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        # The address is not stored until confirmed; the link goes to it.
        self.assertEqual(user.email, "")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["newcomer@example.com"])
        self.assertNotIn("newcomer@example.com", _link_from_outbox())  # the token is encrypted

    @override_settings(SELF_SIGNUP_EMAIL_VERIFICATION=False)
    def test_without_verification_the_address_is_stored(self):
        signup_post(self.client)
        self.assertEqual(User.objects.get(username="ss_newcomer").email, "newcomer@example.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_password_validators_apply(self):
        resp = signup_post(self.client, password1="12345678", password2="12345678")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="ss_newcomer").exists())
        self.assertContains(resp, "too common")

    def test_duplicate_username_is_refused_case_insensitively(self):
        make_user("SS_Taken")
        resp = signup_post(self.client, username="ss_taken")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="ss_taken").exists())

    def test_honeypot_creates_nothing(self):
        resp = signup_post(self.client, website="http://spam.example")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="ss_newcomer").exists())
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(SELF_SIGNUP_MIN_FILL_SECONDS=60)
    def test_too_fast_or_unsigned_form_creates_nothing(self):
        signup_post(self.client, form_ts=signing.dumps(int(time.time()), salt=_TS_SALT))
        signup_post(self.client, username="ss_unsigned", form_ts="forged")
        self.assertFalse(User.objects.filter(username__in=["ss_newcomer", "ss_unsigned"]).exists())

    @override_settings(SELF_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR=2)
    def test_per_ip_hourly_limit(self):
        signup_post(self.client, username="ss_a", password2="mismatch")
        signup_post(self.client, username="ss_b", password2="mismatch")
        resp = signup_post(self.client, username="ss_c")
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(User.objects.filter(username="ss_c").exists())
        # Another client address is counted separately.
        resp = self.client.post(reverse("signup"), {
            "username": "ss_d", "email": "d@example.com", "password1": STRONG_PASSWORD,
            "password2": STRONG_PASSWORD, "form_ts": _old_ts()}, REMOTE_ADDR="192.0.2.77")
        self.assertEqual(resp.status_code, 302)

    @override_settings(SELF_SIGNUP_ACCOUNTS_PER_HOUR=1)
    def test_site_wide_hourly_limit(self):
        signup_post(self.client, username="ss_first")
        self.client.logout()
        resp = signup_post(self.client, username="ss_second", email="second@example.com")
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(User.objects.filter(username="ss_second").exists())

    def test_signed_in_users_are_sent_to_teams(self):
        self.client.force_login(make_user("ss_existing"))
        self.assertRedirects(self.client.get(reverse("signup")), reverse("team_list"), fetch_redirect_response=False)

    @override_settings(**GATE_ON)
    def test_new_account_goes_through_the_agreement_first(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        signup_post(self.client)
        resp = self.client.get(reverse("team_list"), HTTP_ACCEPT="text/html")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("agreement")))


@override_settings(**SIGNUP_ON)
class EmailVerificationTests(CacheClearMixin, TestCase):
    def setUp(self):
        super().setUp()
        signup_post(self.client)
        self.user = User.objects.get(username="ss_newcomer")
        self.path = _link_from_outbox().split("://", 1)[1]
        self.path = self.path[self.path.index("/"):]

    def test_get_shows_confirm_and_post_stores_the_address(self):
        self.client.logout()  # works signed out (another browser)
        page = self.client.get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Confirm")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")  # a GET (mail scanner prefetch) changes nothing
        resp = self.client.post(self.path)
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "newcomer@example.com")
        # Single use: the account's address changed, so the old link no longer works.
        self.assertEqual(self.client.post(self.path).status_code, 400)

    def test_tampered_and_expired_links_are_refused(self):
        token = self.path.rstrip("/").rsplit("/", 1)[1]
        bad = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
        self.assertEqual(self.client.get(reverse("signup_verify_email", args=[bad])).status_code, 400)
        with mock.patch("cryptography.fernet.time.time", return_value=time.time() + 80 * 3600):
            self.assertEqual(self.client.post(self.path).status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_address_already_used_by_another_account(self):
        make_user("ss_holder", email="NEWCOMER@example.com")
        self.assertEqual(self.client.post(self.path).status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_resend_from_the_email_page(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_email")).status_code, 200)
        self.client.post(reverse("account_email"), {"email": "other@example.com"})
        self.assertEqual(mail.outbox[-1].to, ["other@example.com"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")
        for _ in range(6):
            self.client.post(reverse("account_email"), {"email": "other@example.com"})
        self.assertLessEqual(len(mail.outbox), 5)  # hourly cap per user


# ---------------------------------------------------------------------------
# membership requests


@override_settings(**SIGNUP_ON)
class MembershipRequestTests(CacheClearMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.public, _ = Group.objects.get_or_create(name="Public")
        self.writers, _ = Group.objects.get_or_create(name="api_writers")
        self.team = Group.objects.create(name="ss-team-alpha")
        self.team2 = Group.objects.create(name="ss-team-beta")
        self.ownerless = Group.objects.create(name="ss-team-ownerless")
        self.old = Group.objects.create(name="ss-team-old")
        self.owner = make_user("ss_owner")
        self.owner2 = make_user("ss_owner2")
        self.member = make_user("ss_member")
        self.requester = make_user("ss_requester")
        self.owner.groups.add(self.team)
        self.member.groups.add(self.team)
        group_svc.profile_for(self.team).owners.add(self.owner)
        group_svc.profile_for(self.team2).owners.add(self.owner2)
        group_svc.profile_for(self.old).owners.add(self.owner)
        GroupProfile.objects.filter(group=self.old).update(is_active=False)

    def test_only_active_owned_non_system_teams_are_requestable(self):
        names = [g.name for g in membership.requestable_groups(self.requester)]
        self.assertEqual(names, ["ss-team-alpha", "ss-team-beta"])
        self.assertNotIn("ss-team-alpha", [g.name for g in membership.requestable_groups(self.member)])

    def test_system_groups_cannot_be_requested_even_by_post(self):
        self.client.force_login(self.requester)
        for group in (self.public, self.writers, self.ownerless, self.old):
            self.client.post(reverse("team_request_create"), {"group": group.pk})
        self.assertFalse(GroupMembershipRequest.objects.exists())
        with self.assertRaises(membership.MembershipRequestError):
            membership.create_request(self.requester, self.writers)

    def test_request_notifies_owners_and_approval_adds_the_member(self):
        self.client.force_login(self.requester)
        page = self.client.get(reverse("team_list"))
        self.assertContains(page, "ss-team-alpha")
        self.client.post(reverse("team_request_create"), {"group": self.team.pk, "message": "synthetic note"})
        req = GroupMembershipRequest.objects.get()
        self.assertEqual((req.user, req.group, req.status, req.message),
                         (self.requester, self.team, "pending", "synthetic note"))
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.member).exists())
        # A second click does not duplicate the request.
        self.client.post(reverse("team_request_create"), {"group": self.team.pk})
        self.assertEqual(GroupMembershipRequest.objects.count(), 1)

        self.client.force_login(self.owner)
        page = self.client.get(reverse("team_detail", args=[self.team.pk]))
        self.assertContains(page, "synthetic note")
        resp = self.client.post(reverse("team_request_decide", args=[self.team.pk, req.pk]), {"action": "approve"})
        self.assertRedirects(resp, reverse("team_detail", args=[self.team.pk]), fetch_redirect_response=False)
        req.refresh_from_db()
        self.assertEqual((req.status, req.decided_by), ("approved", self.owner))
        self.assertTrue(self.requester.groups.filter(pk=self.team.pk).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.requester).exists())
        # Already decided: a second decision is refused.
        self.client.post(reverse("team_request_decide", args=[self.team.pk, req.pk]), {"action": "deny"})
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")

    def test_deny(self):
        req = membership.create_request(self.requester, self.team)
        membership.decide(self.owner, req, approve=False)
        req.refresh_from_db()
        self.assertEqual(req.status, "denied")
        self.assertFalse(self.requester.groups.filter(pk=self.team.pk).exists())

    def test_only_owners_and_staff_decide(self):
        req = membership.create_request(self.requester, self.team)
        for user in (self.member, self.owner2, self.requester):
            self.client.force_login(user)
            resp = self.client.post(reverse("team_request_decide", args=[self.team.pk, req.pk]), {"action": "approve"})
            self.assertEqual(resp.status_code, 404, user.username)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("team_request_decide", args=[self.team.pk, req.pk])).status_code, 405)
        staff = make_user("ss_staff", is_staff=True)
        membership.decide(staff, req, approve=True)
        self.assertTrue(self.requester.groups.filter(pk=self.team.pk).exists())

    def test_withdraw_and_purge(self):
        req = membership.create_request(self.requester, self.team)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(reverse("team_request_cancel", args=[req.pk])).status_code, 404)
        self.client.force_login(self.requester)
        self.client.post(reverse("team_request_cancel", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, "cancelled")
        pending = membership.create_request(self.requester, self.team2)
        GroupMembershipRequest.objects.filter(pk=req.pk).update(decided_at=timezone.now() - datetime.timedelta(days=200))
        call_command("purge_group_membership_requests", "--days", "90", stdout=StringIO())
        self.assertFalse(GroupMembershipRequest.objects.filter(pk=req.pk).exists())
        self.assertTrue(GroupMembershipRequest.objects.filter(pk=pending.pk).exists())

    @override_settings(GROUP_REQUESTS_PER_USER_PER_DAY=1)
    def test_daily_request_limit(self):
        membership.create_request(self.requester, self.team)
        with self.assertRaises(membership.MembershipRequestError):
            membership.create_request(self.requester, self.team2)

    @override_settings(**GATE_ON)
    def test_post_before_onboarding_does_not_create_a_request(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        self.client.force_login(self.requester)
        resp = self.client.post(reverse("team_request_create"), {"group": self.team.pk})
        self.assertTrue(resp["Location"].startswith(reverse("agreement")))
        self.assertFalse(GroupMembershipRequest.objects.exists())


@override_settings(**SIGNUP_ON, **GATE_ON)
class SignupAcceptanceTests(CacheClearMixin, TestCase):
    """Sign up -> confirm email -> agreement -> profile -> request two teams -> one approved, one denied."""

    def setUp(self):
        super().setUp()
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        self.team = Group.objects.create(name="ss-acc-alpha")
        self.team2 = Group.objects.create(name="ss-acc-beta")
        self.owner = make_user("ss_acc_owner")
        self.owner2 = make_user("ss_acc_owner2")
        group_svc.profile_for(self.team).owners.add(self.owner)
        group_svc.profile_for(self.team2).owners.add(self.owner2)

    def test_full_flow(self):
        signup_post(self.client, username="ss_acc_new", email="acc@example.com")
        user = User.objects.get(username="ss_acc_new")
        link = _link_from_outbox()
        self.client.post(link[link.index("/", link.index("://") + 3):])
        user.refresh_from_db()
        self.assertEqual(user.email, "acc@example.com")

        teams = reverse("team_list")
        self.client.post(reverse("agreement"), {"agree": "on", "next": teams})
        self.client.post(reverse("researcher_profile_edit"), {
            "full_name": "Synthetic Applicant", "institution": "Example University", "department": "Physics",
            "career_level": "tenured", "orcid": VALID_ORCID, "next": teams,
        })
        self.assertEqual(self.client.get(teams, HTTP_ACCEPT="text/html").status_code, 200)
        for team in (self.team, self.team2):
            self.client.post(reverse("team_request_create"), {"group": team.pk})
        self.assertEqual(GroupMembershipRequest.objects.filter(user=user, status="pending").count(), 2)

        membership.decide(self.owner, GroupMembershipRequest.objects.get(group=self.team), approve=True)
        membership.decide(self.owner2, GroupMembershipRequest.objects.get(group=self.team2), approve=False)
        self.assertEqual(sorted(user.groups.values_list("name", flat=True)), ["Public", "ss-acc-alpha"])


# ---------------------------------------------------------------------------
# #440 follow-up: Public is always offered


class PublicAlwaysVisibleTests(TestCase):
    def setUp(self):
        self.public, _ = Group.objects.get_or_create(name="Public")
        self.team = Group.objects.create(name="ss-pub-team")
        self.staff = make_user("ss_pub_staff", is_staff=True)
        self.user = make_user("ss_pub_user")
        self.user.groups.add(self.team, self.public)
        self.transient = create_minimal_transient(self.staff, name="2026sspub")

    def _comment_labels(self):
        form = TransientCommentForm(user=self.user, transient_id=self.transient.id)
        return [label for _v, label in form.fields["audience_groups"].choices]

    def _followup(self):
        return {c["group"].name: c for c in audience.build_followup_audience_choices(self.user, None)}

    def test_a_stale_hidden_preference_cannot_hide_public(self):
        pref = UserPreference.objects.create(user=self.user)
        pref.hidden_groups.add(self.public, self.team)
        group_svc.forget_preferences(self.user)
        self.assertNotIn(self.public.pk, group_svc.hidden_group_ids(self.user))
        self.assertIn("Public", self._comment_labels())
        self.assertFalse(self._followup()["Public"]["hidden"])

    @override_settings(YSE_PICKER_VISIBLE_SYSTEM_GROUPS=())
    def test_the_setting_cannot_hide_public(self):
        self.assertIn("Public", group_svc.picker_visible_system_group_names())
        self.assertIn("Public", self._comment_labels())
        self.assertIn("Public", self._followup())

    def test_an_inactive_flag_cannot_hide_public(self):
        GroupProfile.objects.filter(group=self.public).update(is_active=False)
        self.assertIn("Public", self._comment_labels())
        self.assertFalse(self._followup()["Public"]["hidden"])

    def test_settings_form_does_not_offer_hiding_public(self):
        form = UserPreferenceForm(user=self.user, instance=UserPreference(user=self.user))
        self.assertNotIn(self.public, form.fields["hidden_groups"].queryset)
        self.assertIn(self.team, form.fields["hidden_groups"].queryset)
        self.client.force_login(self.user)
        self.client.post(reverse("user_settings"), {"hidden_groups": [self.public.pk]})
        pref = UserPreference.objects.filter(user=self.user).first()
        self.assertTrue(pref is None or not pref.hidden_groups.filter(pk=self.public.pk).exists())


# ---------------------------------------------------------------------------
# #643: PI contact fields


class PrincipalInvestigatorApiTests(TestCase):
    def setUp(self):
        self.staff = make_user("ss_pi_staff", is_staff=True)
        self.user = make_user("ss_pi_user")
        self.pi = PrincipalInvestigator.objects.create(
            name="Synthetic PI", email="synthetic-pi@example.com", phone="000-0000", institution="Synthetic U",
            **audit_fields(self.staff))

    def test_non_staff_see_only_id_and_name(self):
        self.client.force_login(self.user)
        detail = self.client.get("/api/principalinvestigators/%d/" % self.pi.pk).json()
        self.assertEqual(set(detail), {"url", "id", "name"})
        self.assertEqual((detail["id"], detail["name"]), (self.pi.pk, "Synthetic PI"))
        listing = self.client.get("/api/principalinvestigators/").json()
        rows = listing.get("results", listing)
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("email", row)
            self.assertNotIn("phone", row)
        self.assertNotIn("synthetic-pi@example.com", json.dumps(listing))

    def test_staff_see_contact_fields(self):
        self.client.force_login(self.staff)
        detail = self.client.get("/api/principalinvestigators/%d/" % self.pi.pk).json()
        self.assertEqual(detail["email"], "synthetic-pi@example.com")
        self.assertEqual(detail["phone"], "000-0000")

    def test_non_staff_can_still_create_with_contact_fields(self):
        self.client.force_login(self.user)
        resp = self.client.post("/api/principalinvestigators/", data=json.dumps({
            "name": "Synthetic PI Two", "email": "pi-two@example.com", "phone": "111", "institution": "Synthetic U",
            "obs_group": [],
        }), content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertNotIn("email", resp.json())
        self.assertEqual(PrincipalInvestigator.objects.get(name="Synthetic PI Two").email, "pi-two@example.com")

    def test_anonymous_refused(self):
        self.assertIn(self.client.get("/api/principalinvestigators/").status_code, (401, 403))

    def test_explorer_excludes_the_pi_and_request_tables(self):
        for name in ("YSE_App_principalinvestigator", "YSE_App_groupmembershiprequest"):
            self.assertIn(name, settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)
            self.assertIn(name, settings.EXPLORER_SQL_BLACKLIST)
