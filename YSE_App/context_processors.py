"""Template context shared by every page."""

from django.conf import settings


def feedback(request):
    """``feedback_enabled`` for the navbar Feedback button (#443; off by default)."""
    return {"feedback_enabled": bool(getattr(settings, "FEEDBACK_ENABLED", False))}
