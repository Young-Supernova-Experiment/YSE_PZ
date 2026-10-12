"""Background jobs that keep ``TransientPhotStat`` current (#269, #271).

* ``photstat.refresh_stale``: recompute up to ``limit`` rows that are missing
  or were written under an older ``photstat.SCHEMA_VERSION`` (the
  ``rebuild_photstats --stale-only`` selection), so a rules change or a
  write path that bypassed the signals is repaired without a manual run.
* ``photstat.bazin_extrapolate``: store the Bazin-extrapolated magnitude
  (``services.bazin.store_extrapolations``) for the given transients, by
  default the ones with an open follow-up request, at ``at_mjd`` (default:
  now).

``data_ingest/PhotStat_Jobs.PhotStatRefresh`` enqueues them from the
existing ``runcrons`` pass; the job runner (``RunQueuedJobs``) executes them.
The stale refresh is ``manage.py rebuild_photstats --stale-only`` in bounded
passes.
"""

from __future__ import annotations

from typing import List, Optional

from django.conf import settings
from django.utils import timezone

from YSE_App.jobs import enqueue, job

REFRESH_STALE_KIND = "photstat.refresh_stale"
BAZIN_KIND = "photstat.bazin_extrapolate"

DEFAULT_STALE_LIMIT = 2000
DEFAULT_BAZIN_LIMIT = 500


def _setting_int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


@job(REFRESH_STALE_KIND, max_attempts=2, backoff_seconds=600)
def refresh_stale(payload=None, job=None):
    from YSE_App.services import photstat

    payload = payload or {}
    limit = int(payload.get("limit") or _setting_int("PHOTSTAT_REFRESH_LIMIT", DEFAULT_STALE_LIMIT))
    result = photstat.refresh_stale(limit)
    return {
        "processed": result.processed, "created": result.created,
        "updated": result.updated, "unchanged": result.unchanged,
    }


def open_followup_transient_ids(limit: Optional[int] = None, now=None) -> List[int]:
    """Transients with a follow-up request whose window has not closed, newest window first."""
    from YSE_App.models.followup_models import TransientFollowup

    now = now or timezone.now()
    rows = (
        TransientFollowup.objects.filter(valid_stop__gte=now)
        .order_by("-valid_stop")
        .values_list("transient_id", flat=True)
    )
    out: List[int] = []
    seen = set()
    for tid in rows.iterator():
        if tid is None or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
        if limit and len(out) >= limit:
            break
    return out


@job(BAZIN_KIND, max_attempts=2, backoff_seconds=600)
def bazin_extrapolate(payload=None, job=None):
    from YSE_App.services import bazin

    payload = payload or {}
    ids = payload.get("transient_ids")
    if ids is None:
        ids = open_followup_transient_ids(
            int(payload.get("limit") or _setting_int("PHOTSTAT_BAZIN_LIMIT", DEFAULT_BAZIN_LIMIT))
        )
    at_mjd = payload.get("at_mjd")
    result = bazin.store_extrapolations(ids, None if at_mjd is None else float(at_mjd))
    result["transients"] = len(ids)
    return result


def queue_job(kind: str, payload=None, created_by=None):
    """Enqueue ``kind`` unless one is already queued or running; returns the job or ``None``."""
    from YSE_App.models.job_models import Job

    if Job.objects.filter(kind=kind, status__in=Job.ACTIVE_STATUSES).exists():
        return None
    return enqueue(kind, payload or {}, created_by=created_by)
