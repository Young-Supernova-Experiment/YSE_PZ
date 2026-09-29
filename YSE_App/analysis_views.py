"""The Analysis tab of the transient detail page and its small JSON endpoints (#314).

* ``GET  transient_detail/<id>/analysis_fragment/``  the tab body (runs, result cards, run form)
* ``POST transient_detail/<id>/analysis_run/``       ``{service: slug, params: {...}}`` -> start a run
* ``GET  analysis_runs/<uuid>/status.json``          polled until the run is final
* ``GET  analysis_runs/<uuid>/files/<file id>/<name>``  a plot or result file (access-checked)
* ``POST analysis_runs/<uuid>/delete/`` / ``cancel/``  own runs (staff: any)

A user may see a run when they may see its transient (photometry / spectra
group access, ``services.visibility``); files are served through this module,
never through ``MEDIA_URL``.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.encoding import iri_to_uri
from django.views.decorators.http import require_GET, require_POST

from YSE_App.models import AnalysisResultFile, AnalysisService, ExternalService, ExternalServiceRun, Transient
from YSE_App.services import analysis_services as svc
from YSE_App.services import external_services as runs
from YSE_App.services.visibility import filter_transients_by_user_access

POLL_SECONDS = 4


def user_may_see_transient(user, transient) -> bool:
    if user is None or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return bool(list(filter_transients_by_user_access(user, [transient])))


def _body(request):
    if request.content_type and "json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {k: v for k, v in request.POST.items()}


def _service_entry(profile: AnalysisService, user):
    return {
        "profile": profile,
        "slug": profile.service.slug,
        "name": profile.service.name,
        "description": profile.service.description,
        "runner_kind": profile.runner_kind,
        "fields": svc.form_fields(profile),
        "cap": svc.cap_status(profile.service, user),
    }


def _run_entry(run: ExternalServiceRun, user):
    files = list(run.files.all())
    return {
        "r": run,
        "profile": getattr(run.service, "analysis", None),
        "summary": svc.summary_pairs(run),
        "rows": svc.result_rows(run),
        "plots": [f for f in files if f.is_image],
        "downloads": [f for f in files if not f.is_image],
        "params": svc.run_params(run),
        "can_manage": svc.can_manage_run(run, user),
        "summary_text": (run.result or {}).get("_summary", "") if isinstance(run.result, dict) else "",
    }


def analysis_tab_context(request, transient):
    """Runs of the transient (only the user's own when they may not see its data), the run form, cap state."""
    svc.fail_timed_out_runs()
    services = [_service_entry(p, request.user) for p in svc.services_for_user(request.user)]
    sees_data = user_may_see_transient(request.user, transient)
    qs = svc.runs_for_transient(transient, request.user, limit=None)
    if not sees_data:
        qs = qs.filter(created_by=request.user)
    run_rows = [_run_entry(r, request.user) for r in qs[:50]]
    return {
        "transient": transient,
        "analysis_sees_data": sees_data,
        "analysis_services": services,
        "analysis_runs": run_rows,
        "analysis_active": any(not e["r"].is_finished for e in run_rows),
        "analysis_poll_seconds": POLL_SECONDS,
        "analysis_services_json": json.dumps(
            [{"slug": s["slug"], "name": s["name"], "fields": s["fields"], "cap": s["cap"],
              "runner_kind": s["runner_kind"]} for s in services],
            default=str,
        ),
    }


@login_required
def transient_analysis_fragment(request, transient_id):
    transient = get_object_or_404(Transient, pk=transient_id)
    return render(request, "YSE_App/transient_detail_analysis_tab.html", analysis_tab_context(request, transient))


def run_json(run: ExternalServiceRun, request=None):
    files = []
    for f in run.files.all():
        files.append({
            "id": f.pk, "name": f.name, "kind": f.kind, "content_type": f.content_type, "size": f.size,
            "is_image": f.is_image, "url": file_url(run, f),
        })
    return {
        "uuid": str(run.uuid),
        "service": run.service.slug,
        "service_name": run.service.name,
        "status": run.status,
        "is_finished": run.is_finished,
        "created_by": run.created_by.username if run.created_by_id else "",
        "created_date": run.created_date.isoformat() if run.created_date else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": run.duration.total_seconds() if run.duration else None,
        "error": run.error,
        "params": svc.run_params(run),
        "result": run.result if isinstance(run.result, (dict, list)) else {},
        "summary": svc.summary_pairs(run),
        "files": files,
    }


def file_url(run: ExternalServiceRun, f: AnalysisResultFile) -> str:
    from django.urls import reverse

    return reverse("analysis_run_file", kwargs={"run_uuid": str(run.uuid), "file_id": f.pk, "name": f.name})


@login_required
@require_POST
def transient_analysis_run(request, transient_id):
    """``POST {service: slug, params: {...}}`` (JSON or form; unknown form keys become params)."""
    transient = get_object_or_404(Transient, pk=transient_id)
    body = _body(request)
    slug = str(body.get("service") or "").strip()
    profile = svc.services_for_user(request.user).filter(service__slug=slug).first() if slug else None
    if profile is None:
        return JsonResponse({"ok": False, "error": "choose an analysis service you may run"}, status=400)
    params = body.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "params must be a JSON object"}, status=400)
    if params is None:
        params = {k: v for k, v in body.items() if k not in ("service", "params", "csrfmiddlewaretoken")}
    if not isinstance(params, dict):
        return JsonResponse({"ok": False, "error": "params must be a JSON object"}, status=400)
    try:
        run = svc.start_analysis(profile, transient, request.user, params)
    except svc.InvalidParams as exc:
        return JsonResponse({"ok": False, "error": str(exc), "errors": exc.errors}, status=400)
    except runs.RunLimitExceeded as exc:
        return JsonResponse({"ok": False, "error": str(exc), "limit": exc.limit}, status=429)
    except runs.ServiceDisabled as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=403)
    run = ExternalServiceRun.objects.select_related("service", "created_by").get(pk=run.pk)
    return JsonResponse({"ok": True, "run": run_json(run, request)})


def _run_for_user(request, run_uuid) -> ExternalServiceRun:
    try:
        run = ExternalServiceRun.objects.select_related("service", "transient", "created_by").get(
            uuid=run_uuid, service__kind=ExternalService.KIND_ANALYSIS)
    except (ExternalServiceRun.DoesNotExist, ValueError):
        raise Http404("unknown analysis run")
    # The requester (and staff) always; anyone else only when they may see the transient's data,
    # since results derive from it.
    if svc.can_manage_run(run, request.user):
        return run
    if run.transient_id is None or not user_may_see_transient(request.user, run.transient):
        raise PermissionDenied("you may not see this transient's data")
    return run


@login_required
@require_GET
def analysis_run_status(request, run_uuid):
    run = _run_for_user(request, run_uuid)
    if not run.is_finished:
        svc.fail_timed_out_runs()
        run.refresh_from_db()
    return JsonResponse(run_json(run, request))


@login_required
@require_GET
def analysis_run_file(request, run_uuid, file_id, name=None):
    run = _run_for_user(request, run_uuid)
    f = get_object_or_404(AnalysisResultFile, pk=file_id, run=run)
    try:
        handle = f.file.open("rb")
    except (FileNotFoundError, ValueError):
        raise Http404("file is missing from storage")
    response = FileResponse(handle, content_type=f.content_type or "application/octet-stream")
    disposition = "inline" if f.is_image or f.content_type in ("application/json", "text/plain") else "attachment"
    response["Content-Disposition"] = "%s; filename=\"%s\"" % (disposition, iri_to_uri(f.name))
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_POST
def analysis_run_delete(request, run_uuid):
    run = _run_for_user(request, run_uuid)
    if not svc.can_manage_run(run, request.user):
        return JsonResponse({"ok": False, "error": "only the requester or staff may delete a run"}, status=403)
    svc.delete_run(run)
    return JsonResponse({"ok": True, "uuid": str(run_uuid)})


@login_required
@require_POST
def analysis_run_cancel(request, run_uuid):
    run = _run_for_user(request, run_uuid)
    if not svc.can_manage_run(run, request.user):
        return JsonResponse({"ok": False, "error": "only the requester or staff may cancel a run"}, status=403)
    svc.cancel_run(run)
    return JsonResponse({"ok": True, "run": run_json(run, request)})
