"""Candidate scanning page and its save / reject actions (issue #279), the
transient-detail Brokers tab and the broker cone-search page (issue #275)."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from django.conf import settings

from YSE_App.brokers import CAPABILITY_LABELS, registry
from YSE_App.brokers.base import CONE_SEARCH, CUTOUTS, GET_ALERT, PHOTOMETRY, SAVE_AS_TRANSIENT, BrokerError, BrokerUnavailable
from YSE_App.brokers.filters import describe_criteria
from YSE_App.brokers import ingest
from YSE_App.models.candidate_models import BrokerFilter, Candidate
from YSE_App.models.enum_models import ObservationGroup, TransientStatus
from YSE_App.models.transient_models import Transient

logger = logging.getLogger(__name__)

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
    qs = Candidate.objects.filter(Q(filters__in=filters) | Q(filters__isnull=True))
    params = request.GET
    status = params.get("status", Candidate.NEW)
    if status and status != "all":
        qs = qs.filter(status=status)
    # #279: a rejection by one of the user's groups hides the row from that group's
    # queue only; other groups still see it. Staff and the explicit rejected view see all.
    if status == Candidate.NEW and not (request.user.is_staff or request.user.is_superuser):
        qs = qs.exclude(rejected_by_groups__in=request.user.groups.all())
    qs = qs.distinct()
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
    return qs.select_related("transient", "status_changed_by").prefetch_related("filters", "rejected_by_groups").order_by(*SORTS[sort]), {
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
            "rejected_groups": [g.name for g in c.rejected_by_groups.all()],
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
    scope = "all" if request.POST.get("scope") == "all" else "auto"
    ingest.reject_candidate(candidate, request.user, note=(request.POST.get("note") or "")[:255], scope=scope)
    if _wants_json(request):
        return JsonResponse({"ok": True, "candidate": candidate.pk, "status": candidate.status,
                             "rejected_by_groups": [g.name for g in candidate.rejected_by_groups.all()]})
    if candidate.status == Candidate.REJECTED:
        messages.info(request, "Rejected %s." % candidate.alert_id)
    else:
        messages.info(request, "Rejected %s for your group(s); other groups still see it." % candidate.alert_id)
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


# --- transient-detail Brokers tab and cone-search page (#275) --------------------------

def detail_radius_arcsec() -> float:
    return float(getattr(settings, "BROKER_DETAIL_RADIUS_ARCSEC", 5.0) or 5.0)


def _separation(ra, dec, alert):
    from YSE_App.common.utilities import getSeparation

    try:
        return float(getSeparation(float(ra), float(dec), float(alert.ra), float(alert.dec)))
    except (TypeError, ValueError):
        return None


def _known_map(alerts):
    """``{object_id: Transient}`` for alerts already saved (name, alias, candidate)."""
    from YSE_App.models.transient_models import AlternateTransientNames

    ids = [a.object_id for a in alerts]
    out = {t.name: t for t in Transient.objects.filter(name__in=ids)}
    for alias in AlternateTransientNames.objects.filter(name__in=ids).select_related("transient"):
        out.setdefault(alias.name, alias.transient)
    for c in Candidate.objects.filter(alert_id__in=ids, transient__isnull=False).select_related("transient"):
        out.setdefault(c.alert_id, c.transient)
    return out


def broker_matches(ra, dec, *, radius_arcsec=None, limit=5, providers=None):
    """Nearest alerts per available provider with cone search; errors stay inline per provider."""
    radius = float(radius_arcsec if radius_arcsec is not None else detail_radius_arcsec())
    sections = []
    for provider in (providers if providers is not None else registry.all_providers()):
        if not provider.has(CONE_SEARCH):
            continue
        section = {"provider": provider.describe(), "slug": provider.slug, "name": provider.name, "alerts": [], "error": "",
                   "has_cutouts": provider.has(CUTOUTS), "has_photometry": provider.has(PHOTOMETRY),
                   "has_save": provider.has(SAVE_AS_TRANSIENT), "radius": radius}
        if not provider.available():
            section["error"] = "unavailable: %s" % provider.unavailable_reason()
            sections.append(section)
            continue
        try:
            alerts = provider.cone_search(float(ra), float(dec), radius, limit=int(limit))
        except (BrokerError, BrokerUnavailable) as exc:
            section["error"] = str(exc)
            sections.append(section)
            continue
        except Exception as exc:  # noqa: BLE001 - a broken provider must not break the page
            logger.exception("broker %s cone search failed", provider.slug)
            section["error"] = "%s: %s" % (type(exc).__name__, exc)
            sections.append(section)
            continue
        known = _known_map(alerts)
        rows = []
        for alert in alerts:
            rows.append({"alert": alert, "sep": _separation(ra, dec, alert), "cutouts": _ordered_cutouts(alert.cutout_urls),
                         "known": known.get(alert.object_id)})
        rows.sort(key=lambda r: (r["sep"] is None, r["sep"] or 0))
        section["alerts"] = rows
        sections.append(section)
    return sections


@login_required
@require_GET
def transient_brokers_fragment(request, transient_id):
    """Lazily-loaded *Brokers* tab: alerts within the configured radius, per provider."""
    transient = get_object_or_404(Transient, pk=transient_id)
    sections = broker_matches(transient.ra, transient.dec)
    linked = {c.alert_id: c for c in Candidate.objects.filter(transient=transient)}
    return render(request, "YSE_App/transient_detail_brokers_tab.html", {
        "transient": transient, "sections": sections, "radius": detail_radius_arcsec(), "linked": linked,
        "any_provider": bool(sections),
    })


@login_required
@require_POST
def transient_broker_import(request, transient_id):
    """*Import photometry* button: queue ``brokers.import_photometry`` for the transient (#275)."""
    from YSE_App.brokers.jobs import enqueue_import_photometry

    transient = get_object_or_404(Transient, pk=transient_id)
    broker = request.POST.get("broker") or ""
    object_id = (request.POST.get("object_id") or "").strip()
    provider = registry.get_provider(broker)
    if provider is None or not provider.has(PHOTOMETRY):
        return JsonResponse({"ok": False, "error": "broker %r has no photometry here" % broker}, status=400)
    if not object_id:
        return JsonResponse({"ok": False, "error": "object_id is required"}, status=400)
    job = enqueue_import_photometry(transient, provider.slug, object_id, user=request.user,
                                    instrument=request.POST.get("instrument") or None,
                                    obs_group=request.POST.get("obs_group") or None)
    if request.POST.get("alias") not in (None, "", "0", "false"):
        from YSE_App.brokers.base import BrokerAlert

        ingest.link_existing_transient(BrokerAlert(broker=provider.slug, object_id=object_id, ra=transient.ra, dec=transient.dec,
                                                   obs_group=request.POST.get("obs_group") or provider.default_obs_group),
                                       transient, request.user)
    return JsonResponse({"ok": True, "job": job.pk, "kind": job.kind, "transient": transient.name, "object_id": object_id})


def _parse_position(request):
    """``(ra, dec, radius, name, error)`` from the search form; a name resolves through a
    YSE transient of that name / alias or the providers' ``get_alert``."""
    params = request.GET
    name = (params.get("name") or "").strip()
    try:
        radius = float(params.get("radius") or detail_radius_arcsec())
    except ValueError:
        return None, None, None, name, "radius must be a number (arcsec)"
    radius = max(0.1, min(radius, 600.0))
    ra_raw, dec_raw = (params.get("ra") or "").strip(), (params.get("dec") or "").strip()
    if ra_raw and dec_raw:
        try:
            return float(ra_raw), float(dec_raw), radius, name, ""
        except ValueError:
            pass
        try:
            from astropy.coordinates import SkyCoord

            sc = SkyCoord(ra_raw, dec_raw, unit=("hourangle", "deg"))
            return float(sc.ra.deg), float(sc.dec.deg), radius, name, ""
        except Exception:  # noqa: BLE001 - astropy raises several types for bad strings
            return None, None, radius, name, "could not parse the coordinates"
    if name:
        t = Transient.objects.filter(Q(name=name) | Q(alternatetransientnames__name=name)).first()
        if t is not None:
            return float(t.ra), float(t.dec), radius, name, ""
        for provider in registry.enabled_providers():
            if not provider.has(GET_ALERT):
                continue
            try:
                alert = provider.get_alert(name)
            except (BrokerError, BrokerUnavailable):
                continue
            if alert is not None:
                return float(alert.ra), float(alert.dec), radius, name, ""
        return None, None, radius, name, "no transient or broker object named %r" % name
    return None, None, radius, name, ""


@login_required
def broker_search(request):
    """``/brokers/search/``: cone search across the enabled providers with *Save as transient*."""
    ra, dec, radius, name, error = _parse_position(request)
    sections = []
    if ra is not None and dec is not None:
        try:
            limit = max(1, min(int(request.GET.get("limit") or 20), 200))
        except ValueError:
            limit = 20
        sections = broker_matches(ra, dec, radius_arcsec=radius, limit=limit)
    providers = registry.all_providers()
    return render(request, "YSE_App/broker_search.html", {
        "ra": ra, "dec": dec, "radius": radius if radius is not None else detail_radius_arcsec(), "name": name,
        "error": error, "sections": sections, "searched": ra is not None and dec is not None,
        "providers": [p.describe() for p in providers],
        "transient_statuses": list(TransientStatus.objects.order_by("name").values_list("name", flat=True)),
        "obs_groups": list(ObservationGroup.objects.order_by("name").values_list("name", flat=True)),
        "nearby": ingest.nearby_transients(ra, dec, radius) if ra is not None and dec is not None else [],
    })


@login_required
@require_POST
def broker_search_save(request):
    """Save one cone-search result as a transient (status New by default) through the provider."""
    broker = request.POST.get("broker") or ""
    object_id = (request.POST.get("object_id") or "").strip()
    status = request.POST.get("status") or "New"
    obs_group = request.POST.get("obs_group") or None
    import_phot = request.POST.get("import_photometry", "1") not in ("0", "false", "off")
    fallback = reverse("broker_search")
    try:
        provider = registry.get_provider(broker, require_available=True)
        if provider is None or not provider.has(GET_ALERT) or not provider.has(SAVE_AS_TRANSIENT):
            raise ingest.IngestError("broker %r cannot save transients here" % broker)
        alert = provider.get_alert(object_id)
        if alert is None:
            raise ingest.IngestError("%s knows no object %s" % (provider.name, object_id))
        transient = ingest.save_alert(provider, alert, request.user, status=status, obs_group=obs_group,
                                      import_photometry=import_phot)
    except (ingest.IngestError, BrokerError, BrokerUnavailable) as exc:
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        messages.error(request, "Could not save %s: %s" % (object_id, exc))
        return redirect(_safe_next(request, fallback))
    detail_url = reverse("transient_detail", args=[transient.slug]) if transient.slug else ""
    if _wants_json(request):
        return JsonResponse({"ok": True, "transient": transient.name, "transient_url": detail_url})
    messages.success(request, "Saved %s as %s." % (object_id, transient.name))
    return redirect(_safe_next(request, fallback))
