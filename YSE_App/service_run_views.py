"""Staff pages for external-service runs and the token-protected callback (#265)."""

from __future__ import annotations

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from YSE_App.models import ExternalService, ExternalServiceRun
from YSE_App.services import analysis_services as analysis_svc
from YSE_App.services import external_services as svc

PAGE_SIZE = 100


@staff_member_required
def external_service_runs(request):
    runs = ExternalServiceRun.objects.select_related("service", "transient", "created_by")
    service_slug = request.GET.get("service", "").strip()
    status = request.GET.get("status", "").strip()
    if service_slug:
        runs = runs.filter(service__slug=service_slug)
    if status:
        runs = runs.filter(status=status)
    context = {
        "runs": runs[:PAGE_SIZE],
        "page_size": PAGE_SIZE,
        "services": ExternalService.objects.order_by("name"),
        "statuses": ExternalServiceRun.STATUS_CHOICES,
        "selected_service": service_slug,
        "selected_status": status,
    }
    return render(request, "YSE_App/service_runs.html", context)


@staff_member_required
def external_service_run_detail(request, run_uuid):
    run = get_object_or_404(
        ExternalServiceRun.objects.select_related("service", "transient", "created_by"),
        uuid=run_uuid,
    )
    context = {
        "run": run,
        "request_json": json.dumps(run.request_payload or {}, indent=2, sort_keys=True, default=str),
        "result_json": json.dumps(run.result or {}, indent=2, sort_keys=True, default=str),
        "callback_url": svc.callback_url(run, request),
    }
    return render(request, "YSE_App/service_run_detail.html", context)


def _presented_token(request, body):
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.META.get(svc.CALLBACK_TOKEN_HEADER)
    if header:
        return header.strip()
    if isinstance(body, dict):
        return body.get("token")
    return None


@csrf_exempt
@require_POST
def external_service_run_callback(request, run_uuid):
    """``POST /api/service_runs/<uuid>/callback/`` from a remote service.

    Auth: ``Authorization: Bearer <token>``, ``X-Run-Token: <token>`` or
    ``"token"`` in the JSON body; the token was returned by ``start_run``.
    Body: ``{"status": "succeeded"|"failed"|"cancelled", "result": {...},
    "error": "...", "artifact_url": "...", "external_id": "..."}``.
    """
    try:
        run = ExternalServiceRun.objects.select_related("service").get(uuid=run_uuid)
    except (ExternalServiceRun.DoesNotExist, ValueError):
        raise Http404("unknown run")
    body, files = _callback_body(request)
    token = _presented_token(request, body)
    if not svc.verify_callback_token(run, token):
        return JsonResponse({"error": "invalid or missing run token"}, status=403)
    if not isinstance(body, dict):
        return JsonResponse({"error": "body must be a JSON object"}, status=400)
    status = str(body.get("status", "")).lower()
    if status == "running":
        if run.is_finished:
            return JsonResponse({"error": "run is already %s" % run.status}, status=409)
        run.mark_running(external_id=str(body.get("external_id", "") or ""))
        return JsonResponse({"uuid": str(run.uuid), "status": run.status})
    if status not in ExternalServiceRun.FINAL_STATUSES:
        return JsonResponse(
            {"error": "status must be one of running, %s" % ", ".join(ExternalServiceRun.FINAL_STATUSES)},
            status=400,
        )
    result = body.get("result")
    if result is not None and not isinstance(result, (dict, list)):
        return JsonResponse({"error": "result must be a JSON object or array"}, status=400)
    if run.is_finished:
        return JsonResponse({"error": "run is already %s" % run.status}, status=409)
    # Analysis services (#313) may attach plots / files: JSON base64 entries or multipart uploads.
    stored, attachment_errors = analysis_svc.store_callback_attachments(run, body, files)
    if stored and isinstance(result, dict):
        result = dict(result)
        result["_files"] = [f.name for f in stored]
    elif stored and result is None:
        result = {"_files": [f.name for f in stored]}
    try:
        svc.record_completion(
            run, status,
            result=result,
            error=str(body.get("error", "") or ""),
            artifact_url=str(body.get("artifact_url", "") or ""),
            external_id=str(body.get("external_id", "") or ""),
        )
    except svc.InvalidTransition as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    reply = {"uuid": str(run.uuid), "status": run.status}
    if stored:
        reply["files"] = [f.name for f in stored]
    if attachment_errors:
        reply["attachment_errors"] = attachment_errors
    return JsonResponse(reply)


def _callback_body(request):
    """``(body dict or None, uploaded files)``: JSON, or multipart/form-data with a JSON ``result`` field."""
    content_type = (request.content_type or "").lower()
    if content_type.startswith("multipart/form-data") or content_type.startswith("application/x-www-form-urlencoded"):
        body = {}
        for key in request.POST:
            value = request.POST.get(key)
            if key in ("result", "plots", "files", "kinds", "meta") and isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    return None, None
            body[key] = value
        return body, (request.FILES if request.FILES else None)
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = None
    return body, None
