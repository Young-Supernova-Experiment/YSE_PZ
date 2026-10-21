"""Favorite transients (#323): star toggle, ids JSON, ``/my/favorites/`` and the dashboard fragment."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from django_tables2 import RequestConfig

from YSE_App.models import Transient, TransientStatus
from YSE_App.services import favorites as svc
from YSE_App.services.visibility import user_can_view_transient


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json"


def _safe_next(request, fallback):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and (candidate.startswith("/") or url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure())):
        return candidate
    return fallback


@login_required
@require_POST
def favorite_toggle(request, transient_id):
    """Star / unstar (``state=on|off`` forces one); JSON ``{favorite, count}`` for XHR, else redirect."""
    transient = get_object_or_404(Transient, pk=transient_id)
    if not user_can_view_transient(request.user, transient.id):
        if _wants_json(request):
            return JsonResponse({"error": "You do not have access to this transient."}, status=403)
        messages.error(request, "You do not have access to this transient.")
        return redirect(_safe_next(request, reverse("my_favorites")))
    wanted = request.POST.get("state")
    if wanted == "on":
        svc.add(request.user, transient)
        state = True
    elif wanted == "off":
        svc.remove(request.user, transient)
        state = False
    else:
        state = svc.toggle(request.user, transient)
    _mine, count = svc.favorite_state(request.user, transient.id)
    if _wants_json(request):
        return JsonResponse({"transient": transient.id, "favorite": state, "count": count})
    messages.success(request, "%s %s your favorites." % (transient.name, "added to" if state else "removed from"))
    return redirect(_safe_next(request, reverse("transient_detail", kwargs={"slug": transient.slug})))


@login_required
@require_GET
def favorite_ids(request):
    """JSON ``{"ids": [...]}``: the transients the user starred (paints the table stars, one fetch per page)."""
    return JsonResponse({"ids": svc.favorite_ids(request.user)})


def _favorites_table(request, per_page=50):
    from YSE_App.table_utils import FavoriteTransientTable

    table = FavoriteTransientTable(svc.favorites_for_user(request.user), prefix="fav-")
    RequestConfig(request, paginate={"per_page": per_page}).configure(table)
    return table


@login_required
def my_favorites(request):
    table = _favorites_table(request)
    return render(request, "YSE_App/my_favorites.html", {
        "table": table,
        "count": table.page.paginator.count if hasattr(table, "page") else len(table.rows),
        "all_transient_statuses": TransientStatus.objects.order_by("name"),
        "batch_minutes": svc.batch_minutes(),
    })


@login_required
def my_favorites_section(request):
    """Personal-dashboard fragment (loaded after the page, like the saved-query sections)."""
    table = _favorites_table(request, per_page=25)
    return render(request, "YSE_App/my_favorites_section.html", {
        "table": table,
        "count": table.page.paginator.count if hasattr(table, "page") else len(table.rows),
        "all_transient_statuses": TransientStatus.objects.order_by("name"),
    })
