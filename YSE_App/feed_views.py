"""External feeds page (#280): the configured Hermes / Einstein Probe / Scout sources, their poll status
and the candidates they produced; staff can queue a poll or a minor-planet screening from here."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from YSE_App.feeds import registry
from YSE_App.feeds.jobs import POLL_KIND, SCREEN_KIND, enqueue_poll, enqueue_screen, poll_active
from YSE_App.models.candidate_models import Candidate
from YSE_App.models.feed_models import FeedSource
from YSE_App.models.job_models import Job
from YSE_App.models.transient_models import Transient

RECENT_JOBS = 15


def candidate_counts_by_kind():
    """``{feed slug: {status: n}}`` for the feed providers' candidates."""
    kinds = list(registry.feed_classes())
    out = {k: {} for k in kinds}
    for row in Candidate.objects.filter(broker__in=kinds).values("broker", "status").annotate(n=Count("id")):
        out.setdefault(row["broker"], {})[row["status"]] = row["n"]
    return out


def _is_staff(user):
    return bool(user.is_staff or user.is_superuser)


def _sources_for(user):
    qs = FeedSource.objects.select_related("credential").order_by("kind", "name")
    return qs if _is_staff(user) else qs.filter(enabled=True)


def _rows(user):
    providers = {d["feed_kind"]: d for d in registry.describe_all()}
    counts = candidate_counts_by_kind()
    rows = []
    for source in _sources_for(user):
        info = providers.get(source.kind, {})
        c = counts.get(source.kind, {})
        rows.append({
            "source": source,
            "provider": info,
            "available": info.get("available", False),
            "unavailable_reason": info.get("unavailable_reason", ""),
            "can_consume": info.get("can_consume", False),
            "polling": poll_active(source),
            "candidates_new": c.get(Candidate.NEW, 0),
            "candidates_saved": c.get(Candidate.SAVED, 0),
            "candidates_rejected": c.get(Candidate.REJECTED, 0),
            "candidates_url": "%s?broker=%s&status=all" % (reverse("candidate_list"), source.kind),
        })
    return rows, providers


@login_required
def feed_sources(request):
    rows, providers = _rows(request.user)
    recent = (Job.objects.filter(kind__in=(POLL_KIND, SCREEN_KIND)).select_related("created_by", "transient")
              .order_by("-id")[:RECENT_JOBS])
    context = {
        "rows": rows,
        "providers": sorted(providers.values(), key=lambda d: d["name"]),
        "recent_jobs": recent,
        "is_staff": _is_staff(request.user),
        "poll_cron_enabled": bool(getattr(settings, "FEEDS_POLL_CRON_ENABLED", False)),
        "screen_cron_enabled": bool(getattr(settings, "FEEDS_MPC_SCREEN_CRON_ENABLED", False)),
        "screen_on_create": bool(getattr(settings, "FEEDS_MPC_SCREEN_ON_CREATE", False)),
    }
    return TemplateResponse(request, "YSE_App/feed_sources.html", context)


@login_required
def feed_sources_json(request):
    rows, providers = _rows(request.user)
    return JsonResponse({
        "providers": list(providers.values()),
        "sources": [{
            "id": r["source"].pk, "slug": r["source"].slug, "name": r["source"].name, "kind": r["source"].kind,
            "topic": r["source"].topic, "enabled": r["source"].enabled, "available": r["available"],
            "unavailable_reason": r["unavailable_reason"], "polling": r["polling"],
            "last_polled": r["source"].last_polled.isoformat() if r["source"].last_polled else None,
            "last_summary": r["source"].last_summary, "last_error": r["source"].last_error,
            "candidates": {"new": r["candidates_new"], "saved": r["candidates_saved"], "rejected": r["candidates_rejected"]},
        } for r in rows],
    })


@login_required
@require_POST
def feed_source_poll(request, source_id):
    if not _is_staff(request.user):
        return JsonResponse({"detail": "staff only"}, status=403)
    source = get_object_or_404(FeedSource, pk=source_id)
    if poll_active(source):
        messages.info(request, "A poll of %s is already queued or running." % source.name)
    else:
        job = enqueue_poll(source, created_by=request.user, force=True, dry_run=bool(request.POST.get("dry_run")))
        messages.success(request, "Queued %spoll of %s (job %s)." % ("dry-run " if request.POST.get("dry_run") else "",
                                                                      source.name, job.pk))
    return redirect(reverse("feed_sources"))


@login_required
@require_POST
def feed_screen_transient(request, transient_id):
    """Queue the sb_ident minor-planet check for one transient (#283); the annotations tab shows the result."""
    transient = get_object_or_404(Transient, pk=transient_id)
    job = enqueue_screen(transient, created_by=request.user)
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({"job_id": job.pk, "status": job.status})
    messages.success(request, "Queued minor-planet screening of %s (job %s)." % (transient.name, job.pk))
    return redirect(request.POST.get("next") or reverse("transient_detail", args=[transient.slug]))
