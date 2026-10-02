"""Las Cumbres Observatory adapter (#301): request groups through the LCO observation portal API.

The request-group payload is built by the request-building code YSE-PZ already
ships in :mod:`YSE_App.util.lcogt` (``make_requests`` and friends), so a request
submitted here is the same shape ``AddAutomatedSpectrumRequestFormView`` sends
today; the difference is that credentials come from the allocation's encrypted
credential (#264) instead of ``settings.LCOGTUSER`` / ``LCOGTPASS``, and the
request is recorded, polled and cancellable.

Credential payload: ``{"api_token": "..."}`` (preferred) or
``{"username": "...", "password": "..."}``; ``proposal_id`` on the allocation
(or ``proposal`` in the parameters) names the LCO proposal.

Status and cancel follow the LCO portal API (``GET /api/requestgroups/<id>/``,
``POST /api/requestgroups/<id>/cancel/``); the mapping of LCO states to our
states is in :data:`LCO_STATES`. The portal has no edit endpoint, so ``update``
cancels the pending request group and submits the changed one (the request
then carries the new group id). Per-instrument request builders (Sinistro,
Spectral, MuSCAT, QHY, FLOYDS) are in :mod:`YSE_App.facilities.lco_requests`;
the ``strategy`` field is kept for the older imaging / spectroscopy shortcut. SkyPortal's ``facility_apis/lco.py``
(BSD-3-Clause) was consulted for the endpoints and the state names; no code
is copied.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict

import requests

from YSE_App.facilities import lco_requests
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

PORTAL = "https://observe.lco.global/api/"
REQUESTGROUPS = PORTAL + "requestgroups/"
TOKEN_AUTH = PORTAL + "api-token-auth/"
PORTAL_UI = "https://observe.lco.global/requestgroups/"

STRATEGIES = (("default", "Imaging (1m SINISTRO u,g,r,i)"), ("spectroscopy", "Spectroscopy (FLOYDS / SOAR)"),
              ("instrument", "Use the instrument chosen below"))
LCO_INSTRUMENTS = ("1M0-SCICAM-SINISTRO", "2M0-SCICAM-SPECTRAL", "2M0-SCICAM-MUSCAT", "0M4-SCICAM-QHY600",
                   "2M0-FLOYDS-SCICAM")
OBSERVATION_TYPES = ("NORMAL", "RAPID_RESPONSE", "TIME_CRITICAL")

#: LCO request-group state -> FacilityRequest state
LCO_STATES = {
    "PENDING": "accepted",
    "SCHEDULED": "accepted",
    "COMPLETED": "complete",
    "CANCELED": "cancelled",
    "WINDOW_EXPIRED": "failed",
    "FAILURE_LIMIT_REACHED": "failed",
}


def _iso(value: datetime.datetime) -> str:
    return value.replace(tzinfo=None, microsecond=0).isoformat()


@register
class LCOFacility(FacilityAPI):
    slug = "lco"
    name = "Las Cumbres Observatory"
    description = "Submits request groups to the LCO observation portal; polls and cancels them."
    capabilities = frozenset({"submit", "update", "delete", "status"})
    credential_keys = ["api_token", "username", "password"]
    manual_status = False
    setup_notes = ("proposal_id = the LCO proposal code; credential {\"api_token\": \"<portal token>\"} "
                   "(or username/password). Pick the instrument per request or fix it in default_request_params.")
    instruments = LCO_INSTRUMENTS
    default_instrument = lco_requests.DEFAULT_INSTRUMENT

    def fields(self, allocation=None):
        now = datetime.datetime.utcnow()
        return [
            Field("strategy", "choice", label="Strategy", default="default", choices=STRATEGIES, required=True),
            Field("instrument", "choice", label="Instrument", default=self.default_instrument,
                  choices=lco_requests.instrument_choices(self.instruments),
                  help="Used with strategy 'instrument'"),
            Field("exposure_time", "number", label="Exposure time (s)", required=True, minimum=1),
            Field("exposure_count", "integer", label="Exposures per filter", default=1, minimum=1, maximum=50),
            Field("filters", "list", label="Filters (imaging)", default=["up", "gp", "rp", "ip"]),
            Field("start", "datetime", label="Window start (UTC)", default=_iso(now), required=True),
            Field("end", "datetime", label="Window end (UTC)", default=_iso(now + datetime.timedelta(days=3)),
                  required=True),
            Field("max_airmass", "number", label="Max airmass", default=2.5, minimum=1.0, maximum=5.0),
            Field("min_lunar_distance", "number", label="Min lunar distance (deg)", default=15, minimum=0, maximum=180),
            Field("ipp_value", "number", label="IPP", default=1.0, minimum=0.5, maximum=2.0),
            Field("observation_type", "choice", label="Observation type", default="NORMAL",
                  choices=[(t, t) for t in OBSERVATION_TYPES]),
            Field("proposal", "text", label="Proposal", help="Defaults to the allocation's proposal id"),
        ]

    def validate_extra(self, params, allocation=None):
        errors = {}
        if allocation is not None and not (params.get("proposal") or allocation.proposal_id):
            errors["proposal"] = "set a proposal id on the allocation or in the request"
        if params.get("start") and params.get("end") and params["end"] <= params["start"]:
            errors["end"] = "must be after the window start"
        if params.get("strategy") == "spectroscopy" and allocation is not None:
            telescope = allocation.telescope.name.lower()
            if "faulkes" not in telescope and "soar" not in telescope:
                errors["strategy"] = "spectroscopy needs a Faulkes (FLOYDS) or SOAR telescope on the allocation"
        if errors:
            raise FacilityValidationError(errors)
        if allocation is not None and not params.get("proposal"):
            params["proposal"] = allocation.proposal_id
        return params

    def instrument_for(self, params) -> str:
        strategy = params.get("strategy") or "default"
        if strategy == "spectroscopy":
            return "2M0-FLOYDS-SCICAM"
        if strategy == "default":
            return "1M0-SCICAM-SINISTRO"
        return params.get("instrument") or self.default_instrument

    def estimate_hours(self, params, allocation=None):
        # Imaging: one exposure per filter plus ~90 s overhead each; spectroscopy: exposure + calibrations.
        return lco_requests.estimate_hours(self.instrument_for(params), params)

    # -- payload ------------------------------------------------------------
    def build_payload(self, request) -> Dict[str, Any]:
        """The LCO request-group document (``YSE_App.util.lcogt`` for the legacy strategies)."""
        params = dict(request.payload or {})
        allocation = request.allocation
        transient = request.transient
        proposal = params.get("proposal") or allocation.proposal_id
        strategy = params.get("strategy") or "default"
        if strategy in ("default", "spectroscopy"):
            requests_block = self._legacy_requests(strategy, params, allocation, transient, proposal)
        else:
            instrument = self.instrument_for(params)
            requests_block = [lco_requests.build_request(instrument, transient.name, transient.ra, transient.dec,
                                                         params, allocation.telescope.name, proposal)]
        return {
            "name": transient.name,
            "proposal": proposal,
            "ipp_value": float(params.get("ipp_value") or 1.0),
            "operator": "SINGLE",
            "observation_type": params.get("observation_type") or "NORMAL",
            "requests": requests_block,
        }

    def _legacy_requests(self, strategy, params, allocation, transient, proposal):
        from YSE_App.util.lcogt import lcogt

        telescope = allocation.telescope.name.lower()
        builder = lcogt(None, None, proposal, telescope, params["start"], params["end"])
        strat = dict(builder.params["strategy"][strategy])
        if strategy == "default" and params.get("filters"):
            strat["filters"] = list(params["filters"])
        builder.params["constraints"] = lco_requests.constraints(params)
        requests_block = builder.make_requests(transient.name, transient.ra, transient.dec,
                                               float(params["exposure_time"]), strat)
        if not requests_block[0]["configurations"]:
            raise FacilityError("the %s strategy produced no configurations for telescope %r"
                                % (strategy, allocation.telescope.name))
        count = int(params.get("exposure_count") or 1)
        if count > 1:
            for configuration in requests_block[0]["configurations"]:
                for ic in configuration.get("instrument_configs", []):
                    ic["exposure_count"] = count
        return requests_block

    # -- auth ---------------------------------------------------------------
    def auth_headers(self, allocation) -> Dict[str, str]:
        secret = allocation.secret(touch=True)
        token = secret.get("api_token") or secret.get("token")
        if not token:
            username, password = secret.get("username"), secret.get("password")
            if not (username and password):
                raise FacilityError("the LCO credential needs api_token, or username and password")
            try:
                response = requests.post(TOKEN_AUTH, data={"username": username, "password": password},
                                         timeout=http_timeout())
            except requests.RequestException as exc:
                raise FacilityUnreachable("could not reach the LCO token endpoint: %s" % exc) from exc
            token = (response.json() if response.ok else {}).get("token")
            if not token:
                raise FacilityError("LCO rejected the username/password (status %s)" % response.status_code)
        return {"Authorization": "Token %s" % token}

    # -- transport ----------------------------------------------------------
    def submit(self, request) -> SubmitResult:
        payload = self.build_payload(request)
        headers = self.auth_headers(request.allocation)
        try:
            response = requests.post(REQUESTGROUPS, json=payload, headers=headers, timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach the LCO portal: %s" % exc) from exc
        body = _json_or_text(response)
        if response.status_code not in (200, 201):
            raise FacilityError("LCO answered %s: %s" % (response.status_code, str(body)[:500]))
        group_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        state = LCO_STATES.get((body.get("state") if isinstance(body, dict) else "") or "PENDING", "submitted")
        return SubmitResult(state, external_id=group_id, external_url=PORTAL_UI + group_id if group_id else "",
                            detail="request group %s %s" % (group_id, body.get("state") if isinstance(body, dict) else ""),
                            response=body, hours=self.estimate_hours(request.payload or {}, request.allocation))

    def get_status(self, request) -> StatusResult:
        if not request.external_id:
            raise FacilityError("request has no LCO request-group id")
        headers = self.auth_headers(request.allocation)
        try:
            response = requests.get(REQUESTGROUPS + request.external_id + "/", headers=headers, timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach the LCO portal: %s" % exc) from exc
        body = _json_or_text(response)
        if response.status_code != 200:
            raise FacilityError("LCO answered %s: %s" % (response.status_code, str(body)[:500]))
        lco_state = body.get("state", "") if isinstance(body, dict) else ""
        return StatusResult(LCO_STATES.get(lco_state, request.state), detail="LCO state %s" % lco_state, response=body)

    def delete(self, request) -> StatusResult:
        if not request.external_id:
            raise FacilityError("request has no LCO request-group id")
        headers = self.auth_headers(request.allocation)
        try:
            response = requests.post(REQUESTGROUPS + request.external_id + "/cancel/", headers=headers,
                                     timeout=http_timeout())
        except requests.RequestException as exc:
            raise FacilityUnreachable("could not reach the LCO portal: %s" % exc) from exc
        body = _json_or_text(response)
        if response.status_code not in (200, 201):
            raise FacilityError("LCO cancel answered %s: %s" % (response.status_code, str(body)[:500]))
        return StatusResult("cancelled", detail="cancelled at LCO", response=body)

    def update(self, request) -> SubmitResult:
        """Cancel the pending request group and submit the changed one (LCO has no edit endpoint)."""
        old_id = request.external_id
        if old_id:
            try:
                self.delete(request)
            except FacilityError as exc:
                if "404" not in str(exc) and "not found" not in str(exc).lower():
                    raise
        result = self.submit(request)
        result.detail = "replaced request group %s: %s" % (old_id or "?", result.detail)
        return result


def _json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return response.text
