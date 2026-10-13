"""Two-factor sign-in for staff and superusers (django-otp TOTP, #512).

The settings tests and the "flag off" tests run in the main suite (flag off).
The flow tests need django-otp installed and STAFF_MFA_REQUIRED on when the
test database is created; CI runs this module a second time that way
(YSE_STAFF_MFA_REQUIRED=1, .github/workflows/ci.yml).
"""

import base64
import datetime
import sys
import types
import unittest
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from YSE_App.tests.test_settings_hardening import _load_settings

MFA_ON = bool(getattr(settings, 'STAFF_MFA_REQUIRED', False))
_needs_mfa = unittest.skipUnless(MFA_ON, 'needs django-otp and STAFF_MFA_REQUIRED (CI runs this module with it on)')
_PASSWORD = 'pw-for-mfa-tests'


def _security(**keys):
    return "\n[security]\n" + "\n".join("%s: %s" % kv for kv in keys.items())


class StaffMFASettingsTests(SimpleTestCase):
    def test_default_off(self):
        mod = _load_settings(debug="True")
        self.assertIs(mod.STAFF_MFA_REQUIRED, False)
        self.assertIsNone(mod.STAFF_MFA_ENFORCE_AFTER)
        self.assertFalse([a for a in mod.INSTALLED_APPS if a.startswith('django_otp')])
        self.assertNotIn('django_otp.middleware.OTPMiddleware', mod.MIDDLEWARE)
        self.assertNotIn('YSE_App.staff_mfa.middleware.StaffMFAMiddleware', mod.MIDDLEWARE)

    def test_flag_without_package_stays_off(self):
        with mock.patch.dict(sys.modules, {"django_otp": None}):
            with self.assertLogs("yse_settings_under_test", level="WARNING"):
                mod = _load_settings(debug="True", site_extra=_security(STAFF_MFA_REQUIRED="True"))
        self.assertIs(mod.STAFF_MFA_REQUIRED, False)
        self.assertNotIn('django_otp', mod.INSTALLED_APPS)
        self.assertNotIn('YSE_App.staff_mfa.middleware.StaffMFAMiddleware', mod.MIDDLEWARE)

    def test_flag_with_package_wires_otp(self):
        with mock.patch.dict(sys.modules, {"django_otp": types.ModuleType("django_otp")}):
            mod = _load_settings(debug="True", site_extra=_security(
                STAFF_MFA_REQUIRED="True", STAFF_MFA_ENFORCE_AFTER="2026-11-01"))
        self.assertIs(mod.STAFF_MFA_REQUIRED, True)
        for app in ('django_otp', 'django_otp.plugins.otp_totp', 'django_otp.plugins.otp_static'):
            self.assertIn(app, mod.INSTALLED_APPS)
        mw = list(mod.MIDDLEWARE)
        auth = mw.index('django.contrib.auth.middleware.AuthenticationMiddleware')
        self.assertEqual(mw[auth + 1], 'django_otp.middleware.OTPMiddleware')
        login_required = mw.index('YSE_App.login_required.LoginRequiredMiddleware')
        self.assertEqual(mw[login_required + 1], 'YSE_App.staff_mfa.middleware.StaffMFAMiddleware')
        self.assertEqual(mod.STAFF_MFA_ENFORCE_AFTER, datetime.date(2026, 11, 1))
        self.assertEqual(mod.OTP_TOTP_ISSUER, 'YSE_PZ')
        self.assertGreater(mod.OTP_TOTP_THROTTLE_FACTOR, 0)
        self.assertGreater(mod.OTP_STATIC_THROTTLE_FACTOR, 0)
        self.assertIs(mod.OTP_ADMIN_HIDE_SENSITIVE_DATA, True)
        self.assertIn('otp_', mod.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)

    def test_env_var_turns_it_on(self):
        with mock.patch.dict(sys.modules, {"django_otp": types.ModuleType("django_otp")}):
            mod = _load_settings(debug="True", env={"YSE_STAFF_MFA_REQUIRED": "1"})
        self.assertIs(mod.STAFF_MFA_REQUIRED, True)

    def test_bad_grace_date_rejected(self):
        with mock.patch.dict(sys.modules, {"django_otp": types.ModuleType("django_otp")}):
            with self.assertRaises(ImproperlyConfigured):
                _load_settings(debug="True", site_extra=_security(
                    STAFF_MFA_REQUIRED="True", STAFF_MFA_ENFORCE_AFTER="next month"))


@unittest.skipIf(MFA_ON, 'checks the default (flag off) stack')
class StaffMFAFlagOffTests(TestCase):
    def test_staff_session_unchanged(self):
        staff = User.objects.create_user('mfa_off_staff', password=_PASSWORD, is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(reverse('dashboard')).status_code, 200)
        self.assertEqual(client.get('/admin/').status_code, 200)

    def test_pages_not_routed(self):
        with self.assertRaises(NoReverseMatch):
            reverse('staff_mfa_verify')


def _totp_code(device):
    from django_otp.oath import TOTP

    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return '%06d' % totp.token()


def _enroll(user, backup_codes=()):
    """A confirmed authenticator (and optional backup codes) for ``user``."""
    from django_otp.plugins.otp_static.models import StaticDevice
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = TOTPDevice.objects.create(user=user, name='authenticator', confirmed=True)
    if backup_codes:
        static = StaticDevice.objects.create(user=user, name='backup codes', confirmed=True)
        for code in backup_codes:
            static.token_set.create(token=code)
    return device


@_needs_mfa
class StaffMFAFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user('mfa_staff', password=_PASSWORD, is_staff=True)
        cls.superuser = User.objects.create_user('mfa_super', password=_PASSWORD, is_staff=True, is_superuser=True)
        cls.member = User.objects.create_user('mfa_member', password=_PASSWORD)

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _assert_redirects_to(self, response, url_name):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(reverse(url_name)), response['Location'])

    # -- who is affected -------------------------------------------------

    def test_staff_without_device_sent_to_setup(self):
        response = self._client(self.staff).get(reverse('dashboard'))
        self._assert_redirects_to(response, 'staff_mfa_setup')
        self.assertIn('next=', response['Location'])

    def test_superuser_without_device_sent_to_setup(self):
        self._assert_redirects_to(self._client(self.superuser).get(reverse('dashboard')), 'staff_mfa_setup')

    def test_staff_with_device_sent_to_code_page(self):
        _enroll(self.staff)
        self._assert_redirects_to(self._client(self.staff).get(reverse('dashboard')), 'staff_mfa_verify')

    def test_admin_and_explorer_covered(self):
        _enroll(self.superuser)
        client = self._client(self.superuser)
        self._assert_redirects_to(client.get('/admin/'), 'staff_mfa_verify')
        self._assert_redirects_to(client.get('/explorer/'), 'staff_mfa_verify')

    def test_unverified_staff_post_and_api_refused(self):
        _enroll(self.staff)
        client = self._client(self.staff)
        self.assertEqual(client.get('/api/transients/', {'limit': 1}).status_code, 403)
        self.assertEqual(client.post(reverse('dashboard')).status_code, 403)

    def test_non_staff_unaffected(self):
        self.assertEqual(self._client(self.member).get(reverse('dashboard')).status_code, 200)

    def test_non_staff_sent_on_from_mfa_pages(self):
        client = self._client(self.member)
        self.assertEqual(client.get(reverse('staff_mfa_setup')).status_code, 302)
        self.assertEqual(client.get(reverse('staff_mfa_verify')).status_code, 302)

    def test_token_and_basic_requests_unaffected(self):
        _enroll(self.staff)
        token = Token.objects.create(user=self.staff)
        response = Client().get('/api/transients/', {'limit': 1}, HTTP_AUTHORIZATION='Token %s' % token.key)
        self.assertEqual(response.status_code, 200)
        cred = base64.b64encode(('mfa_staff:%s' % _PASSWORD).encode()).decode()
        response = Client().get('/api/transients/', {'limit': 1}, HTTP_AUTHORIZATION='Basic ' + cred)
        self.assertEqual(response.status_code, 200)

    def test_basic_auth_decorated_view_unaffected(self):
        from YSE_App.tests.fixtures_minimal import create_minimal_transient

        _enroll(self.staff)
        transient = create_minimal_transient(self.staff, name='mfa-basic-sn')
        cred = base64.b64encode(('mfa_staff:%s' % _PASSWORD).encode()).decode()
        response = Client().get(reverse('download_data', kwargs={'slug': transient.slug}),
                                HTTP_AUTHORIZATION='Basic ' + cred)
        self.assertEqual(response.status_code, 200)

    def test_anonymous_still_sent_to_login(self):
        response = Client().get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('/mfa/', response['Location'])

    def test_sign_out_reachable_before_code(self):
        _enroll(self.staff)
        client = self._client(self.staff)
        self.assertEqual(client.get(reverse('auth_logout')).status_code, 200)
        client.post(reverse('auth_logout'))
        self.assertNotIn('_auth_user_id', client.session)

    @override_settings(STAFF_MFA_REQUIRED=False)
    def test_runtime_flag_off_means_no_check(self):
        _enroll(self.staff)
        self.assertEqual(self._client(self.staff).get(reverse('dashboard')).status_code, 200)

    # -- code page -------------------------------------------------------

    def test_wrong_code_rejected(self):
        device = _enroll(self.staff)
        client = self._client(self.staff)
        wrong = '%06d' % ((int(_totp_code(device)) + 500000) % 1000000)
        response = client.post(reverse('staff_mfa_verify'), {'token': wrong})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not accepted')
        self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_verify')

    def test_correct_code_lets_user_in(self):
        device = _enroll(self.staff)
        client = self._client(self.staff)
        old_key = client.session.session_key
        response = client.post(reverse('staff_mfa_verify'), {'token': _totp_code(device), 'next': '/admin/'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/admin/')
        self.assertNotEqual(client.session.session_key, old_key)
        self.assertEqual(client.get(reverse('dashboard')).status_code, 200)

    def test_full_sign_in_password_then_code(self):
        device = _enroll(self.staff)
        client = Client()
        response = client.post(reverse('auth_login'), {'username': 'mfa_staff', 'password': _PASSWORD})
        self.assertEqual(response.status_code, 302)
        self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_verify')
        client.post(reverse('staff_mfa_verify'), {'token': _totp_code(device)})
        self.assertEqual(client.get(reverse('dashboard')).status_code, 200)

    def test_off_site_next_ignored(self):
        device = _enroll(self.staff)
        response = self._client(self.staff).post(
            reverse('staff_mfa_verify'), {'token': _totp_code(device), 'next': 'https://elsewhere.example/'})
        self.assertEqual(response['Location'], reverse('dashboard'))

    @override_settings(OTP_TOTP_THROTTLE_FACTOR=60)
    def test_attempts_throttled(self):
        device = _enroll(self.staff)
        client = self._client(self.staff)
        good = _totp_code(device)
        wrong = '%06d' % ((int(good) + 500000) % 1000000)
        client.post(reverse('staff_mfa_verify'), {'token': wrong})
        # The next attempt right after a failure is refused even with the right code.
        response = client.post(reverse('staff_mfa_verify'), {'token': good})
        self.assertContains(response, 'Too many attempts')
        self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_verify')

    def test_backup_code_works_once(self):
        _enroll(self.staff, backup_codes=['abcd2345', 'efgh6723'])
        first = self._client(self.staff)
        response = first.post(reverse('staff_mfa_verify'), {'token': 'ABCD 2345'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(first.get(reverse('dashboard')).status_code, 200)

        second = self._client(self.staff)
        response = second.post(reverse('staff_mfa_verify'), {'token': 'abcd2345'})
        self.assertEqual(response.status_code, 200)
        self._assert_redirects_to(second.get(reverse('dashboard')), 'staff_mfa_verify')

    # -- setup -----------------------------------------------------------

    def test_setup_page_shows_qr_and_uri(self):
        response = self._client(self.staff).get(reverse('staff_mfa_setup'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<svg')
        self.assertContains(response, 'otpauth://totp/YSE_PZ%3Amfa_staff?')
        self.assertContains(response, 'issuer=YSE_PZ')
        self.assertIn('no-store', response['Cache-Control'])

    def test_setup_confirms_with_one_code_and_shows_backup_codes(self):
        from django_otp.plugins.otp_static.models import StaticToken
        from django_otp.plugins.otp_totp.models import TOTPDevice

        client = self._client(self.staff)
        client.get(reverse('staff_mfa_setup'))
        pending = TOTPDevice.objects.get(user=self.staff, confirmed=False)
        wrong = '%06d' % ((int(_totp_code(pending)) + 500000) % 1000000)
        response = client.post(reverse('staff_mfa_setup'), {'token': wrong})
        self.assertContains(response, 'not accepted')
        pending.refresh_from_db()
        self.assertFalse(pending.confirmed)
        pending.throttle_reset()

        response = client.post(reverse('staff_mfa_setup'), {'token': _totp_code(pending), 'next': '/admin/'})
        self.assertEqual(response.status_code, 200)
        pending.refresh_from_db()
        self.assertTrue(pending.confirmed)
        codes = list(StaticToken.objects.filter(device__user=self.staff).values_list('token', flat=True))
        self.assertEqual(len(codes), 10)
        for code in codes:
            self.assertContains(response, code)
        self.assertEqual(client.get(reverse('dashboard')).status_code, 200)

    def test_setup_same_key_across_reloads(self):
        client = self._client(self.staff)
        first = client.get(reverse('staff_mfa_setup')).context['otpauth_uri']
        self.assertEqual(client.get(reverse('staff_mfa_setup')).context['otpauth_uri'], first)

    def test_enrolled_but_unverified_cannot_replace_device(self):
        _enroll(self.staff)
        self._assert_redirects_to(self._client(self.staff).get(reverse('staff_mfa_setup')), 'staff_mfa_verify')

    def test_backup_codes_regenerate_requires_post_and_verified(self):
        device = _enroll(self.staff, backup_codes=['oldcode22'])
        client = self._client(self.staff)
        self.assertEqual(client.get(reverse('staff_mfa_backup_codes')).status_code, 405)
        client.post(reverse('staff_mfa_verify'), {'token': _totp_code(device)})
        response = client.post(reverse('staff_mfa_backup_codes'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['codes']), 10)
        self.assertNotIn('oldcode22', response.context['codes'])

    # -- grace period ----------------------------------------------------

    def test_grace_period_offers_setup_once(self):
        future = timezone.localdate() + datetime.timedelta(days=30)
        with override_settings(STAFF_MFA_ENFORCE_AFTER=future):
            client = self._client(self.staff)
            self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_setup')
            self.assertEqual(client.get(reverse('dashboard')).status_code, 200)

    def test_grace_period_still_requires_code_when_enrolled(self):
        _enroll(self.staff)
        future = timezone.localdate() + datetime.timedelta(days=30)
        with override_settings(STAFF_MFA_ENFORCE_AFTER=future):
            client = self._client(self.staff)
            self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_verify')

    def test_past_grace_date_enforces(self):
        past = timezone.localdate() - datetime.timedelta(days=1)
        with override_settings(STAFF_MFA_ENFORCE_AFTER=past):
            client = self._client(self.staff)
            self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_setup')
            self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_setup')

    # -- reset -----------------------------------------------------------

    def test_reset_command(self):
        from django_otp import user_has_device

        device = _enroll(self.staff, backup_codes=['abcd2345'])
        client = self._client(self.staff)
        client.post(reverse('staff_mfa_verify'), {'token': _totp_code(device)})
        out = StringIO()
        call_command('reset_staff_mfa', 'mfa_staff', stdout=out)
        self.assertIn('Removed 2', out.getvalue())
        self.assertFalse(user_has_device(self.staff, confirmed=None))
        # The verified session is no longer verified.
        self._assert_redirects_to(client.get(reverse('dashboard')), 'staff_mfa_setup')

    def test_admin_reset_action_superuser_only(self):
        from django_otp import user_has_device

        _enroll(self.staff)
        su_device = _enroll(self.superuser)
        admin_client = self._client(self.superuser)
        admin_client.post(reverse('staff_mfa_verify'), {'token': _totp_code(su_device)})
        changelist = reverse('admin:auth_user_changelist')
        self.assertContains(admin_client.get(changelist), 'reset_two_factor')
        admin_client.post(changelist, {'action': 'reset_two_factor', '_selected_action': [self.staff.pk]})
        self.assertFalse(user_has_device(self.staff, confirmed=None))
        self.assertTrue(user_has_device(self.superuser))

    def test_admin_reset_action_hidden_from_staff(self):
        from django.contrib.auth.models import Permission

        staff_device = _enroll(self.staff)
        self.staff.user_permissions.add(*Permission.objects.filter(codename__in=['view_user', 'change_user']))
        client = self._client(self.staff)
        client.post(reverse('staff_mfa_verify'), {'token': _totp_code(staff_device)})
        response = client.get(reverse('admin:auth_user_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'reset_two_factor')


@_needs_mfa
class StaffMFAWithOnboardingGateTests(TestCase):
    """Two-factor runs before the onboarding gate (develop backport, 2026-10-12).

    StaffMFAMiddleware sends unverified staff to the two-factor pages; the gate
    must not send them on to /agreement/ (which would bounce back: a loop).
    Once the code is entered, the gate asks for the agreement as usual.
    """

    def setUp(self):
        from YSE_App import onboarding
        from YSE_App.services import research_crypto
        from YSE_App.tests.test_onboarding_gate import GATE_ON

        research_crypto.reset_cache()
        onboarding.invalidate_current_agreement()
        self.addCleanup(onboarding.invalidate_current_agreement)
        override = override_settings(**GATE_ON)
        override.enable()
        self.addCleanup(override.disable)
        self.staff = User.objects.create_user('mfa_gate_staff', password=_PASSWORD, is_staff=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def _location(self, response):
        self.assertEqual(response.status_code, 302)
        return response['Location']

    def test_setup_page_is_not_gated(self):
        self.assertTrue(self._location(self.client.get(reverse('dashboard'))).startswith(reverse('staff_mfa_setup')))
        self.assertEqual(self.client.get(reverse('staff_mfa_setup')).status_code, 200)

    def test_code_page_is_not_gated_then_agreement_follows(self):
        device = _enroll(self.staff)
        self.assertTrue(self._location(self.client.get(reverse('dashboard'))).startswith(reverse('staff_mfa_verify')))
        self.assertEqual(self.client.get(reverse('staff_mfa_verify')).status_code, 200)
        self.client.post(reverse('staff_mfa_verify'), {'token': _totp_code(device)})
        self.assertTrue(self._location(self.client.get(reverse('dashboard'))).startswith(reverse('agreement')))
