"""Per-user UI preview switch for the Tier 2 UX work (#947).

A signed-in browser user can opt in to the new layouts from the user menu; everyone
else keeps today's pages. Templates read ``ui_preview`` (context processor
``YSE_App.context_processors.ui_preview``); Python code calls ``ui_preview_enabled``.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.handlers.wsgi import get_script_name
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

PREVIEW_COOKIE = "yse-ui-preview"
PREVIEW_COOKIE_MAX_AGE = 180 * 24 * 60 * 60


def ui_preview_enabled(request):
    """True when this request should see the preview layout.

    Only for a signed-in browser user who turned the preview on (cookie ``"1"``), or for
    every signed-in user when ``UI_PREVIEW_DEFAULT`` is set. Never for anonymous users or
    for script requests that authenticate with an Authorization header (Basic/Token).
    """
    if request is None:
        return False
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if request.META.get("HTTP_AUTHORIZATION"):
        return False
    if getattr(settings, "UI_PREVIEW_DEFAULT", False):
        return True
    return request.COOKIES.get(PREVIEW_COOKIE) == "1"


def _cookie_path(request):
    # The stack's URL prefix (/yse/, /yse_test/, /yse_experimental/), so stacks on one
    # host keep separate switches. Same source as Django's script prefix:
    # FORCE_SCRIPT_NAME if set, else the WSGI SCRIPT_NAME.
    prefix = get_script_name(request.META) or ""
    return prefix.rstrip("/") + "/"


@require_POST
@login_required
def ui_preview_switch(request):
    """Turn the preview on (``on=1``) or off (``on=0``), then go back to ``next``."""
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = reverse("dashboard")
    response = HttpResponseRedirect(next_url)
    path = _cookie_path(request)
    if request.POST.get("on") == "1":
        response.set_cookie(
            PREVIEW_COOKIE,
            "1",
            max_age=PREVIEW_COOKIE_MAX_AGE,
            path=path,
            secure=request.is_secure(),
            httponly=True,
            samesite="Lax",
        )
    else:
        response.delete_cookie(PREVIEW_COOKIE, path=path, samesite="Lax")
    return response
