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
states is in :data:`LCO_STATES`. SkyPortal's ``facility_apis/lco.py``
(BSD-3-Clause) was consulted for the endpoints and the state names; no code
is copied.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict

import requests

from YSE_App.facilities.base import (
    FacilityAPI,
    FacilityError,
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

STRATEGIES = (("default", "Imaging (1m SINISTRO u,g,r,i)"), ("spectroscopy", "Spectroscopy (FLOYDS / SOAR)"))
OBSERVATION_TYPES = ("NORMAL", "RAPID_RESPONSE", "TIME_CRITICAL")

#: LCO request-group state -> FacilityRequest state
LCO_STATES = {
    "PENDING": "accepted",
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
    capabilities = frozenset({"submit", "delete", "status"})
    credential_keys = ["api_token", "username", "password"]
    manual_status = False

    def fields(self, allocation=None):
        now = datetime.datetime.utcnow()
        return [
            Field("strategy", "choice", label="Strategy", default="default", choices=STRATEGIES, required=True),
            Field("exposure_time", "number", label="Exposure time (s)", required=True, minimum=1),
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

    def estimate_hours(self, params, allocation=None):
        # Imaging: one exposure per filter plus ~90 s overhead each; spectroscopy: exposure + calibrations.
        try:
            exposure = float(params.get("exposure_time") or 0)
        except (TypeError, ValueError):
            return 0.0
        if params.get("strategy") == "spectroscopy":
            return round((exposure + 400.0) / 3600.0, 4)
        n = len(params.get("filters") or []) or 4
        return round(n * (exposure + 90.0) / 3600.0, 4)

    # -- payload ------------------------------------------------------------
    def build_payload(self, request) -> Dict[str, Any]:
        """The LCO request-group document, built with ``YSE_App.util.lcogt``."""
        from YSE_App.util.lcogt import lcogt

        params = dict(request.payload or {})
        allocation = request.allocation
        transient = request.transient
        proposal = params.get("proposal") or allocation.proposal_id
        telescope = allocation.telescope.name.lower()
        builder = lcogt(None, None, proposal, telescope, params["start"], params["end"])
        strategy = params.get("strategy") or "default"
        strat = dict(builder.params["strategy"][strategy])
        if strategy == "default" and params.get("filters"):
            strat["filters"] = list(params["filters"])
        builder.params["constraints"] = {
            "max_airmass": float(params.get("max_airmass") or 2.5),
            "min_lunar_distance": float(params.get("min_lunar_distance") or 15),
        }
        requests_block = builder.make_requests(transient.name, transient.ra, transient.dec,
                                               float(params["exposure_time"]), strat)
        if not requests_block[0]["configurations"]:
            raise FacilityError("the %s strategy produced no configurations for telescope %r"
                                % (strategy, allocation.telescope.name))
        return {
            "name": transient.name,
            "proposal": proposal,
            "ipp_value": float(params.get("ipp_value") or strat.get("ipp") or 1.0),
            "operator": "SINGLE",
            "observation_type": params.get("observation_type") or "NORMAL",
            "requests": requests_block,
        }

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
                raise FacilityError("could not reach the LCO token endpoint: %s" % exc) from exc
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
            raise FacilityError("could not reach the LCO portal: %s" % exc) from exc
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
            raise FacilityError("could not reach the LCO portal: %s" % exc) from exc
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
            raise FacilityError("could not reach the LCO portal: %s" % exc) from exc
        body = _json_or_text(response)
        if response.status_code not in (200, 201):
            raise FacilityError("LCO cancel answered %s: %s" % (response.status_code, str(body)[:500]))
        return StatusResult("cancelled", detail="cancelled at LCO", response=body)


def _json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return response.text
