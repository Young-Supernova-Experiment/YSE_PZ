"""Onboarding gate: user agreement, then profile (#630); exemptions for scripts, crons and brokers.

All users here are synthetic. The suite's runner turns the gate off globally
(YSE_App/test_runner.py); these tests switch it on.
"""

from __future__ import annotations

import base64

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from YSE_App import onboarding
from YSE_App.models import AgreementAcceptance, Mentorship, ResearcherProfile, UserAgreement
from YSE_App.onboarding import OnboardingGateMiddleware
from YSE_App.services import research_crypto

NO_KEY = dict(RESEARCH_PUBLIC_KEY="", RESEARCH_PUBLIC_KEY_PATH="")
GATE_ON = dict(ONBOARDING_GATE_ENABLED=True, **NO_KEY)
VALID_ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (fictitious researcher)

# Ingest, download and query endpoints wrapped in login_or_basic_auth_required on develop.
BASIC_AUTH_URL_NAMES = (
    "add_transient", "add_transient_phot", "add_transient_spec", "add_gw_candidate",
    "add_yse_survey_obs", "add_yse_survey_fields", "add_survey_obs", "upload_spectrum",
)
BASIC_AUTH_URLS_WITH_ARGS = (
    ("query_api", ["some_query"]),
    ("get_transient", ["2026abc"]),
    ("get_rising_transients", ["3"]),
    ("download_data", ["2026abc"]),
    ("download_photometry", ["2026abc"]),
    ("download_spectra", ["2026abc"]),
    ("download_bulk_photometry", ["some_query"]),
    ("change_status_for_query", ["1", "2"]),
    ("get_host", ["10.0", "-5.0", "3"]),
    ("box_search", ["10.0", "-5.0", "3"]),
)


def complete_profile(user, **extra):
    fields = dict(full_name="Test Person", institution="Example University", department="Physics",
                  career_level="tenured", orcid=VALID_ORCID)
    fields.update(extra)
    return ResearcherProfile.objects.create(user=user, **fields)


def onboard(user):
    agreement = UserAgreement.objects.get(is_current=True)
    AgreementAcceptance.objects.get_or_create(user=user, agreement=agreement)
    complete_profile(user)


@override_settings(**GATE_ON)
class GateFlowTests(TestCase):
    def setUp(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.user = User.objects.create_user("gate_newcomer", password="x")
        self.client.force_login(self.user)
        self.page = reverse("notification_list")

    def tearDown(self):
        onboarding.invalidate_current_agreement()

    def test_seeded_agreement_is_current(self):
        current = UserAgreement.objects.get(is_current=True)
        self.assertEqual(current.version, "1")
        self.assertIn("Only anonymized data will be used.", current.body)
        self.assertIn("{acknowledgements_url}", current.body)

    def test_new_user_goes_agreement_then_profile_then_through(self):
        resp = self.client.get(self.page)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("agreement") + "?next="))
        self.assertIn("notifications", resp["Location"])

        page = self.client.get(resp["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "I have read and agree to the YSE-PZ User Agreement")
        self.assertContains(page, 'href="%s"' % reverse("acknowledgements"))
        self.assertNotContains(page, "{acknowledgements_url}")

        # The box must be ticked.
        resp = self.client.post(reverse("agreement"), {"next": self.page})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AgreementAcceptance.objects.filter(user=self.user).exists())

        resp = self.client.post(reverse("agreement"), {"agree": "on", "next": self.page})
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        self.assertTrue(AgreementAcceptance.objects.filter(user=self.user).exists())

        resp = self.client.get(self.page)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("researcher_profile_edit") + "?next="))

        resp = self.client.post(reverse("researcher_profile_edit"), {
            "full_name": "Test Person", "institution": "Example University", "department": "Physics",
            "career_level": "tenured", "orcid": VALID_ORCID, "next": self.page,
        })
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.page).status_code, 200)
        # Cached in the session: a second view still passes.
        self.assertEqual(self.client.get(self.page).status_code, 200)

    def test_new_agreement_version_reprompts(self):
        onboard(self.user)
        self.assertEqual(self.client.get(self.page).status_code, 200)
        UserAgreement.objects.create(version="2", title="v2", body="<p>New terms.</p>", is_current=True)
        self.assertEqual(UserAgreement.objects.filter(is_current=True).count(), 1)
        resp = self.client.get(self.page)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("agreement"), resp["Location"])
        self.client.post(reverse("agreement"), {"agree": "on"})
        self.assertEqual(self.client.get(self.page).status_code, 200)

    def test_incomplete_profile_is_redirected_to_edit(self):
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        ResearcherProfile.objects.create(user=self.user, full_name="Test Person", career_level="grad",
                                         institution="Example University", department="Physics")
        resp = self.client.get(self.page)  # a student without a current advisor
        self.assertIn(reverse("researcher_profile_edit"), resp["Location"])
        Mentorship.objects.create(mentee=self.user, mentor_name="Free Form Advisor",
                                  kind=Mentorship.KIND_CURRENT_ADVISOR)
        self.assertEqual(self.client.get(self.page).status_code, 200)

    def test_next_must_be_same_site(self):
        resp = self.client.post(reverse("agreement"), {"agree": "on", "next": "https://elsewhere.example/x"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("dashboard"))

    def test_rollback_flag_disables_the_gate(self):
        with override_settings(ONBOARDING_GATE_ENABLED=False):
            self.assertEqual(self.client.get(self.page).status_code, 200)

    def test_anonymous_users_are_not_gated(self):
        self.client.logout()
        resp = self.client.get(reverse("acknowledgements"))
        self.assertEqual(resp.status_code, 200)

    def test_basic_and_token_authorization_headers_are_not_gated(self):
        basic = "Basic " + base64.b64encode(b"gate_newcomer:x").decode()
        self.assertEqual(self.client.get(self.page, HTTP_AUTHORIZATION=basic).status_code, 200)
        self.assertEqual(self.client.get(self.page, HTTP_AUTHORIZATION="Token abc123").status_code, 200)

    def test_api_paths_are_not_gated(self):
        resp = self.client.get("/api/transients/?limit=1")
        self.assertNotIn(reverse("agreement"), resp.get("Location", ""))
        self.assertEqual(resp.status_code, 200)
        basic = "Basic " + base64.b64encode(b"gate_newcomer:x").decode()
        client = self.client_class()
        resp = client.get("/api/transients/?limit=1", HTTP_AUTHORIZATION=basic)
        self.assertEqual(resp.status_code, 200)

    def test_onboarding_pages_themselves_are_reachable(self):
        for name in ("agreement", "researcher_profile", "researcher_profile_edit", "acknowledgements"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


@override_settings(**GATE_ON)
class GateExemptionTests(TestCase):
    """Drive the middleware directly so ingest endpoints are not executed."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("gate_member", password="x")
        cls.staff = User.objects.create_user("gate_staff", password="x", is_staff=True)
        cls.superuser = User.objects.create_superuser("gate_super", password="x")

    def setUp(self):
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.factory = RequestFactory()
        self.mw = OnboardingGateMiddleware(lambda r: None)

    def run_gate(self, path, user=None, method="get", **extra):
        request = getattr(self.factory, method)(path, **extra)
        request.user = user or self.user
        request.session = self.client.session
        match = resolve(request.path_info)
        request.resolver_match = match
        return self.mw.process_view(request, match.func, match.args, match.kwargs)

    def assertGated(self, path, **kw):
        resp = self.run_gate(path, **kw)
        self.assertIsNotNone(resp, path)
        self.assertIn(reverse("agreement"), resp["Location"])

    def assertExempt(self, path, **kw):
        self.assertIsNone(self.run_gate(path, **kw), path)

    def test_ordinary_pages_are_gated(self):
        self.assertGated(reverse("dashboard"))
        self.assertGated(reverse("notification_list"))
        self.assertGated(reverse("explorer_index"))

    def test_ingest_and_basic_auth_views_are_exempt(self):
        for name in BASIC_AUTH_URL_NAMES:
            path = reverse(name)
            self.assertTrue(getattr(resolve(path).func, "onboarding_exempt", False), name)
            self.assertExempt(path)
            self.assertExempt(path, method="post")
        for name, args in BASIC_AUTH_URLS_WITH_ARGS:
            path = reverse(name, args=args)
            self.assertTrue(getattr(resolve(path).func, "onboarding_exempt", False), name)
            self.assertExempt(path)

    def test_every_basic_auth_view_in_the_urlconf_is_exempt(self):
        from django.urls import get_resolver
        from YSE_App import basicauth

        def walk(patterns):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    yield from walk(p.url_patterns)
                else:
                    yield p

        basic_code = basicauth.login_or_basic_auth_required(lambda r: r).__code__

        def uses_basic_auth(func):
            candidates = [func]
            view_class = getattr(func, "view_class", None)
            if view_class is not None:
                candidates.append(view_class.dispatch)
            for f in candidates:
                while f is not None:
                    if getattr(f, "__code__", None) is basic_code:
                        return True
                    f = getattr(f, "__wrapped__", None)
            return False

        wrapped = 0
        for pattern in walk(get_resolver().url_patterns):
            if uses_basic_auth(pattern.callback):
                wrapped += 1
                self.assertTrue(getattr(pattern.callback, "onboarding_exempt", False), pattern)
        self.assertGreaterEqual(wrapped, 15)

    def test_integration_callbacks_static_and_json_are_exempt(self):
        self.assertExempt(reverse("slack_events"), method="post")
        self.assertExempt(reverse("external_service_run_callback", args=["0" * 8 + "-0000-0000-0000-" + "0" * 12]),
                          method="post")
        self.assertExempt(reverse("jobs_status_json"))
        self.assertExempt(reverse("notification_unread_count"))
        self.assertExempt(reverse("brokers_status_json"))

    def test_static_and_media_are_exempt(self):
        request = self.factory.get("/static/YSE_App/favicon.ico")
        request.user = self.user
        self.assertTrue(onboarding.is_exempt(request, lambda r: None))
        request = self.factory.get("/media/service_runs/x.txt")
        request.user = self.user
        self.assertTrue(onboarding.is_exempt(request, lambda r: None))

    def test_login_logout_password_and_onboarding_pages_are_exempt(self):
        for name in ("auth_login", "auth_logout", "password_reset", "password_change", "password_reset_done",
                     "agreement", "researcher_profile", "researcher_profile_edit", "acknowledgements"):
            self.assertExempt(reverse(name))

    def test_non_page_requests_are_not_gated(self):
        page = reverse("notification_list")
        self.assertExempt(page, method="post")
        self.assertExempt(page, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertExempt(page, HTTP_SEC_FETCH_MODE="cors")
        self.assertExempt(page, HTTP_ACCEPT="application/json")
        self.assertGated(page, HTTP_SEC_FETCH_MODE="navigate", HTTP_ACCEPT="text/html,*/*")

    def test_authorization_header_is_exempt(self):
        page = reverse("notification_list")
        self.assertExempt(page, HTTP_AUTHORIZATION="Basic Zm9vOmJhcg==")
        self.assertExempt(page, HTTP_AUTHORIZATION="Token abc")

    def test_admin_is_exempt_for_superusers_only(self):
        self.assertExempt(reverse("admin:index"), user=self.superuser)
        self.assertGated(reverse("admin:index"), user=self.staff)
        self.assertGated(reverse("dashboard"), user=self.superuser)

    def test_onboarded_user_passes(self):
        onboard(self.user)
        self.assertExempt(reverse("notification_list"))
