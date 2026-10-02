"""Summary-tab view of the stored analysis runs: the SALT3 fit and the NGSF classification (#315).

The Summary tab used to refit SALT2 with sncosmo inside the plot request
(``salt2plot/<id>/1``), so every click blocked on a model download and a
minimiser. It now shows the **last successful** ``sncosmo_fit`` run of the
analysis-service framework (#313, #360): the plot endpoints overlay that
run's stored model curves (``model_curves.json``) and print its parameters,
and a **Refit** button starts a new run on the job queue. The same fragment
shows the last NGSF classification (:mod:`YSE_App.analysis.ngsf`) with a
**Run NGSF** button, or says that NGSF is not installed on this server.

Nothing here computes anything: it finds runs and reads their files.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Dict, List, Optional

from django.core.cache import cache

from YSE_App.models.analysis_models import AnalysisService
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun

log = logging.getLogger(__name__)

SNCOSMO_RUNNER = "sncosmo_fit"
NGSF_RUNNER = "ngsf"
CURVES_FILE = "model_curves.json"
CURVES_CACHE_SECONDS = 600
SALT_LABEL_ORDER = ("t0", "z", "x1", "c", "mB")


def services_for_runner(runner_path: str, *, enabled_only: bool = False):
    """In-process analysis services backed by the built-in ``runner_path`` (a stack may register several)."""
    qs = (AnalysisService.objects.filter(runner_kind=AnalysisService.RUNNER_INPROCESS, runner_path=runner_path)
          .select_related("service").order_by("-service__enabled", "display_order", "service__name"))
    if enabled_only:
        qs = qs.filter(service__enabled=True)
    return qs


def runs_of_runner(transient, runner_path: str):
    return (ExternalServiceRun.objects.filter(
        transient=transient, service__kind=ExternalService.KIND_ANALYSIS,
        service__analysis__runner_path=runner_path)
        .select_related("service", "created_by").order_by("-created_date", "-pk"))


def latest_run(transient, runner_path: str) -> Optional[ExternalServiceRun]:
    return runs_of_runner(transient, runner_path).first()


def latest_successful_run(transient, runner_path: str) -> Optional[ExternalServiceRun]:
    return runs_of_runner(transient, runner_path).filter(status=ExternalServiceRun.STATUS_SUCCEEDED).first()


def active_run(transient, runner_path: str) -> Optional[ExternalServiceRun]:
    return (runs_of_runner(transient, runner_path)
            .filter(status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING)).first())


# --- the stored SALT fit --------------------------------------------------------------------

def stored_salt_fit(transient) -> Optional[ExternalServiceRun]:
    """The run whose model the Summary-tab plots overlay: the newest successful sncosmo fit."""
    return latest_successful_run(transient, SNCOSMO_RUNNER)


def model_curves(run: ExternalServiceRun) -> Dict:
    """``{"mjd": [...], "bands": {sncosmo band: [mag | None]}}`` from the run's ``model_curves.json`` (cached)."""
    key = "analysis_model_curves_v1_%s" % run.pk
    cached = cache.get(key)
    if cached is not None:
        return cached
    curves = {"mjd": [], "bands": {}}
    row = run.files.filter(name=CURVES_FILE).first()
    if row is not None:
        try:
            with row.file.open("rb") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("bands"), dict):
                curves = {"mjd": list(data.get("mjd") or []), "bands": dict(data["bands"])}
        except (OSError, ValueError) as exc:
            log.warning("run %s: cannot read %s: %s", run.uuid, CURVES_FILE, exc)
    cache.set(key, curves, CURVES_CACHE_SECONDS)
    return curves


def salt_fit_labels(run: ExternalServiceRun, today_mjd: Optional[float] = None) -> List[str]:
    """The lines the plot prints for a stored fit (phase, z, t0, mB, x1, c and the run's date)."""
    result = run.result if isinstance(run.result, dict) else {}
    lines = []
    t0 = _num(result.get("t0"))
    if t0 is not None and today_mjd is not None:
        phase = today_mjd - t0
        lines.append("phase = %s%.1f days" % ("+" if phase > 0 else "", phase))
    z = _num(result.get("z"))
    if z is not None:
        lines.append("\U0001D63B  = %.3f" % z)  # italic z
    if t0 is not None:
        lines.append("\U0001D461₀  = %i" % t0)  # t0
    mB = _num(result.get("mB"))
    if mB is not None:
        lines.append("\U0001D45A₈ = %.2f" % mB)  # mB
    x1 = _num(result.get("x1"))
    if x1 is not None:
        lines.append("\U0001D465₁ = %.2f" % x1)  # x1
    c = _num(result.get("c"))
    if c is not None:
        lines.append("\U0001D450  = %.2f" % c)  # c
    stamp = run.finished_at.strftime("%Y-%m-%d") if run.finished_at else ""
    lines.append("%s fit %s%s" % (result.get("model") or "SALT3", stamp,
                                  (" by " + run.created_by.username) if run.created_by_id else ""))
    return lines


def _num(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --- Summary-tab fragment context ---------------------------------------------------------------

def _run_entry(run: Optional[ExternalServiceRun]) -> Optional[Dict]:
    if run is None:
        return None
    result = run.result if isinstance(run.result, dict) else {}
    return {
        "run": run,
        "uuid": str(run.uuid),
        "status": run.status,
        "is_finished": run.is_finished,
        "result": result,
        "error": run.error,
        "user": run.created_by.username if run.created_by_id else "",
        "when": run.finished_at or run.started_at or run.created_date,
        "summary_text": result.get("_summary", ""),
    }


def _service_entry(user, runner_path: str) -> Dict:
    """Which service the Refit / Run button uses: the first enabled one the user may run."""
    from YSE_App.services import analysis_services as svc

    registered = list(services_for_runner(runner_path))
    allowed = None
    if registered and user is not None and getattr(user, "is_authenticated", False):
        slugs = [p.service.slug for p in registered if p.service.enabled]
        if slugs:
            allowed = svc.services_for_user(user).filter(service__slug__in=slugs).order_by("display_order").first()
    return {
        "registered": bool(registered),
        "enabled": any(p.service.enabled for p in registered),
        "profile": allowed,
        "slug": allowed.service.slug if allowed is not None else "",
        "cap": svc.cap_status(allowed.service, user) if allowed is not None else None,
    }


def salt_fit_context(transient, user) -> Dict:
    """Context of ``transient_detail/salt_fit_status.html``."""
    service = _service_entry(user, SNCOSMO_RUNNER)
    latest = latest_run(transient, SNCOSMO_RUNNER)
    success = stored_salt_fit(transient)
    active = latest if (latest is not None and not latest.is_finished) else None
    entry = _run_entry(success)
    if entry is not None:
        r = entry["result"]
        entry["values"] = [(k, _fmt(r.get(k), r.get(k + "_err"))) for k in SALT_LABEL_ORDER if r.get(k) is not None]
        chi2 = _num(r.get("reduced_chi2"))
        if chi2 is not None:
            entry["values"].append(("chi2/dof", "%.2f" % chi2))
    return {
        "transient": transient,
        "salt_service": service,
        "salt_fit": entry,
        "salt_active": _run_entry(active),
        "salt_failed": _run_entry(latest) if (latest is not None and latest.status == ExternalServiceRun.STATUS_FAILED
                                               and (success is None or latest.pk != success.pk)) else None,
        "salt_can_run": bool(service["slug"]) and active is None
        and not (service["cap"] and service["cap"]["remaining"] == 0),
    }


def ngsf_context(transient, user, *, availability: Optional[Dict] = None) -> Dict:
    """Context of ``transient_detail/ngsf_status.html``."""
    from YSE_App.analysis import ngsf as ngsf_mod

    service = _service_entry(user, NGSF_RUNNER)
    avail = availability if availability is not None else ngsf_mod.availability()
    latest = latest_run(transient, NGSF_RUNNER)
    success = latest_successful_run(transient, NGSF_RUNNER)
    active = latest if (latest is not None and not latest.is_finished) else None
    entry = _run_entry(success)
    if entry is not None:
        r = entry["result"]
        entry["best"] = {
            "type": r.get("best_type") or "?",
            "template": r.get("best_template") or "",
            "phase": _num(r.get("best_phase")),
            "z": _num(r.get("best_z")),
            "chi2_dof": _num(r.get("best_chi2_dof")),
            "galaxy": r.get("best_galaxy") or "",
        }
        votes = r.get("type_votes") if isinstance(r.get("type_votes"), dict) else {}
        entry["votes"] = sorted(votes.items(), key=lambda kv: (-int(kv[1] or 0), kv[0]))
        entry["spectrum_id"] = r.get("spectrum_id")
        entry["spectrum_mjd"] = _num(r.get("spectrum_mjd"))
    return {
        "transient": transient,
        "ngsf_service": service,
        "ngsf_available": bool(avail.get("available")),
        "ngsf_reason": avail.get("reason", ""),
        "ngsf_fit": entry,
        "ngsf_active": _run_entry(active),
        "ngsf_failed": _run_entry(latest) if (latest is not None and latest.status == ExternalServiceRun.STATUS_FAILED
                                              and (success is None or latest.pk != success.pk)) else None,
        "ngsf_can_run": bool(service["slug"]) and bool(avail.get("available")) and active is None
        and not (service["cap"] and service["cap"]["remaining"] == 0),
    }


def _fmt(value, err=None) -> str:
    from YSE_App.analysis.base import format_value

    return format_value(value, err)
