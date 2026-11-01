"""Login, session, transport and Slack-request settings (#509-#511, #513-#519)."""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import types
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from YSE_App.tests.test_settings_hardening import _CREDENTIALS_SECTION, _load_settings

_PROD = "SECRET_KEY: k" + _CREDENTIALS_SECTION


def _security(**keys):
    """A [security] block for _load_settings(site_extra=...)."""
    return "\n[security]\n" + "\n".join("%s: %s" % kv for kv in keys.items())


class TransportSettingsTests(SimpleTestCase):
    def test_defaults_keep_previous_behaviour(self):
        for debug, extra in (("True", ""), ("False", _PROD)):
            mod = _load_settings(debug=debug, site_extra=extra)
            self.assertIs(mod.SESSION_COOKIE_SECURE, False)
            self.assertIs(mod.CSRF_COOKIE_SECURE, False)
            self.assertIs(mod.SECURE_SSL_REDIRECT, False)
            self.assertEqual(mod.SECURE_HSTS_SECONDS, 0)
            self.assertIs(mod.SECURE_HSTS_INCLUDE_SUBDOMAINS, False)
            self.assertEqual(mod.SESSION_COOKIE_AGE, 1209600)
            self.assertIs(mod.USE_X_FORWARDED_HOST, True)
            self.assertIs(mod.USE_X_FORWARDED_PORT, True)
            self.assertEqual(mod.PASSWORD_RESET_DOMAIN, "")
            self.assertIs(mod.CSP_REPORT_ONLY_ENABLED, False)
            self.assertIs(mod.LOGIN_LOCKOUT_ENABLED, False)
            self.assertEqual(mod.ALLOWED_HOSTS, ["*"])

    def test_flags_from_ini(self):
        mod = _load_settings(debug="False", site_extra=_PROD + _security(
            SESSION_COOKIE_SECURE="True", CSRF_COOKIE_SECURE="True", SECURE_SSL_REDIRECT="True",
            SECURE_HSTS_SECONDS="3600", SECURE_HSTS_INCLUDE_SUBDOMAINS="True", SESSION_COOKIE_AGE="43200",
            USE_X_FORWARDED_HOST="False", USE_X_FORWARDED_PORT="False", PASSWORD_RESET_DOMAIN="yse.example.org",
            PASSWORD_RESET_USE_HTTPS="False"))
        self.assertIs(mod.SESSION_COOKIE_SECURE, True)
        self.assertIs(mod.CSRF_COOKIE_SECURE, True)
        self.assertIs(mod.SECURE_SSL_REDIRECT, True)
        self.assertEqual(mod.SECURE_HSTS_SECONDS, 3600)
        self.assertIs(mod.SECURE_HSTS_INCLUDE_SUBDOMAINS, True)
        self.assertEqual(mod.SESSION_COOKIE_AGE, 43200)
        self.assertIs(mod.USE_X_FORWARDED_HOST, False)
        self.assertIs(mod.USE_X_FORWARDED_PORT, False)
        self.assertEqual(mod.PASSWORD_RESET_DOMAIN, "yse.example.org")
        self.assertIs(mod.PASSWORD_RESET_USE_HTTPS, False)

    def test_env_overrides_ini(self):
        mod = _load_settings(debug="True", site_extra=_security(SESSION_COOKIE_SECURE="True"),
                             env={"YSE_SESSION_COOKIE_SECURE": "0", "YSE_SESSION_COOKIE_AGE": "86400"})
        self.assertIs(mod.SESSION_COOKIE_SECURE, False)
        self.assertEqual(mod.SESSION_COOKIE_AGE, 86400)

    def test_proxy_ssl_header_default_follows_debug(self):
        self.assertEqual(_load_settings(debug="False", site_extra=_PROD).SECURE_PROXY_SSL_HEADER,
                         ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertFalse(hasattr(_load_settings(debug="True"), "SECURE_PROXY_SSL_HEADER"))

    def test_proxy_ssl_header_flag(self):
        mod = _load_settings(debug="False", site_extra=_PROD + _security(TRUST_X_FORWARDED_PROTO="False"))
        self.assertFalse(hasattr(mod, "SECURE_PROXY_SSL_HEADER"))
        mod = _load_settings(debug="True", site_extra=_security(TRUST_X_FORWARDED_PROTO="True"))
        self.assertEqual(mod.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))


class AllowedHostsRequirementTests(SimpleTestCase):
    def test_off_by_default(self):
        self.assertEqual(_load_settings(debug="False", site_extra=_PROD).ALLOWED_HOSTS, ["*"])

    def test_required_without_hosts_refuses_to_start(self):
        for hosts in ("", "ALLOWED_HOSTS: *\n"):
            with self.assertRaises(ImproperlyConfigured):
                _load_settings(debug="False", site_extra=hosts + _PROD + _security(REQUIRE_ALLOWED_HOSTS="True"))

    def test_required_with_hosts_starts(self):
        mod = _load_settings(debug="False", site_extra="ALLOWED_HOSTS: yse.example.org\n" + _PROD
                             + _security(REQUIRE_ALLOWED_HOSTS="True"))
        self.assertEqual(mod.ALLOWED_HOSTS, ["yse.example.org"])

    def test_not_enforced_in_debug(self):
        mod = _load_settings(debug="True", site_extra=_security(REQUIRE_ALLOWED_HOSTS="True"))
        self.assertEqual(mod.ALLOWED_HOSTS, ["*"])


class DebugAndKeyTests(SimpleTestCase):
    def test_env_turns_debug_on_and_off(self):
        self.assertIs(_load_settings(debug="False", env={"YSE_IS_DEBUG": "1"}).DEBUG, True)
        self.assertIs(_load_settings(debug="True", site_extra=_PROD, env={"YSE_IS_DEBUG": "0"}).DEBUG, False)

    def test_production_never_derives_credentials_key(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _load_settings(debug="False", site_extra="SECRET_KEY: k")
        self.assertIn("CREDENTIALS_KEY", str(ctx.exception))

    def test_template_defaults_to_debug_off_and_docker_env_turns_it_on(self):
        with open(os.path.join(settings.BASE_DIR, "YSE_PZ", "public_settings.ini")) as fh:
            self.assertIn("\nIS_DEBUG: False\n", fh.read())
        env_template = os.path.join(settings.BASE_DIR, "docker", "public.env")
        if os.path.exists(env_template):
            with open(env_template) as fh:
                self.assertIn("YSE_IS_DEBUG=1", fh.read())


class DatabaseTlsTests(SimpleTestCase):
    def test_default_options_unchanged(self):
        mod = _load_settings(debug="True", database_extra="SSL_CA: /etc/ca.pem")
        self.assertEqual(mod.DATABASES["default"]["OPTIONS"], {"ssl": {"ssl_disabled": True}})
        self.assertNotIn("OPTIONS", mod.DATABASES["explorer"])

    def test_tls_paths_and_mode(self):
        mod = _load_settings(debug="True", database_extra=(
            "SSL_DISABLED: False\nSSL_CA: /etc/ca.pem\nSSL_CERT: /etc/c.pem\nSSL_KEY: /etc/k.pem\nSSL_MODE: verify_ca"))
        opts = mod.DATABASES["default"]["OPTIONS"]
        self.assertEqual(opts["ssl"], {"ssl_disabled": False, "ca": "/etc/ca.pem", "cert": "/etc/c.pem",
                                       "key": "/etc/k.pem"})
        self.assertEqual(opts["ssl_mode"], "VERIFY_CA")
        self.assertNotIn("OPTIONS", mod.DATABASES["explorer"])

    def test_explorer_tls_opt_in(self):
        mod = _load_settings(debug="True", database_extra="SSL_DISABLED: False\nSSL_CA: /etc/ca.pem\nDB_TLS_EXPLORER: True")
        self.assertEqual(mod.DATABASES["explorer"]["OPTIONS"]["ssl"]["ca"], "/etc/ca.pem")


class ApiThrottleSettingsTests(SimpleTestCase):
    def test_default_off(self):
        rf = _load_settings(debug="True").REST_FRAMEWORK
        self.assertEqual(rf["DEFAULT_THROTTLE_RATES"], {})
        self.assertEqual(rf["DEFAULT_THROTTLE_CLASSES"], [])

    def test_rates_opt_in(self):
        rf = _load_settings(debug="True", site_extra=_security(API_THROTTLE_ANON="300/min",
                                                               API_THROTTLE_USER="1000/min")).REST_FRAMEWORK
        self.assertEqual(rf["DEFAULT_THROTTLE_RATES"], {"anon": "300/min", "user": "1000/min"})
        self.assertEqual(rf["DEFAULT_THROTTLE_CLASSES"], [
            "rest_framework.throttling.AnonRateThrottle", "rest_framework.throttling.UserRateThrottle"])

    def test_rates_configurable_and_off(self):
        rf = _load_settings(debug="True", site_extra=_security(API_THROTTLE_ANON="off",
                                                               API_THROTTLE_USER="50/sec")).REST_FRAMEWORK
        self.assertEqual(rf["DEFAULT_THROTTLE_RATES"], {"user": "50/sec"})
        self.assertEqual(rf["DEFAULT_THROTTLE_CLASSES"], ["rest_framework.throttling.UserRateThrottle"])


class LoginLockoutSettingsTests(SimpleTestCase):
    def test_flag_without_package_stays_off(self):
        with mock.patch.dict(sys.modules, {"axes": None}):
            with self.assertLogs("yse_settings_under_test", level="WARNING"):
                mod = _load_settings(debug="True", site_extra=_security(LOGIN_LOCKOUT_ENABLED="True"))
        self.assertIs(mod.LOGIN_LOCKOUT_ENABLED, False)
        self.assertNotIn("axes", mod.INSTALLED_APPS)
        self.assertEqual(tuple(mod.AUTHENTICATION_BACKENDS), ("django.contrib.auth.backends.ModelBackend",))

    def test_flag_with_package_wires_axes(self):
        with mock.patch.dict(sys.modules, {"axes": types.ModuleType("axes")}):
            mod = _load_settings(debug="True", site_extra=_security(
                LOGIN_LOCKOUT_ENABLED="True", LOGIN_LOCKOUT_FAILURE_LIMIT="5", LOGIN_LOCKOUT_COOLOFF_MINUTES="30",
                LOGIN_LOCKOUT_NEVER_LOCK_IPS="10.0.0.1, 10.0.0.2"))
        self.assertIn("axes", mod.INSTALLED_APPS)
        self.assertEqual(mod.AUTHENTICATION_BACKENDS[0], "YSE_App.auth_backends.LoginLockoutBackend")
        self.assertEqual(mod.MIDDLEWARE[-1], "axes.middleware.AxesMiddleware")
        self.assertEqual(mod.AXES_FAILURE_LIMIT, 5)
        self.assertEqual(mod.AXES_COOLOFF_TIME, timedelta(minutes=30))
        self.assertIs(mod.AXES_LOCK_OUT_BY_COMBINATION_USER_AND_IP, True)
        self.assertIs(mod.AXES_DISABLE_ACCESS_LOG, True)
        self.assertEqual(mod.AXES_IP_WHITELIST, ["10.0.0.1", "10.0.0.2"])
        self.assertIs(mod.AXES_NEVER_LOCKOUT_WHITELIST, True)

    def test_bad_mode_rejected(self):
        with mock.patch.dict(sys.modules, {"axes": types.ModuleType("axes")}):
            with self.assertRaises(ImproperlyConfigured):
                _load_settings(debug="True", site_extra=_security(LOGIN_LOCKOUT_ENABLED="True",
                                                                  LOGIN_LOCKOUT_MODE="everyone"))


def _slack_headers(secret, body, ts=None):
    ts = ts or str(int(time.time()))
    digest = hmac.new(secret.encode(), ("v0:%s:%s" % (ts, body.decode())).encode(), hashlib.sha256).hexdigest()
    return {"HTTP_X_SLACK_SIGNATURE": "v0=" + digest, "HTTP_X_SLACK_REQUEST_TIMESTAMP": ts}


class SlackEventsRequestTests(TestCase):
    SECRET = "unit-test-signing-secret"

    def setUp(self):
        self.client = Client()
        self.url = reverse("slack_events")
        self.challenge = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()

    def _post(self, body, **headers):
        return self.client.post(self.url, data=body, content_type="application/json", **headers)

    def test_signed_url_verification_returns_challenge(self):
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=self.SECRET):
            resp = self._post(self.challenge, **_slack_headers(self.SECRET, self.challenge))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"challenge": "abc123"})

    def test_signed_event_without_thread_is_accepted(self):
        body = json.dumps({"type": "event_callback", "event": {"type": "message", "channel": "C1",
                                                                "ts": "1.0", "text": "hello"}}).encode()
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=self.SECRET):
            resp = self._post(body, **_slack_headers(self.SECRET, body))
        self.assertEqual(resp.status_code, 200)

    def test_bad_signature_refused(self):
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=self.SECRET):
            resp = self._post(self.challenge, **_slack_headers("another-secret", self.challenge))
            self.assertEqual(resp.status_code, 403)
            resp = self._post(self.challenge)
            self.assertEqual(resp.status_code, 403)

    def test_stale_timestamp_refused(self):
        old = str(int(time.time()) - 3600)
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=self.SECRET):
            resp = self._post(self.challenge, **_slack_headers(self.SECRET, self.challenge, ts=old))
        self.assertEqual(resp.status_code, 403)

    def test_unconfigured_secret_refused(self):
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=""):
            resp = self._post(self.challenge)
        self.assertEqual(resp.status_code, 403)

    def test_slack_disabled_refused_even_when_signed(self):
        with override_settings(SLACK_ENABLED=False, SLACK_SIGNING_SECRET=self.SECRET):
            resp = self._post(self.challenge, **_slack_headers(self.SECRET, self.challenge))
        self.assertEqual(resp.status_code, 403)

    def test_get_not_allowed(self):
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=self.SECRET):
            self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_signed_malformed_body_is_bad_request(self):
        body = b"not json"
        with override_settings(SLACK_ENABLED=True, SLACK_SIGNING_SECRET=self.SECRET):
            resp = self._post(body, **_slack_headers(self.SECRET, body))
        self.assertEqual(resp.status_code, 400)


class CspReportOnlyTests(TestCase):
    URL = "/login/"

    def test_off_by_default(self):
        self.assertNotIn("Content-Security-Policy-Report-Only", Client().get(self.URL))

    @override_settings(CSP_REPORT_ONLY_ENABLED=True, CSP_REPORT_ONLY_POLICY="", CSP_REPORT_URI="")
    def test_default_policy_lists_template_cdns(self):
        header = Client().get(self.URL)["Content-Security-Policy-Report-Only"]
        self.assertIn("default-src 'self'", header)
        self.assertIn("https://code.jquery.com", header)
        self.assertIn("https://fonts.googleapis.com", header)
        self.assertFalse(Client().get(self.URL).has_header("Content-Security-Policy"))

    @override_settings(CSP_REPORT_ONLY_ENABLED=True, CSP_REPORT_ONLY_POLICY="default-src 'self'",
                       CSP_REPORT_URI="https://csp.example.org/report")
    def test_custom_policy_and_report_uri(self):
        header = Client().get(self.URL)["Content-Security-Policy-Report-Only"]
        self.assertEqual(header, "default-src 'self'; report-uri https://csp.example.org/report")


class PasswordResetDomainTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("reset_user", email="reset_user@example.com", password="pw-123-abc")

    def _request_reset(self, **extra):
        resp = Client().post(reverse("password_reset"), {"email": "reset_user@example.com"}, **extra)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0].body

    def test_unset_uses_request_host(self):
        body = self._request_reset()
        self.assertIn("http://testserver/", body)

    @override_settings(PASSWORD_RESET_DOMAIN="yse.example.org", PASSWORD_RESET_USE_HTTPS=True,
                       ALLOWED_HOSTS=["testserver", "other.example.net"])
    def test_configured_domain_is_used_for_the_link(self):
        body = self._request_reset(HTTP_X_FORWARDED_HOST="other.example.net")
        self.assertIn("https://yse.example.org/", body)
        self.assertNotIn("other.example.net", body)
        self.assertNotIn("testserver", body)

    def test_unknown_email_sends_nothing(self):
        resp = Client().post(reverse("password_reset"), {"email": "nobody@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)


class DeactivateInactiveUsersTests(TestCase):
    def _user(self, username, days_since_login=None, joined_days_ago=800, **extra):
        user = User.objects.create_user(username, password="pw-123-abc", **extra)
        user.date_joined = timezone.now() - timedelta(days=joined_days_ago)
        user.last_login = None if days_since_login is None else timezone.now() - timedelta(days=days_since_login)
        user.save()
        return user

    def setUp(self):
        self.stale = self._user("acct_stale", days_since_login=500)
        self.never = self._user("acct_never")
        self.recent = self._user("acct_recent", days_since_login=5)
        self.new = self._user("acct_new", joined_days_ago=3)
        self.admin = self._user("acct_admin", days_since_login=500, is_superuser=True, is_staff=True)
        self.service = self._user("acct_service", days_since_login=500)
        self.token = self._user("acct_token", days_since_login=500)
        Token.objects.create(user=self.token)
        self.slack = self._user("acct_slack")

    def _run(self, *args):
        out = StringIO()
        with override_settings(INACTIVE_USER_EXEMPT_USERNAMES=["acct_service"], SLACK_SERVICE_USERNAME="acct_slack"):
            call_command("deactivate_inactive_users", "--days", "365", *args, stdout=out)
        return out.getvalue()

    def _active(self):
        return set(User.objects.filter(is_active=True, username__startswith="acct_").values_list("username", flat=True))

    def test_dry_run_counts_only_and_changes_nothing(self):
        before = self._active()
        out = self._run()
        self.assertEqual(self._active(), before)
        self.assertIn("dry run: 2 accounts", out)
        self.assertIn("1 superusers, 2 exempt service accounts, 1 token holders", out)
        self.assertNotIn("acct_", out)

    def test_apply_deactivates_only_inactive_unexempt_accounts(self):
        out = self._run("--apply")
        self.assertIn("deactivated 2 accounts", out)
        self.assertNotIn("acct_", out)
        self.assertEqual(self._active(), {"acct_recent", "acct_new", "acct_admin", "acct_service", "acct_token",
                                          "acct_slack"})

    def test_include_token_holders(self):
        self._run("--apply", "--include-token-holders")
        self.assertNotIn("acct_token", self._active())

    def test_days_must_be_positive(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("deactivate_inactive_users", "--days", "0", stdout=StringIO())


class ApiThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser("throttle_admin", "throttle_admin@example.com", "pw-123-abc")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def test_user_rate_limit_returns_429(self):
        from rest_framework.throttling import UserRateThrottle
        from rest_framework.views import APIView

        # Throttles are off by default; switch the user throttle on as the flag would.
        with mock.patch.object(APIView, "throttle_classes", [UserRateThrottle]), \
                mock.patch.object(UserRateThrottle, "THROTTLE_RATES", {"user": "2/min", "anon": "2/min"}):
            codes = [self.client.get("/api/transientstatuses/").status_code for _ in range(3)]
        self.assertEqual(codes[:2], [200, 200])
        self.assertEqual(codes[2], 429)


@override_settings(AXES_FAILURE_LIMIT=3, AXES_COOLOFF_TIME=timedelta(minutes=30))
class LoginLockoutTests(TestCase):
    """Runs when django-axes is installed and LOGIN_LOCKOUT_ENABLED is on (CI sets YSE_LOGIN_LOCKOUT=1)."""

    PASSWORD = "pw-123-abc"

    @classmethod
    def setUpClass(cls):
        if not getattr(settings, "LOGIN_LOCKOUT_ENABLED", False):
            raise __import__("unittest").SkipTest("login lockout is not enabled in this environment")
        super().setUpClass()

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("lock_user", password=self.PASSWORD)
        self.other = User.objects.create_user("lock_other", password=self.PASSWORD)

    def _form_login(self, username, password):
        client = Client()
        resp = client.post("/login/", {"username": username, "password": password})
        return resp, client

    def _fail(self, n, username="lock_user"):
        for _ in range(n):
            self._form_login(username, "not-the-password")

    def test_login_form_locks_after_failure_limit(self):
        resp, client = self._form_login("lock_user", self.PASSWORD)
        self.assertEqual(resp.status_code, 302)
        self._fail(3)
        resp, client = self._form_login("lock_user", self.PASSWORD)
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("_auth_user_id", client.session)
        resp, _ = self._form_login("lock_other", self.PASSWORD)
        self.assertEqual(resp.status_code, 302)

    def test_success_before_limit_resets_count(self):
        self._fail(2)
        self.assertEqual(self._form_login("lock_user", self.PASSWORD)[0].status_code, 302)
        self._fail(2)
        self.assertEqual(self._form_login("lock_user", self.PASSWORD)[0].status_code, 302)

    def test_api_basic_auth_counts_failures(self):
        def basic(password):
            token = base64.b64encode(("lock_user:%s" % password).encode()).decode()
            return Client().get("/api/transientstatuses/", HTTP_AUTHORIZATION="Basic " + token)

        self.assertEqual(basic(self.PASSWORD).status_code, 200)
        for _ in range(3):
            self.assertIn(basic("not-the-password").status_code, (401, 403))
        self.assertIn(basic(self.PASSWORD).status_code, (401, 403))

    def test_authenticate_without_request_still_works(self):
        self.assertTrue(Client().login(username="lock_user", password=self.PASSWORD))

    def test_force_login_session_resolves_the_user(self):
        client = Client()
        client.force_login(self.user)
        self.assertEqual(client.get("/api/transientstatuses/").status_code, 200)
