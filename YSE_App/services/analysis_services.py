"""Analysis services: registry helpers, starting runs, storing results (#313, #314).

Sits on :mod:`YSE_App.services.external_services` (#265): an analysis run is
an ``ExternalServiceRun`` on an ``ExternalService`` of kind ``analysis`` that
has an :class:`AnalysisService` profile. This module knows how to

* register a service (:func:`register_service`; the management command and
  the tests use it),
* list the services a user may run and their daily-cap state,
* validate the parameters a user typed against the service's ``param_schema``,
* start a run (payload built later, in the job, by the runner),
* store what a run produced (:func:`store_attachment`, :func:`complete_with_result`),
  including attachments arriving on the callback endpoint,
* fail webhook runs that never called back (:func:`fail_timed_out_runs`).

The runner that executes runs lives in :mod:`YSE_App.analysis.runners`.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from YSE_App.analysis import BUILTIN_RUNNERS
from YSE_App.analysis.base import AnalysisResult, Attachment
from YSE_App.models.analysis_models import INPUT_CHOICES, OUTPUT_CHOICES, AnalysisResultFile, AnalysisService
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.services import external_services as runs

log = logging.getLogger(__name__)

DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_FILES_PER_RUN = 20
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AnalysisConfigError(Exception):
    """A service is mis-registered (unknown runner, bad spec)."""


class InvalidParams(Exception):
    """User parameters do not match the service's ``param_schema``."""

    def __init__(self, errors: Dict[str, str]):
        self.errors = errors
        super().__init__("; ".join("%s: %s" % kv for kv in sorted(errors.items())))


# --- registry ------------------------------------------------------------------

def setting(name: str, default):
    return getattr(settings, name, default)


def load_runner_module(path: str):
    """Import the runner module for ``runner_path`` (a built-in name or a dotted path)."""
    import importlib

    if not path:
        raise AnalysisConfigError("in-process analysis service without a runner_path")
    module_path = BUILTIN_RUNNERS.get(path, path)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise AnalysisConfigError("cannot import analysis runner %r: %s" % (module_path, exc))
    if not callable(getattr(module, "run", None)):
        raise AnalysisConfigError("analysis runner %r has no run(payload, params)" % module_path)
    return module


def register_service(
    slug: str,
    name: str = "",
    *,
    runner: str = "",
    base_url: str = "",
    user: Optional[User] = None,
    description: str = "",
    input_spec: Optional[Iterable[str]] = None,
    output_spec: Optional[Iterable[str]] = None,
    param_schema: Optional[Dict] = None,
    default_params: Optional[Dict] = None,
    timeout_seconds: Optional[int] = None,
    max_runs_per_user_per_day: Optional[int] = None,
    groups: Optional[Iterable] = None,
    credential=None,
    enabled: bool = True,
    display_order: Optional[int] = None,
    summary_keys: Optional[Iterable[str]] = None,
) -> AnalysisService:
    """Create or update an analysis service. ``runner`` names an in-process module, ``base_url`` a webhook.

    For an in-process runner the module's ``NAME`` / ``DESCRIPTION`` /
    ``INPUT_SPEC`` / ``OUTPUT_SPEC`` / ``PARAM_SCHEMA`` fill whatever the caller
    left blank.
    """
    if bool(runner) == bool(base_url):
        raise AnalysisConfigError("give exactly one of runner= (in-process) or base_url= (webhook)")
    module = load_runner_module(runner) if runner else None
    name = name or (getattr(module, "NAME", "") if module else "") or slug
    description = description or (getattr(module, "DESCRIPTION", "") if module else "")
    input_spec = list(input_spec) if input_spec is not None else list(getattr(module, "INPUT_SPEC", None) or ["photometry", "redshift"])
    output_spec = list(output_spec) if output_spec is not None else list(getattr(module, "OUTPUT_SPEC", None) or ["results"])
    param_schema = dict(param_schema) if param_schema is not None else dict(getattr(module, "PARAM_SCHEMA", None) or {})
    if summary_keys is None and module is not None:
        summary_keys = list(getattr(module, "SUMMARY_KEYS", None) or []) or None
    bad = [i for i in input_spec if i not in INPUT_CHOICES and i != "all"]
    if bad:
        raise AnalysisConfigError("unknown input_spec item(s): %s" % ", ".join(bad))
    bad = [o for o in output_spec if o not in OUTPUT_CHOICES]
    if bad:
        raise AnalysisConfigError("unknown output_spec item(s): %s" % ", ".join(bad))

    audit = {"created_by": user, "modified_by": user} if user is not None else {}
    with transaction.atomic():
        service, created = ExternalService.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "kind": ExternalService.KIND_ANALYSIS, **audit},
        )
        service.name = name
        service.kind = ExternalService.KIND_ANALYSIS
        service.description = description
        service.base_url = base_url or ""
        service.enabled = enabled
        if default_params is not None:
            service.default_params = dict(default_params)
        if max_runs_per_user_per_day is not None:
            service.max_runs_per_user_per_day = int(max_runs_per_user_per_day)
        if credential is not None:
            service.credential = credential
        if user is not None:
            service.modified_by = user
            if not service.created_by_id:
                service.created_by = user
        service.save()
        if groups is not None:
            resolved = [g if isinstance(g, Group) else Group.objects.get(name=g) for g in groups]
            service.groups.set(resolved)
        profile, _ = AnalysisService.objects.get_or_create(service=service, defaults=audit)
        profile.runner_kind = AnalysisService.RUNNER_INPROCESS if runner else AnalysisService.RUNNER_WEBHOOK
        profile.runner_path = runner or ""
        profile.input_spec = input_spec
        profile.output_spec = output_spec
        profile.param_schema = param_schema
        if timeout_seconds is not None:
            profile.timeout_seconds = int(timeout_seconds)
        if display_order is not None:
            profile.display_order = int(display_order)
        if summary_keys is not None:
            profile.summary_keys = list(summary_keys)
        if user is not None:
            profile.modified_by = user
            if not profile.created_by_id:
                profile.created_by = user
        profile.save()
    return profile


def services_for_user(user, *, include_disabled: bool = False):
    """Analysis services the user may run, in display order (staff: every enabled one)."""
    qs = AnalysisService.objects.select_related("service").prefetch_related("service__groups")
    if not include_disabled:
        qs = qs.filter(service__enabled=True)
    if user is None or not user.is_authenticated:
        return qs.none()
    if user.is_staff or user.is_superuser:
        return qs
    return qs.filter(
        Q(service__groups__isnull=True) | Q(service__groups__in=user.groups.all())
    ).distinct()


def cap_status(service: ExternalService, user) -> Dict:
    """``{"limit", "used", "remaining", "exempt"}`` for the per-user daily cap (staff exempt)."""
    limit = int(service.max_runs_per_user_per_day or 0)
    exempt = bool(user is not None and (user.is_staff or user.is_superuser))
    used = runs.runs_today_for_user(service, user) if (limit and user is not None and user.pk) else 0
    remaining = None if (not limit or exempt) else max(limit - used, 0)
    return {"limit": limit or None, "used": used, "remaining": remaining, "exempt": exempt}


# --- parameters ----------------------------------------------------------------

def form_fields(profile: AnalysisService) -> List[Dict]:
    """The parameter form as a list of dicts the template / API render, with effective defaults."""
    defaults = profile.default_params()
    fields = []
    for name, spec in (profile.param_schema or {}).items():
        spec = dict(spec) if isinstance(spec, dict) else {}
        fields.append({
            "name": name,
            "type": spec.get("type", "text"),
            "label": spec.get("label") or name.replace("_", " "),
            "help": spec.get("help", ""),
            "choices": list(spec.get("choices") or []),
            "default": defaults.get(name, spec.get("default")),
            "required": bool(spec.get("required", False)),
        })
    return fields


def coerce_params(profile: AnalysisService, raw: Optional[Dict]) -> Dict:
    """Validate/cast ``raw`` against ``param_schema``; unknown keys are kept as given (strings)."""
    raw = dict(raw or {})
    out = profile.default_params()
    errors = {}
    schema = profile.param_schema or {}
    for name, spec in schema.items():
        spec = spec if isinstance(spec, dict) else {}
        if name not in raw or raw[name] in (None, ""):
            if spec.get("required") and out.get(name) in (None, ""):
                errors[name] = "required"
            continue
        value = raw.pop(name)
        kind = spec.get("type", "text")
        try:
            if kind == "number":
                value = float(value)
                if "min" in spec and value < float(spec["min"]):
                    raise ValueError("below %s" % spec["min"])
                if "max" in spec and value > float(spec["max"]):
                    raise ValueError("above %s" % spec["max"])
            elif kind == "integer":
                value = int(value)
            elif kind == "boolean":
                value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
            elif kind == "choice":
                choices = [str(c) for c in spec.get("choices") or []]
                value = str(value)
                # the service's own default (default_params) is always acceptable
                if choices and value not in choices and value != str(out.get(name)):
                    raise ValueError("must be one of %s" % ", ".join(choices))
            else:
                value = str(value)
        except (TypeError, ValueError) as exc:
            errors[name] = str(exc) or "invalid"
            continue
        out[name] = value
    for key, value in raw.items():
        if key in ("csrfmiddlewaretoken", "service", "transient"):
            continue
        out[key] = value
    if errors:
        raise InvalidParams(errors)
    return out


# --- starting runs ---------------------------------------------------------------

def start_analysis(profile: AnalysisService, transient, user, params: Optional[Dict] = None,
                   *, dispatch: bool = True) -> ExternalServiceRun:
    """Create and dispatch a run of ``profile`` on ``transient`` for ``user``.

    Raises ``runs.ServiceDisabled`` / ``runs.RunLimitExceeded`` (from ``start_run``)
    or :class:`InvalidParams`. The payload itself is built by the runner when
    the job executes, with this user's data visibility.
    """
    clean = coerce_params(profile, params)
    request_payload = {
        "analysis": True,
        "params": clean,
        "input_spec": list(profile.input_spec or []),
        "requested_by": getattr(user, "username", ""),
    }
    exempt = bool(user is not None and (user.is_staff or user.is_superuser))
    run, _token = runs.start_run(profile.service, user, request_payload, transient=transient, dispatch=False,
                                 enforce_limit=not exempt)
    if dispatch:
        runs.dispatch_run(run)
    return run


def run_params(run: ExternalServiceRun) -> Dict:
    payload = run.request_payload or {}
    params = payload.get("params")
    if isinstance(params, dict):
        return dict(params)
    # a run started through the generic API with the parameters at the top level
    return {k: v for k, v in payload.items() if k not in ("analysis", "input_spec", "requested_by")}


def runs_for_transient(transient, user=None, *, limit: Optional[int] = None):
    qs = (ExternalServiceRun.objects.filter(transient=transient, service__kind=ExternalService.KIND_ANALYSIS)
          .select_related("service", "service__analysis", "created_by")
          .prefetch_related("files")
          .order_by("-created_date"))
    return qs[:limit] if limit else qs


def can_manage_run(run: ExternalServiceRun, user) -> bool:
    if user is None or not user.is_authenticated:
        return False
    return bool(user.is_staff or user.is_superuser or run.created_by_id == user.pk)


def delete_run(run: ExternalServiceRun) -> None:
    for f in run.files.all():
        try:
            f.file.delete(save=False)
        except Exception:  # pragma: no cover - storage specific
            log.warning("could not delete result file %s of run %s", f.name, run.uuid)
    run.delete()


def cancel_run(run: ExternalServiceRun, reason: str = "cancelled by user") -> ExternalServiceRun:
    if not run.is_finished:
        run.mark_cancelled(reason)
    return run


# --- storing results ---------------------------------------------------------------

def safe_name(name: str, default: str = "file") -> str:
    base = os.path.basename(str(name or "")).strip() or default
    base = SAFE_NAME_RE.sub("_", base).strip("._") or default
    return base[:120]


def guess_kind(name: str, content_type: str, declared: str = "") -> str:
    if declared in dict(AnalysisResultFile.KIND_CHOICES):
        return declared
    if (content_type or "").startswith("image/"):
        return AnalysisResultFile.KIND_PLOT
    lower = (name or "").lower()
    if lower.endswith((".nc", ".netcdf", ".joblib", ".pkl", ".pickle", ".h5", ".hdf5")):
        return AnalysisResultFile.KIND_INFERENCE
    if lower.endswith((".json", ".csv", ".ecsv", ".txt", ".fits")):
        return AnalysisResultFile.KIND_DATA
    return AnalysisResultFile.KIND_OTHER


def store_attachment(run: ExternalServiceRun, name: str, data: bytes, content_type: str = "",
                     kind: str = "", meta: Optional[Dict] = None) -> AnalysisResultFile:
    """Save ``data`` as a result file of ``run`` (under ``MEDIA_ROOT/service_runs/<uuid>/``)."""
    max_bytes = int(setting("ANALYSIS_MAX_ATTACHMENT_BYTES", DEFAULT_MAX_ATTACHMENT_BYTES))
    if len(data) > max_bytes:
        raise ValueError("attachment %s is %d bytes; the limit is %d" % (name, len(data), max_bytes))
    max_files = int(setting("ANALYSIS_MAX_FILES_PER_RUN", DEFAULT_MAX_FILES_PER_RUN))
    if run.files.count() >= max_files:
        raise ValueError("run %s already has %d files" % (run.uuid, max_files))
    name = safe_name(name)
    content_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    kind = guess_kind(name, content_type, kind)
    # one row per name: a repeated callback replaces the earlier file
    existing = run.files.filter(name=name).first()
    if existing is not None:
        try:
            existing.file.delete(save=False)
        except Exception:  # pragma: no cover
            pass
        existing.delete()
    row = AnalysisResultFile(
        run=run, kind=kind, name=name, content_type=content_type, size=len(data), meta=dict(meta or {}),
        created_by=run.created_by, modified_by=run.created_by,
    )
    row.file.save(name, ContentFile(data), save=False)
    row.save()
    return row


def complete_with_result(run: ExternalServiceRun, result: AnalysisResult) -> ExternalServiceRun:
    """Store an in-process runner's ``AnalysisResult`` and mark the run succeeded."""
    stored = []
    for att in list(result.plots) + list(result.files):
        try:
            stored.append(store_attachment(run, att.name, att.data, att.content_type, att.kind, att.meta))
        except ValueError as exc:
            log.warning("run %s: %s", run.uuid, exc)
    results = dict(result.results or {})
    if result.summary:
        results.setdefault("_summary", result.summary)
    results["_files"] = [f.name for f in stored]
    runs.record_completion(run, ExternalServiceRun.STATUS_SUCCEEDED, result=results)
    return run


def _decode_attachment(item) -> Optional[Attachment]:
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
    return Attachment(
        name=str(item.get("name") or "file"), data=raw,
        content_type=str(item.get("content_type") or ""), kind=str(item.get("kind") or ""),
        meta=item.get("meta") if isinstance(item.get("meta"), dict) else {},
    )


def store_callback_attachments(run: ExternalServiceRun, body: Optional[Dict], files=None) -> Tuple[List[AnalysisResultFile], List[str]]:
    """Attachments from a callback: JSON ``plots`` / ``files`` (base64) and multipart uploads.

    Returns ``(stored rows, error messages)``; a bad attachment is skipped, not fatal.
    """
    stored, errors = [], []
    body = body if isinstance(body, dict) else {}
    for key, default_kind in (("plots", AnalysisResultFile.KIND_PLOT), ("files", "")):
        items = body.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            att = _decode_attachment(item)
            if att is None:
                errors.append("%s: entry is not {name, data(base64)}" % key)
                continue
            try:
                stored.append(store_attachment(run, att.name, att.data, att.content_type, att.kind or default_kind, att.meta))
            except ValueError as exc:
                errors.append(str(exc))
    if files:
        kinds = body.get("kinds") if isinstance(body.get("kinds"), dict) else {}
        for field, upload in files.items():
            try:
                stored.append(store_attachment(
                    run, upload.name or field, upload.read(), getattr(upload, "content_type", "") or "",
                    str(kinds.get(field) or kinds.get(upload.name) or ""), {"field": field},
                ))
            except ValueError as exc:
                errors.append(str(exc))
    return stored, errors


# --- housekeeping -------------------------------------------------------------------

def fail_timed_out_runs(now=None) -> int:
    """Mark running/pending analysis runs older than their service's timeout as failed."""
    now = now or timezone.now()
    count = 0
    qs = (ExternalServiceRun.objects.filter(
        service__kind=ExternalService.KIND_ANALYSIS,
        status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING),
    ).select_related("service__analysis"))
    for run in qs:
        profile = getattr(run.service, "analysis", None)
        timeout = int(getattr(profile, "timeout_seconds", 0) or 0)
        if not timeout:
            continue
        started = run.started_at or run.created_date
        # pending in-process runs wait for the queue; give them the same grace once more
        grace = timeout if run.status == ExternalServiceRun.STATUS_RUNNING else 2 * timeout
        if started and (now - started).total_seconds() > grace:
            run.mark_failed("timed out after %d s without a result" % grace)
            count += 1
    return count


# --- presentation -----------------------------------------------------------------------

def summary_pairs(run: ExternalServiceRun, limit: int = 4) -> List[Tuple[str, str]]:
    """``[(key, value)]`` shown in the runs table: the service's ``summary_keys``, else the first numeric results."""
    from YSE_App.analysis.base import format_value

    result = run.result if isinstance(run.result, dict) else {}
    profile = getattr(run.service, "analysis", None)
    keys = list(getattr(profile, "summary_keys", None) or [])
    if not keys:
        for key, value in result.items():
            if key.startswith("_") or key.endswith("_err") or isinstance(value, (dict, list, bool)) or value is None:
                continue
            if isinstance(value, (int, float)):
                keys.append(key)
            if len(keys) >= limit:
                break
    pairs = []
    for key in keys[:limit]:
        if key in result:
            pairs.append((key, format_value(result.get(key), result.get(key + "_err"))))
    return pairs


def result_rows(run: ExternalServiceRun) -> List[Tuple[str, str]]:
    """Flat ``(key, formatted value)`` rows of the result JSON for the detail card."""
    from YSE_App.analysis.base import format_value

    result = run.result if isinstance(run.result, dict) else {}
    rows = []
    for key in sorted(result):
        if key.startswith("_") or key.endswith("_err"):
            continue
        value = result[key]
        if isinstance(value, dict):
            continue
        if isinstance(value, list):
            rows.append((key, ", ".join(str(v) for v in value[:12]) + (" ..." if len(value) > 12 else "")))
        elif isinstance(value, bool) or isinstance(value, str) or value is None:
            rows.append((key, "-" if value is None else str(value)))
        else:
            rows.append((key, format_value(value, result.get(key + "_err"))))
    return rows
