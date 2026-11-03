"""The requesting user's background exports (#855): a list page and the download link.

Both need a signed-in user and ``BACKGROUND_EXPORTS_ENABLED`` (404 otherwise). An export is
downloadable only by the user who requested it (``Job.created_by``), only while its file is within
``EXPORT_RETENTION_DAYS``; anybody else gets the same 404 as for an export that does not exist.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from YSE_App.models.job_models import Job
from YSE_App.services import background_exports


def _require_enabled():
    if not background_exports.enabled():
        raise Http404("Background exports are not enabled.")


@login_required
@require_GET
def export_list(request):
    _require_enabled()
    return render(request, "YSE_App/export_list.html", {
        "exports": background_exports.user_exports(request.user),
        "retention_days": background_exports.retention_days(),
    })


@login_required
@require_GET
def export_download(request, job_id):
    _require_enabled()
    row = Job.objects.filter(pk=job_id, kind=background_exports.KIND, created_by=request.user).first()
    if row is None:
        raise Http404("No such export.")
    name = background_exports.export_file(row)
    if name is None:
        raise Http404("This export is not ready, or its file has been deleted.")
    filename = row.result.get("filename") or name.rsplit("/", 1)[-1]
    response = FileResponse(default_storage.open(name, "rb"), as_attachment=True, filename=filename)
    response["Cache-Control"] = "private, no-store"
    return response
