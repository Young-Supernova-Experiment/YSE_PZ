"""Require a second factor for staff and superuser browser sessions (#512).

Installed only when STAFF_MFA_REQUIRED is on, after django-otp's OTPMiddleware
(which sets ``request.user.otp_device`` / ``is_verified()`` from the session) and
after LoginRequiredMiddleware.

``request.user`` here is the session user set by AuthenticationMiddleware.
Requests that authenticate per request (DRF token or Basic auth, and the
``login_or_basic_auth_required`` decorator) carry no session user at this point,
so crons, brokers and scripts are not affected; nor are non-staff users.

For a signed-in staff or superuser session that has not entered a code:

* the two-factor pages, sign-in, sign-out and the public pages of
  ``YSE_App.login_required.PUBLIC_URL_NAMES`` are served as usual, and so are
  the token links of ``TOKEN_LINK_URL_NAMES`` (team invitation, email confirmation);
* a GET or HEAD for any other page (including the admin and SQL Explorer)
  redirects to the code page, or to setup if the user has no authenticator
  yet; other methods and API views get 403;
* during the grace period (STAFF_MFA_ENFORCE_AFTER in the future) a user
  without an authenticator is offered setup once per session and may skip it.
"""

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden
from django.shortcuts import resolve_url

from YSE_App import login_required as _login_required
from YSE_App import staff_mfa

MFA_URL_NAMES = frozenset({
    'staff_mfa_verify',
    'staff_mfa_setup',
    'staff_mfa_backup_codes',
})
SIGN_IN_OUT_URL_NAMES = frozenset({'auth_login', 'auth_logout', 'login', 'logout'})
# Links that carry a secret token and give no data access by themselves (develop
# backport, 2026-10-15). Sending an unverified staff session to the code page from
# here would copy the token into ``?next=``. ``group_invite_link`` only stores the
# token's hash in the session and redirects to the token-free ``/teams/join/``, which
# is checked as usual; ``signup_verify_email`` confirms the address the token names.
TOKEN_LINK_URL_NAMES = frozenset({'group_invite_link', 'signup_verify_email'})

_FILE_SERVING_MODULES = frozenset({'django.views.static', 'django.contrib.staticfiles.views'})


def _is_sign_out_view(view_func):
    # accounts/logout/ and api-auth/logout/ route to views.auth_logout without a URL name.
    return (getattr(view_func, '__module__', None) == 'YSE_App.views'
            and getattr(view_func, '__name__', None) == 'auth_logout')


def view_is_exempt(view_func, resolver_match):
    if resolver_match is not None:
        name = resolver_match.url_name
        if resolver_match.namespaces:
            # Only sign-in / sign-out inside namespaces (admin:logout,
            # rest_framework:login); admin:index is not the public 'index'.
            if name in ('login', 'logout'):
                return True
        elif (name in MFA_URL_NAMES or name in SIGN_IN_OUT_URL_NAMES or name in TOKEN_LINK_URL_NAMES
              or name in _login_required.PUBLIC_URL_NAMES):
            return True
    if _is_sign_out_view(view_func):
        return True
    return getattr(view_func, '__module__', None) in _FILE_SERVING_MODULES


class StaffMFAMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not staff_mfa.mfa_enabled():
            return None
        user = getattr(request, 'user', None)
        if not staff_mfa.mfa_applies_to(user):
            return None
        if staff_mfa.is_verified(user):
            return None
        if view_is_exempt(view_func, getattr(request, 'resolver_match', None)):
            return None

        browser_get = request.method in ('GET', 'HEAD') and not _login_required._is_drf_view(view_func)
        if staff_mfa.has_confirmed_totp(user):
            target = 'staff_mfa_verify'
        elif staff_mfa.in_grace_period():
            if not browser_get or request.session.get(staff_mfa.GRACE_NOTICE_SESSION_KEY):
                return None
            request.session[staff_mfa.GRACE_NOTICE_SESSION_KEY] = True
            target = 'staff_mfa_setup'
        else:
            target = 'staff_mfa_setup'

        if not browser_get:
            return HttpResponseForbidden('Two-factor verification required.')
        return redirect_to_login(request.get_full_path(), resolve_url(target), 'next')
