"""Execute analysis runs: the runner registered for ``kind:analysis`` (#313).

``execute_run`` (:mod:`YSE_App.services.external_services`, the
``external_service.run`` job) hands every run of an analysis service to
:func:`run_analysis`, which

* builds the payload with :func:`YSE_App.services.analysis_payload.build_payload`
  using the requesting user's data visibility;
* for an **in-process** service imports ``runner_path`` and calls
  ``run(payload, params)`` in a worker thread, abandoning it after
  ``timeout_seconds`` (0 = no limit), then stores results, plots and files
  and marks the run succeeded / failed;
* for a **webhook** service mints a fresh callback token, POSTs the payload
  with the callback URL and token to ``ExternalService.base_url`` (plus a
  bearer token from the service's credential when it has ``api_token`` /
  ``token``) and marks the run running; the remote service reports back on
  ``POST /api/service_runs/<uuid>/callback/`` (results JSON, base64 or
  multipart plots and files). A 2xx response whose body already carries a
  final ``status`` completes the run at once (synchronous services).

A service of kind ``analysis`` without an ``AnalysisService`` profile is left
alone (``handled=False``), as before this module existed.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Dict, Optional

from django.conf import settings

from YSE_App.analysis.base import AnalysisError, AnalysisResult
from YSE_App.models.analysis_models import AnalysisService
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.services import analysis_services as svc
from YSE_App.services import external_services as runs
from YSE_App.services.analysis_payload import build_payload

log = logging.getLogger(__name__)

DEFAULT_HTTP_TIMEOUT = 30
WEBHOOK_USER_AGENT = "YSE-PZ analysis-services/1.0"


def _payload_for(run: ExternalServiceRun, profile: AnalysisService) -> Dict:
    params = svc.run_params(run)
    spec = (run.request_payload or {}).get("input_spec") or profile.input_spec or ["photometry", "redshift"]
    return build_payload(run.transient, run.created_by, spec, params)


# --- in-process ------------------------------------------------------------------

def call_with_timeout(func, args, timeout: Optional[float]):
    """Run ``func(*args)`` in a daemon thread; ``(finished, value, exception)``."""
    if not timeout or timeout <= 0:
        try:
            return True, func(*args), None
        except Exception as exc:  # noqa: BLE001 - reported on the run
            return True, None, exc
    box = {}

    def target():
        try:
            box["value"] = func(*args)
        except Exception as exc:  # noqa: BLE001
            box["exc"] = exc

    worker = threading.Thread(target=target, name="analysis-runner", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return False, None, None
    return True, box.get("value"), box.get("exc")


def execute_inprocess(run: ExternalServiceRun, profile: AnalysisService) -> Dict:
    try:
        module = svc.load_runner_module(profile.runner_path)
    except svc.AnalysisConfigError as exc:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=str(exc))
        return {"status": run.status, "error": str(exc)}
    run.mark_running()
    if run.transient_id is None:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="analysis runs need a transient")
        return {"status": run.status}
    try:
        payload = _payload_for(run, profile)
    except Exception as exc:  # noqa: BLE001
        log.exception("run %s: payload build failed", run.uuid)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="payload: %s: %s" % (type(exc).__name__, exc))
        return {"status": run.status}
    params = payload.get("params") or {}
    timeout = float(profile.timeout_seconds or 0)
    finished, value, exc = call_with_timeout(module.run, (payload, params), timeout)
    if not finished:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED,
                               error="timed out after %d s" % int(timeout))
        return {"status": run.status, "timed_out": True}
    if exc is not None:
        if isinstance(exc, AnalysisError):
            error = str(exc)
        else:
            log.warning("run %s: runner raised", run.uuid, exc_info=exc)
            error = "%s: %s" % (type(exc).__name__, exc)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
        return {"status": run.status, "error": error}
    if not isinstance(value, AnalysisResult):
        if isinstance(value, dict):
            value = AnalysisResult(results=value)
        else:
            runs.record_completion(run, ExternalServiceRun.STATUS_FAILED,
                                   error="runner returned %s, not an AnalysisResult" % type(value).__name__)
            return {"status": run.status}
    svc.complete_with_result(run, value)
    return {"status": run.status, "files": run.files.count()}


# --- webhook ----------------------------------------------------------------------

def _bearer_from_credential(service: ExternalService) -> str:
    cred = service.credential
    if cred is None:
        return ""
    try:
        secret = cred.get_secret(touch=True) or {}
    except Exception:  # noqa: BLE001 - a key problem is reported on the run by the caller
        log.warning("service %s: credential %s cannot be decrypted", service.slug, cred.pk)
        return ""
    for key in ("api_token", "token", "bearer", "api_key"):
        if secret.get(key):
            return str(secret[key])
    return ""


def webhook_document(run: ExternalServiceRun, profile: AnalysisService, payload: Dict, token: str) -> Dict:
    doc = dict(payload)
    doc.update({
        "run_id": str(run.uuid),
        "service": profile.service.slug,
        "callback_url": runs.callback_url(run),
        "callback_token": token,
        "callback_method": "POST",
        "params": payload.get("params") or {},
        "output_spec": list(profile.output_spec or []),
    })
    return doc


def dispatch_webhook(run: ExternalServiceRun, profile: AnalysisService) -> Dict:
    import requests

    service = profile.service
    if not service.base_url:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="webhook service without a base_url")
        return {"status": run.status}
    if run.transient_id is None:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="analysis runs need a transient")
        return {"status": run.status}
    try:
        payload = _payload_for(run, profile)
    except Exception as exc:  # noqa: BLE001
        log.exception("run %s: payload build failed", run.uuid)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="payload: %s: %s" % (type(exc).__name__, exc))
        return {"status": run.status}
    # A fresh token for this dispatch: the one start_run returned was never stored.
    token = runs.new_callback_token()
    run.callback_token_hash = runs.hash_token(token)
    run.save(update_fields=["callback_token_hash"])
    run.mark_running()
    headers = {"Content-Type": "application/json", "User-Agent": WEBHOOK_USER_AGENT, "X-Run-Token": token}
    bearer = _bearer_from_credential(service)
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    body = json.dumps(webhook_document(run, profile, payload, token), default=str)
    timeout = float(getattr(settings, "ANALYSIS_HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT))
    try:
        response = requests.post(service.base_url, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        error = "POST %s failed: %s" % (service.base_url, exc)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error[:2000])
        return {"status": run.status, "error": error}
    if response.status_code >= 400:
        error = "%s answered %d: %s" % (service.base_url, response.status_code, (response.text or "")[:500])
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
        return {"status": run.status, "http_status": response.status_code}
    try:
        reply = response.json()
    except ValueError:
        reply = {}
    if not isinstance(reply, dict):
        reply = {}
    external_id = str(reply.get("external_id") or reply.get("id") or reply.get("job_id") or "")
    if external_id:
        run.external_id = external_id[:255]
        run.save(update_fields=["external_id"])
    status = str(reply.get("status") or "").lower()
    if status in ExternalServiceRun.FINAL_STATUSES:
        # synchronous service: the answer is the result
        result = reply.get("result") if isinstance(reply.get("result"), (dict, list)) else None
        svc.store_callback_attachments(run, reply)
        runs.record_completion(run, status, result=result, error=str(reply.get("error") or ""),
                               artifact_url=str(reply.get("artifact_url") or ""))
    return {"status": run.status, "http_status": response.status_code, "external_id": external_id}


# --- the registered runner --------------------------------------------------------------

def run_analysis(run: ExternalServiceRun) -> Dict:
    profile = getattr(run.service, "analysis", None)
    if profile is None:
        log.info("run %s: analysis service %s has no AnalysisService profile; leaving it %s",
                 run.uuid, run.service.slug, run.status)
        return {"handled": False}
    if run.is_finished:
        return {"status": run.status, "skipped": "already finished"}
    if profile.is_webhook:
        return dispatch_webhook(run, profile)
    return execute_inprocess(run, profile)


runs.register_runner("kind:" + ExternalService.KIND_ANALYSIS, run_analysis)
