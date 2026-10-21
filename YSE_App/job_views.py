"""Staff status page and JSON endpoint for the background job queue (issue #263)."""

from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render

from YSE_App.jobs import autodiscover, queue_counts, registered_kinds
from YSE_App.models.job_models import Job

RECENT_LIMIT = 50


def _status_context(limit=RECENT_LIMIT):
    autodiscover()
    counts = queue_counts()
    by_kind = list(
        Job.objects.order_by().values("kind", "status").annotate(n=Count("id")).order_by("kind", "status")
    )
    kinds = {}
    for row in by_kind:
        kinds.setdefault(row["kind"], {})[row["status"]] = row["n"]
    recent = list(Job.objects.select_related("created_by").order_by("-id")[:limit])
    statuses = [s for s, _ in Job.STATUS_CHOICES]
    return {"counts": counts, "kinds": kinds, "recent": recent, "registered_kinds": registered_kinds(),
            "statuses": statuses, "count_rows": [(s, counts.get(s, 0)) for s in statuses]}


@login_required
@staff_member_required
def jobs_status(request):
    """HTML overview: counts per status, per kind, and the most recent jobs."""
    return render(request, "YSE_App/jobs_status.html", _status_context())


@login_required
@staff_member_required
def jobs_status_json(request):
    """Machine-readable version of the status page (monitoring)."""
    ctx = _status_context(limit=20)
    recent = [
        {
            "id": j.pk, "kind": j.kind, "status": j.status, "attempts": j.attempts,
            "max_attempts": j.max_attempts, "run_after": j.run_after.isoformat() if j.run_after else None,
            "locked_by": j.locked_by, "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "error": (j.error or "").strip().splitlines()[-1][:200] if j.error else "",
        }
        for j in ctx["recent"]
    ]
    return JsonResponse({"counts": ctx["counts"], "kinds": ctx["kinds"],
                         "registered_kinds": ctx["registered_kinds"], "recent": recent})
