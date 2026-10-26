"""Onboarding gate: user agreement, then profile (#630); exemptions for scripts, crons and brokers.

All users here are synthetic. The suite's runner turns the gate off globally
(YSE_App/test_runner.py); these tests switch it on.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from YSE_App import login_required as _login_required
from YSE_App import onboarding
from YSE_App.models import AgreementAcceptance, Mentorship, ResearchConsentRecord, ResearcherProfile, UserAgreement
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
    ("download_bulk_photometry_by_id", ["1"]),
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
        # v2 (#743) replaced v1, which stays on record but is no longer current.
        current = UserAgreement.objects.get(is_current=True)
        self.assertEqual(current.version, "2")
        self.assertNotIn("Use of platform data for research", current.body)
        self.assertIn("{acknowledgements_url}", current.body)
        self.assertFalse(UserAgreement.objects.get(version="1").is_current)

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
        # The voluntary research consent form comes next, once (#744); declining continues.
        self.assertRedirects(resp, "%s?%s" % (reverse("research_consent"), urlencode({"next": self.page})),
                             fetch_redirect_response=False)
        self.assertTrue(AgreementAcceptance.objects.filter(user=self.user).exists())
        resp = self.client.post(reverse("research_consent"), {"action": "decline", "next": self.page})
        # Declining still goes through the incomplete profile (#766), then on to the page.
        self.assertRedirects(resp, "%s?%s" % (reverse("researcher_profile_edit"), urlencode({"next": self.page})),
                             fetch_redirect_response=False)

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
        UserAgreement.objects.create(version="3", title="v3", body="<p>New terms.</p>", is_current=True)
        self.assertEqual(UserAgreement.objects.filter(is_current=True).count(), 1)
        resp = self.client.get(self.page)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("agreement"), resp["Location"])
        self.client.post(reverse("agreement"), {"agree": "on"})
        self.assertEqual(self.client.get(self.page).status_code, 200)

    def test_incomplete_profile_is_redirected_to_edit(self):
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        # Student requirements apply only with research consent (#744).
        from YSE_App.services import research_consent
        research_consent.record_decision(self.user, research_consent.CONSENTED)
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
        self.assertEqual(parse_qs(urlsplit(resp["Location"]).query)["next"], [reverse("dashboard")])
        resp = self.client.post(reverse("research_consent"), {"action": "decline",
                                                              "next": "https://elsewhere.example/x"})
        # the consent page's fallback, reached through the incomplete profile (#766)
        self.assertEqual(resp["Location"], "%s?%s" % (reverse("researcher_profile_edit"),
                                                      urlencode({"next": reverse("researcher_profile")})))

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

    def test_experimental_endpoints_are_exempt(self):
        # Develop backport (2026-10-12): the two-factor pages (only routed when
        # STAFF_MFA_REQUIRED is on; test_staff_mfa covers the flow) and the
        # feedback POST target are exempt by name; query_api and the
        # external-service callback stay exempt.
        for name in ("staff_mfa_verify", "staff_mfa_setup", "staff_mfa_backup_codes", "feedback_submit",
                     "query_api", "external_service_run_callback", "slack_events"):
            self.assertIn(name, onboarding.EXEMPT_URL_NAMES, name)
        self.assertExempt(reverse("feedback_submit"))
        self.assertExempt(reverse("feedback_submit"), method="post")
        self.assertExempt(reverse("query_api", args=["some_query"]))

    def test_drf_views_outside_api_prefix_are_exempt(self):
        from rest_framework.views import APIView

        class _Probe(APIView):
            pass

        request = self.factory.get("/not-under-api/", **BROWSER)
        request.user = self.user
        self.assertTrue(onboarding.is_exempt(request, _Probe.as_view()))


# --- Served under a URL prefix, as /yse_test/ and /yse_experimental/ are on Ziggy (#634) ---

PREFIX = "/yse_test"
BROWSER = dict(HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               HTTP_SEC_FETCH_MODE="navigate", HTTP_SEC_FETCH_DEST="document")


@override_settings(FORCE_SCRIPT_NAME=PREFIX, **GATE_ON)
class GateUnderUrlPrefixTests(TestCase):
    """Deep links under SCRIPT_NAME: every page goes agreement -> profile -> back to that page.

    Django's test client does not set the script prefix the way the WSGI handler
    does, so ``browse`` sets it per request, like mod_wsgi with WSGIScriptAlias.
    """

    @classmethod
    def setUpTestData(cls):
        from YSE_App.tests.fixtures_minimal import create_minimal_transient
        cls.user = User.objects.create_user("gate_prefix_newcomer", password="x")
        cls.transient = create_minimal_transient(cls.user, name="2026gateprefix")

    def setUp(self):
        from django.urls import clear_script_prefix
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(clear_script_prefix)
        self.addCleanup(onboarding.invalidate_current_agreement)
        self.client.force_login(self.user)

    def browse(self, method, url, data=None, **extra):
        """Request ``url`` (a full, prefixed path as a browser sends it)."""
        from django.urls import set_script_prefix
        self.assertTrue(url.startswith(PREFIX + "/"), url)
        set_script_prefix(PREFIX + "/")
        headers = dict(BROWSER)
        headers.update(extra)
        return getattr(self.client, method)(url[len(PREFIX):], data, SCRIPT_NAME=PREFIX, **headers)

    def prefixed(self, name, *args, **kwargs):
        from django.urls import set_script_prefix
        set_script_prefix(PREFIX + "/")
        return reverse(name, args=args or None, kwargs=kwargs or None)

    def deep_links(self):
        return [
            self.prefixed("transient_detail", slug=self.transient.slug),
            self.prefixed("transient_summary", status_or_query_name="New"),
            self.prefixed("dashboard"),
        ]

    def assertRedirectsToStep(self, resp, step_name, wanted):
        self.assertEqual(resp.status_code, 302, wanted)
        location = urlsplit(resp["Location"])
        self.assertEqual(location.path, self.prefixed(step_name), resp["Location"])
        self.assertEqual(parse_qs(location.query).get("next"), [wanted], resp["Location"])
        return resp["Location"]

    def test_reverse_carries_the_prefix(self):
        self.assertEqual(self.prefixed("agreement"), PREFIX + "/agreement/")

    def test_every_deep_link_redirects_to_the_prefixed_agreement(self):
        for url in self.deep_links():
            with self.subTest(url=url):
                self.assertRedirectsToStep(self.browse("get", url), "agreement", url)

    def test_deep_link_round_trip_returns_to_the_page_first_asked_for(self):
        for url in self.deep_links():
            with self.subTest(url=url):
                AgreementAcceptance.objects.filter(user=self.user).delete()
                ResearcherProfile.objects.filter(user=self.user).delete()
                session = self.client.session
                session.pop(onboarding.SESSION_KEY, None)
                session.save()

                agreement_url = self.assertRedirectsToStep(self.browse("get", url), "agreement", url)
                page = self.browse("get", agreement_url)
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'action="%s?next=%s"' % (self.prefixed("agreement"), quote(url, safe="")))
                self.assertContains(page, 'value="%s"' % url)

                resp = self.browse("post", self.prefixed("agreement"), {"agree": "on", "next": url})
                self.assertEqual(resp.status_code, 302)
                if ResearchConsentRecord.objects.filter(user=self.user).exists():
                    self.assertEqual(resp["Location"], url)  # the consent form is offered only once
                else:
                    self.assertRedirectsToStep(resp, "research_consent", url)
                    resp = self.browse("post", self.prefixed("research_consent"), {"action": "decline", "next": url})
                    self.assertRedirectsToStep(resp, "researcher_profile_edit", url)  # incomplete profile (#766)

                profile_url = self.assertRedirectsToStep(self.browse("get", url), "researcher_profile_edit", url)
                page = self.browse("get", profile_url)
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'action="%s?next=%s"' % (self.prefixed("researcher_profile_edit"),
                                                                     quote(url, safe="")))
                self.assertContains(page, 'value="%s"' % url)

                resp = self.browse("post", self.prefixed("researcher_profile_edit"), {
                    "full_name": "Test Person", "institution": "Example University", "department": "Physics",
                    "career_level": "tenured", "orcid": VALID_ORCID, "next": url,
                })
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp["Location"], url)

                final = self.browse("get", url)
                self.assertEqual(final.status_code, 200, url)

    def test_onboarded_user_reaches_every_deep_link_directly(self):
        onboard(self.user)
        for url in self.deep_links():
            with self.subTest(url=url):
                self.assertEqual(self.browse("get", url).status_code, 200, url)

    def test_fragments_requested_before_onboarding_do_not_redirect(self):
        fragment = self.prefixed("transient_detail_photometry_fragment", self.transient.id)
        resp = self.browse("get", fragment, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                           HTTP_ACCEPT="text/html, */*; q=0.01", HTTP_SEC_FETCH_MODE="cors",
                           HTTP_SEC_FETCH_DEST="empty")
        self.assertNotEqual(resp.status_code, 302, resp.get("Location"))

    def test_agreement_post_that_bounces_through_login_keeps_next(self):
        """The form posts to /agreement/?next=...: a login bounce on the POST keeps the deep link."""
        url = self.deep_links()[0]
        self.client.logout()
        resp = self.browse("post", "%s?next=%s" % (self.prefixed("agreement"), quote(url, safe="")),
                           {"agree": "on", "next": url})
        self.assertEqual(resp.status_code, 302)
        login_next = parse_qs(urlsplit(resp["Location"]).query)["next"][0]
        self.assertEqual(parse_qs(urlsplit(login_next).query).get("next"), [url])

    def test_already_accepted_agreement_link_continues_to_next(self):
        url = self.deep_links()[0]
        AgreementAcceptance.objects.create(user=self.user, agreement=UserAgreement.objects.get(is_current=True))
        resp = self.browse("get", "%s?next=%s" % (self.prefixed("agreement"), quote(url, safe="")))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], url)
        page = self.browse("get", self.prefixed("agreement"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="yse-agreement-continue"')
        self.assertContains(page, 'href="%s"' % self.prefixed("dashboard"))

    def test_login_without_next_goes_to_this_stacks_dashboard(self):
        self.client.logout()
        resp = self.browse("post", self.prefixed("auth_login"), {"username": "gate_prefix_newcomer", "password": "x"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.prefixed("dashboard"))
        self.client.logout()
        resp = self.browse("post", self.prefixed("auth_login"),
                           {"username": "gate_prefix_newcomer", "password": "x", "next": "https://elsewhere.example/"})
        self.assertEqual(resp["Location"], self.prefixed("dashboard"))
        self.client.logout()
        url = self.deep_links()[0]
        resp = self.browse("post", self.prefixed("auth_login"),
                           {"username": "gate_prefix_newcomer", "password": "x", "next": url})
        self.assertEqual(resp["Location"], url)


@override_settings(FORCE_SCRIPT_NAME=PREFIX, **GATE_ON)
class GateCoversEveryPageTests(TestCase):
    """Every argument-free page in the URLconf is gated for a browser GET, with a prefixed next (#634)."""

    def setUp(self):
        from django.urls import clear_script_prefix, set_script_prefix
        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(clear_script_prefix)
        set_script_prefix(PREFIX + "/")
        self.user = User.objects.create_user("gate_every_page", password="x")
        self.mw = OnboardingGateMiddleware(lambda r: None)

    def test_every_page_redirects_to_the_prefixed_agreement(self):
        from django.contrib.sessions.backends.db import SessionStore
        from django.urls import get_resolver

        def walk(patterns, prefix=""):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    yield from walk(p.url_patterns, prefix + str(p.pattern))
                else:
                    yield prefix + str(p.pattern), p

        factory = RequestFactory()
        checked = 0
        for regex, pattern in walk(get_resolver().url_patterns):
            if "<" in regex or "(" in regex or "[" in regex or "\\" in regex or "*" in regex or "+" in regex:
                continue  # needs arguments
            path = "/" + regex.replace("^", "").replace("$", "")
            if path.endswith(".json") or path.startswith(onboarding.EXEMPT_PATH_PREFIXES):
                continue
            try:
                match = resolve(path)
            except Exception:  # noqa: BLE001 - optional-slash patterns etc.
                continue
            if match.url_name in onboarding.EXEMPT_URL_NAMES or getattr(match.func, "onboarding_exempt", False):
                continue
            if _login_required._is_drf_view(match.func):
                continue  # DRF views are exempt wherever they are routed
            request = factory.get(path, SCRIPT_NAME=PREFIX, **BROWSER)
            request.user = self.user
            request.session = SessionStore()
            request.resolver_match = match
            resp = self.mw.process_view(request, match.func, match.args, match.kwargs)
            with self.subTest(path=path):
                self.assertIsNotNone(resp, path)
                location = urlsplit(resp["Location"])
                self.assertEqual(location.path, PREFIX + "/agreement/", path)
                self.assertEqual(parse_qs(location.query).get("next"), [PREFIX + path], path)
            checked += 1
        self.assertGreater(checked, 40)
