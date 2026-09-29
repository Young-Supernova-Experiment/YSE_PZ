"""Candidate scanning page and its save / reject actions (issue #279)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from django.conf import settings

from YSE_App.brokers import CAPABILITY_LABELS, registry
from YSE_App.brokers.base import CUTOUTS, PHOTOMETRY, BrokerError
from YSE_App.brokers.filters import describe_criteria
from YSE_App.brokers import ingest
from YSE_App.models.candidate_models import BrokerFilter, Candidate
from YSE_App.models.enum_models import ObservationGroup, TransientStatus

SORTS = {
    "last_seen": ("-last_seen", "-id"),
    "mag": ("last_mag", "-last_seen"),
    "mjd": ("-last_mjd", "-id"),
    "rb": ("-rb", "-last_seen"),
    "discovery": ("-discovery_mjd", "-id"),
}


CUTOUT_ORDER = ("science", "template", "difference")


def _ordered_cutouts(urls):
    urls = dict(urls or {})
    ordered = {k: urls[k] for k in CUTOUT_ORDER if k in urls}
    ordered.update({k: v for k, v in urls.items() if k not in CUTOUT_ORDER})
    return ordered


def visible_filters(user):
    qs = BrokerFilter.objects.select_related("group").order_by("broker", "name")
    if user.is_staff or user.is_superuser:
        return qs
    return qs.filter(Q(group__isnull=True) | Q(group__in=user.groups.all()))


def _safe_next(request, fallback):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()},
                                                     require_https=request.is_secure()):
        return candidate
    return fallback


def candidate_queryset(request):
    """Candidates the user may scan, narrowed by the GET filters."""
    filters = visible_filters(request.user)
    qs = Candidate.objects.filter(Q(filters__in=filters) | Q(filters__isnull=True)).distinct()
    params = request.GET
    status = params.get("status", Candidate.NEW)
    if status and status != "all":
        qs = qs.filter(status=status)
    broker = params.get("broker") or ""
    if broker:
        qs = qs.filter(broker=broker)
    filter_id = params.get("filter") or ""
    if filter_id.isdigit():
        qs = qs.filter(filters__pk=int(filter_id))
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(alert_id__icontains=q) | Q(classification__icontains=q) | Q(transient__name__icontains=q))
    for key, field, op in (("mag_min", "last_mag", "gte"), ("mag_max", "last_mag", "lte"), ("rb_min", "rb", "gte")):
        raw = params.get(key)
        if raw not in (None, ""):
            try:
                qs = qs.filter(**{"%s__%s" % (field, op): float(raw)})
            except ValueError:
                pass
    sort = params.get("sort") if params.get("sort") in SORTS else "last_seen"
    return qs.select_related("transient", "status_changed_by").prefetch_related("filters").order_by(*SORTS[sort]), {
        "status": status, "broker": broker, "filter": filter_id, "q": q, "sort": sort,
        "mag_min": params.get("mag_min", ""), "mag_max": params.get("mag_max", ""), "rb_min": params.get("rb_min", ""),
    }


@login_required
def candidate_list(request):
    qs, active = candidate_queryset(request)
    page_size = int(getattr(settings, "BROKER_CANDIDATES_PAGE_SIZE", 50) or 50)
    page = Paginator(qs, page_size).get_page(request.GET.get("page"))
    providers = registry.all_providers()
    provider_info = {
        p.slug: {"slug": p.slug, "name": p.name, "available": p.available(), "capabilities": p.capability_set(),
                 "capability_labels": [CAPABILITY_LABELS[c] for c in p.capability_set()],
                 "has_cutouts": p.has(CUTOUTS), "has_photometry": p.has(PHOTOMETRY),
                 "reason": p.unavailable_reason()}
        for p in providers
    }
    counts = {
        row["status"]: row["n"]
        for row in Candidate.objects.order_by().values("status").annotate(n=Count("id"))
    }
    query_string = request.GET.copy()
    query_string.pop("page", None)
    rows = []
    for c in page.object_list:
        rows.append({
            "c": c,
            "broker_name": provider_info.get(c.broker, {}).get("name", c.broker),
            "cutouts": _ordered_cutouts(c.cutout_urls) if provider_info.get(c.broker, {}).get("has_cutouts") else {},
            "filters": list(c.filters.all()),
            "can_save": c.status != Candidate.SAVED and provider_info.get(c.broker, {}).get("available", False),
        })
    context = {
        "page": page,
        "rows": rows,
        "active": active,
        "filters": visible_filters(request.user),
        "providers": provider_info,
        "provider_list": [provider_info[p.slug] for p in providers],
        "counts": counts,
        "count_rows": [(s, label, counts.get(s, 0)) for s, label in Candidate.STATUS_CHOICES],
        "statuses": Candidate.STATUS_CHOICES,
        "transient_statuses": list(TransientStatus.objects.order_by("name").values_list("name", flat=True)),
        "obs_groups": list(ObservationGroup.objects.order_by("name").values_list("name", flat=True)),
        "sorts": list(SORTS),
        "query_string": query_string.urlencode(),
        "criteria": describe_criteria(),
    }
    return render(request, "YSE_App/candidates.html", context)


def _candidate_for_action(request, candidate_id):
    candidate = get_object_or_404(Candidate, pk=candidate_id)
    if not (request.user.is_staff or request.user.is_superuser):
        allowed = visible_filters(request.user)
        if candidate.filters.exists() and not candidate.filters.filter(pk__in=allowed.values("pk")).exists():
            return None
    return candidate


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", "")


@login_required
@require_POST
def candidate_save(request, candidate_id):
    candidate = _candidate_for_action(request, candidate_id)
    if candidate is None:
        return HttpResponseBadRequest("not your candidate")
    status = request.POST.get("status") or "New"
    obs_group = request.POST.get("obs_group") or None
    import_phot = request.POST.get("import_photometry", "1") not in ("0", "false", "off")
    try:
        transient = ingest.save_candidate(candidate, request.user, status=status, obs_group=obs_group,
                                          import_photometry=import_phot)
    except (ingest.IngestError, BrokerError) as exc:
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        messages.error(request, "Could not save %s: %s" % (candidate.alert_id, exc))
        return redirect(_safe_next(request, reverse("candidate_list")))
    detail_url = reverse("transient_detail", args=[transient.slug]) if transient.slug else ""
    if _wants_json(request):
        return JsonResponse({"ok": True, "candidate": candidate.pk, "status": candidate.status,
                             "transient": transient.name, "transient_url": detail_url})
    messages.success(request, "Saved %s as %s." % (candidate.alert_id, transient.name))
    return redirect(_safe_next(request, reverse("candidate_list")))


@login_required
@require_POST
def candidate_reject(request, candidate_id):
    candidate = _candidate_for_action(request, candidate_id)
    if candidate is None:
        return HttpResponseBadRequest("not your candidate")
    ingest.reject_candidate(candidate, request.user, note=(request.POST.get("note") or "")[:255])
    if _wants_json(request):
        return JsonResponse({"ok": True, "candidate": candidate.pk, "status": candidate.status})
    messages.info(request, "Rejected %s." % candidate.alert_id)
    return redirect(_safe_next(request, reverse("candidate_list")))


@login_required
@require_POST
def candidate_reopen(request, candidate_id):
    candidate = _candidate_for_action(request, candidate_id)
    if candidate is None:
        return HttpResponseBadRequest("not your candidate")
    ingest.reopen_candidate(candidate, request.user)
    if _wants_json(request):
        return JsonResponse({"ok": True, "candidate": candidate.pk, "status": candidate.status})
    messages.info(request, "Re-opened %s." % candidate.alert_id)
    return redirect(_safe_next(request, reverse("candidate_list")))


@login_required
def brokers_status_json(request):
    """Providers, their availability and capabilities (what the UI may show)."""
    return JsonResponse({"brokers": registry.describe_all(), "criteria": describe_criteria()})
