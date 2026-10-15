"""Swift ToO adapter (#302): XRT / UVOT target-of-opportunity requests through the Swift TOO API.

The TOO API takes a JSON document ``{"api_name": "Swift_TOO", "api_version":
"1.2", "api_data": {...}}`` signed as an HS256 JWT with the user's shared
secret and posted as ``jwt=<token>`` to ``submit_json.php`` (this is what the
``swifttools`` package does; the adapter speaks the protocol directly so no new
dependency is needed). The answer carries ``status`` (``Accepted`` /
``Rejected``), ``too_id`` and the error / warning lists. ``debug: true`` sends
the request to the TOO API's dry-run mode, which validates without triggering
Swift; that is the default on an allocation whose ``default_request_params``
do not set ``debug`` to ``false``, so nobody triggers a real ToO by accident.

Credential payload: ``{"username": "...", "shared_secret": "..."}`` from the
Swift TOO web pages. Status has no public endpoint; the observer marks the
request complete once Swift observes (``manual_status``). XRT product requests
(light curves, spectra) are a separate API and not wired yet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict

from YSE_App.facilities.base import (
    FacilityAPI,
    FacilityError,
    FacilityValidationError,
    Field,
    SubmitResult,
    http_request,
    json_or_text,
    short,
)
from YSE_App.facilities.registry import register

SUBMIT_URL = "https://www.swift.psu.edu/toop/submit_json.php"
API_NAME = "Swift_TOO"
API_VERSION = "1.2"
URGENCIES = ((1, "1 - within 4 hours"), (2, "2 - within 24 hours"), (3, "3 - in the next few days"),
             (4, "4 - weeks"))
OBS_TYPES = ("Spectroscopy", "Light Curve", "Position", "Timing")
XRT_MODES = ((6, "6 - Windowed Timing"), (7, "7 - Photon Counting"), (0, "0 - Auto"))
UVOT_MODES = (("0x9999", "0x9999 - filter of the day"), ("0x30ed", "0x30ed - all six filters"),
              ("0x223f", "0x223f - u only"), ("0x011e", "0x011e - uvm2 only"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encode_jwt(payload: Dict[str, Any], secret: str) -> str:
    """HS256 JWT of ``payload`` (the three base64url segments), as PyJWT would produce."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":"), default=str).encode())
    signature = hmac.new(secret.encode(), ("%s.%s" % (header, body)).encode(), hashlib.sha256).digest()
    return "%s.%s.%s" % (header, body, _b64(signature))


@register
class SwiftFacility(FacilityAPI):
    slug = "swift"
    name = "Swift ToO (XRT / UVOT)"
    description = "Submits a Swift target-of-opportunity request through the TOO API (dry run unless debug is off)."
    capabilities = frozenset({"submit"})
    credential_keys = ["username", "shared_secret"]
    manual_status = True
    setup_notes = ("credential {\"username\": \"<TOO API user>\", \"shared_secret\": \"<secret>\"}; requests are dry runs "
                   "(debug=true) until the allocation's default_request_params set \"debug\": false.")

    def fields(self, allocation=None):
        return [
            Field("urgency", "choice", label="Urgency", default=3, choices=URGENCIES, required=True),
            Field("obs_type", "choice", label="Observation type", default="Light Curve", choices=[(t, t) for t in OBS_TYPES]),
            Field("source_type", "text", label="Source type", default="Supernova", required=True),
            Field("exposure", "number", label="Exposure per visit (s)", default=2000, minimum=100, maximum=40000, required=True),
            Field("num_of_visits", "integer", label="Number of visits", default=1, minimum=1, maximum=100),
            Field("monitoring_freq", "text", label="Monitoring cadence", default="1 day", help="Used when visits > 1"),
            Field("xrt_mode", "choice", label="XRT mode", default=7, choices=XRT_MODES),
            Field("uvot_mode", "choice", label="UVOT mode", default="0x9999", choices=UVOT_MODES),
            Field("poserr", "number", label="Position error (arcmin)", default=0.1, minimum=0, maximum=60),
            Field("immediate_objective", "text", label="Immediate objective", required=True,
                  default="Early X-ray and UV follow-up of a young supernova."),
            Field("science_just", "text", label="Science justification", required=True,
                  default="Constrain the shock-breakout / interaction signature with prompt XRT and UVOT coverage."),
            Field("exp_time_just", "text", label="Exposure-time justification", required=True,
                  default="2 ks per visit reaches the XRT limit expected for a nearby supernova."),
            Field("debug", "boolean", label="Dry run (TOO API debug mode)", default=True),
        ]

    def validate_extra(self, params, allocation=None):
        try:
            params["urgency"] = int(params.get("urgency") or 3)
            params["xrt_mode"] = int(params.get("xrt_mode") if params.get("xrt_mode") not in (None, "") else 7)
        except (TypeError, ValueError):
            raise FacilityValidationError({"urgency": "urgency and xrt_mode must be integers"})
        if allocation is not None and (allocation.default_request_params or {}).get("debug") is None:
            params["debug"] = True if params.get("debug") is None else params["debug"]
        return params

    def estimate_hours(self, params, allocation=None):
        try:
            return round(float(params.get("exposure") or 0) * int(params.get("num_of_visits") or 1) / 3600.0, 4)
        except (TypeError, ValueError):
            return 0.0

    def credentials(self, allocation) -> Dict[str, str]:
        secret = allocation.secret(touch=True)
        username, shared = secret.get("username"), secret.get("shared_secret") or secret.get("secret")
        if not (username and shared):
            raise FacilityError("the Swift credential needs username and shared_secret")
        return {"username": username, "shared_secret": shared}

    def build_payload(self, request) -> Dict[str, Any]:
        params = dict(request.payload or {})
        transient = request.transient
        creds = self.credentials(request.allocation)
        data = {
            "username": creds["username"],
            "source_name": transient.name,
            "source_type": params.get("source_type") or "Supernova",
            "ra": float(transient.ra), "dec": float(transient.dec), "poserr": float(params.get("poserr") or 0.1),
            "instrument": "XRT",
            "urgency": int(params.get("urgency") or 3),
            "obs_type": params.get("obs_type") or "Light Curve",
            "exposure": float(params.get("exposure") or 2000),
            "exp_time_per_visit": float(params.get("exposure") or 2000),
            "num_of_visits": int(params.get("num_of_visits") or 1),
            "monitoring_freq": params.get("monitoring_freq") or "1 day",
            "xrt_mode": int(params.get("xrt_mode") if params.get("xrt_mode") not in (None, "") else 7),
            "uvot_mode": str(params.get("uvot_mode") or "0x9999"),
            "immediate_objective": params.get("immediate_objective") or "",
            "science_just": params.get("science_just") or "",
            "exp_time_just": params.get("exp_time_just") or "",
            "debug": bool(params.get("debug", True)),
            "proposal": False,
            "l_name": "YSE-PZ",
        }
        if data["num_of_visits"] <= 1:
            data.pop("monitoring_freq")
        return {"api_name": API_NAME, "api_version": API_VERSION, "api_data": data}

    def submit(self, request) -> SubmitResult:
        creds = self.credentials(request.allocation)
        payload = self.build_payload(request)
        token = encode_jwt(payload, creds["shared_secret"])
        response = http_request("POST", SUBMIT_URL, data={"jwt": token}, what="the Swift TOO API")
        body = json_or_text(response)
        if response.status_code >= 300:
            raise FacilityError("Swift TOO API answered %s: %s" % (response.status_code, short(body)))
        status = {}
        if isinstance(body, dict):
            api_data = body.get("api_data") if isinstance(body.get("api_data"), dict) else body
            status = api_data.get("status") if isinstance(api_data.get("status"), dict) else api_data
        state_text = str(status.get("status") or "").lower()
        errors = status.get("errors") or []
        if errors or state_text.startswith("reject"):
            raise FacilityError("Swift rejected the ToO: %s" % "; ".join(str(e) for e in errors) or short(body))
        too_id = str(status.get("too_id") or "")
        dry_run = bool(payload["api_data"]["debug"])
        detail = "%s%s" % ("dry run accepted" if dry_run else "ToO %s accepted" % (too_id or "?"),
                           ("; warnings: " + "; ".join(str(w) for w in status.get("warnings") or [])) if status.get("warnings") else "")
        return SubmitResult("complete" if dry_run else "accepted", external_id=too_id, detail=detail, response=body,
                            hours=0.0 if dry_run else None)
