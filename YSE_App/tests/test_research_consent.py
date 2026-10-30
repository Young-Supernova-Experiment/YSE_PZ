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
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape
from django.utils import timezone

from YSE_App import onboarding
from YSE_App.models import (
    AffiliationHistory,
    AgreementAcceptance,
    EncryptedProfileValue,
    Mentorship,
    ProfileFieldHistory,
    ResearchConsentForm,
    ResearchConsentPrompt,
    ResearchConsentRecord,
    ResearcherMetrics,
    ResearcherProfile,
    UserAgreement,
)
from YSE_App.models.notification_models import Notification
from YSE_App.services import research_consent as rc
from YSE_App.services import research_crypto
from YSE_App.services import research_dataset as rd
from YSE_App.services import researcher_profile as rp
from YSE_App.tests.test_researcher_profile import with_key

VALID_ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (fictitious researcher)
GATE_ON = dict(ONBOARDING_GATE_ENABLED=True, ONBOARDING_EMAIL_REQUIRED=False, RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")
UTC = datetime.timezone.utc
GO_LIVE_DATE = datetime.date(2026, 1, 1)
GO_LIVE = datetime.datetime(2026, 1, 1, tzinfo=UTC)


def at(month, day=1):
    return datetime.datetime(2026, month, day, 12, tzinfo=UTC)


def complete_profile(user):
    user.first_name, user.last_name = "Synthetic", "Person"
    User.objects.filter(pk=user.pk).update(first_name="Synthetic", last_name="Person")
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
        self.assertEqual(v2.title, "YSE-PZ User Agreement")
        self.assertIn("Use of platform data for research", v1.body)
        self.assertNotIn("Use of platform data for research", v2.body)
        self.assertNotIn("(such as gender)", v2.body)
        self.assertIn("Optional demographic information is stored encrypted.", v2.body)
        self.assertEqual(v1.body.count("<li>"), 9)
        self.assertEqual(v2.body.count("<li>"), 8)
        # Nothing else changed: v1 minus item 5 and minus "(such as gender) " is v2.
        v1_items = [line for line in v1.body.splitlines() if "Use of platform data for research" not in line]
        self.assertEqual("\n".join(v1_items).replace("(such as gender) ", "")
                         .replace(" (cite the YSE-PZ software and PASP papers and include the listed acknowledgment)", ""),
                         v2.body.rstrip("\n"))

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
        complete_profile(self.user)
        # A later agreement version does not offer the consent form again.
        UserAgreement.objects.create(version="3", title="v3", body="<p>Terms.</p>", is_current=True)
        resp = self.accept_agreement()
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)

    def test_consent_needs_the_box_and_records_time_and_version(self):
        resp = self.client.post(reverse("research_consent"), {"action": "consent", "next": self.page})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ResearchConsentRecord.objects.exists())
        before = timezone.now()
        complete_profile(self.user)
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
        complete_profile(self.user)
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        self.assertEqual(ResearchConsentRecord.objects.filter(user=self.user).count(), 1)

    def test_never_a_gate(self):
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        complete_profile(self.user)
        self.assertTrue(rc.prompt_due(self.user))
        self.assertEqual(self.client.get(self.page).status_code, 200)  # unanswered: not redirected
        self.assertEqual(self.client.get(reverse("research_consent")).status_code, 200)

    def test_consent_page_is_exempt_before_onboarding(self):
        # A newcomer who has accepted nothing can still open and answer the form (no loop).
        self.assertEqual(self.client.get(reverse("research_consent")).status_code, 200)
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertEqual(resp.status_code, 302)  # on to the profile (#766), not back to the form

    def test_profile_offers_consent_and_withdrawal(self):
        complete_profile(self.user)
        page = self.client.get(reverse("researcher_profile"))
        self.assertContains(page, "You have not answered the research consent form yet.")
        rc.record_decision(self.user, rc.CONSENTED)
        page = self.client.get(reverse("researcher_profile"))
        self.assertContains(page, "You consented to take part")
        self.assertContains(page, "Withdraw")


class ReminderTests(TestCase):
    """Second request at 10 logins or 30 days, then yearly; never for users who answered."""

    def setUp(self):
        self.user = User.objects.create_user("remind_user", password="x")

    def test_schedule(self):
        first = datetime.datetime(2026, 3, 1, 12, tzinfo=UTC)
        self.assertTrue(rc.prompt_due(self.user, now=first))
        rc.record_prompt(self.user, now=first)
        self.assertFalse(rc.prompt_due(self.user, now=first + datetime.timedelta(days=29)))
        self.assertTrue(rc.prompt_due(self.user, now=first + datetime.timedelta(days=30)))
        for _ in range(9):
            rc.note_login(self.user)
        self.assertFalse(rc.prompt_due(self.user, now=first + datetime.timedelta(days=1)))
        rc.note_login(self.user)
        self.assertTrue(rc.prompt_due(self.user, now=first + datetime.timedelta(days=1)))
        second = first + datetime.timedelta(days=2)
        rc.record_prompt(self.user, now=second)
        self.assertFalse(rc.prompt_due(self.user, now=datetime.datetime(2026, 12, 30, tzinfo=UTC)))
        self.assertTrue(rc.prompt_due(self.user, now=datetime.datetime(2027, 1, 5, tzinfo=UTC)))
        rc.record_prompt(self.user, now=datetime.datetime(2027, 1, 5, tzinfo=UTC))
        self.assertFalse(rc.prompt_due(self.user, now=datetime.datetime(2027, 6, 5, tzinfo=UTC)))

    def test_never_after_an_answer(self):
        rc.record_prompt(self.user, now=at(1))
        rc.record_decision(self.user, rc.DECLINED)
        self.assertFalse(rc.prompt_due(self.user, now=datetime.datetime(2030, 1, 1, tzinfo=UTC)))
        other = User.objects.create_user("remind_consented")
        rc.record_decision(other, rc.CONSENTED)
        self.assertFalse(rc.prompt_due(other))

    @override_settings(**GATE_ON)
    def test_login_flags_one_non_blocking_redirect(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        complete_profile(self.user)
        rc.record_prompt(self.user, now=timezone.now() - datetime.timedelta(days=40))
        page = reverse("notification_list")
        self.client.login(username="remind_user", password="x")
        resp = self.client.get(page)
        self.assertRedirects(resp, "%s?%s" % (reverse("research_consent"), urlencode({"next": page})),
                             fetch_redirect_response=False)
        consent_page = self.client.get(resp["Location"])
        self.assertContains(consent_page, "Not now")
        self.assertEqual(self.client.get(page).status_code, 200)  # once only; nothing recorded
        self.assertFalse(ResearchConsentRecord.objects.filter(user=self.user).exists())
        self.assertEqual(ResearchConsentPrompt.objects.get(user=self.user).prompt_count, 2)

    def test_logins_count(self):
        rc.record_prompt(self.user)
        self.client.login(username="remind_user", password="x")
        self.client.logout()
        self.client.login(username="remind_user", password="x")
        self.assertEqual(ResearchConsentPrompt.objects.get(user=self.user).logins_since_first, 2)


class ResearchFieldsTests(TestCase):
    """Everything but the essential fields is research data, offered and required only with consent."""

    def setUp(self):
        research_crypto.reset_cache()
        self.addCleanup(research_crypto.reset_cache)
        self.user = User.objects.create_user("fields_user", password="x", first_name="Synthetic", last_name="Person")
        self.client.force_login(self.user)

    def post_profile(self, **extra):
        data = dict(first_name="Synthetic", last_name="Person", full_name="Test Person", institution="Example University", department="Physics",
                    career_level="postdoc", orcid=VALID_ORCID, phd_year="2015", pronouns="they/them")
        data.update(extra)
        return self.client.post(reverse("researcher_profile_edit"), data)

    def stored(self):
        return set(EncryptedProfileValue.objects.filter(user=self.user).values_list("field_name", flat=True))

    def test_without_consent_only_essential_fields(self):
        with with_key():
            page = self.client.get(reverse("researcher_profile_edit"))
            self.assertContains(page, 'id="yse-research-fields-locked"')
            for name in ("gender", "country", "phd_year", "career_level", "department", "pronouns",
                         "research_interests", "website", "github_username", "advisor_name"):
                self.assertNotContains(page, 'name="%s"' % name)
            for name in ("full_name", "author_name_format", "institution", "orcid", "acknowledgment_text"):
                self.assertContains(page, 'name="%s"' % name)
            resp = self.client.post(reverse("researcher_profile_edit"), dict(
                first_name="Synthetic", last_name="Person",
                full_name="Test Person", institution="Example University", gender="woman", country="Atlantis",
                career_level="grad", pronouns="they/them"))
            self.assertEqual(resp.status_code, 302)
        profile = ResearcherProfile.objects.get(user=self.user)
        self.assertEqual((profile.career_level, profile.pronouns, profile.department), ("", "", ""))
        self.assertEqual(self.stored(), set())
        self.assertEqual(rp.missing_fields(self.user), [])  # complete without research fields

    def test_consenting_users_are_offered_and_required_the_research_fields(self):
        rc.record_decision(self.user, rc.CONSENTED)
        self.assertEqual(rp.missing_fields(self.user, ResearcherProfile.objects.create(
            user=self.user, full_name="Test Person", institution="Example University"), key_available=False),
            ["department", "career_level"])
        with with_key():
            page = self.client.get(reverse("researcher_profile_edit"))
            self.assertContains(page, 'name="gender"')
            self.post_profile(gender="woman", country="Atlantis")
        self.assertEqual(self.stored(), {"phd_year", "gender", "country"})

    def _store_research_data(self):
        ResearcherProfile.objects.create(user=self.user, full_name="Test Person", institution="Example University",
                                         department="Physics", career_level="grad", pronouns="they/them")
        Mentorship.objects.create(mentee=self.user, mentor_name="Synthetic Advisor",
                                  kind=Mentorship.KIND_CURRENT_ADVISOR)
        with with_key():
            for name, value in (("gender", "man"), ("country", "Atlantis"), ("phd_year", "2015")):
                rp.set_encrypted(self.user, name, value, changed_by=self.user)

    def test_decline_deletes_nothing_and_notifies_admins(self):
        self._store_research_data()
        admin = User.objects.create_superuser("optout_admin", password="x", email="admin@example.com")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("research_consent"), {"action": "decline"})
        self.assertEqual(self.stored(), {"phd_year", "gender", "country"})  # purged by hand later
        note = Notification.objects.get(recipient=admin)
        self.assertEqual(note.kind, "research_optout")
        self.assertIn("fields_user", note.text)
        self.assertIn("purge_unconsented_demographics --user fields_user", note.text)
        self.assertNotIn("Atlantis", note.text)

    @override_settings(RESEARCH_OPTOUT_NOTIFY_USERNAMES=("optout_named",))
    def test_withdraw_notifies_the_configured_admins(self):
        named = User.objects.create_user("optout_named")
        superuser = User.objects.create_superuser("optout_super", password="x")
        rc.record_decision(self.user, rc.CONSENTED)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("research_consent"), {"action": "withdraw"})
        self.assertTrue(Notification.objects.filter(recipient=named, kind="research_optout").exists())
        self.assertFalse(Notification.objects.filter(recipient=superuser).exists())

    def test_unanswered_users_keep_values_and_may_delete_them(self):
        self._store_research_data()
        with with_key():
            self.client.post(reverse("researcher_profile_edit"), dict(
                first_name="Synthetic", last_name="Person",
                full_name="Test Person", institution="Example University", clear_gender="on"))
        self.assertEqual(self.stored(), {"phd_year", "country"})
        self.assertEqual(ResearcherProfile.objects.get(user=self.user).career_level, "grad")  # kept

    def test_metrics_only_for_consenting_users(self):
        from YSE_App.services import research_dataset
        other = User.objects.create_user("fields_consenting")
        rc.record_decision(other, rc.CONSENTED)
        self.assertEqual(research_dataset.consenting_user_ids(), [other.pk])


class PurgeCommandTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        self.addCleanup(research_crypto.reset_cache)
        self.declined = User.objects.create_user("purge_declined")
        self.withdrawn = User.objects.create_user("purge_withdrawn")
        self.unanswered = User.objects.create_user("purge_unanswered")
        self.consenting = User.objects.create_user("purge_consenting")
        rc.record_decision(self.declined, rc.DECLINED)
        rc.record_decision(self.withdrawn, rc.CONSENTED)
        rc.record_decision(self.withdrawn, rc.WITHDRAWN)
        rc.record_decision(self.consenting, rc.CONSENTED)
        self.users = (self.declined, self.withdrawn, self.unanswered, self.consenting)
        for user in self.users:
            ResearcherProfile.objects.create(
                user=user, full_name="Synthetic Person", institution="Example University", orcid=VALID_ORCID,
                author_name_format="Person, S.", department="Physics", career_level="grad", pronouns="they/them",
                research_interests="synthetic interests", website="https://example.org/", github_username="synthetic")
            Mentorship.objects.create(mentee=user, mentor_name="Synthetic Advisor",
                                      kind=Mentorship.KIND_CURRENT_ADVISOR, created_by=user)
            AffiliationHistory.objects.create(user=user, institution="Example College")
            ResearcherMetrics.objects.create(user=user, source="scix", h_index=3)
            with with_key():
                rp.set_encrypted(user, "gender", "prefer_not_to_say", changed_by=user)
                rp.set_encrypted(user, "phd_year", "2010", changed_by=user)

    def run_purge(self, *args):
        out = io.StringIO()
        call_command("purge_unconsented_demographics", *args, stdout=out)
        return out.getvalue()

    def has_research_data(self, user):
        profile = ResearcherProfile.objects.get(user=user)
        return any([EncryptedProfileValue.objects.filter(user=user).exists(), profile.career_level,
                    profile.department, profile.pronouns, profile.research_interests, profile.website,
                    Mentorship.objects.filter(mentee=user).exists(),
                    AffiliationHistory.objects.filter(user=user).exists(),
                    ResearcherMetrics.objects.filter(user=user).exists()])

    def test_dry_run_by_default(self):
        out = self.run_purge()
        self.assertIn("purge_declined: would delete", out)
        self.assertIn("purge_withdrawn: would delete", out)
        self.assertNotIn("purge_unanswered", out)
        self.assertNotIn("purge_consenting", out)
        self.assertIn("Dry run", out)
        self.assertTrue(all(self.has_research_data(u) for u in self.users))

    def test_apply_deletes_research_data_of_decliners_and_withdrawers_only(self):
        self.run_purge("--apply")
        for user in (self.declined, self.withdrawn):
            self.assertFalse(self.has_research_data(user), user.username)
            profile = ResearcherProfile.objects.get(user=user)
            # Essential data are kept.
            self.assertEqual((profile.full_name, profile.institution, profile.orcid, profile.author_name_format),
                             ("Synthetic Person", "Example University", VALID_ORCID, "Person, S."))
            self.assertFalse(ProfileFieldHistory.objects.filter(user=user, field_name="pronouns",
                                                                cleared=False).exists())
        for user in (self.unanswered, self.consenting):
            self.assertTrue(self.has_research_data(user), user.username)
        self.assertIn("Nothing to delete.", self.run_purge("--apply"))  # idempotent

    def test_mentorships_entered_by_the_other_person_are_kept(self):
        # Privacy covers what the person revealed, not information about them (Ryan, 2026-10-20).
        mentor = User.objects.create_user("purge_mentor")
        theirs = Mentorship.objects.create(mentor=mentor, mentee=self.declined, kind=Mentorship.KIND_PHD_ADVISOR,
                                           created_by=mentor)
        own_as_mentor = Mentorship.objects.create(mentor=self.declined, mentee_name="Synthetic Student",
                                                  kind=Mentorship.KIND_RESEARCH_SUPERVISOR, created_by=self.declined)
        self.run_purge("--apply", "--user", "purge_declined")
        self.assertTrue(Mentorship.objects.filter(pk=theirs.pk).exists())
        self.assertFalse(Mentorship.objects.filter(pk=own_as_mentor.pk).exists())
        self.assertFalse(Mentorship.objects.filter(mentee=self.declined, created_by=self.declined).exists())

    def test_observing_experience_is_essential_and_kept(self):
        from YSE_App.models import Instrument, Observatory, Telescope
        audit = {"created_by": self.consenting, "modified_by": self.consenting}
        obs = Observatory.objects.create(name="Synthetic Observatory", utc_offset=0, tz_name="UTC", **audit)
        tel = Telescope.objects.create(name="Synthetic Scope", observatory=obs, latitude=0.0, longitude=0.0,
                                       elevation=0.0, **audit)
        inst = Instrument.objects.create(name="SynthCam", telescope=tel, **audit)
        profile = ResearcherProfile.objects.get(user=self.declined)
        profile.observing_experience.add(inst)
        self.run_purge("--apply", "--user", "purge_declined")
        self.assertEqual(list(profile.observing_experience.all()), [inst])

    def test_user_option(self):
        self.run_purge("--apply", "--user", "purge_declined")
        self.assertFalse(self.has_research_data(self.declined))
        self.assertTrue(self.has_research_data(self.withdrawn))
        for name in ("purge_consenting", "purge_unanswered", "no_such_user"):
            with self.assertRaises(CommandError):
                self.run_purge("--apply", "--user", name)
        self.assertTrue(self.has_research_data(self.consenting))


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
        pseudonym = rd.Pseudonymizer()
        data = rd.build_dataset([source], cohort=self.cohort, pseudonym=pseudonym)
        table = data["tables"]["requests"]
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


class ResearchExportTests(TestCase):
    """The anonymized SQLite export (#746), with synthetic users and go-live fixed at 2026-01-01."""

    def setUp(self):
        import tempfile
        from YSE_App.models import Log
        from YSE_App.tests.fixtures_minimal import create_minimal_transient
        research_crypto.reset_cache()
        self.addCleanup(research_crypto.reset_cache)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        ResearchConsentForm.objects.update(effective_date=GO_LIVE_DATE)
        people = (("exp_alice", "Zebulon", "Quixote"), ("exp_bob", "Xanthippe", "Vandermolen"),
                  ("exp_carol", "Wolfram", "Ulbrichtova"), ("exp_dave", "Thaddeus", "Smorgasbord"))
        self.users = {}
        for username, first, last in people:
            user = User.objects.create_user(username, first_name=first, last_name=last,
                                            email="%s@example.com" % username)
            ResearcherProfile.objects.create(
                user=user, full_name="%s %s" % (first, last), institution="Synthetic Institute of %s" % last,
                author_name_format="%s, %s." % (last, first[0]), career_level="postdoc", department="Physics",
                orcid=VALID_ORCID if username == "exp_alice" else "", website="https://%s.example.org/" % username)
            self.users[username] = user
        alice, bob, carol, dave = (self.users[n] for n in ("exp_alice", "exp_bob", "exp_carol", "exp_dave"))
        rc.record_decision(alice, rc.CONSENTED, now=at(3))
        rc.record_decision(bob, rc.DECLINED, now=at(3))
        rc.record_decision(carol, rc.CONSENTED, now=at(2))
        rc.record_decision(carol, rc.WITHDRAWN, now=at(4))
        with with_key():
            for user in (alice, bob):
                rp.set_encrypted(user, "gender", "prefer_not_to_say", changed_by=user)
        transient = create_minimal_transient(alice, name="2026synthexp")
        text = ("Thanks @exp_bob and Zebulon Quixote (zq@example.com, 0000-0002-1825-0097); "
                "Vandermolen agrees. exp_dave too.")
        for user in (alice, bob, carol, dave):
            for when in (datetime.datetime(2025, 6, 1, tzinfo=UTC), at(5)):
                row = Log.objects.create(transient=transient, comment=text, created_by=user, modified_by=user)
                Log.objects.filter(pk=row.pk).update(created_date=when)

    def export(self, name):
        import os
        import sqlite3
        path = os.path.join(self.tmp, name)
        call_command("export_research_database", "--out", path, stdout=io.StringIO())
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_inclusion_rules(self):
        conn = self.export("a.sqlite")
        comments = conn.execute("SELECT participant, time FROM comments").fetchall()
        self.assertEqual(sum(1 for _p, t in comments if t.startswith("2025")), 4)  # everyone, before go-live
        late = [p for p, t in comments if t.startswith("2026")]
        self.assertEqual(len(late), 1)  # only the consenting user after go-live
        profiles = dict(conn.execute("SELECT participant, research_profile FROM participants").fetchall())
        self.assertEqual(sorted(profiles.values()), [0, 0, 0, 1])
        self.assertEqual(profiles[late[0]], 1)
        demographics = conn.execute("SELECT participant, field FROM demographics").fetchall()
        self.assertEqual(demographics, [(late[0], "gender")])

    def test_no_identifiers_anywhere(self):
        conn = self.export("b.sqlite")
        dump = "\n".join(str(v) for line in conn.iterdump() for v in [line]).lower()
        for user in self.users.values():
            profile = user.researcher_profile
            for value in (user.username, user.first_name, user.last_name, user.email, profile.full_name,
                          profile.author_name_format, profile.institution, profile.website):
                self.assertNotIn(value.lower(), dump, value)
        self.assertNotIn(VALID_ORCID, dump)
        self.assertNotIn("zq@example.com", dump)
        self.assertNotIn("prefer_not_to_say", dump)  # ciphertext only
        self.assertIn("2026synthexp", dump)  # public transient names are kept
        import re
        text = conn.execute("SELECT comment FROM comments LIMIT 1").fetchone()[0]
        # Every person named in the text is replaced by their pseudonym for this export.
        named = set(re.findall(r"p_[0-9a-f]{20}", text))
        participants = {r[0] for r in conn.execute("SELECT participant FROM participants")}
        self.assertEqual(len(named), 3, text)  # alice (name, ORCID), bob (@mention, surname), dave
        self.assertTrue(named <= participants, text)
        self.assertIn("[email]", text)  # an address of nobody known
        self.assertTrue(text.startswith("Thanks @p_"), text)

    def test_value_changes_follow_the_same_rules(self):
        # Experimental's change log (#737): only people's changes, under the same inclusion rules,
        # with names in the values and note replaced (backport of #746).
        from YSE_App.models import Transient, ValueChange
        transient = Transient.objects.get(name="2026synthexp")
        alice, bob = self.users["exp_alice"], self.users["exp_bob"]
        for actor in (alice, bob, None):
            ValueChange.objects.create(transient=transient, category="redshift", field="redshift",
                                       old_value="0.1", new_value="0.2", service="manual", actor=actor,
                                       note="checked with Vandermolen", created_at=at(5))
        conn = self.export("v.sqlite")
        rows = conn.execute("SELECT participant, transient_id, new_value, note FROM value_changes "
                            "WHERE note LIKE 'checked with %'").fetchall()
        self.assertEqual(len(rows), 1, rows)  # alice consented; bob declined; the system row has no person
        participant, transient_id, new_value, note = rows[0]
        self.assertTrue(participant.startswith("p_"))
        self.assertEqual((transient_id, new_value), (transient.pk, "0.2"))
        self.assertNotIn("Vandermolen", note)
        self.assertRegex(note, r"p_[0-9a-f]{20}")

    def test_two_exports_share_no_identifiers(self):
        first, second = self.export("c1.sqlite"), self.export("c2.sqlite")
        for table, column in (("participants", "participant"), ("comments", "id"), ("comments", "participant")):
            a = {r[0] for r in first.execute('SELECT "%s" FROM "%s"' % (column, table))}
            b = {r[0] for r in second.execute('SELECT "%s" FROM "%s"' % (column, table))}
            self.assertTrue(a and b)
            self.assertFalse(a & b, (table, column))

    def test_refuses_to_overwrite(self):
        import os
        path = os.path.join(self.tmp, "d.sqlite")
        open(path, "w").close()
        with self.assertRaises(CommandError):
            call_command("export_research_database", "--out", path, stdout=io.StringIO())
        call_command("export_research_database", "--out", path, "--force", stdout=io.StringIO())

    def test_no_credential_or_session_tables(self):
        conn = self.export("e.sqlite")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for bad in ("auth_user", "django_session", "authtoken_token", "YSE_App_encryptedcredential"):
            self.assertNotIn(bad, names)
        self.assertIn("comments", names)


class NameReplacerTests(TestCase):
    """Whole-word, case-insensitive, longest-first replacement of people in free text (#746)."""

    def setUp(self):
        from YSE_App.services.research_export import NameReplacer
        self.replace = NameReplacer([
            (1, {"direct": ["wli", "wei.li@example.com", "Wei Li", "Li, W.", VALID_ORCID],
                 "names": ["Wei", "Li", "Wei Li", "Li, W."]}),
            (2, {"direct": ["jsj", "Jo Smith-Jones"], "names": ["Jo", "Smith-Jones"]}),
            (3, {"direct": ["kng"], "names": ["Kim", "Ng"]}),
            (4, {"direct": ["kim2"], "names": ["Kim", "Park"]}),
        ])

    def run_replace(self, text):
        return self.replace(text, lambda uid: "P%d" % uid, lambda key: "AMBIG")

    def test_two_letter_names_only_as_whole_words(self):
        self.assertEqual(self.run_replace("Li said: literally Literally, Lithium, oLi, Li2."),
                         "P1 said: literally Literally, Lithium, oLi, Li2.")
        self.assertEqual(self.run_replace("(Li) -Li- @Li li! LI? Ng's"), "(P1) -P1- @P1 P1! P1? P3's")

    def test_direct_matches_first_and_longest(self):
        self.assertEqual(self.run_replace("Thanks @wli and WEI LI (wei.li@example.com, %s)" % VALID_ORCID),
                         "Thanks @P1 and P1 (P1, P1)")
        self.assertEqual(self.run_replace("Li, W. 2024"), "P1 2024")
        self.assertEqual(self.run_replace("Jo Smith-Jones; Smith-Jones; Jones; Smith"), "P2; P2; P2; P2")

    def test_shared_names_and_unknown_identifiers(self):
        self.assertEqual(self.run_replace("Kim and Park"), "AMBIG and P4")
        self.assertEqual(self.run_replace("mail x@example.org, orcid 0000-0001-5109-3700"),
                         "mail [email], orcid [orcid]")


class WordingEditTests(TestCase):
    """#766: titles without version labels, wording edits in place, nobody re-accepts."""

    def test_seeded_text(self):
        agreement = UserAgreement.objects.get(version="2")
        self.assertEqual(agreement.title, "YSE-PZ User Agreement")
        self.assertNotIn("(cite the YSE-PZ software", agreement.body)
        self.assertIn("Acknowledgements page</a>, and follow any acknowledgment", agreement.body)
        form = rc.current_form()
        self.assertEqual(form.title, "YSE-PZ Research Consent Form")
        self.assertIn("basic demographic information such as career stage and country to track science outcomes",
                      form.body)

    def test_migration_edits_old_text_in_place_and_is_idempotent(self):
        migration = importlib.import_module("YSE_App.migrations.0042_agreement_consent_wording")
        agreement = UserAgreement.objects.get(version="2")
        form = ResearchConsentForm.objects.get(version="1")
        user = User.objects.create_user("wording_user", password="x")
        acceptance = AgreementAcceptance.objects.create(user=user, agreement=agreement)
        record = rc.record_decision(user, rc.CONSENTED, form=form)
        # Put back the pre-#766 text.
        UserAgreement.objects.filter(pk=agreement.pk).update(
            title="YSE-PZ User Agreement (v2)",
            body=agreement.body.replace("</a>, and follow", "</a>" + migration.AGREEMENT_BODY_OLD + ", and follow"))
        ResearchConsentForm.objects.filter(pk=form.pk).update(
            title="YSE-PZ Research Consent Form (v1)",
            body=form.body.replace("such as career stage and country ", ""))
        for _ in range(2):
            migration.apply_edits(django_apps, None)
            agreement.refresh_from_db()
            form.refresh_from_db()
            self.assertEqual(agreement.title, "YSE-PZ User Agreement")
            self.assertNotIn("(cite", agreement.body)
            self.assertIn("Acknowledgements page</a>, and follow", agreement.body)
            self.assertEqual(agreement.body.count("<li>"), 8)
            self.assertEqual(form.title, "YSE-PZ Research Consent Form")
            self.assertIn("demographic information such as career stage and country to track", form.body)
        self.assertEqual(agreement.version, "2")
        self.assertEqual(form.version, "1")
        self.assertTrue(AgreementAcceptance.objects.filter(pk=acceptance.pk, agreement=agreement).exists())
        self.assertTrue(ResearchConsentRecord.objects.filter(pk=record.pk, form=form).exists())

    @override_settings(**GATE_ON)
    def test_pages_show_title_only_and_old_acceptors_are_not_asked_again(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        user = User.objects.create_user("wording_page_user", password="x")
        AgreementAcceptance.objects.create(user=user, agreement=UserAgreement.objects.get(version="2"))
        complete_profile(user)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("notification_list")).status_code, 200)
        page = self.client.get(reverse("agreement") + "?next=")
        self.assertContains(page, "YSE-PZ User Agreement")
        self.assertNotContains(page, "version 2")
        self.assertNotContains(page, "(v2)")
        page = self.client.get(reverse("research_consent"))
        self.assertContains(page, "Optional research consent")
        self.assertContains(page, "YSE-PZ Research Consent Form")
        self.assertNotContains(page, "(voluntary)")
        self.assertNotContains(page, "version 1")
        self.assertNotContains(page, "(v1)")

    @override_settings(**GATE_ON)
    def test_acceptance_message_has_no_version(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        user = User.objects.create_user("wording_msg_user", password="x")
        self.client.force_login(user)
        resp = self.client.post(reverse("agreement"), {"agree": "on", "next": reverse("notification_list")}, follow=True)
        texts = [str(m) for m in resp.context["messages"]]
        self.assertIn("Thank you: you accepted the YSE-PZ User Agreement.", texts)


@override_settings(**GATE_ON)
class ConsentToProfileFlowTests(TestCase):
    """#766: Consent, Decline and Not now continue to the profile while it is incomplete."""

    def setUp(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        self.user = User.objects.create_user("consent_profile_user", password="x")
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        self.client.force_login(self.user)
        self.page = reverse("notification_list")
        self.edit = "%s?%s" % (reverse("researcher_profile_edit"), urlencode({"next": self.page}))

    def test_decline_and_consent_continue_to_profile_when_incomplete(self):
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertRedirects(resp, self.edit, fetch_redirect_response=False)
        self.assertEqual(rc.status(self.user), rc.DECLINED)
        # The profile editor opens (no loop back to the gate or the form).
        self.assertEqual(self.client.get(resp["Location"]).status_code, 200)
        # Not now: the link on the form itself.
        page = self.client.get(reverse("research_consent") + "?" + urlencode({"next": self.page}))
        self.assertContains(page, 'id="yse-research-consent-continue"')
        self.assertContains(page, escape(self.edit))

    def test_consent_and_not_now_continue_to_profile_when_incomplete(self):
        page = self.client.get(reverse("research_consent") + "?" + urlencode({"next": self.page}))
        self.assertContains(page, escape(self.edit))  # Not now
        resp = self.client.post(reverse("research_consent"), {"action": "consent", "consent": "on",
                                                              "next": self.page})
        self.assertRedirects(resp, self.edit, fetch_redirect_response=False)

    def test_complete_profile_continues_to_next(self):
        complete_profile(self.user)
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)

    def test_next_already_the_profile_editor_is_not_nested(self):
        edit_only = reverse("researcher_profile_edit")
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": edit_only})
        self.assertRedirects(resp, edit_only, fetch_redirect_response=False)

    def test_agreement_to_consent_to_profile_to_next(self):
        user = User.objects.create_user("consent_chain_user", password="x")
        self.client.force_login(user)
        resp = self.client.post(reverse("agreement"), {"agree": "on", "next": self.page})
        self.assertRedirects(resp, "%s?%s" % (reverse("research_consent"), urlencode({"next": self.page})),
                             fetch_redirect_response=False)
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        self.assertRedirects(resp, self.edit, fetch_redirect_response=False)
