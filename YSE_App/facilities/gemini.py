"""Gemini adapter (#302): URL-based ToO triggers into an active Gemini program.

Gemini rapid ToO programs expose a template observation that is cloned and
activated by an HTTP POST to the observing database (``gnodb`` for Gemini
North, ``gsodb`` for Gemini South) with the program id, the user key, the
template observation number and the target. The answer is the new observation
id. There is no status or cancel endpoint: the observer follows the observation
in the OT and marks the request complete here (``manual_status``).

Credential payload: ``{"user_key": "...", "email": "..."}``; ``proposal_id`` on
the allocation is the program id (``GN-2026B-Q-123``); ``default_request_params``
should fix ``site`` and ``obsnum``.
"""

from __future__ import annotations

from typing import Any, Dict

from YSE_App.facilities.base import (
    FacilityAPI,
    FacilityError,
    FacilityValidationError,
    Field,
    SubmitResult,
    http_request,
    short,
)
from YSE_App.facilities.registry import register

ENDPOINTS = {"north": "https://gnodb.gemini.edu:8443/too", "south": "https://gsodb.gemini.edu:8443/too"}
BANDS = ("u", "g", "r", "i", "z", "J", "H", "K")


@register
class GeminiFacility(FacilityAPI):
    slug = "gemini"
    name = "Gemini North / South (URL ToO)"
    description = "Clones and activates a template observation of a Gemini ToO program through the URL trigger."
    capabilities = frozenset({"submit"})
    credential_keys = ["user_key", "email"]
    manual_status = True
    setup_notes = ("proposal_id = the Gemini program id; credential {\"user_key\": \"<program user key>\", \"email\": "
                   "\"<PI e-mail>\"}; default_request_params {\"site\": \"north\"|\"south\", \"obsnum\": <template obs>}.")

    def fields(self, allocation=None):
        return [
            Field("site", "choice", label="Site", default="north", choices=[("north", "Gemini North"), ("south", "Gemini South")],
                  required=True),
            Field("obsnum", "integer", label="Template observation number", required=True, minimum=1),
            Field("exptime", "number", label="Exposure time (s)", minimum=1, help="Blank keeps the template's value"),
            Field("mag", "number", label="Magnitude", minimum=0, maximum=30),
            Field("band", "choice", label="Magnitude band", default="r", choices=[(b, b) for b in BANDS]),
            Field("posangle", "number", label="Position angle (deg)", default=0, minimum=0, maximum=360),
            Field("group", "text", label="Group name", help="Optional scheduling group"),
            Field("note", "text", label="Note to the observer"),
            Field("ready", "boolean", label="Mark ready for observing", default=True),
        ]

    def validate_extra(self, params, allocation=None):
        if params.get("site") not in ENDPOINTS:
            raise FacilityValidationError({"site": "must be north or south"})
        if allocation is not None and not allocation.proposal_id:
            raise FacilityValidationError({"proposal": "set the Gemini program id on the allocation (proposal_id)"})
        return params

    def estimate_hours(self, params, allocation=None):
        try:
            return round(float(params.get("exptime") or 0) / 3600.0, 4)
        except (TypeError, ValueError):
            return 0.0

    def build_payload(self, request) -> Dict[str, Any]:
        params = dict(request.payload or {})
        transient = request.transient
        secret = request.allocation.secret(touch=True)
        key, email = secret.get("user_key") or secret.get("password"), secret.get("email")
        if not (key and email):
            raise FacilityError("the Gemini credential needs user_key and email")
        payload = {
            "prog": request.allocation.proposal_id, "password": key, "email": email,
            "obsnum": int(params["obsnum"]), "target": transient.name,
            "ra": float(transient.ra), "dec": float(transient.dec),
            "noteTitle": "YSE-PZ ToO trigger", "note": params.get("note") or "Triggered from YSE-PZ by %s" % request.submitted_by.username,
            "posangle": float(params.get("posangle") or 0), "ready": "true" if params.get("ready", True) else "false",
        }
        if params.get("mag") is not None:
            payload["mags"] = "%.2f/%s/AB" % (float(params["mag"]), params.get("band") or "r")
        if params.get("exptime"):
            payload["exptime"] = float(params["exptime"])
        if params.get("group"):
            payload["group"] = params["group"]
        return payload

    def submit(self, request) -> SubmitResult:
        params = request.payload or {}
        url = ENDPOINTS[params.get("site") or "north"]
        payload = self.build_payload(request)
        # the Gemini ODB serves a self-signed certificate
        response = http_request("POST", url, data=payload, verify=False, what="the Gemini ODB")
        if response.status_code >= 300:
            raise FacilityError("Gemini answered %s: %s" % (response.status_code, short(response.text)))
        obs_id = (response.text or "").strip().splitlines()[-1].strip() if (response.text or "").strip() else ""
        return SubmitResult("accepted", external_id=obs_id, detail="observation %s created in %s" % (obs_id or "?", payload["prog"]),
                            response={"text": (response.text or "")[:500]})
