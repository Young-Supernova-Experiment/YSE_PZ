"""The Annotations tab of the transient detail page and its small endpoints (#317, #318).

* ``GET  transient_detail/<id>/annotations_fragment/``  the tab body (annotations grouped by origin, services)
* ``GET  transient_detail/<id>/annotations_summary.json``  ``{count, badges, active}`` for the tab label and Summary badges
* ``POST transient_detail/<id>/annotation_run/``  ``{service: slug}`` -> queue a check (staff, or a service's group)
* ``POST transient_detail/<id>/annotation_delete/``  ``{origin}`` -> delete one annotation (staff, or the user's own)

Everyone logged in sees the annotations their groups allow (``services.annotations``);
the legacy ``Transient`` columns appear as a read-only ``legacy`` entry.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from YSE_App.models import ExternalServiceRun, Transient, TransientAnnotation
from YSE_App.models.annotation_models import LEGACY_ORIGIN
from YSE_App.services import annotations as svc
from YSE_App.services import external_services as runs

POLL_SECONDS = 4


def _body(request):
    if request.content_type and "json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {k: v for k, v in request.POST.items()}


def _service_entries(transient, user):
    services = svc.annotation_services(user)
    latest = svc.latest_runs(transient, services)
    out = []
    for service in services:
        run = latest.get(service.pk)
        out.append({
            "service": service,
            "slug": service.slug,
            "name": service.name,
            "description": service.description,
            "can_run": svc.can_run_service(user, service),
            "run": run,
            "active": bool(run and not run.is_finished),
        })
    return out


def _annotation_entry(annotation, user, service_by_slug):
    service = service_by_slug.get(annotation.origin) or annotation.service
    run = annotation.run
    return {
        "a": annotation,
        "origin": annotation.origin,
        "title": service.name if service is not None else annotation.origin,
        "items": annotation.items,
        "verdict": annotation.verdict,
        "summary": annotation.summary,
        "has_badge": annotation.has_badge,
        "groups": [g.name for g in annotation.groups.all()],
        "by": annotation.modified_by.username if annotation.modified_by_id else "",
        "run": run,
        "run_failed": bool(run is not None and run.status == ExternalServiceRun.STATUS_FAILED),
        "can_manage": svc.can_manage(user, annotation),
        "service": service,
        "can_rerun": bool(service is not None and svc.can_run_service(user, service)),
        "json": annotation.data_json,
        "read_only": False,
    }


def annotations_tab_context(request, transient):
    user = request.user
    entries = _service_entries(transient, user)
    service_by_slug = {e["slug"]: e["service"] for e in entries}
    annotations = svc.visible_annotations(transient, user)
    rows = [_annotation_entry(a, user, service_by_slug) for a in annotations]
    legacy = svc.legacy_annotation(transient)
    if legacy:
        rows.append({
            "a": None, "origin": LEGACY_ORIGIN, "title": "Transient columns (legacy)",
            "items": sorted(legacy["data"].items()), "verdict": "", "summary": "", "has_badge": False,
            "groups": [], "by": "", "run": None, "run_failed": False, "can_manage": False, "service": None,
            "can_rerun": False, "json": json.dumps(legacy["data"], indent=2, sort_keys=True, default=str),
            "read_only": True,
        })
    done_slugs = {a.origin for a in annotations}
    pending_services = [e for e in entries if e["slug"] not in done_slugs or e["active"]]
    return {
        "transient": transient,
        "annotation_rows": rows,
        "annotation_services": entries,
        "annotation_pending_services": pending_services,
        "annotation_can_run_any": any(e["can_run"] for e in entries),
        "annotation_active": any(e["active"] for e in entries),
        "annotation_poll_seconds": POLL_SECONDS,
        "annotation_user_origin": svc.user_origin(user),
        "annotation_is_staff": bool(user.is_staff or user.is_superuser),
    }


@login_required
def transient_annotations_fragment(request, transient_id):
    transient = get_object_or_404(Transient, pk=transient_id)
    svc.fail_stale_runs()
    return render(request, "YSE_App/transient_detail_annotations_tab.html", annotations_tab_context(request, transient))


@login_required
@require_GET
def transient_annotations_summary(request, transient_id):
    """Tab label count and Summary-tab badges without rendering the fragment."""
    transient = get_object_or_404(Transient, pk=transient_id)
    annotations = svc.visible_annotations(transient, request.user)
    count = len(annotations) + (1 if svc.legacy_data(transient) else 0)
    active = ExternalServiceRun.objects.filter(
        transient=transient, service__kind="annotation",
        status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING),
    ).exists()
    return JsonResponse({"count": count, "badges": svc.badges_for(annotations), "active": active})


def _run_json(run):
    return {
        "uuid": str(run.uuid), "service": run.service.slug, "status": run.status,
        "is_finished": run.is_finished, "error": run.error,
    }


@login_required
@require_POST
def transient_annotation_run(request, transient_id):
    """``POST {service: slug}``: queue one check; 403 unless the user may run that service."""
    transient = get_object_or_404(Transient, pk=transient_id)
    slug = str(_body(request).get("service") or "").strip()
    service = next((s for s in svc.annotation_services(request.user, include_disabled=True) if s.slug == slug), None)
    if service is None:
        return JsonResponse({"ok": False, "error": "unknown annotation service"}, status=400)
    if not svc.can_run_service(request.user, service):
        return JsonResponse({"ok": False, "error": "you may not run this check; ask a staff member"}, status=403)
    if ExternalServiceRun.objects.filter(
        transient=transient, service=service,
        status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING),
    ).exists():
        return JsonResponse({"ok": False, "error": "a %s check is already running" % service.name}, status=409)
    try:
        run = svc.start_annotation_run(service, transient, request.user)
    except runs.RunLimitExceeded as exc:
        return JsonResponse({"ok": False, "error": str(exc), "limit": exc.limit}, status=429)
    except (runs.ServiceDisabled, svc.AnnotationError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=403)
    run = ExternalServiceRun.objects.select_related("service").get(pk=run.pk)
    return JsonResponse({"ok": True, "run": _run_json(run)})


@login_required
@require_POST
def transient_annotation_delete(request, transient_id):
    """``POST {origin}``: delete one annotation (staff any origin; a user their ``user:<name>`` one)."""
    transient = get_object_or_404(Transient, pk=transient_id)
    origin = str(_body(request).get("origin") or "").strip()
    annotation = TransientAnnotation.objects.filter(transient=transient, origin=origin).first()
    if annotation is None:
        return JsonResponse({"ok": False, "error": "no such annotation"}, status=404)
    if not svc.can_manage(request.user, annotation):
        return JsonResponse({"ok": False, "error": "only staff or the owner may delete this annotation"}, status=403)
    svc.delete_annotation(annotation)
    return JsonResponse({"ok": True, "origin": origin})
