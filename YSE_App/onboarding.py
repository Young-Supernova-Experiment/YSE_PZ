"""Onboarding gate: accept the current User Agreement, then complete the profile (#630).

``OnboardingGateMiddleware`` (after AuthenticationMiddleware and
MessageMiddleware) redirects **authenticated browser requests for HTML pages**:

1. no acceptance of the current ``UserAgreement`` -> ``/agreement/?next=...``;
2. researcher profile missing or incomplete -> ``/profile/edit/?next=...``.

It never touches requests that crons, scripts, brokers and integrations make:

* anything under ``/api/`` or ``/api-auth/`` (DRF, external-service callbacks);
* any request with an ``Authorization`` header (Basic or Token);
* views marked ``onboarding_exempt``: every view wrapped in
  ``login_or_basic_auth_required`` (the ingest endpoints add_transient,
  add_transient_phot, add_transient_spec, upload_spectrum, add_gw_candidate,
  add_yse_survey_*, the downloads, query_api, ...) and the views decorated with
  ``onboarding_exempt`` below;
* the URL names in ``EXEMPT_URL_NAMES`` (Slack events, login/logout, signup and
  its email confirmation link, password reset and change, the agreement, research
  consent, profile and acknowledgements pages, and on experimental the two-factor
  pages and the feedback POST);
* DRF views wherever they are routed;
* static and media files, ``.json`` endpoints (status and health polling),
  AJAX/fetch requests and non-GET methods;
* ``/admin/`` for superusers (other staff go through the gate).

``ONBOARDING_GATE_ENABLED = False`` (settings.ini ``[site_settings]`` or env
``YSE_ONBOARDING_GATE=0``) turns the whole gate off.

Research consent reminders (#744): at login, a user who has never answered the research consent
form and is due a reminder (``research_consent.reminder_due``) gets a session flag; the first page
view that passes the gate then redirects once to ``/research-consent/?next=...``. Every choice
there, including "Not now", continues to the page, so this is never a gate.

A session that has passed stores the agreement version, the key state and the
rules version it passed with, so a page view normally costs no queries; a new
current agreement version, a newly installed research key or a rules change
re-checks.
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

SESSION_KEY = "_yse_onboarding_ok"
REMIND_KEY = "_yse_research_consent_remind"
CURRENT_AGREEMENT_CACHE_KEY = "yse:onboarding:current_agreement_id"
CURRENT_AGREEMENT_CACHE_SECONDS = 60

EXEMPT_PATH_PREFIXES = ("/api/", "/api-auth/", "/static/", "/media/", "/slack/")

EXEMPT_URL_NAMES = frozenset({
    # login and logout (site views and django.contrib.auth.urls)
    "auth_login", "auth_logout", "login", "logout",
    "password_change", "password_change_done",
    "password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete",
    # self-service signup and its emailed confirmation link (#448)
    "signup", "signup_verify_email",
    # the onboarding pages themselves and the acknowledgements page they link to
    "agreement", "research_consent", "researcher_profile", "researcher_profile_edit", "researcher_advisor_suggest",
    "researcher_student_add", "researcher_student_update", "researcher_profile_confirm",
    "acknowledgements",
    # integrations and machine endpoints
    "slack_events", "external_service_run_callback", "query_api",
    # experimental-only pages (backport of develop, 2026-10-12): the two-factor
    # setup and verify pages (StaffMFAMiddleware runs first and sends staff there;
    # gating them would loop between the two) and the feedback button's POST target.
    "staff_mfa_verify", "staff_mfa_setup", "staff_mfa_backup_codes",
    "feedback_submit",
})


def onboarding_exempt(view):
    """Mark a view as never gated (machine endpoints, essential pages)."""
    @wraps(view)
    def _wrapped(*args, **kwargs):
        return view(*args, **kwargs)
    _wrapped.onboarding_exempt = True
    return _wrapped


def gate_enabled() -> bool:
    return bool(getattr(settings, "ONBOARDING_GATE_ENABLED", True))


def current_agreement_id():
    """Primary key of the current UserAgreement (cached briefly), or None."""
    value = cache.get(CURRENT_AGREEMENT_CACHE_KEY)
    if value is None:
        from YSE_App.models import UserAgreement
        row = UserAgreement.objects.filter(is_current=True).order_by("-effective_date", "-id").values("pk").first()
        value = row["pk"] if row else 0
        cache.set(CURRENT_AGREEMENT_CACHE_KEY, value, CURRENT_AGREEMENT_CACHE_SECONDS)
    return value or None


def invalidate_current_agreement() -> None:
    cache.delete(CURRENT_AGREEMENT_CACHE_KEY)


def has_accepted(user, agreement_id) -> bool:
    from YSE_App.models import AgreementAcceptance
    return AgreementAcceptance.objects.filter(user=user, agreement_id=agreement_id).exists()


def _session_token(agreement_id) -> str:
    from YSE_App.services import research_crypto
    from YSE_App.services.researcher_profile import RULES_VERSION
    return "%s:%s:%s" % (agreement_id or 0, research_crypto.current_fingerprint()[:16] or "nokey", RULES_VERSION)


def clear_session_flag(request) -> None:
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(SESSION_KEY, None)


def safe_next(request, value, fallback=None):
    """``value`` when it is a same-site URL, else ``fallback`` (default: the dashboard)."""
    if value and url_has_allowed_host_and_scheme(
            value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return value
    return fallback if fallback is not None else reverse("dashboard")


def redirect_with_next(url_name, request):
    return HttpResponseRedirect("%s?%s" % (reverse(url_name), urlencode({"next": request.get_full_path()})))


def pending_step(request):
    """``"agreement"``, ``"profile"`` or ``None`` for an authenticated user."""
    from YSE_App.services.researcher_profile import is_complete
    agreement_id = current_agreement_id()
    if agreement_id and not has_accepted(request.user, agreement_id):
        return "agreement"
    if not is_complete(request.user):
        return "profile"
    return None


def _is_page_request(request) -> bool:
    """True for a browser navigation that expects HTML."""
    if request.method not in ("GET", "HEAD"):
        return False
    meta = request.META
    if meta.get("HTTP_X_REQUESTED_WITH") == "XMLHttpRequest":
        return False
    fetch_mode = meta.get("HTTP_SEC_FETCH_MODE")
    if fetch_mode and fetch_mode != "navigate":
        return False
    if request.path_info.endswith(".json"):
        return False
    accept = meta.get("HTTP_ACCEPT", "")
    if accept and "text/html" not in accept and "*/*" not in accept and "application/xhtml" not in accept:
        return False
    return True


def is_exempt(request, view_func) -> bool:
    if "HTTP_AUTHORIZATION" in request.META:
        return True
    if getattr(view_func, "onboarding_exempt", False):
        return True
    # DRF views anywhere in the URL tree (not only under /api/) are never gated.
    from YSE_App import login_required as _login_required
    if _login_required._is_drf_view(view_func):
        return True
    path = request.path_info
    if path.startswith(EXEMPT_PATH_PREFIXES):
        return True
    for url in (getattr(settings, "STATIC_URL", None), getattr(settings, "MEDIA_URL", None)):
        if url and url.startswith("/") and url != "/" and (path.startswith(url) or request.path.startswith(url)):
            return True
    match = getattr(request, "resolver_match", None)
    if match is not None:
        if match.url_name in EXEMPT_URL_NAMES:
            return True
        if match.namespace in ("rest_framework",):
            return True
        if match.app_name == "admin" or path.startswith("/admin/"):
            return bool(request.user.is_superuser)
    elif path.startswith("/admin/"):
        return bool(request.user.is_superuser)
    return False


class OnboardingGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not gate_enabled():
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if is_exempt(request, view_func) or not _is_page_request(request):
            return None
        agreement_id = current_agreement_id()
        token = _session_token(agreement_id)
        session = getattr(request, "session", None)
        if session is not None and session.get(SESSION_KEY) == token:
            return _consent_reminder(request)
        step = pending_step(request)
        if step == "agreement":
            return redirect_with_next("agreement", request)
        if step == "profile":
            return redirect_with_next("researcher_profile_edit", request)
        if session is not None:
            session[SESSION_KEY] = token
        return _consent_reminder(request)


def _consent_reminder(request):
    """Redirect once to the research consent form when the login flagged a reminder (#744)."""
    session = getattr(request, "session", None)
    if session is None or not session.get(REMIND_KEY):
        return None
    session.pop(REMIND_KEY, None)
    from YSE_App.services import research_consent
    if not research_consent.reminder_due(request.user):
        return None
    research_consent.record_prompt(request.user)
    return redirect_with_next("research_consent", request)


def on_user_logged_in(sender, request, user, **kwargs):
    """Count the login toward the reminder schedule and flag a due reminder (#744)."""
    try:
        from YSE_App.services import research_consent
        research_consent.note_login(user)
        session = getattr(request, "session", None)
        if session is not None and research_consent.reminder_due(user):
            session[REMIND_KEY] = True
    except Exception:  # a reminder must never break a login
        import logging
        logging.getLogger(__name__).exception("research consent reminder check failed")
