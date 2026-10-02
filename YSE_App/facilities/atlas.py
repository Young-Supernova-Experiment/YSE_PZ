"""ATLAS forced-photometry adapter (#301).

Uses the ATLAS forced-photometry server at ``fallingstar-data.com``:

* submit: ``POST /forcedphot/queue/`` with ``Authorization: Token <api_token>``
  and ``ra``, ``dec``, ``mjd_min``, ``mjd_max``, ``use_reduced`` → ``201`` with
  the task URL (external id); ``429`` means the queue is full and the
  submission is retried later;
* status: ``GET <task url>`` → ``finishtimestamp`` and ``result_url`` when done,
  ``starttimestamp`` while running;
* results: the result file (``###MJD m dm uJy duJy F err chi/N RA Dec ...``)
  is parsed into cyan (``c``) and orange (``o``) points on the ``ATLAS`` instrument.

Credential payload: ``{"api_token": "..."}``, or ``{"username", "password"}``
(a token is fetched from ``/forcedphot/api-token-auth/``). SkyPortal's
``facility_apis/atlas.py`` (BSD-3-Clause) documents the same endpoints; no code
is copied.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from YSE_App.facilities.base import (
    KIND_PHOTOMETRY,
    FacilityAPI,
    FacilityError,
    FacilityUnreachable,
    Field,
    StatusResult,
    SubmitResult,
    http_request,
    json_or_text,
    short,
)
from YSE_App.facilities.registry import register

BASE = "https://fallingstar-data.com/forcedphot"
QUEUE_URL = BASE + "/queue/"
TOKEN_URL = BASE + "/api-token-auth/"
INSTRUMENT = "ATLAS"
OBS_GROUP = "ATLAS"
ZP_MICROJANSKY = 23.9
#: ATLAS filter letter -> YSE band token on the ATLAS instrument (short names, see common.tns_photometry_map)
BANDS = {"c": "c", "o": "o"}
MJD_UNIX_EPOCH = 40587.0


def mjd_now() -> float:
    return MJD_UNIX_EPOCH + datetime.datetime.utcnow().timestamp() / 86400.0


def mjd_to_datetime(mjd: float) -> datetime.datetime:
    return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=float(mjd) - MJD_UNIX_EPOCH)


def parse_result(text: str) -> List[Dict[str, Any]]:
    """Rows of an ATLAS forced-photometry result file as dicts (header line starts with ``###``)."""
    columns: Optional[List[str]] = None
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            if body.startswith("MJD"):
                columns = body.split()
            continue
        if columns is None:
            continue
        values = stripped.split()
        if len(values) < len(columns):
            continue
        rows.append(dict(zip(columns, values)))
    return rows


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def points_from_rows(rows: List[Dict[str, Any]], snr_limit: float = 3.0) -> List[Dict[str, Any]]:
    points = []
    for row in rows:
        mjd, flux, unc = _float(row.get("MJD")), _float(row.get("uJy")), _float(row.get("duJy"))
        if mjd is None or flux is None or unc is None:
            continue
        band = BANDS.get(str(row.get("F", "")).strip().lower())
        if band is None:
            continue
        mag = mag_err = None
        if unc > 0 and flux / unc >= snr_limit:
            mag, mag_err = _float(row.get("m")), _float(row.get("dm"))
            if mag is not None and mag <= 0:
                mag = mag_err = None
        points.append({
            "mjd": mjd, "obs_date": mjd_to_datetime(mjd), "band": band, "mag": mag, "mag_err": mag_err,
            "flux": flux, "flux_err": unc, "flux_zero_point": ZP_MICROJANSKY, "forced": True, "diffim": True,
        })
    return points


@register
class ATLASForcedPhotometry(FacilityAPI):
    slug = "atlas"
    name = "ATLAS forced photometry"
    description = "Queues a forced-photometry task at the ATLAS server and ingests the c / o light curve."
    kind = KIND_PHOTOMETRY
    capabilities = frozenset({"submit", "status", "results"})
    credential_keys = ["api_token", "username", "password"]
    manual_status = False
    poll_interval_minutes = 10
    results_instrument = INSTRUMENT
    results_obs_group = OBS_GROUP
    setup_notes = ("credential {\"api_token\": \"<fallingstar-data.com token>\"} (or username/password); no proposal "
                   "id. A 429 from the server (queue full) is retried by the job runner.")

    def fields(self, allocation=None):
        return [
            Field("days", "integer", label="Days before now", default=200, minimum=1, maximum=5000,
                  help="Window length when mjd_min is not given"),
            Field("mjd_min", "number", label="MJD start", minimum=50000),
            Field("mjd_max", "number", label="MJD end", minimum=50000),
            Field("use_reduced", "boolean", label="Use reduced (non-difference) images", default=False),
        ]

    def estimate_hours(self, params, allocation=None):
        return 0.0

    def auth_headers(self, allocation) -> Dict[str, str]:
        secret = allocation.secret(touch=True)
        token = secret.get("api_token") or secret.get("token")
        if not token:
            username, password = secret.get("username"), secret.get("password")
            if not (username and password):
                raise FacilityError("the ATLAS credential needs api_token, or username and password")
            response = http_request("POST", TOKEN_URL, data={"username": username, "password": password},
                                    what="the ATLAS token endpoint")
            body = json_or_text(response) if response.status_code < 300 else {}
            token = body.get("token") if isinstance(body, dict) else None
            if not token:
                raise FacilityError("ATLAS rejected the username/password (status %s)" % response.status_code)
        return {"Authorization": "Token %s" % token, "Accept": "application/json"}

    def build_payload(self, request) -> Dict[str, Any]:
        params = request.payload or {}
        mjd_max = float(params.get("mjd_max") or mjd_now())
        mjd_min = float(params.get("mjd_min") or (mjd_max - float(params.get("days") or 200)))
        return {"ra": float(request.transient.ra), "dec": float(request.transient.dec), "mjd_min": round(mjd_min, 5),
                "mjd_max": round(mjd_max, 5), "send_email": False, "use_reduced": bool(params.get("use_reduced"))}

    def submit(self, request) -> SubmitResult:
        headers = self.auth_headers(request.allocation)
        payload = self.build_payload(request)
        response = http_request("POST", QUEUE_URL, data=payload, headers=headers, what="the ATLAS forced-photometry server")
        if response.status_code == 429:
            raise FacilityUnreachable("ATLAS queue is full (429); retry later: %s" % short(response.text))
        body = json_or_text(response)
        if response.status_code not in (200, 201) or not isinstance(body, dict):
            raise FacilityError("ATLAS answered %s: %s" % (response.status_code, short(body)))
        task_url = str(body.get("url") or "")
        if not task_url:
            raise FacilityError("ATLAS did not return a task url: %s" % short(body))
        state = "complete" if body.get("finishtimestamp") else ("running" if body.get("starttimestamp") else "accepted")
        return SubmitResult(state, external_id=task_url, external_url=task_url, detail="task %s queued" % task_url,
                            response=body, hours=0.0)

    def get_status(self, request) -> StatusResult:
        if not request.external_id:
            raise FacilityError("request has no ATLAS task url")
        headers = self.auth_headers(request.allocation)
        response = http_request("GET", request.external_id, headers=headers, what="the ATLAS forced-photometry server")
        body = json_or_text(response)
        if response.status_code != 200 or not isinstance(body, dict):
            raise FacilityError("ATLAS status answered %s: %s" % (response.status_code, short(body)))
        if body.get("finishtimestamp"):
            if body.get("result_url"):
                return StatusResult("complete", detail="finished %s" % body["finishtimestamp"], response=body)
            return StatusResult("failed", detail="finished %s without a result file" % body["finishtimestamp"], response=body)
        if body.get("starttimestamp"):
            return StatusResult("running", detail="started %s" % body["starttimestamp"], response=body)
        return StatusResult("accepted", detail="queued at ATLAS", response=body)

    def fetch_results(self, request) -> List[Dict[str, Any]]:
        headers = self.auth_headers(request.allocation)
        response = http_request("GET", request.external_id, headers=headers, what="the ATLAS forced-photometry server")
        body = json_or_text(response)
        result_url = body.get("result_url") if isinstance(body, dict) else None
        if not result_url:
            raise FacilityError("the ATLAS result file is not available yet")
        response = http_request("GET", result_url, headers=headers, what="the ATLAS result file")
        if response.status_code >= 300:
            raise FacilityError("ATLAS result download answered %s" % response.status_code)
        return points_from_rows(parse_result(response.text))
