"""Given and family names (on the account) and a confirmed email address, required at
signup and from every account through the onboarding gate.

All users, names, addresses and data here are synthetic.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from YSE_App import onboarding
from YSE_App.models import AgreementAcceptance, ResearcherProfile, UserAgreement
from YSE_App.profile_forms import ResearcherProfileForm
from YSE_App.services import groups as group_svc
from YSE_App.services import research_crypto
from YSE_App.services import researcher_profile as rp
from YSE_App.tests.test_self_signup import SIGNUP_ON, signup_post

GATE = dict(ONBOARDING_GATE_ENABLED=True, ONBOARDING_EMAIL_REQUIRED=True,
            RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
VALID_ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (fictitious researcher)
PROFILE_POST = {"first_name": "Synthetic", "last_name": "Person", "full_name": "Synthetic Person",
                "institution": "Example University", "department": "Physics", "career_level": "tenured",
                "orcid": VALID_ORCID}


class GateBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)

    def accept_agreement(self, user):
        agreement = UserAgreement.objects.filter(is_current=True).first()
        if agreement is not None:
            AgreementAcceptance.objects.get_or_create(user=user, agreement=agreement)

    def page(self, url):
        return self.client.get(url, HTTP_ACCEPT="text/html")


# ---------------------------------------------------------------------------
# signup form


@override_settings(**SIGNUP_ON)
class SignupNameTests(GateBase):
    def test_form_asks_for_given_and_family_names(self):
        page = self.client.get(reverse("signup"))
        self.assertContains(page, 'name="first_name"')
        self.assertContains(page, 'name="last_name"')
        self.assertContains(page, 'autocomplete="family-name"')

    def test_names_are_required(self):
        for missing in ("first_name", "last_name"):
            with self.subTest(missing=missing):
                resp = signup_post(self.client, **{missing: "   "})
                self.assertEqual(resp.status_code, 200)
                self.assertIn(missing, resp.context["form"].errors)
                self.assertFalse(User.objects.filter(username="ss_newcomer").exists())

    def test_names_saved_on_the_account_as_typed(self):
        signup_post(self.client, first_name=" María  José ", last_name="de la  Cruz")
        user = User.objects.get(username="ss_newcomer")
        self.assertEqual((user.first_name, user.last_name), ("María José", "de la Cruz"))


# ---------------------------------------------------------------------------
# email step


@override_settings(**GATE, SELF_SIGNUP_ENABLED=False, SELF_SIGNUP_EMAIL_VERIFICATION=False, EMAIL_BACKEND=LOCMEM)
class EmailStepTests(GateBase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("ne_noemail", password="x", first_name="Synthetic", last_name="Person")
        self.client.force_login(self.user)

    def test_account_without_email_goes_to_the_email_page_first(self):
        resp = self.page(reverse("team_list"))
        self.assertEqual(resp.status_code, 302)
        location = urlsplit(resp["Location"])
        self.assertEqual(location.path, reverse("account_email"))
        self.assertEqual(parse_qs(location.query).get("next"), [reverse("team_list")])

    def test_email_page_works_with_signup_off_and_continues(self):
        page = self.page(reverse("account_email") + "?next=" + reverse("team_list"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "needs a confirmed email address")
        resp = self.client.post(reverse("account_email"), {"email": "noemail@example.com",
                                                           "next": reverse("team_list")})
        self.assertRedirects(resp, reverse("team_list"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "noemail@example.com")
        # Next step: the agreement (when one is current), not the email page again.
        nxt = self.page(reverse("team_list"))
        if nxt.status_code == 302:
            self.assertNotEqual(urlsplit(nxt["Location"]).path, reverse("account_email"))

    def test_post_actions_check_the_email_step_too(self):
        team = Group.objects.create(name="ne-team")
        group_svc.profile_for(team).owners.add(User.objects.create_user("ne_owner", email="o@example.com"))
        resp = self.client.post(reverse("team_request_create"), {"group": team.pk})
        self.assertEqual(urlsplit(resp["Location"]).path, reverse("account_email"))

    @override_settings(ONBOARDING_EMAIL_REQUIRED=False)
    def test_step_can_be_turned_off(self):
        self.assertNotEqual(onboarding.pending_step_for(self.user), "email")

    @override_settings(SELF_SIGNUP_EMAIL_VERIFICATION=True)
    def test_with_verification_the_link_confirms_even_with_signup_off(self):
        self.client.post(reverse("account_email"), {"email": "noemail@example.com", "next": reverse("team_list")})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")  # not stored before the link is confirmed
        self.assertEqual(onboarding.pending_step_for(self.user), "email")
        body = mail.outbox[-1].body
        self.assertIn("asked to use this email address", body)
        link = re.search(r"https?://\S+/signup/verify/\S+/", body).group(0)
        path = urlsplit(link).path
        self.assertEqual(self.client.get(path).status_code, 200)
        self.client.post(path)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "noemail@example.com")
        page = self.page(reverse("account_email") + "?next=" + reverse("team_list"))
        self.assertContains(page, "Continue")


# ---------------------------------------------------------------------------
# names in the profile and the gate


@override_settings(**GATE)
class NameStepTests(GateBase):
    def setUp(self):
        super().setUp()
        # An existing account: email, agreement and a profile with only one name, no account names.
        self.user = User.objects.create_user("ne_oneword", password="x", email="oneword@example.com")
        self.accept_agreement(self.user)
        ResearcherProfile.objects.create(user=self.user, full_name="Mononym", institution="Example University",
                                         department="Physics", career_level="tenured", orcid=VALID_ORCID)
        self.client.force_login(self.user)

    def test_existing_account_without_names_is_sent_to_the_profile(self):
        self.assertEqual(rp.missing_fields(self.user), ["first_name", "last_name"])
        resp = self.page(reverse("team_list"))
        self.assertEqual(urlsplit(resp["Location"]).path, reverse("researcher_profile_edit"))
        page = self.page(reverse("researcher_profile_edit"))
        self.assertContains(page, 'name="first_name"')
        self.assertContains(page, "Given name(s)")

    def test_saving_the_names_completes_onboarding(self):
        resp = self.client.post(reverse("researcher_profile_edit"), dict(PROFILE_POST, next=reverse("team_list")))
        self.assertRedirects(resp, reverse("team_list"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Synthetic", "Person"))
        self.assertIsNone(onboarding.pending_step_for(self.user))
        self.assertEqual(self.page(reverse("team_list")).status_code, 200)

    def test_profile_form_requires_both_names(self):
        resp = self.client.post(reverse("researcher_profile_edit"), dict(PROFILE_POST, last_name=""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("last_name", resp.context["form"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, "")

    def test_full_name_is_prefilled_from_the_account_names_only_when_empty(self):
        user = User.objects.create_user("ne_prefill", first_name="Synthetic", last_name="Person")
        self.assertEqual(ResearcherProfileForm(user=user, instance=ResearcherProfile(user=user)).initial["full_name"],
                         "Synthetic Person")
        kept = ResearcherProfileForm(user=user, instance=ResearcherProfile(user=user, full_name="S. Person"))
        self.assertEqual(kept.initial["full_name"], "S. Person")

    def test_rules_version_changed(self):
        """Sessions that passed the gate before this rule are checked again."""
        self.assertEqual(rp.RULES_VERSION, "3")


class MemberSortTests(TestCase):
    def test_team_members_sort_by_family_name(self):
        owner = User.objects.create_user("ne_sort_owner", first_name="Synthetic", last_name="Zed")
        team = group_svc.create_team(owner, "ne-sort-team")
        for username, first, last in (("ne_a", "Bea", "Young"), ("ne_b", "Al", "Abel"), ("ne_c", "Cy", "Abel")):
            User.objects.create_user(username, first_name=first, last_name=last).groups.add(team)
        self.client.force_login(owner)
        rows = self.client.get(reverse("team_detail", args=[team.pk])).context["members"].rows
        self.assertEqual([r["user"].username for r in rows], ["ne_b", "ne_c", "ne_a", "ne_sort_owner"])


# ---------------------------------------------------------------------------
# filling account names from the profile's full name


class SplitFullNameTests(TestCase):
    def test_unambiguous_names_split(self):
        from YSE_App.common.names import split_full_name
        cases = {
            "Ada Lovelace": ("Ada", "Lovelace"),
            "  Ada   Lovelace ": ("Ada", "Lovelace"),
            "Ada B. Lovelace": ("Ada B.", "Lovelace"),
            "A. Byron Lovelace": ("A. Byron", "Lovelace"),
            "Ada B Lovelace": ("Ada B", "Lovelace"),
            "Mary-Jane O'Neil": ("Mary-Jane", "O'Neil"),
            "Zoë Müller": ("Zoë", "Müller"),
        }
        for name, expected in cases.items():
            self.assertEqual(split_full_name(name), expected, name)

    def test_everything_else_is_left_to_the_person(self):
        from YSE_App.common.names import split_full_name
        for name in ("", "Mononym", "A. Lovelace", "Ada L.", "Ada Byron Lovelace", "A. B. Lovelace",
                     "Ada Lovelace Jr.", "Lovelace, Ada", "Ada van Lovelace Byron", "Ada B. L.",
                     "Ada (Byron) Lovelace", "Ada Lovelace 2", "Ada King Byron Lovelace"):
            self.assertIsNone(split_full_name(name), name)


@override_settings(**GATE)
class FillAccountNamesTests(GateBase):
    def make(self, username, full_name, **names):
        user = User.objects.create_user(username, password="x", email="%s@example.com" % username, **names)
        self.accept_agreement(user)
        ResearcherProfile.objects.create(user=user, full_name=full_name, institution="Example University")
        return user

    def test_gate_fills_unambiguous_names_without_asking(self):
        user = self.make("ne_fill", "Synthetic Q. Person")
        self.client.force_login(user)
        self.assertEqual(self.page(reverse("team_list")).status_code, 200)
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Synthetic Q.", "Person"))

    def test_ambiguous_names_still_go_to_the_profile(self):
        user = self.make("ne_ambiguous", "Synthetic van der Person")
        self.client.force_login(user)
        resp = self.page(reverse("team_list"))
        self.assertEqual(urlsplit(resp["Location"]).path, reverse("researcher_profile_edit"))
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("", ""))

    def test_existing_names_are_never_overwritten(self):
        user = self.make("ne_partial", "Synthetic Person", first_name="Given")
        self.assertFalse(rp.fill_account_names(user))
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Given", ""))

    def test_migration_fills_only_unambiguous_empty_accounts(self):
        import importlib

        from django.apps import apps as django_apps
        migration = importlib.import_module("YSE_App.migrations.0060_fill_account_names")
        a = self.make("ne_mig_a", "Synthetic Person")
        b = self.make("ne_mig_b", "S. Q. Person")
        c = self.make("ne_mig_c", "Synthetic Person", first_name="Kept", last_name="Names")
        migration.fill(django_apps, None)
        migration.fill(django_apps, None)  # idempotent
        for user in (a, b, c):
            user.refresh_from_db()
        self.assertEqual((a.first_name, a.last_name), ("Synthetic", "Person"))
        self.assertEqual((b.first_name, b.last_name), ("", ""))
        self.assertEqual((c.first_name, c.last_name), ("Kept", "Names"))

    def test_command_reports_counts_only(self):
        from io import StringIO

        from django.core.management import call_command
        self.make("ne_cmd_a", "Synthetic Person")
        self.make("ne_cmd_b", "Mononym")
        out = StringIO()
        call_command("fill_account_names", "--dry-run", stdout=out)
        self.assertEqual(out.getvalue().strip(), "account names: would fill 1; 1 left for their owners to enter")
        self.assertEqual(User.objects.get(username="ne_cmd_a").last_name, "")
        call_command("fill_account_names", stdout=StringIO())
        self.assertEqual(User.objects.get(username="ne_cmd_a").last_name, "Person")
