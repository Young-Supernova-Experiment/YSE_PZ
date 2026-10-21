"""The AI-summary card of the transient detail page and the summary search page (#295, #296, #297).

* ``GET  transient_detail/<id>/summary_fragment/``   the card body (text, provenance, history, buttons)
* ``POST transient_detail/<id>/summary_generate/``   queue a summariser run (opt-in + service groups)
* ``POST transient_detail/<id>/summary_edit/``       ``{text}`` -> a human version (kept in the history)
* ``POST transient_detail/<id>/summary_optin/``      ``{enabled}`` -> the per-user opt-in
* ``GET  summary_search/?q=...``                     natural-language search over the summaries
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from YSE_App.models import ExternalServiceRun, Transient
from YSE_App.services import external_services as runs
from YSE_App.services import llm
from YSE_App.services import summaries as svc

POLL_SECONDS = 3
HISTORY_LIMIT = 12


def _body(request):
    if request.content_type and "json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {k: v for k, v in request.POST.items()}


def _transient_or_404(request, transient_id) -> Transient:
    transient = get_object_or_404(Transient.objects.select_related("status", "host"), pk=transient_id)
    if not svc.can_view(request.user, transient):
        raise Http404("no such transient")
    return transient


def summary_card_context(request, transient: Transient) -> dict:
    user = request.user
    service = svc.service_row()
    run = svc.latest_run(transient)
    active = bool(run is not None and not run.is_finished)
    provider_ok, provider_reason = (llm.chat_available(service, service.default_params or {})
                                    if service is not None else (False, "no service"))
    return {
        "transient": transient,
        "summary_text": transient.summary or "",
        "summary_version": svc.current_version(transient),
        "summary_history": svc.history(transient, HISTORY_LIMIT),
        "summary_can_edit": svc.can_edit(user, transient),
        "summary_service": service,
        "summary_group_allows": svc.group_allows(user, service),
        "summary_opted_in": svc.opted_in(user),
        "summary_can_generate": svc.can_generate(user, transient, service),
        "summary_provider_ok": provider_ok,
        "summary_provider_reason": provider_reason,
        "summary_provider": llm.chat_config(service.default_params or {})["provider"] if service is not None else "",
        "summary_run": run,
        "summary_active": active,
        "summary_run_failed": bool(run is not None and run.status == ExternalServiceRun.STATUS_FAILED),
        "summary_poll_seconds": POLL_SECONDS,
        "summary_is_staff": bool(user.is_staff or user.is_superuser),
    }


@login_required
@require_GET
def transient_summary_fragment(request, transient_id):
    transient = _transient_or_404(request, transient_id)
    svc.fail_stale_runs()
    return render(request, "YSE_App/transient_detail/summary_card.html", summary_card_context(request, transient))


def _run_json(run):
    return {"uuid": str(run.uuid), "status": run.status, "is_finished": run.is_finished, "error": run.error}


@login_required
@require_POST
def transient_summary_generate(request, transient_id):
    """Queue one summariser run for the transient (409 when one is running, 429 at the daily cap)."""
    transient = _transient_or_404(request, transient_id)
    service = svc.service_row()
    if service is None:
        return JsonResponse({"ok": False, "error": "the AI summary service is not registered or is disabled"}, status=404)
    if not svc.group_allows(request.user, service):
        return JsonResponse({"ok": False, "error": "AI summaries are not enabled for your groups"}, status=403)
    if not svc.opted_in(request.user):
        return JsonResponse({"ok": False, "error": "enable AI summaries for your account first"}, status=403)
    if svc.active_run(transient) is not None:
        return JsonResponse({"ok": False, "error": "a summary is already being generated"}, status=409)
    try:
        run = svc.request_summary(transient, request.user, service=service)
    except runs.RunLimitExceeded as exc:
        return JsonResponse({"ok": False, "error": str(exc), "limit": exc.limit}, status=429)
    except (runs.ServiceDisabled, svc.SummaryError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=403)
    run = ExternalServiceRun.objects.get(pk=run.pk)
    return JsonResponse({"ok": True, "run": _run_json(run)})


@login_required
@require_POST
def transient_summary_edit(request, transient_id):
    """``{text}``: store a human-written version (every previous version stays in the history)."""
    transient = _transient_or_404(request, transient_id)
    if not svc.can_edit(request.user, transient):
        return JsonResponse({"ok": False, "error": "you may not edit this summary"}, status=403)
    text = str(_body(request).get("text") or "")
    try:
        version = svc.set_summary(transient, text, request.user, source=svc.SOURCE_HUMAN)
    except svc.SummaryError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "version_id": version.pk, "summary": transient.summary,
                         "summary_modified": transient.summary_modified.isoformat()})


@login_required
@require_POST
def transient_summary_optin(request, transient_id):
    """``{enabled}``: the per-user opt-in to LLM-generated summaries."""
    _transient_or_404(request, transient_id)
    raw = _body(request).get("enabled")
    enabled = str(raw).lower() in ("1", "true", "on", "yes") if not isinstance(raw, bool) else raw
    pref = svc.set_opt_in(request.user, enabled)
    return JsonResponse({"ok": True, "enabled": pref.ai_enabled})


@login_required
@require_GET
def summary_search(request):
    """``/summary_search/?q=``: ranked transients with a snippet of their summary."""
    query = request.GET.get("q", "")
    result = svc.search_summaries(query, request.user)
    context = {
        "query": result["query"],
        "results": result["results"],
        "search_mode": result["mode"],
        "embedder": result["model"],
        "result_count": len(result["results"]) if result["query"] else None,
        "summary_count": Transient.objects.exclude(summary__isnull=True).exclude(summary="").count(),
    }
    return render(request, "YSE_App/summary_search.html", context)
