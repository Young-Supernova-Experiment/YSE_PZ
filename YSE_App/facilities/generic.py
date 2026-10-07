"""GENERIC facility: reach a facility by HTTP POST, email or Slack webhook (#302).

Configuration lives on the allocation:

* ``default_request_params["notification_type"]``: ``api`` (default when an
  endpoint is set), ``email`` or ``slack``;
* ``allocation.endpoint_url`` or the credential's ``endpoint``: where the JSON
  payload is POSTed; the credential's ``api_token`` (optional) is sent as
  ``Authorization: token ...``;
* ``default_request_params["recipients"]`` (list or comma-separated) for email;
* the credential's ``slack_webhook_url`` for Slack;
* ``default_request_params["payload_template"]``: optional JSON object whose
  string values are filled from the request context (``{transient_name}``,
  ``{ra}``, ``{dec}``, ``{requester}``, ``{param_<name>}``, ...); without it the
  standard payload (transient, allocation, parameters, requester) is sent.

Status is manual: a person marks the request complete (or cancelled) once the
facility reports back. A submission that was sent is ``submitted``. When
``default_request_params["status_url"]`` (or the credential's ``status_url``)
is set, the adapter polls it instead: ``GET <status_url>`` with ``{external_id}``
/ ``{request_id}`` filled in (or appended as query parameters) must answer JSON
holding ``state`` / ``status``; the value is mapped through ``status_map`` in
the parameters (default: pending/accepted/queued -> accepted, running ->
running, complete(d)/done/success -> complete, failed/error -> failed,
cancel(l)ed -> cancelled). Modify and cancel re-send the payload with
``"action": "update"`` / ``"action": "cancel"`` (API mode: to the endpoint, or to
``cancel_url`` when set; email / Slack: a message whose subject says so), so a
facility without an API still learns about the change.

Instrument logs (#310): when ``default_request_params["instrument_log_url"]`` (or
the credential's ``instrument_log_endpoint``) is set, ``fetch_instrument_log``
GETs it with ``start`` / ``end`` (ISO, UTC) and ``instrument`` / ``telescope``
query parameters (or filled into ``{start}`` / ``{end}`` / ``{instrument}`` /
``{telescope}`` placeholders in the URL) and expects JSON: a list of entries, or
an object holding one under ``logs`` / ``data`` / ``results``. An entry is a
dict with ``message`` (or ``msg`` / ``text``), optional ``timestamp`` (or
``time`` / ``date`` / ``created_at``) and ``level``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import requests
from django.core.mail import send_mail

from YSE_App.facilities.base import (
    FacilityAPI,
    FacilityError,
    FacilityUnreachable,
    FacilityValidationError,
    Field,
    StatusResult,
    SubmitResult,
    http_timeout,
)
from YSE_App.facilities.registry import register

logger = logging.getLogger(__name__)

MODE_API = "api"
MODE_EMAIL = "email"
MODE_SLACK = "slack"
MODES = (MODE_API, MODE_EMAIL, MODE_SLACK)

#: lower-cased facility state -> FacilityRequest state (status_url polling)
DEFAULT_STATUS_MAP = {
    "pending": "accepted", "accepted": "accepted", "queued": "accepted", "scheduled": "accepted",
    "submitted": "submitted", "running": "running", "in_progress": "running", "observing": "running",
    "complete": "complete", "completed": "complete", "done": "complete", "success": "complete",
    "succeeded": "complete", "failed": "failed", "error": "failed", "rejected": "failed", "expired": "failed",
    "cancelled": "cancelled", "canceled": "cancelled", "aborted": "cancelled",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{%s}" % key


def render_template(template: Any, context: Dict[str, Any]) -> Any:
    """Fill ``{name}`` placeholders in every string of a JSON-like structure."""
    if isinstance(template, str):
        return template.format_map(_SafeDict(context))
    if isinstance(template, dict):
        return {k: render_template(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render_template(v, context) for v in template]
    return template


def recipients_from(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


@register
class GenericFacility(FacilityAPI):
    slug = "generic"
    name = "Generic (API / email / Slack)"
    description = (
        "Sends the request as JSON to an HTTP endpoint, as an email to a list of observers, "
        "or as a Slack webhook message. Status is updated by hand."
    )
    capabilities = frozenset({"submit", "update", "delete", "status", "instrument_log"})
    credential_keys = ["api_token", "endpoint", "slack_webhook_url", "instrument_log_endpoint", "status_url",
                       "cancel_url"]
    manual_status = True
    setup_notes = ("No code needed: set notification_type (api/email/slack) and, per mode, endpoint_url (or the "
                   "credential's endpoint + api_token), recipients, or slack_webhook_url; optional status_url "
                   "makes the request pollable.")

    def supports(self, capability, allocation=None):
        if capability == "status":
            return allocation is not None and bool(self.status_url(allocation))
        return self.can(capability)

    def status_url(self, allocation) -> str:
        params = allocation.default_request_params or {}
        return str(params.get("status_url") or allocation.secret().get("status_url") or "")

    def fields(self, allocation=None):
        return [
            Field("exposure_time", "number", label="Exposure time (s)", default=300, minimum=0),
            Field("exposure_count", "integer", label="Exposures", default=1, minimum=1),
            Field("filters", "list", label="Filters", default=[], help="Comma-separated, e.g. g,r,i"),
            Field("priority", "integer", label="Priority (1 highest)", default=3, minimum=1, maximum=5),
            Field("start", "datetime", label="Window start (UTC)"),
            Field("end", "datetime", label="Window end (UTC)"),
            Field("comment", "text", label="Comment"),
        ]

    # -- configuration -------------------------------------------------------
    def mode(self, allocation) -> str:
        params = allocation.default_request_params or {}
        mode = str(params.get("notification_type") or "").strip().lower()
        if mode:
            return mode
        return MODE_API if (allocation.endpoint_url or allocation.secret().get("endpoint")) else MODE_EMAIL

    def validate_extra(self, params, allocation=None):
        if allocation is None:
            return params
        mode = self.mode(allocation)
        if mode not in MODES:
            raise FacilityValidationError({"notification_type": "must be one of %s" % ", ".join(MODES)})
        if mode == MODE_EMAIL and not recipients_from((allocation.default_request_params or {}).get("recipients")):
            raise FacilityValidationError({"recipients": "the allocation has no email recipients configured"})
        return params

    def build_payload(self, request) -> Any:
        context = self.transient_context(request)
        params = dict(request.payload or {})
        context.update({"param_%s" % k: v for k, v in params.items()})
        template = (request.allocation.default_request_params or {}).get("payload_template")
        if template:
            return render_template(template, context)
        return {
            "transient": {"name": context["transient_name"], "ra": context["ra"], "dec": context["dec"]},
            "allocation": {"id": context["allocation_id"], "name": context["allocation_name"],
                           "proposal_id": context["proposal_id"], "telescope": context["telescope"],
                           "instrument": context["instrument"]},
            "parameters": {k: v for k, v in params.items()
                           if k not in ("notification_type", "recipients", "payload_template")},
            "requester": context["requester"],
            "request_id": context["request_id"],
        }

    # -- transport -----------------------------------------------------------
    def submit(self, request) -> SubmitResult:
        return self._send(request, "submit")

    def update(self, request) -> SubmitResult:
        """Re-send the (changed) payload flagged as an update; the external id is kept."""
        result = self._send(request, "update")
        result.external_id = result.external_id or request.external_id
        result.state = request.state if request.is_open else "submitted"
        result.detail = "update sent: " + result.detail
        return result

    def delete(self, request) -> StatusResult:
        """Tell the facility the request is withdrawn (best effort), then report ``cancelled``."""
        try:
            result = self._send(request, "cancel")
        except FacilityError as exc:
            return StatusResult("cancelled", detail="cancelled locally; the facility was not told (%s)" % exc)
        return StatusResult("cancelled", detail="cancellation sent: " + result.detail, response=result.response)

    def get_status(self, request) -> StatusResult:
        allocation = request.allocation
        template = self.status_url(allocation)
        if not template:
            raise FacilityError("no status_url configured for allocation %s" % allocation.name)
        context = {"external_id": request.external_id, "request_id": request.pk}
        url = render_template(template, context)
        query = None if url != template else {"external_id": request.external_id, "request_id": request.pk}
        secret = allocation.secret(touch=True)
        headers = {"Accept": "application/json"}
        if secret.get("api_token"):
            headers["Authorization"] = "token %s" % secret["api_token"]
        try:
            response = requests.get(url, params=query, headers=headers, timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach %s: %s" % (url, exc)) from exc
        body = _json_or_text(response)
        if response.status_code >= 300:
            raise FacilityError("status endpoint answered %s: %s" % (response.status_code, _short(body)))
        raw = ""
        if isinstance(body, dict):
            inner = body.get("data") if isinstance(body.get("data"), dict) else body
            raw = str(inner.get("state") or inner.get("status") or "")
        elif isinstance(body, str):
            raw = body.strip()
        mapping = dict(DEFAULT_STATUS_MAP)
        custom = (allocation.default_request_params or {}).get("status_map")
        if isinstance(custom, dict):
            mapping.update({str(k).lower(): str(v) for k, v in custom.items()})
        state = mapping.get(raw.lower(), request.state)
        return StatusResult(state, detail="facility state %s" % (raw or "?"), response=body)

    def _send(self, request, action: str) -> SubmitResult:
        allocation = request.allocation
        mode = self.mode(allocation)
        payload = self.build_payload(request)
        if isinstance(payload, dict) and action != "submit":
            payload = dict(payload, action=action, external_id=request.external_id)
        if mode == MODE_API:
            return self._submit_api(request, payload, action)
        if mode == MODE_EMAIL:
            return self._submit_email(request, payload, action)
        if mode == MODE_SLACK:
            return self._submit_slack(request, payload, action)
        raise FacilityError("unknown notification_type %r" % mode)

    def _submit_api(self, request, payload, action="submit") -> SubmitResult:
        allocation = request.allocation
        secret = allocation.secret(touch=True)
        endpoint = allocation.endpoint_url or secret.get("endpoint")
        if action == "cancel":
            endpoint = ((allocation.default_request_params or {}).get("cancel_url") or secret.get("cancel_url")
                        or endpoint)
            endpoint = render_template(str(endpoint or ""), {"external_id": request.external_id,
                                                             "request_id": request.pk})
        if not endpoint:
            raise FacilityError("no endpoint configured for this allocation (endpoint_url or credential 'endpoint')")
        headers = {"Content-Type": "application/json"}
        if secret.get("api_token"):
            headers["Authorization"] = "token %s" % secret["api_token"]
        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach %s: %s" % (endpoint, exc)) from exc
        body = _json_or_text(response)
        if response.status_code >= 300:
            raise FacilityError("endpoint answered %s: %s" % (response.status_code, _short(body)))
        external_id = ""
        if isinstance(body, dict):
            external_id = str(body.get("id") or body.get("request_id") or (body.get("data") or {}).get("id") or "")
        return SubmitResult("submitted", external_id=external_id, detail="POST %s -> %s" % (endpoint, response.status_code),
                            response=body)

    def _submit_email(self, request, payload, action="submit") -> SubmitResult:
        allocation = request.allocation
        recipients = recipients_from((allocation.default_request_params or {}).get("recipients"))
        if not recipients:
            raise FacilityError("no email recipients configured for this allocation")
        transient = request.transient
        prefix = {"update": "UPDATED observation request", "cancel": "CANCELLED observation request"}.get(
            action, "Observation request")
        subject = "[YSE-PZ] %s: %s on %s" % (prefix, transient.name, allocation.telescope.name)
        body = self.email_body(request, payload)
        send_mail(subject, body, None, recipients, fail_silently=False)
        return SubmitResult("submitted", detail="email sent to %s" % ", ".join(recipients),
                            response={"subject": subject, "recipients": recipients, "body": body})

    def email_body(self, request, payload) -> str:
        transient = request.transient
        lines = [
            "Observation request from YSE-PZ",
            "",
            "Transient: %s  (RA %.6f, Dec %.6f)" % (transient.name, transient.ra, transient.dec),
            "Allocation: %s  (%s%s)" % (request.allocation.name, request.allocation.telescope.name,
                                        ", proposal %s" % request.allocation.proposal_id
                                        if request.allocation.proposal_id else ""),
            "Requested by: %s" % request.submitted_by.username,
            "",
            "Parameters:",
            json.dumps(payload, indent=2, sort_keys=True, default=str),
        ]
        return "\n".join(lines)

    def _submit_slack(self, request, payload, action="submit") -> SubmitResult:
        allocation = request.allocation
        secret = allocation.secret(touch=True)
        webhook = secret.get("slack_webhook_url") or (allocation.default_request_params or {}).get("slack_webhook_url")
        if not webhook:
            raise FacilityError("no slack_webhook_url in the allocation's credential")
        transient = request.transient
        title = {"update": "*UPDATED observation request*", "cancel": "*CANCELLED observation request*"}.get(
            action, "*Observation request*")
        text = "%s for *%s* (RA %.5f, Dec %.5f) on %s by %s\n```%s```" % (
            title, transient.name, transient.ra, transient.dec, allocation.telescope.name,
            request.submitted_by.username, json.dumps(payload, indent=1, sort_keys=True, default=str)[:2500])
        try:
            response = requests.post(webhook, json={"text": text}, timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach the Slack webhook: %s" % exc) from exc
        if response.status_code >= 300:
            raise FacilityError("Slack webhook answered %s: %s" % (response.status_code, response.text[:300]))
        return SubmitResult("submitted", detail="Slack webhook posted", response={"text": text})

    # -- instrument logs (#310) ----------------------------------------------
    def instrument_log_url(self, allocation) -> str:
        params = allocation.default_request_params or {}
        return str(params.get("instrument_log_url") or allocation.secret().get("instrument_log_endpoint") or "")

    def fetch_instrument_log(self, allocation, instrument, start, end) -> List[Dict[str, Any]]:
        template = self.instrument_log_url(allocation)
        if not template:
            raise FacilityError("no instrument_log_url configured for allocation %s" % allocation.name)
        context = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "instrument": instrument.name, "telescope": instrument.telescope.name,
            "instrument_id": instrument.pk, "proposal_id": allocation.proposal_id,
        }
        url = render_template(template, context)
        query = {} if url != template else {"start": context["start"], "end": context["end"],
                                              "instrument": context["instrument"], "telescope": context["telescope"]}
        headers = {"Accept": "application/json"}
        secret = allocation.secret(touch=True)
        if secret.get("api_token"):
            headers["Authorization"] = "token %s" % secret["api_token"]
        try:
            response = requests.get(url, params=query or None, headers=headers, timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach %s: %s" % (url, exc)) from exc
        body = _json_or_text(response)
        if response.status_code >= 300:
            raise FacilityError("instrument log endpoint answered %s: %s" % (response.status_code, _short(body)))
        return instrument_log_entries(body)


def instrument_log_entries(body) -> List[Dict[str, Any]]:
    """Pull the list of entries out of whatever JSON shape the endpoint answered with."""
    if isinstance(body, dict):
        for key in ("logs", "data", "results", "entries", "log"):
            inner = body.get(key)
            if isinstance(inner, dict):
                inner = inner.get("logs") or inner.get("entries") or inner.get("results")
            if isinstance(inner, list):
                body = inner
                break
        else:
            body = [body] if body.get("message") or body.get("msg") or body.get("text") else []
    if not isinstance(body, list):
        raise FacilityError("instrument log endpoint did not answer with a list of entries")
    entries = []
    for item in body:
        if isinstance(item, str):
            entries.append({"message": item})
        elif isinstance(item, dict):
            entries.append(item)
    return entries


def _json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def _short(value, n=300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:n]
