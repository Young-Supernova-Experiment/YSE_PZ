"""MMT adapter (#302): Binospec / MMIRS catalog targets through the MMT scheduler API.

The MMT queue accepts targets by ``POST /APIv2/catalogTarget/`` (token in the
form data); the answer is the target record with its ``id``. ``PUT
/APIv2/catalogTarget/<id>/`` updates a target and ``DELETE`` removes it, which
is how ``update`` / ``delete`` are implemented. The scheduler has no per-target
observing status in the API, so completion is recorded by hand.

Credential payload: ``{"api_token": "..."}``; ``proposal_id`` on the allocation
is the MMT program id. ``default_request_params`` fixes the instrument
(``binospec`` or ``mmirs``) and the observing mode.
"""

from __future__ import annotations

from typing import Any, Dict

from YSE_App.facilities.base import (
    FacilityAPI,
    FacilityError,
    FacilityValidationError,
    Field,
    StatusResult,
    SubmitResult,
    http_request,
    json_or_text,
    short,
)
from YSE_App.facilities.registry import register

BASE = "https://scheduler.mmto.arizona.edu/APIv2/catalogTarget/"
INSTRUMENT_IDS = {"binospec": 16, "mmirs": 15}
OBS_TYPES = ("imaging", "longslit", "mask")
BINOSPEC_FILTERS = ("g", "r", "i", "z")
BINOSPEC_GRATINGS = ("270", "600", "1000")
MMIRS_FILTERS = ("J", "H", "K", "Ks", "zJ", "HK", "HK3")
SLIT_WIDTHS = ("Longslit0_75", "Longslit1", "Longslit1_25", "Longslit1_5", "Longslit5")


def sexagesimal(ra: float, dec: float):
    ra_h = float(ra) / 15.0
    h = int(ra_h)
    m = int((ra_h - h) * 60)
    s = ((ra_h - h) * 60 - m) * 60
    sign = "-" if dec < 0 else "+"
    d = abs(float(dec))
    dd = int(d)
    dm = int((d - dd) * 60)
    ds = ((d - dd) * 60 - dm) * 60
    return "%02d:%02d:%06.3f" % (h, m, s), "%s%02d:%02d:%05.2f" % (sign, dd, dm, ds)


@register
class MMTFacility(FacilityAPI):
    slug = "mmt"
    name = "MMT (Binospec / MMIRS)"
    description = "Adds, updates and removes catalog targets in the MMT queue scheduler."
    capabilities = frozenset({"submit", "update", "delete"})
    credential_keys = ["api_token"]
    manual_status = True
    setup_notes = ("proposal_id = the MMT program id; credential {\"api_token\": \"<scheduler API token>\"}; "
                   "default_request_params {\"instrument\": \"binospec\"|\"mmirs\"}.")

    def fields(self, allocation=None):
        return [
            Field("instrument", "choice", label="Instrument", default="binospec",
                  choices=[("binospec", "Binospec"), ("mmirs", "MMIRS")], required=True),
            Field("observationtype", "choice", label="Observation type", default="longslit",
                  choices=[(t, t) for t in OBS_TYPES], required=True),
            Field("exposure_time", "number", label="Exposure time (s)", required=True, minimum=1),
            Field("exposure_count", "integer", label="Exposures", default=1, minimum=1, maximum=50),
            Field("visits", "integer", label="Visits", default=1, minimum=1, maximum=20),
            Field("filter", "choice", label="Filter", default="r",
                  choices=[(f, f) for f in BINOSPEC_FILTERS + MMIRS_FILTERS]),
            Field("grating", "choice", label="Grating (Binospec longslit)", default="270",
                  choices=[(g, g) for g in BINOSPEC_GRATINGS]),
            Field("centralwavelength", "number", label="Central wavelength (A)", default=6500, minimum=3800, maximum=9000),
            Field("slitwidth", "choice", label="Slit", default="Longslit1", choices=[(s, s) for s in SLIT_WIDTHS]),
            Field("magnitude", "number", label="Magnitude", minimum=0, maximum=30),
            Field("priority", "integer", label="Priority (1 highest)", default=2, minimum=1, maximum=3),
            Field("pa", "number", label="Position angle (deg)", default=0, minimum=-360, maximum=360),
            Field("photometric", "boolean", label="Needs photometric conditions", default=False),
            Field("targetofopportunity", "boolean", label="Target of opportunity", default=True),
            Field("notes", "text", label="Notes"),
        ]

    def validate_extra(self, params, allocation=None):
        if params.get("instrument") not in INSTRUMENT_IDS:
            raise FacilityValidationError({"instrument": "must be binospec or mmirs"})
        if allocation is not None and not allocation.proposal_id:
            raise FacilityValidationError({"proposal": "set the MMT program id on the allocation (proposal_id)"})
        if params.get("instrument") == "mmirs" and params.get("filter") in BINOSPEC_FILTERS:
            params["filter"] = "J"
        return params

    def estimate_hours(self, params, allocation=None):
        try:
            return round(float(params["exposure_time"]) * int(params.get("exposure_count") or 1)
                         * int(params.get("visits") or 1) / 3600.0 + 0.1, 4)
        except (TypeError, ValueError, KeyError):
            return 0.0

    def token(self, allocation) -> str:
        token = allocation.secret(touch=True).get("api_token") or allocation.secret().get("token")
        if not token:
            raise FacilityError("the MMT credential needs api_token")
        return token

    def build_payload(self, request) -> Dict[str, Any]:
        params = dict(request.payload or {})
        transient = request.transient
        ra, dec = sexagesimal(transient.ra, transient.dec)
        payload = {
            "token": self.token(request.allocation), "program_id": request.allocation.proposal_id,
            "objectid": transient.name, "ra": ra, "dec": dec, "epoch": 2000.0, "pm_ra": 0.0, "pm_dec": 0.0,
            "instrumentid": INSTRUMENT_IDS[params.get("instrument") or "binospec"],
            "observationtype": params.get("observationtype") or "longslit",
            "exposuretime": float(params["exposure_time"]), "numberexposures": int(params.get("exposure_count") or 1),
            "visits": int(params.get("visits") or 1), "priority": int(params.get("priority") or 2),
            "pa": float(params.get("pa") or 0), "photometric": 1 if params.get("photometric") else 0,
            "targetofopportunity": 1 if params.get("targetofopportunity", True) else 0,
            "notes": params.get("notes") or "Requested from YSE-PZ by %s" % request.submitted_by.username,
            "filter": params.get("filter") or "r",
        }
        if params.get("magnitude") is not None:
            payload["magnitude"] = float(params["magnitude"])
        if payload["observationtype"] == "longslit":
            payload.update({"grating": str(params.get("grating") or "270"),
                            "centralwavelength": float(params.get("centralwavelength") or 6500),
                            "slitwidth": params.get("slitwidth") or "Longslit1"})
        return payload

    def _parse(self, response, verb: str):
        body = json_or_text(response)
        if response.status_code not in (200, 201, 204):
            raise FacilityError("MMT %s answered %s: %s" % (verb, response.status_code, short(body)))
        return body

    def submit(self, request) -> SubmitResult:
        payload = self.build_payload(request)
        body = self._parse(http_request("POST", BASE, data=payload, what="the MMT scheduler"), "submit")
        target_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if not target_id:
            raise FacilityError("MMT did not return a target id: %s" % short(body))
        return SubmitResult("accepted", external_id=target_id, detail="catalog target %s created" % target_id, response=body)

    def update(self, request) -> SubmitResult:
        if not request.external_id:
            return self.submit(request)
        payload = self.build_payload(request)
        body = self._parse(http_request("PUT", BASE + request.external_id + "/", data=payload, what="the MMT scheduler"), "update")
        return SubmitResult(request.state if request.is_open else "accepted", external_id=request.external_id,
                            detail="catalog target %s updated" % request.external_id, response=body)

    def delete(self, request) -> StatusResult:
        if not request.external_id:
            raise FacilityError("request has no MMT target id")
        body = self._parse(http_request("DELETE", BASE + request.external_id + "/", data={"token": self.token(request.allocation)},
                                        what="the MMT scheduler"), "delete")
        return StatusResult("cancelled", detail="catalog target %s removed" % request.external_id, response=body)
