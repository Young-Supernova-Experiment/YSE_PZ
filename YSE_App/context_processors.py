"""Template context shared by every page."""

from django.conf import settings


def feedback(request):
    """``feedback_enabled`` for the navbar Feedback button (#443; off by default)."""
    return {"feedback_enabled": bool(getattr(settings, "FEEDBACK_ENABLED", False))}


def csrf_cookie_name(request):
    """``csrf_cookie_name`` for the inline scripts that read the CSRF cookie (#637: the cookie
    name is per stack, [site_settings] CSRF_COOKIE_NAME, so templates must not hard-code
    ``csrftoken``). SQL Explorer's own base template reads the same variable."""
    return {"csrf_cookie_name": getattr(settings, "CSRF_COOKIE_NAME", "csrftoken") or "csrftoken"}
