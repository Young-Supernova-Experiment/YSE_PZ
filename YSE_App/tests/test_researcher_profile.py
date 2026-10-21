"""Researcher profile, encrypted fields, field history, mentorship, metrics, Explorer exclusion (#627-#629).

Every name and value here is synthetic.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from unittest import mock

from cryptography.hazmat.primitives import serialization
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from YSE_App.models import (
    EncryptedCredential,
    EncryptedProfileValue,
    Mentorship,
    ProfileFieldHistory,
    ResearcherMetrics,
    ResearcherProfile,
)
from YSE_App.models.researcher_models import (
    AffiliationHistory,
    AgreementAcceptance,
    ResearchConsentForm,
    ResearchConsentPrompt,
    ResearchConsentRecord,
    UserAgreement,
)
from YSE_App.models import Instrument, Observatory, Telescope
from YSE_App.services import research_crypto
from YSE_App.services import researcher_metrics
from YSE_App.services import researcher_profile as rp

VALID_ORCIDS = ("0000-0002-1825-0097", "0000-0001-5109-3700", "0000-0002-1694-233X")
NO_KEY = dict(RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")

_KEYPAIR = {}


def test_keypair():
    """One throwaway 2048-bit key pair for the whole module (generated, never stored)."""
    if not _KEYPAIR:
        private_pem, public_pem, fingerprint = research_crypto.generate_keypair(bits=2048)
        _KEYPAIR.update(private=serialization.load_pem_private_key(private_pem, password=None),
                        public_pem=public_pem.decode("ascii"), fingerprint=fingerprint)
    return _KEYPAIR


def with_key():
    return override_settings(RESEARCH_PUBLIC_KEY=test_keypair()["public_pem"], RESEARCH_PUBLIC_KEY_PATH="")


def consent(*users):
    """Research fields are offered and required only with research consent (#744)."""
    from YSE_App.services import research_consent
    for user in users:
        research_consent.record_decision(user, research_consent.CONSENTED)


BASE_POST = {"full_name": "Test Person", "institution": "Example University", "department": "Physics"}


class OrcidAndRuleTests(TestCase):
    def test_orcid_checksum(self):
        for orcid in VALID_ORCIDS:
            self.assertTrue(rp.orcid_checksum_ok(orcid), orcid)
        self.assertFalse(rp.orcid_checksum_ok("0000-0002-1825-0098"))
        self.assertFalse(rp.orcid_checksum_ok("0000-0002-1825-009"))
        self.assertFalse(rp.orcid_checksum_ok("0000000218250097"))
        self.assertEqual(rp.normalize_orcid("https://orcid.org/0000-0002-1825-0097"), "0000-0002-1825-0097")
        self.assertEqual(rp.normalize_orcid("000000021694233x"), "0000-0002-1694-233X")

    def test_required_fields_per_career_level(self):
        everyone = ["full_name", "institution", "department", "career_level"]
        self.assertEqual(rp.required_fields("undergrad", True), everyone + ["current_advisor"])
        self.assertEqual(rp.required_fields("grad", False), everyone + ["current_advisor"])
        for level in ("postdoc", "scientific_staff", "tenure_track_untenured", "tenured"):
            self.assertEqual(rp.required_fields(level, True), everyone + ["orcid", "phd_year"], level)
            # Without a public key the encrypted PhD year is waived.
            self.assertEqual(rp.required_fields(level, False), everyone + ["orcid"], level)
        self.assertEqual(rp.required_fields("other", True), everyone + ["career_level_other"])

    def test_missing_fields(self):
        user = User.objects.create_user("rule_user")
        self.assertEqual(rp.missing_fields(user, key_available=False), ["profile"])
        profile = ResearcherProfile.objects.create(user=user, career_level="grad", **BASE_POST)
        # Without research consent only the essential fields are required (#744).
        self.assertEqual(rp.missing_fields(user, profile, key_available=True), [])
        self.assertEqual(rp.required_fields("grad", True, research=False), ["full_name", "institution"])
        consent(user)
        self.assertEqual(rp.missing_fields(user, profile, key_available=False), ["current_advisor"])
        Mentorship.objects.create(mentee=user, mentor_name="Free Form Advisor", kind=Mentorship.KIND_CURRENT_ADVISOR)
        self.assertEqual(rp.missing_fields(user, profile, key_available=False), [])
        profile.career_level = "postdoc"
        profile.orcid = "0000-0002-1825-0098"  # bad checksum
        self.assertEqual(rp.missing_fields(user, profile, key_available=True), ["orcid", "phd_year"])
        profile.orcid = VALID_ORCIDS[0]
        self.assertEqual(rp.missing_fields(user, profile, key_available=False), [])
        profile.career_level = "other"
        self.assertEqual(rp.missing_fields(user, profile, key_available=False), ["career_level_other"])


class EncryptionTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        self.user = User.objects.create_user("crypto_user", password="x")
        consent(self.user)
        self.client.force_login(self.user)

    def tearDown(self):
        research_crypto.reset_cache()

    def test_round_trip_and_not_plaintext(self):
        kp = test_keypair()
        with with_key():
            self.assertTrue(research_crypto.key_available())
            self.assertEqual(research_crypto.current_fingerprint(), kp["fingerprint"])
            ciphertext, fingerprint = research_crypto.encrypt_value("prefer_not_to_say", "gender")
        self.assertEqual(fingerprint, kp["fingerprint"])
        self.assertNotIn("prefer_not_to_say", ciphertext)
        self.assertEqual(research_crypto.decrypt_value(ciphertext, "gender", kp["private"]), "prefer_not_to_say")
        with self.assertRaises(Exception):  # bound to its field name
            research_crypto.decrypt_value(ciphertext, "country", kp["private"])

    def test_profile_form_encrypts_validated_values(self):
        kp = test_keypair()
        with with_key():
            resp = self.client.post(reverse("researcher_profile_edit"), dict(
                BASE_POST, career_level="postdoc", orcid=VALID_ORCIDS[1], phd_year="2015",
                gender="self_describe", gender_self="synthetic description", country="Atlantis"))
            self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        rows = {r.field_name: r for r in EncryptedProfileValue.objects.filter(user=self.user)}
        self.assertEqual(set(rows), {"phd_year", "gender", "country"})
        for name, plain in (("phd_year", "2015"), ("gender", "self_describe:synthetic description"),
                            ("country", "Atlantis")):
            self.assertNotIn(plain, rows[name].ciphertext)
            self.assertEqual(rows[name].key_fingerprint, kp["fingerprint"])
            self.assertEqual(research_crypto.decrypt_value(rows[name].ciphertext, name, kp["private"]), plain)
        self.assertEqual(rp.missing_fields(self.user, key_available=True), [])
        # The view, the admin and the history show only "(encrypted, set on <date>)".
        with with_key():
            page = self.client.get(reverse("researcher_profile"))
        self.assertContains(page, "(encrypted, set on ")
        for plain in ("2015", "Atlantis", "synthetic description"):
            self.assertNotContains(page, plain)
        history = ProfileFieldHistory.objects.filter(user=self.user, field_name="phd_year").get()
        self.assertEqual(history.value_plain, "")
        self.assertEqual(history.encrypted_value_id, rows["phd_year"].pk)

    def test_phd_year_is_validated_before_encryption(self):
        with with_key():
            for bad in ("1900", "3000"):
                resp = self.client.post(reverse("researcher_profile_edit"), dict(
                    BASE_POST, career_level="tenured", orcid=VALID_ORCIDS[0], phd_year=bad))
                self.assertEqual(resp.status_code, 200)
                self.assertIn("phd_year", resp.context["form"].errors)
            # Required for postdocs and above when a key is installed.
            resp = self.client.post(reverse("researcher_profile_edit"), dict(
                BASE_POST, career_level="tenured", orcid=VALID_ORCIDS[0]))
            self.assertIn("phd_year", resp.context["form"].errors)
        self.assertFalse(EncryptedProfileValue.objects.exists())
        self.assertFalse(ResearcherProfile.objects.filter(user=self.user).exists())

    def test_clearing_an_encrypted_field_deletes_its_ciphertexts(self):
        with with_key():
            rp.set_encrypted(self.user, "gender", "woman", changed_by=self.user)
            rp.set_encrypted(self.user, "gender", "prefer_not_to_say", changed_by=self.user)
            self.assertEqual(EncryptedProfileValue.objects.filter(user=self.user).count(), 2)
            self.assertEqual(rp.clear_encrypted(self.user, "gender", changed_by=self.user), 2)
        self.assertFalse(EncryptedProfileValue.objects.filter(user=self.user).exists())
        last = ProfileFieldHistory.objects.filter(user=self.user, field_name="gender").first()
        self.assertTrue(last.cleared)

    @override_settings(**NO_KEY)
    def test_no_key_waives_encrypted_fields_and_nothing_breaks(self):
        self.assertFalse(research_crypto.key_available())
        with self.assertRaises(research_crypto.ResearchKeyError):
            research_crypto.encrypt_value("x", "gender")
        page = self.client.get(reverse("researcher_profile_edit"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "not available yet")
        # A tenured user without a PhD year is complete; posted encrypted values are ignored.
        resp = self.client.post(reverse("researcher_profile_edit"), dict(
            BASE_POST, career_level="tenured", orcid=VALID_ORCIDS[0], phd_year="2010", gender="man"))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(EncryptedProfileValue.objects.exists())
        self.assertEqual(rp.missing_fields(self.user), [])
        self.assertIsNotNone(ResearcherProfile.objects.get(user=self.user).profile_completed_at)
        self.assertContains(self.client.get(reverse("researcher_profile")), "not available yet")

    @override_settings(**NO_KEY)
    def test_admin_index_warns_staff_without_a_key(self):
        admin = User.objects.create_superuser("crypto_admin", password="x")
        self.client.force_login(admin)
        resp = self.client.get(reverse("admin:index"))
        self.assertContains(resp, "No research public key is installed")
        with with_key():
            research_crypto.reset_cache()
            self.assertNotContains(self.client.get(reverse("admin:index")), "No research public key is installed")

    def test_unreadable_key_file_counts_as_not_configured(self):
        with override_settings(RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="/nonexistent/research.pem"):
            self.assertFalse(research_crypto.key_available())
        with override_settings(RESEARCH_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----"):
            self.assertFalse(research_crypto.key_available())

    def test_generate_research_keypair_command(self):
        out_dir = tempfile.mkdtemp()
        try:
            out = io.StringIO()
            call_command("generate_research_keypair", "--out-dir", out_dir, "--bits", "2048", stdout=out)
            public_path = os.path.join(out_dir, "research_public_key.pem")
            private_path = os.path.join(out_dir, "research_private_key.pem")
            self.assertEqual(os.stat(private_path).st_mode & 0o777, 0o600)
            with override_settings(RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH=public_path):
                research_crypto.reset_cache()
                self.assertTrue(research_crypto.key_available())
                self.assertIn("Fingerprint: %s" % research_crypto.current_fingerprint(), out.getvalue())
        finally:
            shutil.rmtree(out_dir)


@override_settings(**NO_KEY)
class HistoryTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        self.user = User.objects.create_user("history_user", password="x")
        consent(self.user)
        self.client.force_login(self.user)

    def post(self, **fields):
        data = dict(BASE_POST, career_level="tenured", orcid=VALID_ORCIDS[0])
        data.update(fields)
        return self.client.post(reverse("researcher_profile_edit"), data)

    def test_each_change_is_recorded(self):
        self.assertEqual(self.post(pronouns="they/them").status_code, 302)
        names = set(ProfileFieldHistory.objects.filter(user=self.user).values_list("field_name", flat=True))
        self.assertTrue({"full_name", "institution", "department", "career_level", "orcid", "pronouns"} <= names)
        before = ProfileFieldHistory.objects.filter(user=self.user).count()
        self.post(pronouns="they/them")  # unchanged: no new rows
        self.assertEqual(ProfileFieldHistory.objects.filter(user=self.user).count(), before)
        self.post(pronouns="they/them", institution="Other Institute")
        latest = ProfileFieldHistory.objects.filter(user=self.user, field_name="institution").first()
        self.assertEqual(latest.value_plain, "Other Institute")
        self.assertEqual(latest.changed_by, self.user)
        self.assertIsNotNone(latest.changed_at)

    def test_clearing_an_optional_field_blanks_its_old_values(self):
        self.post(pronouns="they/them")
        self.post(pronouns="")
        rows = list(ProfileFieldHistory.objects.filter(user=self.user, field_name="pronouns"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.value_plain == "" for r in rows))
        self.assertTrue(rows[0].cleared)


@override_settings(**NO_KEY)
class MentorshipTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        self.mentor = User.objects.create_user("mentor_user", password="x", first_name="Mentor", last_name="Person")
        ResearcherProfile.objects.create(user=self.mentor, career_level="tenure_track_untenured",
                                         orcid=VALID_ORCIDS[2], **BASE_POST)
        self.student = User.objects.create_user("student_user", password="x")
        consent(self.mentor, self.student)

    def test_constraint_requires_both_sides(self):
        with self.assertRaises(ValidationError):
            Mentorship(kind=Mentorship.KIND_PHD_ADVISOR, mentee=self.student).clean()
        with self.assertRaises(ValidationError):
            Mentorship(kind=Mentorship.KIND_PHD_ADVISOR, mentor=self.mentor).clean()
        Mentorship(kind=Mentorship.KIND_PHD_ADVISOR, mentor_name="A Name", mentee_name="B Name").clean()
        row = Mentorship.objects.create(kind=Mentorship.KIND_PHD_ADVISOR, mentor=self.mentor, mentee=self.student)
        self.assertEqual(row.mentor_name, "Test Person")  # kept so the row survives an account deletion

    def test_student_names_advisor_and_appears_in_my_students(self):
        self.client.force_login(self.student)
        resp = self.client.post(reverse("researcher_profile_edit"), dict(
            BASE_POST, career_level="grad", advisor_name="Test Person (mentor_user)",
            advisor_user_id=str(self.mentor.pk)))
        self.assertEqual(resp.status_code, 302)
        advisor = rp.current_advisor(self.student)
        self.assertEqual(advisor.mentor, self.mentor)
        self.assertEqual([m.mentee for m in rp.my_students(self.mentor)], [self.student])
        # Switching to a free-form advisor ends the previous row.
        self.client.post(reverse("researcher_profile_edit"), dict(
            BASE_POST, career_level="grad", advisor_name="Unlisted Advisor", advisor_user_id=""))
        self.assertEqual(rp.current_advisor(self.student).mentor_name, "Unlisted Advisor")
        self.assertIsNotNone(Mentorship.objects.get(mentor=self.mentor).end_year)

    def test_student_without_advisor_is_refused(self):
        self.client.force_login(self.student)
        resp = self.client.post(reverse("researcher_profile_edit"), dict(BASE_POST, career_level="undergrad"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("advisor_name", resp.context["form"].errors)

    def test_mentor_manages_students_inline(self):
        self.client.force_login(self.mentor)
        resp = self.client.post(reverse("researcher_student_add"), {
            "student_name": "Free Form Student", "kind": Mentorship.KIND_RESEARCH_SUPERVISOR,
            "start_year": "2024", "end_year": ""})
        self.assertEqual(resp.status_code, 302)
        self.client.post(reverse("researcher_student_add"), {
            "student_name": "x", "student_user_id": str(self.student.pk), "kind": Mentorship.KIND_PHD_ADVISOR,
            "start_year": "2023"})
        students = list(rp.my_students(self.mentor))
        self.assertEqual(len(students), 2)
        page = self.client.get(reverse("researcher_profile"))
        self.assertContains(page, "My students")
        self.assertContains(page, "Free Form Student")
        free = Mentorship.objects.get(mentee_name="Free Form Student")
        self.client.post(reverse("researcher_student_update", args=[free.pk]), {"end_year": "2026"})
        free.refresh_from_db()
        self.assertEqual(free.end_year, 2026)
        self.client.post(reverse("researcher_student_update", args=[free.pk]), {"remove": "1"})
        self.assertFalse(Mentorship.objects.filter(pk=free.pk).exists())

    def test_non_mentors_cannot_manage_students(self):
        ResearcherProfile.objects.create(user=self.student, career_level="grad", **BASE_POST)
        row = Mentorship.objects.create(kind=Mentorship.KIND_PHD_ADVISOR, mentor=self.mentor, mentee_name="Someone")
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(reverse("researcher_student_add"),
                                          {"student_name": "X", "kind": "phd_advisor"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("researcher_student_update", args=[row.pk]),
                                          {"remove": "1"}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("researcher_profile")), "My students")
        # A mentor can only edit their own rows.
        other = User.objects.create_user("other_mentor", password="x")
        ResearcherProfile.objects.create(user=other, career_level="tenured", orcid=VALID_ORCIDS[0], **BASE_POST)
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("researcher_student_update", args=[row.pk]),
                                          {"remove": "1"}).status_code, 404)

    def test_advisor_suggest(self):
        inactive = User.objects.create_user("mentor_inactive", password="x", is_active=False)
        self.client.force_login(self.student)
        data = self.client.get(reverse("researcher_advisor_suggest"), {"q": "mentor"}).json()["results"]
        self.assertEqual([r["id"] for r in data], [self.mentor.pk])
        self.assertNotIn(inactive.pk, [r["id"] for r in data])
        self.assertEqual(self.client.get(reverse("researcher_advisor_suggest"), {"q": "m"}).json()["results"], [])

    def test_affiliation_history_model(self):
        AffiliationHistory.objects.create(user=self.mentor, institution="Example University", start_year=2020)
        self.assertEqual(self.mentor.affiliation_history.count(), 1)


class UserSuggestEndpointTests(TestCase):
    """Type-ahead user search (#638): minimal fields, login required, limited and throttled."""

    def setUp(self):
        cache.clear()
        research_crypto.reset_cache()
        self.me = User.objects.create_user("suggest_me", password="x", first_name="Searcher")
        self.url = reverse("researcher_advisor_suggest")

    def tearDown(self):
        cache.clear()

    def _user(self, username, first, last, institution="Example University", **kw):
        user = User.objects.create_user(username, password="x", first_name=first, last_name=last,
                                        email="%s@example.invalid" % username, **kw)
        ResearcherProfile.objects.create(user=user, full_name="%s %s" % (first, last), institution=institution)
        return user

    def test_returns_only_name_and_institution(self):
        target = self._user("zed_user", "Zedorf", "Synthetic")
        self.client.force_login(self.me)
        resp = self.client.get(self.url, {"q": "zedo"})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"id", "name", "institution", "label"})
        self.assertEqual(rows[0]["id"], target.pk)
        self.assertEqual(rows[0]["name"], "Zedorf Synthetic")
        self.assertEqual(rows[0]["institution"], "Example University")
        body = resp.content.decode()
        self.assertNotIn("example.invalid", body)
        self.assertNotIn("zed_user", body)  # no username either

    def test_username_is_not_searchable_and_inactive_and_self_excluded(self):
        self._user("hidden_handle", "Quentin", "Plain")
        self._user("gone_user", "Quentin", "Gone", is_active=False)
        self.client.force_login(self.me)
        self.assertEqual(self.client.get(self.url, {"q": "hidden_handle"}).json()["results"], [])
        names = [r["name"] for r in self.client.get(self.url, {"q": "Quentin"}).json()["results"]]
        self.assertEqual(names, ["Quentin Plain"])
        self.assertEqual(self.client.get(self.url, {"q": "Searcher"}).json()["results"], [])

    def test_anonymous_gets_no_data(self):
        self._user("anon_target", "Anontarget", "Person")
        resp = self.client.get(self.url, {"q": "Anontarget"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])
        self.assertNotIn("Anontarget", resp.content.decode())

    def test_minimum_two_characters(self):
        self._user("short_q", "Bartholomew", "Synthetic")
        self.client.force_login(self.me)
        self.assertEqual(self.client.get(self.url, {"q": "b"}).json()["results"], [])
        self.assertEqual(self.client.get(self.url, {"q": " b "}).json()["results"], [])
        self.assertEqual(self.client.get(self.url).json()["results"], [])
        self.assertEqual(len(self.client.get(self.url, {"q": "ba"}).json()["results"]), 1)

    def test_at_most_ten_results_and_unique_labels(self):
        for i in range(14):
            self._user("many_%d" % i, "Manyname", "Same")
        self.client.force_login(self.me)
        rows = self.client.get(self.url, {"q": "Manyname"}).json()["results"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({r["label"] for r in rows}), 10)

    def test_post_refused(self):
        self.client.force_login(self.me)
        self.assertEqual(self.client.post(self.url, {"q": "ab"}).status_code, 405)

    def test_throttled_per_user(self):
        from YSE_App import profile_views
        self.client.force_login(self.me)
        for _ in range(profile_views.SUGGEST_THROTTLE_REQUESTS):
            self.assertEqual(self.client.get(self.url, {"q": "ab"}).status_code, 200)
        resp = self.client.get(self.url, {"q": "ab"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["results"], [])
        other = User.objects.create_user("suggest_other", password="x")
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url, {"q": "ab"}).status_code, 200)

    def test_available_to_users_who_have_not_onboarded(self):
        """JSON endpoint: the onboarding gate must not redirect it (plain or AJAX request)."""
        self._user("gate_target", "Gatetarget", "Person")
        self.client.force_login(self.me)  # no agreement accepted, no profile
        with override_settings(ONBOARDING_GATE_ENABLED=True, **NO_KEY):
            plain = self.client.get(self.url, {"q": "Gatetarget"})
            ajax = self.client.get(self.url, {"q": "Gatetarget"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                                   HTTP_ACCEPT="application/json")
        for resp in (plain, ajax):
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.json()["results"]), 1)


class ProfileFormWidgetTests(TestCase):
    """Observing experience as grouped check boxes; students linked or free-text (#638)."""

    def setUp(self):
        research_crypto.reset_cache()
        creator = User.objects.create_user("inst_creator", password="x")
        audit = {"created_by": creator, "modified_by": creator}
        obs = Observatory.objects.create(name="Synthetic Observatory", utc_offset=0, tz_name="UTC", **audit)
        self.insts = []
        for tel_name, inst_names in (("Synthetic Scope A", ("CamA1", "CamA2")), ("Synthetic Scope B", ("SpecB1",))):
            tel = Telescope.objects.create(name=tel_name, observatory=obs, latitude=0.0, longitude=0.0,
                                           elevation=0.0, **audit)
            for name in inst_names:
                self.insts.append(Instrument.objects.create(name=name, telescope=tel, **audit))
        self.user = User.objects.create_user("profile_user", password="x")
        consent(self.user)
        self.client.force_login(self.user)

    def test_observing_experience_renders_grouped_checkboxes(self):
        with override_settings(**NO_KEY):
            page = self.client.get(reverse("researcher_profile_edit"))
        html = page.content.decode()
        self.assertEqual(html.count('name="observing_experience"'), 3)
        self.assertEqual(html.count('type="checkbox" name="observing_experience"'), 3)
        self.assertLess(html.index("Synthetic Scope A"), html.index("CamA1"))
        self.assertLess(html.index("CamA2"), html.index("Synthetic Scope B"))
        self.assertLess(html.index("Synthetic Scope B"), html.index("SpecB1"))

    def test_checked_instruments_are_saved(self):
        chosen = [self.insts[0].pk, self.insts[2].pk]
        with override_settings(**NO_KEY):
            resp = self.client.post(reverse("researcher_profile_edit"), dict(
                BASE_POST, career_level="tenured", orcid=VALID_ORCIDS[0], observing_experience=chosen))
        self.assertEqual(resp.status_code, 302)
        saved = ResearcherProfile.objects.get(user=self.user)
        self.assertEqual(sorted(saved.observing_experience.values_list("pk", flat=True)), sorted(chosen))
        with override_settings(**NO_KEY):
            page = self.client.get(reverse("researcher_profile_edit"))
        import re
        tags = re.findall(r'<input[^>]*name="observing_experience"[^>]*>', page.content.decode())
        self.assertEqual(len(tags), 3)
        self.assertEqual(sum(1 for t in tags if "checked" in t), 2)

    def test_unknown_instrument_is_refused(self):
        with override_settings(**NO_KEY):
            resp = self.client.post(reverse("researcher_profile_edit"), dict(
                BASE_POST, career_level="tenured", orcid=VALID_ORCIDS[0], observing_experience=[999999]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("observing_experience", resp.context["form"].errors)

    def test_students_saved_linked_and_free_text(self):
        mentor = User.objects.create_user("mentor_form", password="x")
        consent(mentor)
        ResearcherProfile.objects.create(user=mentor, career_level="tenured", orcid=VALID_ORCIDS[0], **BASE_POST)
        linked = User.objects.create_user("linked_student", password="x")
        self.client.force_login(mentor)
        self.client.post(reverse("researcher_student_add"), {
            "student_name": "Linked Student (picked)", "student_user_id": str(linked.pk),
            "kind": Mentorship.KIND_PHD_ADVISOR})
        self.client.post(reverse("researcher_student_add"), {
            "student_name": "Typed Only Student", "student_user_id": "", "kind": Mentorship.KIND_PHD_ADVISOR})
        rows = {m.mentee_id: m for m in rp.my_students(mentor)}
        self.assertEqual(len(rows), 2)
        self.assertIn(linked.pk, rows)
        free = rows[None]
        self.assertEqual(free.mentee_name, "Typed Only Student")

    def test_advisor_linked_and_free_text_from_profile_form(self):
        advisor = User.objects.create_user("advisor_form", password="x")
        with override_settings(**NO_KEY):
            self.client.post(reverse("researcher_profile_edit"), dict(
                BASE_POST, career_level="grad", advisor_name="Picked Name", advisor_user_id=str(advisor.pk)))
            self.assertEqual(rp.current_advisor(self.user).mentor, advisor)
            self.client.post(reverse("researcher_profile_edit"), dict(
                BASE_POST, career_level="grad", advisor_name="Free Text Advisor", advisor_user_id=""))
        self.assertEqual(rp.current_advisor(self.user).mentor_name, "Free Text Advisor")


class ExplorerAndApiExclusionTests(TestCase):
    MODELS = (ResearcherProfile, EncryptedProfileValue, ProfileFieldHistory, Mentorship, AffiliationHistory,
              ResearcherMetrics, UserAgreement, AgreementAcceptance, ResearchConsentForm, ResearchConsentRecord,
              ResearchConsentPrompt)

    def tables(self):
        names = [m._meta.db_table for m in self.MODELS]
        names.append(ResearcherProfile.observing_experience.through._meta.db_table)
        return names

    def test_tables_hidden_from_explorer_schema_and_refused_in_sql(self):
        from explorer.schema import _include_table
        from explorer.utils import passes_blacklist
        for table in self.tables():
            self.assertFalse(_include_table(table), table)
            for sql in ("SELECT * FROM %s" % table, "select id from `%s` limit 1" % table.lower(),
                        "SELECT 1 FROM YSE_test.%s" % table):
                ok, _fails = passes_blacklist(sql)
                self.assertFalse(ok, sql)
        self.assertTrue(_include_table("YSE_App_transient"))
        self.assertTrue(passes_blacklist("SELECT name FROM YSE_App_transient")[0])
        self.assertFalse(passes_blacklist("DELETE FROM YSE_App_transient")[0])  # Explorer's defaults kept

    def test_no_rest_api_for_these_models(self):
        from YSE_App.urls import router
        for _prefix, viewset, _basename in router.registry:
            queryset = getattr(viewset, "queryset", None)
            model = getattr(queryset, "model", None)
            self.assertNotIn(model, self.MODELS, viewset)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, orcid):
        self.orcid = orcid
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        assert kw["headers"]["Authorization"] == "Bearer synthetic-token"
        docs = [
            {"bibcode": "2024XXXX...1..1A", "property": ["REFEREED"], "orcid_pub": [self.orcid, "-"]},
            {"bibcode": "2025XXXX...2..2B", "property": ["NOT REFEREED"], "orcid_user": ["-", self.orcid]},
        ]
        return FakeResponse({"response": {"numFound": 2, "docs": docs}})

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        assert kw["json"]["bibcodes"] == ["2024XXXX...1..1A", "2025XXXX...2..2B"]
        return FakeResponse({"indicators": {"h": 2}, "citation stats": {"total number of citations": 17},
                             "basic stats refereed": {"number of papers": 1}, "skipped bibcodes": []})


class MetricsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("metrics_admin", password="x")
        self.user = User.objects.create_user("metrics_user")
        consent(self.user)
        ResearcherProfile.objects.create(user=self.user, career_level="tenured", orcid=VALID_ORCIDS[0], **BASE_POST)

    def add_token(self):
        cred = EncryptedCredential(name="SciX service account", service="scix", created_by=self.admin,
                                   modified_by=self.admin)
        cred.set_secret({"token": "synthetic-token"})
        cred.save()

    def test_refresh_with_mocked_http(self):
        self.add_token()
        http = FakeHttp(VALID_ORCIDS[0])
        sleeps = []
        summary = researcher_metrics.refresh_all(http=http, sleep=sleeps.append)
        self.assertEqual(summary.fetched, 1, str(summary))
        row = ResearcherMetrics.objects.get(user=self.user)
        self.assertEqual((row.source, row.h_index, row.citation_count, row.refereed_count, row.first_author_count),
                         ("scix", 2, 17, 1, 1))
        self.assertIn("orcid:%s" % VALID_ORCIDS[0], http.calls[0][2]["params"]["q"])
        # Fetched recently: skipped on the next run unless forced.
        summary = researcher_metrics.refresh_all(http=FakeHttp(VALID_ORCIDS[0]), sleep=sleeps.append)
        self.assertEqual((summary.fetched, summary.skipped), (0, 1))

    def test_rate_limit_stops_the_run(self):
        self.add_token()
        other = User.objects.create_user("metrics_user2")
        ResearcherProfile.objects.create(user=other, career_level="tenured", orcid=VALID_ORCIDS[1], **BASE_POST)
        http = mock.Mock()
        http.get.return_value = FakeResponse({}, status=429)
        summary = researcher_metrics.refresh_all(http=http, sleep=lambda s: None)
        self.assertEqual(summary.failed, 1)
        self.assertIn("rate limit", summary.stopped)
        self.assertEqual(http.get.call_count, 1)

    def test_no_token_does_nothing(self):
        summary = researcher_metrics.refresh_all(http=mock.Mock())
        self.assertEqual(summary.fetched, 0)
        self.assertIn("no SciX token", summary.stopped)

    @override_settings(RESEARCHER_METRICS_ENABLED=False)
    def test_disabled_by_default(self):
        out = io.StringIO()
        with mock.patch("YSE_App.services.researcher_metrics.refresh_all") as refresh:
            call_command("refresh_researcher_metrics", stdout=out)
            refresh.assert_not_called()
        self.assertIn("disabled", out.getvalue())
        from YSE_App.data_ingest.Researcher_Metrics import RefreshResearcherMetrics
        self.assertEqual(RefreshResearcherMetrics().do(), "disabled")
