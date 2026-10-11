"""Deny-by-default login requirement for every view (#496).

``LoginRequiredMiddleware`` redirects an anonymous request to ``LOGIN_URL``
unless the resolved view is public. A view is public when one of these holds:

* its URL name is in ``PUBLIC_URL_NAMES`` (the landing page and survey pages,
  login/logout, the ``django.contrib.auth`` password-reset pages, the Slack
  events endpoint and the external-service run callback, which check their own
  signatures or tokens);
* its URL namespace is in ``PUBLIC_URL_NAMESPACES`` (Django admin and the DRF
  browsable-API login, which have their own login pages and checks);
* it is marked with ``@login_exempt``;
* it is a Django REST framework view (DRF authenticates Basic, token and
  session requests itself, and its permission classes decide);
* it is wrapped in ``YSE_App.basicauth.login_or_basic_auth_required``, which
  accepts a session or HTTP Basic credentials and refuses everything else
  (crons and broker scripts call these with Basic auth, so they never have a
  session user);
* it is Django's static or media file view.

The decision uses the resolved view and URL name, never the request path, so it
is the same under a sub-path deployment (``SCRIPT_NAME`` such as
``/yse_experimental``). The redirect is built the same way as
``django.contrib.auth.decorators.login_required``.

Rollback: ``LOGIN_REQUIRED_MIDDLEWARE = False`` in ``[site_settings]`` of
settings.ini, or ``YSE_LOGIN_REQUIRED_MIDDLEWARE=0`` in the environment, turns
the check off (views keep their own decorators). See docs/ziggy-runbook.md.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import resolve_url

# URL names (from YSE_App/urls.py and django.contrib.auth.urls) that anonymous
# users may open.
PUBLIC_URL_NAMES = frozenset({
    # public landing page and survey pages (YSE_App/surveypages.py)
    'index',
    'survey',
    'news',
    'team',
    'contact',
    'acknowledgements',
    # sign in / sign out
    'auth_login',
    'auth_logout',
    # django.contrib.auth.urls (mounted at accounts/)
    'login',
    'logout',
    'password_reset',
    'password_reset_done',
    'password_reset_confirm',
    'password_reset_complete',
    # external callers that authenticate with a signature or a token
    'slack_events',
    'external_service_run_callback',
})

# URL namespaces whose views run their own login checks.
PUBLIC_URL_NAMESPACES = frozenset({
    'admin',
    'rest_framework',
})

# Modules of Django's file-serving views (only routed in development).
_FILE_SERVING_MODULES = frozenset({
    'django.views.static',
    'django.contrib.staticfiles.views',
})

_BASIC_AUTH_QUALNAME = 'login_or_basic_auth_required.'


def login_exempt(view):
    """Mark a view (function or class) as reachable without a login."""
    view.login_exempt = True
    return view


def login_required_enabled():
    return bool(getattr(settings, 'LOGIN_REQUIRED_MIDDLEWARE', True))


def _wrapped_chain(func):
    """``func`` and everything it wraps (``functools.wraps`` / ``__wrapped__``)."""
    seen = set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        yield func
        func = getattr(func, '__wrapped__', None) or getattr(func, '__func__', None)


def _marked(func):
    for f in _wrapped_chain(func):
        if getattr(f, 'login_exempt', False):
            return True
        if _BASIC_AUTH_QUALNAME in (getattr(f, '__qualname__', '') or ''):
            return True
        if getattr(f, '__module__', None) in _FILE_SERVING_MODULES:
            return True
    return False


def _is_drf_view(view_func):
    cls = getattr(view_func, 'cls', None)
    if cls is None:
        return False
    try:
        from rest_framework.views import APIView
    except ImportError:  # pragma: no cover
        return False
    return isinstance(cls, type) and issubclass(cls, APIView)


def view_is_public(view_func, resolver_match=None):
    """True when anonymous users may reach ``view_func`` (see the module docstring)."""
    if resolver_match is not None:
        if resolver_match.url_name in PUBLIC_URL_NAMES:
            return True
        if PUBLIC_URL_NAMESPACES.intersection(resolver_match.namespaces or ()):
            return True
    if _marked(view_func):
        return True
    if _is_drf_view(view_func):
        return True
    view_class = getattr(view_func, 'view_class', None)
    if view_class is not None:
        if getattr(view_class, 'login_exempt', False):
            return True
        if _marked(getattr(view_class, 'dispatch', None)):
            return True
    return False


class LoginRequiredMiddleware:
    """Redirect anonymous requests for non-public views to ``LOGIN_URL``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not login_required_enabled():
            return None
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return None
        if view_is_public(view_func, getattr(request, 'resolver_match', None)):
            return None
        return self._redirect_to_login(request)

    @staticmethod
    def _redirect_to_login(request):
        # Same construction as django.contrib.auth.decorators.user_passes_test.
        path = request.build_absolute_uri()
        resolved_login_url = resolve_url(settings.LOGIN_URL)
        login_scheme, login_netloc = urlparse(resolved_login_url)[:2]
        current_scheme, current_netloc = urlparse(path)[:2]
        if ((not login_scheme or login_scheme == current_scheme)
                and (not login_netloc or login_netloc == current_netloc)):
            path = request.get_full_path()
        return redirect_to_login(path, resolved_login_url, REDIRECT_FIELD_NAME)
