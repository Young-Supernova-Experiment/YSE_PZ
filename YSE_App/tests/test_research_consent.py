"""User Agreement v2 (#743) and the voluntary research consent form (#744).

Every user, name and value here is synthetic.
"""

from __future__ import annotations

import datetime
import importlib
import io
from urllib.parse import urlencode

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import onboarding
from YSE_App.models import (
    AgreementAcceptance,
    EncryptedProfileValue,
    ResearchConsentForm,
    ResearchConsentRecord,
    ResearcherProfile,
    UserAgreement,
)
from YSE_App.services import research_consent as rc
from YSE_App.services import research_crypto
from YSE_App.services import research_dataset as rd
from YSE_App.services import researcher_profile as rp
from YSE_App.tests.test_researcher_profile import with_key

VALID_ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (fictitious researcher)
GATE_ON = dict(ONBOARDING_GATE_ENABLED=True, RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")
UTC = datetime.timezone.utc
GO_LIVE_DATE = datetime.date(2026, 1, 1)
GO_LIVE = datetime.datetime(2026, 1, 1, tzinfo=UTC)


def at(month, day=1):
    return datetime.datetime(2026, month, day, 12, tzinfo=UTC)


def complete_profile(user):
    return ResearcherProfile.objects.create(
        user=user, full_name="Test Person", institution="Example University", department="Physics",
        career_level="tenured", orcid=VALID_ORCID)


class AgreementV2Tests(TestCase):
    def setUp(self):
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)

    def test_v2_replaces_v1_and_v1_is_kept(self):
        v1 = UserAgreement.objects.get(version="1")
        v2 = UserAgreement.objects.get(version="2")
        self.assertFalse(v1.is_current)
        self.assertTrue(v2.is_current)
        self.assertEqual(v2.title, "YSE-PZ User Agreement (v2)")
        self.assertIn("Use of platform data for research", v1.body)
        self.assertNotIn("Use of platform data for research", v2.body)
        self.assertNotIn("(such as gender)", v2.body)
        self.assertIn("Optional demographic information is stored encrypted.", v2.body)
        self.assertEqual(v1.body.count("<li>"), 9)
        self.assertEqual(v2.body.count("<li>"), 8)
        # Nothing else changed: v1 minus item 5 and minus "(such as gender) " is v2.
        v1_items = [line for line in v1.body.splitlines() if "Use of platform data for research" not in line]
        self.assertEqual("\n".join(v1_items).replace("(such as gender) ", ""), v2.body.rstrip("\n"))

    def test_seed_is_idempotent(self):
        migration = importlib.import_module("YSE_App.migrations.0038_user_agreement_v2")
        migration.seed(django_apps, None)
        self.assertEqual(UserAgreement.objects.filter(version="2").count(), 1)
        self.assertEqual(list(UserAgreement.objects.filter(is_current=True).values_list("version", flat=True)), ["2"])
        consent = importlib.import_module("YSE_App.migrations.0040_seed_research_consent")
        consent.seed(django_apps, None)
        self.assertEqual(ResearchConsentForm.objects.filter(version="1").count(), 1)

    @override_settings(**GATE_ON)
    def test_v1_acceptors_must_accept_v2_once(self):
        research_crypto.reset_cache()
        user = User.objects.create_user("v1_acceptor", password="x")
        AgreementAcceptance.objects.create(user=user, agreement=UserAgreement.objects.get(version="1"))
        complete_profile(user)
        self.client.force_login(user)
        page = reverse("notification_list")
        resp = self.client.get(page)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("agreement"), resp["Location"])
        self.client.post(reverse("agreement"), {"agree": "on", "next": page})
        self.assertEqual(self.client.get(page).status_code, 200)
        self.assertEqual(self.client.get(page).status_code, 200)
        self.assertEqual(AgreementAcceptance.objects.filter(user=user).count(), 2)  # v1 acceptance kept


class ConsentFormSeedTests(TestCase):
    def test_form_text_is_seeded_as_version_1(self):
        form = rc.current_form()
        self.assertIsNotNone(form)
        self.assertEqual(form.version, "1")
        self.assertIsNotNone(form.effective_date)
        for phrase in ("This study is looking to learn more about how scientists make decisions",
                       "Your participation will take about 5 minutes.",
                       "it will not affect your grade, job, or any benefits you’re entitled to.",
                       "you can contact Prof. Ryan Foley at",
                       "By agreeing to participate, you are saying that:"):
            self.assertIn(phrase, form.body)
        self.assertEqual(form.body.count("<li>"), 3)
        self.assertEqual(rc.go_live(), datetime.datetime.combine(form.effective_date, datetime.time.min, tzinfo=UTC))


@override_settings(**GATE_ON)
class ConsentFlowTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        self.user = User.objects.create_user("consent_user", password="x")
        self.client.force_login(self.user)
        self.page = reverse("notification_list")

    def accept_agreement(self):
        return self.client.post(reverse("agreement"), {"agree": "on", "next": self.page})

    def test_offered_once_after_the_agreement(self):
        resp = self.accept_agreement()
        self.assertRedirects(resp, "%s?%s" % (reverse("research_consent"), urlencode({"next": self.page})),
                             fetch_redirect_response=False)
        page = self.client.get(resp["Location"])
        self.assertContains(page, "I consent to participate in this study")
        self.assertContains(page, 'value="consent"')
        self.assertContains(page, 'value="decline"')
        self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        # A later agreement version does not offer the consent form again.
        UserAgreement.objects.create(version="3", title="v3", body="<p>Terms.</p>", is_current=True)
        resp = self.accept_agreement()
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)

    def test_consent_needs_the_box_and_records_time_and_version(self):
        resp = self.client.post(reverse("research_consent"), {"action": "consent", "next": self.page})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ResearchConsentRecord.objects.exists())
        before = timezone.now()
        resp = self.client.post(reverse("research_consent"), {"action": "consent", "consent": "on",
                                                              "next": self.page})
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        row = ResearchConsentRecord.objects.get(user=self.user)
        self.assertEqual(row.decision, rc.CONSENTED)
        self.assertEqual(row.form, rc.current_form())
        self.assertGreaterEqual(row.decided_at, before)
        self.assertTrue(rc.has_consented(self.user))

    def test_withdraw_then_consent_again_keeps_every_record(self):
        rc.record_decision(self.user, rc.CONSENTED, now=at(2))
        page = self.client.get(reverse("research_consent"))
        self.assertContains(page, 'value="withdraw"')
        self.assertNotContains(page, 'value="decline"')
        self.client.post(reverse("research_consent"), {"action": "withdraw"})
        self.assertEqual(rc.status(self.user), rc.WITHDRAWN)
        self.client.post(reverse("research_consent"), {"action": "consent", "consent": "on"})
        decisions = list(ResearchConsentRecord.objects.filter(user=self.user)
                         .order_by("decided_at", "id").values_list("decision", flat=True))
        self.assertEqual(decisions, [rc.CONSENTED, rc.WITHDRAWN, rc.CONSENTED])

    def test_decisions_that_do_not_fit_the_state_are_refused(self):
        with self.assertRaises(rc.ConsentError):
            rc.record_decision(self.user, rc.WITHDRAWN)
        rc.record_decision(self.user, rc.CONSENTED)
        for decision in (rc.CONSENTED, rc.DECLINED):
            with self.assertRaises(rc.ConsentError):
                rc.record_decision(self.user, decision)
        # Through the page: nothing changes, and the user still continues.
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        self.assertEqual(ResearchConsentRecord.objects.filter(user=self.user).count(), 1)

    def test_never_a_gate(self):
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        complete_profile(self.user)
        self.assertTrue(rc.needs_prompt(self.user))
        self.assertEqual(self.client.get(self.page).status_code, 200)  # unanswered: not redirected
        self.assertEqual(self.client.get(reverse("research_consent")).status_code, 200)

    def test_consent_page_is_exempt_before_onboarding(self):
        # A newcomer who has accepted nothing can still open and answer the form (no loop).
        self.assertEqual(self.client.get(reverse("research_consent")).status_code, 200)
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)

    def test_profile_offers_consent_and_withdrawal(self):
        complete_profile(self.user)
        page = self.client.get(reverse("researcher_profile"))
        self.assertContains(page, "You have not answered the research consent form yet.")
        rc.record_decision(self.user, rc.CONSENTED)
        page = self.client.get(reverse("researcher_profile"))
        self.assertContains(page, "You consented to take part")
        self.assertContains(page, "Withdraw")


class DemographicsTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        self.addCleanup(research_crypto.reset_cache)
        self.user = User.objects.create_user("demo_user", password="x")
        self.client.force_login(self.user)

    def post_profile(self, **extra):
        data = dict(full_name="Test Person", institution="Example University", department="Physics",
                    career_level="postdoc", orcid=VALID_ORCID, phd_year="2015")
        data.update(extra)
        return self.client.post(reverse("researcher_profile_edit"), data)

    def stored(self):
        return set(EncryptedProfileValue.objects.filter(user=self.user).values_list("field_name", flat=True))

    def test_fields_hidden_and_ignored_without_consent(self):
        with with_key():
            page = self.client.get(reverse("researcher_profile_edit"))
            self.assertContains(page, "Offered only if you")
            self.assertNotContains(page, 'name="gender"')
            self.assertNotContains(page, 'name="country"')
            resp = self.post_profile(gender="woman", country="Atlantis")
            self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.stored(), {"phd_year"})  # PhD year is career information, not demographic

    def test_offered_to_consenting_users(self):
        rc.record_decision(self.user, rc.CONSENTED)
        with with_key():
            page = self.client.get(reverse("researcher_profile_edit"))
            self.assertContains(page, 'name="gender"')
            self.post_profile(gender="woman", country="Atlantis")
        self.assertEqual(self.stored(), {"phd_year", "gender", "country"})

    def _store_demographics(self):
        with with_key():
            for name, value in (("gender", "man"), ("country", "Atlantis"), ("phd_year", "2015")):
                rp.set_encrypted(self.user, name, value, changed_by=self.user)

    def test_decline_deletes_demographics(self):
        self._store_demographics()
        rc.record_decision(self.user, rc.DECLINED)
        self.assertEqual(self.stored(), {"phd_year"})

    def test_withdraw_deletes_demographics(self):
        rc.record_decision(self.user, rc.CONSENTED)
        self._store_demographics()
        resp = self.client.post(reverse("research_consent"), {"action": "withdraw"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.stored(), {"phd_year"})

    def test_unanswered_users_keep_values_and_may_delete_them(self):
        self._store_demographics()
        self.assertEqual(self.stored(), {"phd_year", "gender", "country"})
        with with_key():
            self.post_profile(clear_gender="on")
        self.assertEqual(self.stored(), {"phd_year", "country"})


class PurgeCommandTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        self.addCleanup(research_crypto.reset_cache)
        self.unanswered = User.objects.create_user("purge_unanswered")
        self.consenting = User.objects.create_user("purge_consenting")
        rc.record_decision(self.consenting, rc.CONSENTED)
        with with_key():
            for user in (self.unanswered, self.consenting):
                rp.set_encrypted(user, "gender", "prefer_not_to_say", changed_by=user)
                rp.set_encrypted(user, "phd_year", "2010", changed_by=user)

    def run_purge(self, *args):
        out = io.StringIO()
        call_command("purge_unconsented_demographics", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_by_default(self):
        out = self.run_purge()
        self.assertIn("1 demographic value(s) of 1 user(s)", out)
        self.assertIn("Dry run", out)
        self.assertEqual(EncryptedProfileValue.objects.count(), 4)
        self.assertNotIn("purge_unanswered", out)

    def test_apply_deletes_only_unconsented_demographics(self):
        self.run_purge("--apply")
        remaining = set(EncryptedProfileValue.objects.values_list("user__username", "field_name"))
        self.assertEqual(remaining, {("purge_unanswered", "phd_year"), ("purge_consenting", "gender"),
                                     ("purge_consenting", "phd_year")})

    def test_older_than_days(self):
        self.run_purge("--apply", "--older-than-days", "30")
        self.assertEqual(EncryptedProfileValue.objects.count(), 4)  # stored today: kept
        EncryptedProfileValue.objects.update(set_at=timezone.now() - datetime.timedelta(days=45))
        self.run_purge("--apply", "--older-than-days", "30")
        self.assertEqual(EncryptedProfileValue.objects.count(), 3)


class ResearchDatasetTests(TestCase):
    """Inclusion rules, with the go-live date fixed at 2026-01-01."""

    def setUp(self):
        research_crypto.reset_cache()
        self.addCleanup(research_crypto.reset_cache)
        ResearchConsentForm.objects.update(effective_date=GO_LIVE_DATE)
        make = User.objects.create_user
        self.consented = make("ds_consented")
        self.declined = make("ds_declined")
        self.withdrawn = make("ds_withdrawn")
        self.unanswered = make("ds_unanswered")
        self.rejoined = make("ds_rejoined")
        rc.record_decision(self.consented, rc.CONSENTED, now=at(3))
        rc.record_decision(self.declined, rc.DECLINED, now=at(3))
        rc.record_decision(self.withdrawn, rc.CONSENTED, now=at(2))
        rc.record_decision(self.withdrawn, rc.WITHDRAWN, now=at(4))
        rc.record_decision(self.rejoined, rc.CONSENTED, now=at(2))
        rc.record_decision(self.rejoined, rc.WITHDRAWN, now=at(4))
        rc.record_decision(self.rejoined, rc.CONSENTED, now=at(6))
        self.cohort = rd.ResearchCohort.load()

    def test_go_live_is_the_form_effective_date(self):
        self.assertEqual(self.cohort.go_live, GO_LIVE)

    def test_before_go_live_everyone_even_withdrawn(self):
        # Public before go-live: usable anonymized for everyone (Ryan, 2026-10-20).
        before = datetime.datetime(2025, 12, 1, tzinfo=UTC)
        for user in (self.consented, self.declined, self.unanswered, self.rejoined, self.withdrawn):
            self.assertTrue(self.cohort.includes(user.pk, before), user.username)

    def test_after_go_live_only_inside_consent_periods(self):
        c = self.cohort
        self.assertFalse(c.includes(self.consented.pk, at(2)))  # before consenting
        self.assertTrue(c.includes(self.consented.pk, at(3, 2)))
        self.assertTrue(c.includes(self.consented.pk, at(12)))
        for user in (self.declined, self.unanswered):
            self.assertFalse(c.includes(user.pk, at(5)), user.username)
        for when in (at(3), at(5), at(7)):
            self.assertFalse(c.includes(self.withdrawn.pk, when))  # withdrawn: nothing after go-live
        self.assertTrue(c.includes(self.rejoined.pk, at(3)))
        self.assertFalse(c.includes(self.rejoined.pk, at(5)))  # between withdrawal and re-consent
        self.assertTrue(c.includes(self.rejoined.pk, at(7)))

    def test_dataset_is_anonymized_and_filtered(self):
        users = (self.consented, self.declined, self.withdrawn, self.unanswered)
        rows = [{"user_id": u.pk, "when": when, "transient_id": 7}
                for u in users for when in (datetime.datetime(2025, 6, 1, tzinfo=UTC), at(8))]
        with with_key():
            for user in (self.consented, self.declined, self.unanswered):
                EncryptedProfileValue.objects.create(
                    user=user, field_name="gender", key_fingerprint="synthetic",
                    ciphertext=research_crypto.encrypt_value("prefer_not_to_say", "gender")[0])
        source = rd.ActivitySource("requests", rows, user_key="user_id", time_key="when", fields=("transient_id",))
        data = rd.build_dataset([source], cohort=self.cohort, salt=b"synthetic-salt")
        table = data["tables"]["requests"]
        pseudonym = rd.Pseudonymizer(b"synthetic-salt")
        got = sorted((r["participant"], r["time"][:4]) for r in table)
        expected = sorted([(pseudonym(self.consented.pk), "2025"), (pseudonym(self.consented.pk), "2026"),
                           (pseudonym(self.declined.pk), "2025"), (pseudonym(self.unanswered.pk), "2025"),
                           (pseudonym(self.withdrawn.pk), "2025")])
        self.assertEqual(got, expected)
        for row in table:
            self.assertEqual(set(row), {"participant", "time", "transient_id"})
            self.assertNotIn("ds_", repr(row))  # no usernames
        # Demographics only for the consenting user, as ciphertext.
        self.assertEqual([(d["participant"], d["field"]) for d in data["demographics"]],
                         [(pseudonym(self.consented.pk), "gender")])
        self.assertNotIn("prefer_not_to_say", data["demographics"][0]["ciphertext"])

    def test_sources_may_not_copy_identifying_fields(self):
        for fields in (("username",), ("email",), ("user_id",)):
            with self.assertRaises(ValueError):
                rd.ActivitySource("bad", [], user_key="user_id", time_key="when", fields=fields)


class ConsentAdminTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user("consent_member")
        rc.record_decision(self.member, rc.CONSENTED)

    def test_superusers_read_only(self):
        superuser = User.objects.create_superuser("consent_super", password="x")
        self.client.force_login(superuser)
        url = reverse("admin:YSE_App_researchconsentrecord_changelist")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "consent_member")
        self.assertEqual(self.client.get(reverse("admin:YSE_App_researchconsentrecord_add")).status_code, 403)
        row = ResearchConsentRecord.objects.get()
        delete = reverse("admin:YSE_App_researchconsentrecord_delete", args=[row.pk])
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        self.assertTrue(ResearchConsentRecord.objects.filter(pk=row.pk).exists())

    def test_other_staff_cannot_view(self):
        staff = User.objects.create_user("consent_staff", password="x", is_staff=True)
        from django.contrib.auth.models import Permission
        staff.user_permissions.add(*Permission.objects.filter(codename__endswith="researchconsentrecord"))
        self.client.force_login(staff)
        resp = self.client.get(reverse("admin:YSE_App_researchconsentrecord_changelist"))
        self.assertEqual(resp.status_code, 403)
