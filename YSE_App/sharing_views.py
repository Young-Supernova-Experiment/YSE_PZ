"""Pages and endpoints for sharing services (#325, #326).

- ``/sharing/`` submissions list (filters, errors, retry) and ``/sharing/submissions/<id>/``
- ``/sharing/services/`` the services the user may report through and their auto-publisher rules
- ``transient_detail/<id>/report/preview/`` and ``.../report/submit/`` behind the
  "Report to TNS" dialog on the transient page: build the payload for review, then queue it.

Permissions: a service is usable by staff and by members of its ``groups``
(every authenticated user when the list is empty). Submissions are visible
through the same rule; retrying needs the service to be usable by the user.
"""

from __future__ import annotations

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from YSE_App.models import SharingService, SharingSubmission, Transient, TransientClass, TransientSpectrum
from YSE_App.models.sharing_models import AutoPublisher
from YSE_App.sharing import tns

PAGE_SIZE = 100


def _visible_services(user, include_disabled=False):
    qs = SharingService.objects.all().prefetch_related("groups")
    if not include_disabled:
        qs = qs.filter(enabled=True)
    return [s for s in qs if s.visible_to(user)]


def _service_for(user, service_id):
    service = get_object_or_404(SharingService, pk=service_id)
    if not service.visible_to(user):
        raise Http404("unknown service")
    return service


@login_required
def sharing_submissions(request):
    services = _visible_services(request.user, include_disabled=True)
    qs = SharingSubmission.objects.filter(service__in=[s.pk for s in services]).select_related(
        "service", "transient", "created_by", "auto_publisher")
    status = request.GET.get("status", "").strip()
    kind = request.GET.get("kind", "").strip()
    service_slug = request.GET.get("service", "").strip()
    query = request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if kind:
        qs = qs.filter(kind=kind)
    if service_slug:
        qs = qs.filter(service__slug=service_slug)
    if query:
        qs = qs.filter(transient__name__icontains=query) | qs.filter(tns_name__icontains=query)
    counts = {row["status"]: row["n"] for row in
              SharingSubmission.objects.filter(service__in=[s.pk for s in services])
              .values("status").order_by().annotate(n=Count("id"))}
    context = {
        "submissions": qs[:PAGE_SIZE],
        "page_size": PAGE_SIZE,
        "services": services,
        "statuses": SharingSubmission.STATUS_CHOICES,
        "kinds": SharingSubmission.KIND_CHOICES,
        "selected_status": status,
        "selected_kind": kind,
        "selected_service": service_slug,
        "query": query,
        "counts": [(value, label, counts.get(value, 0)) for value, label in SharingSubmission.STATUS_CHOICES],
        "can_retry": True,
    }
    return render(request, "YSE_App/sharing_submissions.html", context)


@login_required
def sharing_submission_detail(request, submission_id):
    submission = get_object_or_404(
        SharingSubmission.objects.select_related("service", "transient", "created_by", "job", "auto_publisher"),
        pk=submission_id)
    if not submission.service.visible_to(request.user):
        raise Http404("unknown submission")
    context = {
        "submission": submission,
        "payload_json": json.dumps(tns.strip_private(submission.payload or {}), indent=2, sort_keys=True, default=str),
        "response_json": json.dumps(submission.response or {}, indent=2, sort_keys=True, default=str),
    }
    return render(request, "YSE_App/sharing_submission_detail.html", context)


@login_required
@require_POST
def sharing_submission_retry(request, submission_id):
    submission = get_object_or_404(SharingSubmission.objects.select_related("service", "transient"), pk=submission_id)
    if not submission.service.visible_to(request.user):
        raise Http404("unknown submission")
    if not submission.service.enabled and not request.user.is_staff:
        return HttpResponseForbidden("This service is disabled.")
    try:
        tns.retry_submission(submission, request.user)
    except tns.SharingError as exc:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": str(exc)}, status=409)
        return HttpResponseForbidden(str(exc))
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"id": submission.pk, "status": submission.status})
    return redirect(request.POST.get("next") or reverse("sharing_submission_detail", args=[submission.pk]))


@login_required
def sharing_services(request):
    services = _visible_services(request.user, include_disabled=request.user.is_staff)
    rules = AutoPublisher.objects.filter(service__in=[s.pk for s in services]).select_related("service", "group")
    rules_by_service = {}
    for rule in rules:
        rules_by_service.setdefault(rule.service_id, []).append(rule)
    rows = []
    for service in services:
        rows.append({
            "service": service,
            "rules": rules_by_service.get(service.pk, []),
            "instruments": list(service.allowed_instruments.values_list("name", flat=True)),
            "obs_groups": list(service.allowed_obs_groups.values_list("name", flat=True)),
            "groups": list(service.groups.values_list("name", flat=True)),
            "credential_keys": service.credential.secret_keys() if service.credential_id else [],
            "n_submissions": service.submissions.count(),
        })
    return render(request, "YSE_App/sharing_services.html", {"rows": rows})


@staff_member_required
def autopublisher_dry_run(request, service_id, rule_id):
    rule = get_object_or_404(AutoPublisher.objects.select_related("service"), pk=rule_id, service_id=service_id)
    limit = min(int(request.GET.get("limit", 50) or 50), 200)
    transients = rule.qualifying_transients(limit=limit)
    return JsonResponse({
        "rule": rule.name,
        "kind": rule.kind,
        "count": len(transients),
        "transients": [{"name": t.name, "slug": t.slug, "status": t.status.name if t.status_id else "",
                        "url": reverse("transient_detail", args=[t.slug])} for t in transients],
    })


# --- the report dialog --------------------------------------------------------

def _report_options(request, transient):
    data = request.POST if request.method == "POST" else request.GET
    kind = (data.get("kind") or tns.KIND_DISCOVERY).strip()
    options = {
        "coauthors": (data.get("coauthors") or "").strip(),
        "remarks": (data.get("remarks") or "").strip(),
    }
    if kind == tns.KIND_CLASSIFICATION:
        spectrum_id = data.get("spectrum")
        options["spectrum"] = TransientSpectrum.objects.filter(pk=spectrum_id, transient=transient).select_related(
            "instrument", "obs_group").first() if spectrum_id else None
        options["classification"] = (data.get("classification") or "").strip()
        redshift = (data.get("redshift") or "").strip()
        options["redshift"] = redshift if redshift else None
    return kind, options


def _dialog_context(request, transient):
    service_id = (request.POST if request.method == "POST" else request.GET).get("service")
    if not service_id:
        return None, JsonResponse({"error": "Choose a service."}, status=400)
    try:
        service = _service_for(request.user, int(service_id))
    except (TypeError, ValueError):
        return None, JsonResponse({"error": "Choose a service."}, status=400)
    if not service.enabled:
        return None, JsonResponse({"error": "%s is disabled." % service.name}, status=403)
    return service, None


@login_required
def report_preview(request, transient_id):
    """JSON ``{"payload": {...}, "problems": [...]}`` for the dialog's Preview button."""
    transient = get_object_or_404(Transient, pk=transient_id)
    service, error = _dialog_context(request, transient)
    if error is not None:
        return error
    kind, options = _report_options(request, transient)
    payload, problems = tns.preview_payload(service, transient, kind, **options)
    return JsonResponse({
        "service": service.name,
        "kind": kind,
        "sandbox": service.testing,
        "endpoint": service.api_base_url() + "/" + tns.BULK_REPORT,
        "payload": tns.strip_private(payload),
        "problems": problems,
    })


@login_required
@require_POST
def report_submit(request, transient_id):
    """Queue the report: ``{"id": ..., "status": "pending", "url": ...}`` or ``{"error": ...}``."""
    transient = get_object_or_404(Transient, pk=transient_id)
    service, error = _dialog_context(request, transient)
    if error is not None:
        return error
    kind, options = _report_options(request, transient)
    try:
        submission = tns.create_submission(service, transient, kind, request.user, **options)
    except tns.PayloadError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except tns.SharingError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({
        "id": submission.pk,
        "status": submission.status,
        "sandbox": service.testing,
        "url": reverse("sharing_submission_detail", args=[submission.pk]),
        "list_url": reverse("sharing_submissions"),
    })


def report_dialog_context(user, transient):
    """Template context for the "Report to TNS" dialog on the transient page (#326)."""
    services = [s for s in SharingService.for_user(user, kind=SharingService.KIND_TNS)]
    if not services:
        return {"sharing_services": []}
    spectra = TransientSpectrum.objects.filter(transient=transient).select_related("instrument").order_by("-obs_date")
    recent = SharingSubmission.objects.filter(transient=transient).select_related("service").order_by("-created_date")[:5]
    return {
        "sharing_services": [{
            "id": s.pk, "name": s.name, "testing": s.testing, "default_coauthors": s.default_coauthors,
            "default_remarks": s.default_remarks, "group": s.tns_group_name or s.tns_group_id,
        } for s in services],
        "sharing_spectra": [{"id": sp.pk, "label": "%s %s" % (sp.instrument.name, sp.obs_date.strftime("%Y-%m-%d %H:%M"))}
                            for sp in spectra],
        "sharing_classes": list(TransientClass.objects.order_by("name").values_list("name", flat=True)),
        "sharing_default_class": transient.best_spec_class.name if transient.best_spec_class_id else "",
        "sharing_recent": recent,
        "sharing_has_tns_name": bool(tns.tns_name_for(transient)),
    }
