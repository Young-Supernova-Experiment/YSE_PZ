"""SALT and Bazin fit tables: writing rows, default values, and scheduled refits (#846).

* :func:`record_run` writes the ``BazinFit`` rows (one per band) or the ``SaltFit`` row of a
  succeeded Bazin / SALT analysis run. ``on_run_saved`` calls it from a ``post_save`` hook
  on :class:`ExternalServiceRun` (``YSE_App/signals.py``); ``manage.py backfill_fit_tables``
  calls it for older runs. It is idempotent (a run that already has rows is skipped), writes
  nothing for failed runs, and never raises into the run that triggered it.
* :func:`latest_bazin`, :func:`latest_salt` and :func:`reference_epoch` give the default
  values: always the **most recent** non-subset run, no pinning. Bazin is the default
  reference epoch; SALT ``t0`` is the alternative.
* :func:`refit_candidates` / :func:`queue_refits` apply the refit rule: a transient with an
  earlier Bazin or SALT run is refitted when, since that run was requested, a new detection
  arrived, or a non-detection within ``FIT_REFIT_WINDOW_DAYS`` (7) days after the last
  detection. Refits are ordinary runs on the job queue (``external_service.run``), requested
  as the previous run's user (so they see the same data) with its parameters. A transient
  with a pending or running fit of that service is skipped, so a pass is idempotent.
  ``FIT_REFIT_ENABLED`` (default False) gates the ``fits.refit_pass`` job, its cron class
  (``data_ingest/Fit_Refit.FitRefit``) and ``manage.py refit_fits --apply``.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from YSE_App.common.filter_display import normalize_filter_name
from YSE_App.jobs import job
from YSE_App.models.analysis_models import AnalysisService
from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.models.fit_models import BazinFit, SaltFit

log = logging.getLogger(__name__)

KIND_BAZIN = "bazin"
KIND_SALT = "salt"
BAZIN_RUNNERS = ("bazin_fit", "YSE_App.analysis.bazin_fit")
SALT_RUNNERS = ("sncosmo_fit", "YSE_App.analysis.sncosmo_fit")

REFERENCE_BAZIN = "bazin"
REFERENCE_SALT = "salt"

REFIT_KIND = "fits.refit_pass"
DEFAULT_REFIT_WINDOW_DAYS = 7.0
DEFAULT_REFIT_LIMIT = 200
_CHUNK = 500


# ------------------------------------------------------------------ helpers


def _float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value) -> Optional[int]:
    out = _float(value)
    return int(out) if out is not None else None


def runner_kind(runner_path: str) -> Optional[str]:
    if runner_path in BAZIN_RUNNERS:
        return KIND_BAZIN
    if runner_path in SALT_RUNNERS:
        return KIND_SALT
    return None


def fit_kind(run: ExternalServiceRun) -> Optional[str]:
    """``bazin`` / ``salt`` for a run of the built-in in-process fitters, else None."""
    try:
        profile = run.service.analysis
    except ObjectDoesNotExist:
        return None
    if profile.runner_kind != AnalysisService.RUNNER_INPROCESS:
        return None
    return runner_kind(profile.runner_path)


def selection_of(run: ExternalServiceRun):
    """The subset selection of a run (#847: ``params["selection"]``), or None for a full fit."""
    from YSE_App.services.analysis_services import run_params

    selection = run_params(run).get("selection")
    return selection if selection else None


def has_rows(run: ExternalServiceRun) -> bool:
    return BazinFit.objects.filter(run_id=run.pk).exists() or SaltFit.objects.filter(run_id=run.pk).exists()


# ------------------------------------------------------------------ writing


def _common(run: ExternalServiceRun, selection) -> Dict:
    return {
        "run": run,
        "transient_id": run.transient_id,
        "fit_date": run.finished_at or timezone.now(),
        "is_subset": selection is not None,
        "selection": selection,
    }


def _last_non_detection(run: ExternalServiceRun, result: Dict) -> Optional[float]:
    """What the run saw (newer runs record it); for older runs, the stored photometry statistic."""
    if "last_non_detection_mjd" in result:
        return _float(result.get("last_non_detection_mjd"))
    from YSE_App.models.phot_stat_models import TransientPhotStat

    value = (TransientPhotStat.objects.filter(transient_id=run.transient_id)
             .values_list("last_non_detection_mjd", flat=True).first())
    return _float(value)


def _bazin_rows(run: ExternalServiceRun, result: Dict, selection) -> List[BazinFit]:
    per_band = result.get("per_band")
    if not isinstance(per_band, dict) or not per_band:
        return []
    reference = result.get("reference_band")
    last_nd = _last_non_detection(run, result)
    rows = []
    for band, values in sorted(per_band.items()):
        if not isinstance(values, dict):
            continue
        peak = _float(values.get("peak_mjd"))
        rows.append(BazinFit(
            band=str(band)[:64],
            band_key=(normalize_filter_name(str(band)) or "")[:16],
            is_reference_band=(band == reference),
            method=str(values.get("method") or "")[:32],
            peak_mjd=peak,
            peak_mag=_float(values.get("peak_mag")),
            t0=_float(values.get("t0")), t0_err=_float(values.get("t0_err")),
            tau_rise=_float(values.get("tau_rise")), tau_rise_err=_float(values.get("tau_rise_err")),
            tau_fall=_float(values.get("tau_fall")), tau_fall_err=_float(values.get("tau_fall_err")),
            mjd_min=_float(values.get("mjd_min")), mjd_max=_float(values.get("mjd_max")),
            n_points=_int(values.get("n_points")),
            chi2=_float(values.get("chi2")), dof=_int(values.get("dof")),
            reduced_chi2=_float(values.get("reduced_chi2")),
            last_non_detection_mjd=last_nd,
            peak_after_last_non_detection=(peak >= last_nd) if peak is not None and last_nd is not None else None,
            **_common(run, selection),
        ))
    return rows


def _salt_row(run: ExternalServiceRun, result: Dict, selection) -> Optional[SaltFit]:
    model = str(result.get("model") or "")
    if not model.lower().startswith("salt") or _float(result.get("t0")) is None:
        return None  # another sncosmo source: no x1 / c to keep
    bands = result.get("bands")
    return SaltFit(
        model=model[:32],
        model_version=str(result.get("model_version") or "")[:32],
        t0=_float(result.get("t0")), t0_err=_float(result.get("t0_err")),
        x0=_float(result.get("x0")), x0_err=_float(result.get("x0_err")),
        x1=_float(result.get("x1")), x1_err=_float(result.get("x1_err")),
        c=_float(result.get("c")), c_err=_float(result.get("c_err")),
        m_b=_float(result.get("mB")), m_b_err=_float(result.get("mB_err")),
        z=_float(result.get("z")), z_err=_float(result.get("z_err")),
        z_source=str(result.get("z_source") or "")[:16],
        mwebv=_float(result.get("mwebv")),
        bands=[str(b) for b in bands] if isinstance(bands, list) else [],
        n_points=_int(result.get("n_points")),
        chi2=_float(result.get("chi2")), dof=_int(result.get("ndof")),
        reduced_chi2=_float(result.get("reduced_chi2")),
        **_common(run, selection),
    )


def record_run(run: ExternalServiceRun) -> int:
    """Write the fit rows of a succeeded Bazin / SALT run; returns how many were written."""
    if run.status != ExternalServiceRun.STATUS_SUCCEEDED or not run.transient_id:
        return 0
    kind = fit_kind(run)
    if kind is None:
        return 0
    result = run.result if isinstance(run.result, dict) else {}
    selection = selection_of(run)
    with transaction.atomic():
        if has_rows(run):
            return 0
        if kind == KIND_BAZIN:
            rows = _bazin_rows(run, result, selection)
            BazinFit.objects.bulk_create(rows)
            return len(rows)
        row = _salt_row(run, result, selection)
        if row is None:
            return 0
        row.save()
        return 1


def on_run_saved(sender, instance, created=False, update_fields=None, raw=False, **kwargs):
    """``post_save`` hook: a run that just succeeded writes its rows. Never breaks the run."""
    if raw or instance.status != ExternalServiceRun.STATUS_SUCCEEDED:
        return
    if update_fields is not None and "status" not in update_fields:
        return
    try:
        record_run(instance)
    except Exception:  # noqa: BLE001 - the run itself has succeeded; the rows can be backfilled
        log.exception("fit tables: could not record run %s", instance.pk)


# ------------------------------------------------------------------ defaults


def latest_bazin(transient_id: int) -> List[BazinFit]:
    """Every band of the most recent non-subset Bazin run of the transient (empty when none)."""
    run_id = (BazinFit.objects.filter(transient_id=transient_id, is_subset=False)
              .order_by("-fit_date", "-run_id").values_list("run_id", flat=True).first())
    if run_id is None:
        return []
    return list(BazinFit.objects.filter(run_id=run_id).order_by("band"))


def latest_bazin_reference(transient_id: int) -> Optional[BazinFit]:
    rows = latest_bazin(transient_id)
    for row in rows:
        if row.is_reference_band:
            return row
    return rows[0] if rows else None


def latest_salt(transient_id: int) -> Optional[SaltFit]:
    return (SaltFit.objects.filter(transient_id=transient_id, is_subset=False)
            .order_by("-fit_date", "-run_id").first())


def reference_epoch(transient_id: int, prefer: str = REFERENCE_BAZIN) -> Tuple[Optional[float], Optional[str]]:
    """``(mjd, source)`` of the transient's reference epoch: Bazin peak by default, SALT t0 when asked.

    Falls back to the other fit when the preferred one has none; ``(None, None)`` without fits.
    """
    def bazin():
        row = latest_bazin_reference(transient_id)
        return row.peak_mjd if row is not None else None

    def salt():
        row = latest_salt(transient_id)
        return row.t0 if row is not None else None

    order = ((REFERENCE_SALT, salt), (REFERENCE_BAZIN, bazin)) if prefer == REFERENCE_SALT else (
        (REFERENCE_BAZIN, bazin), (REFERENCE_SALT, salt))
    for source, getter in order:
        mjd = getter()
        if mjd is not None:
            return mjd, source
    return None, None


def history(transient_id: int, run_ids: Optional[Iterable[int]] = None) -> Dict[str, list]:
    """Fit history for display, newest first: Bazin rows per run (reference band first) and SALT rows.

    ``run_ids`` limits it to runs the viewer may see. The newest non-subset row of each table
    is marked ``current``.
    """
    bazin_qs = BazinFit.objects.filter(transient_id=transient_id).select_related("run", "run__created_by")
    salt_qs = SaltFit.objects.filter(transient_id=transient_id).select_related("run", "run__created_by")
    if run_ids is not None:
        ids = list(run_ids)
        bazin_qs = bazin_qs.filter(run_id__in=ids)
        salt_qs = salt_qs.filter(run_id__in=ids)
    bazin_runs: Dict[int, List[BazinFit]] = defaultdict(list)
    order: List[int] = []
    for row in bazin_qs.order_by("-fit_date", "-run_id", "-is_reference_band", "band"):
        if row.run_id not in bazin_runs:
            order.append(row.run_id)
        bazin_runs[row.run_id].append(row)
    current_bazin = next((rid for rid in order if not bazin_runs[rid][0].is_subset), None)
    salt_rows = list(salt_qs.order_by("-fit_date", "-run_id"))
    current_salt = next((row.pk for row in salt_rows if not row.is_subset), None)
    return {
        "bazin": [{"run": bazin_runs[rid][0].run, "rows": bazin_runs[rid], "current": rid == current_bazin}
                  for rid in order],
        "salt": [{"row": row, "current": row.pk == current_salt} for row in salt_rows],
    }


# ------------------------------------------------------------------ refits


@dataclass
class RefitCandidate:
    profile: AnalysisService
    transient_id: int
    last_run: ExternalServiceRun
    reason: str


def refit_window_days() -> float:
    value = _float(getattr(settings, "FIT_REFIT_WINDOW_DAYS", DEFAULT_REFIT_WINDOW_DAYS))
    return value if value is not None and value >= 0 else DEFAULT_REFIT_WINDOW_DAYS


def refit_enabled() -> bool:
    return bool(getattr(settings, "FIT_REFIT_ENABLED", False))


def fit_profiles():
    return list(AnalysisService.objects.filter(
        runner_kind=AnalysisService.RUNNER_INPROCESS, runner_path__in=BAZIN_RUNNERS + SALT_RUNNERS,
        service__enabled=True,
    ).select_related("service"))


def _latest_runs(profiles) -> Tuple[Dict[Tuple[int, int], ExternalServiceRun], set]:
    """Latest non-subset run per (service, transient), and the pairs with a run still going."""
    latest: Dict[Tuple[int, int], ExternalServiceRun] = {}
    active = set()
    runs = (ExternalServiceRun.objects.filter(service__in=[p.service for p in profiles], transient__isnull=False)
            .select_related("created_by").order_by("created_date", "pk"))
    for run in runs.iterator():
        key = (run.service_id, run.transient_id)
        if not run.is_finished:
            active.add(key)
        if selection_of(run) is None:
            latest[key] = run
    return latest, active


def _chunks(values: List[int]):
    for i in range(0, len(values), _CHUNK):
        yield values[i:i + _CHUNK]


def _new_points(transient_ids: List[int], since) -> Dict[int, list]:
    """``{transient: [(created, PhotPoint)]}`` of unflagged points stored after ``since``."""
    from YSE_App.common.quality_mask import is_legacy_flagged
    from YSE_App.models.phot_models import TransientPhotData
    from YSE_App.models.phot_stat_models import datetime_to_mjd
    from YSE_App.services.photstat import PhotPoint

    out: Dict[int, list] = defaultdict(list)
    for chunk in _chunks(transient_ids):
        rows = (TransientPhotData.objects.filter(photometry__transient_id__in=chunk, created_date__gt=since)
                .values_list("photometry__transient_id", "created_date", "obs_date", "band_id", "mag", "mag_err",
                             "flux", "flux_err", "flux_zero_point", "quality_mask"))
        for tid, created, obs_date, band_id, mag, mag_err, flux, flux_err, zp, mask in rows.iterator():
            if obs_date is None or is_legacy_flagged(mask, "TransientPhotData"):
                continue
            out[tid].append((created, PhotPoint(mjd=datetime_to_mjd(obs_date), band_id=band_id, mag=mag,
                                                mag_err=mag_err, flux=flux, flux_err=flux_err, flux_zero_point=zp)))
    return out


def _last_detections(transient_ids: List[int]) -> Dict[int, Optional[float]]:
    from YSE_App.services import photstat

    out: Dict[int, Optional[float]] = {}
    for chunk in _chunks(transient_ids):
        for tid, points in photstat.points_by_transient(chunk).items():
            mjds = [p.mjd for p in points if photstat.is_detection(p) and _float(p.mjd) is not None]
            out[tid] = max(mjds) if mjds else None
    return out


def refit_reason(new_points, since, last_detection_mjd: Optional[float], window_days: float) -> Optional[str]:
    """Why points stored after ``since`` call for a refit, or None (the N-day rule, pure)."""
    from YSE_App.services import photstat

    limits = []
    for created, point in new_points:
        if created <= since:
            continue
        if photstat.is_detection(point):
            return "new detection"
        if photstat.limiting_mag(point) is not None:
            limits.append(point.mjd)
    if last_detection_mjd is not None:
        for mjd in limits:
            if last_detection_mjd < mjd <= last_detection_mjd + window_days:
                return "non-detection within %g days after the last detection" % window_days
    return None


def refit_candidates(limit: Optional[int] = None) -> List[RefitCandidate]:
    """(service, transient) pairs whose newest fit is out of date under the refit rule."""
    profiles = fit_profiles()
    if not profiles:
        return []
    by_service = {p.service_id: p for p in profiles}
    latest, active = _latest_runs(profiles)
    pairs = {key: run for key, run in latest.items() if key not in active}
    if not pairs:
        return []
    transient_ids = sorted({tid for _sid, tid in pairs})
    since = min(run.created_date for run in pairs.values())
    new_points = _new_points(transient_ids, since)
    window = refit_window_days()
    needs_last = sorted(tid for tid, pts in new_points.items() if pts)
    last_det = _last_detections(needs_last) if needs_last else {}
    out = []
    for (service_id, tid), run in sorted(pairs.items(), key=lambda item: item[1].created_date):
        reason = refit_reason(new_points.get(tid, ()), run.created_date, last_det.get(tid), window)
        if reason is None:
            continue
        out.append(RefitCandidate(by_service[service_id], tid, run, reason))
        if limit and len(out) >= limit:
            break
    return out


def start_refit(candidate: RefitCandidate) -> ExternalServiceRun:
    """A new run of the candidate's service, as its previous run's user, with that run's parameters."""
    from YSE_App.services import analysis_services as svc
    from YSE_App.services import external_services as runs

    previous = candidate.last_run
    user = previous.created_by
    params = svc.run_params(previous)
    params.pop("selection", None)
    clean = svc.coerce_params(candidate.profile, params)
    payload = {
        "analysis": True,
        "params": clean,
        "input_spec": list(candidate.profile.input_spec or []),
        "requested_by": getattr(user, "username", ""),
        "scheduled_refit": True,
        "refit_reason": candidate.reason,
    }
    run, _token = runs.start_run(candidate.profile.service, user, payload, transient=previous.transient,
                                 dispatch=False, enforce_limit=False)
    runs.dispatch_run(run)
    return run


def queue_refits(*, apply: bool, limit: Optional[int] = None) -> Dict:
    """One refit pass: find candidates and, with ``apply``, start their runs on the job queue."""
    from YSE_App.services import analysis_services as svc
    from YSE_App.services import external_services as runs

    if limit is None:
        limit = int(getattr(settings, "FIT_REFIT_LIMIT", DEFAULT_REFIT_LIMIT) or DEFAULT_REFIT_LIMIT)
    candidates = refit_candidates(limit)
    summary = {"candidates": len(candidates), "queued": 0, "skipped": 0, "apply": bool(apply)}
    if not apply:
        return summary
    for candidate in candidates:
        user = candidate.last_run.created_by
        if user is None or not user.is_active:
            summary["skipped"] += 1
            continue
        try:
            start_refit(candidate)
        except (runs.ExternalServiceError, svc.InvalidParams) as exc:
            log.info("fit refit: transient %s, service %s skipped: %s", candidate.transient_id,
                     candidate.profile.service.slug, exc)
            summary["skipped"] += 1
            continue
        summary["queued"] += 1
    return summary


@job(REFIT_KIND, max_attempts=1)
def refit_pass(payload=None, job=None):
    """Job handler for :data:`REFIT_KIND`; does nothing unless ``FIT_REFIT_ENABLED``."""
    if not refit_enabled():
        return {"skipped": "FIT_REFIT_ENABLED is off"}
    limit = (payload or {}).get("limit")
    return queue_refits(apply=True, limit=int(limit) if limit else None)
