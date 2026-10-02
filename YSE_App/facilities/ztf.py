"""ZTF forced-photometry service adapter (#301).

Replaces the wget / IMAP e-mail dance of ``data_ingest/ZTF_Forced_Phot.py``
with the service's HTTP interface, so a request is a ``FacilityRequest`` of
kind ``photometry`` that is submitted, polled and, once the light curve is
ready, ingested into the transient's ZTF-Cam photometry.

* submit: ``GET requestForcedPhotometry.cgi?ra&dec&jdstart&jdend&email&userpass``
  (HTTP basic auth with the service's shared login);
* status: ``GET getForcedPhotometryRequests.cgi?email&userpass&option=All&action=Query``
  answers a JSON list of the user's jobs; the one matching our coordinates and
  window gives ``reqid`` (external id), ``started`` / ``ended`` and, when done,
  the ``lightcurve`` path;
* results: the light-curve table is downloaded and parsed (``jd``, ``filter``,
  ``forcediffimflux``, ``forcediffimfluxunc``, ``zpdiff``, ``procstatus``).

Credential payload: ``{"email": "...", "userpass": "..."}`` (the account
registered with the forced-photometry service); optional ``http_user`` /
``http_password`` override the service's shared HTTP login. SkyPortal's
``facility_apis/ztf.py`` (BSD-3-Clause) documents the same endpoints; no code
is copied.
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List, Optional

from YSE_App.facilities.base import (
    KIND_PHOTOMETRY,
    FacilityAPI,
    FacilityError,
    Field,
    StatusResult,
    SubmitResult,
    http_request,
    json_or_text,
    short,
)
from YSE_App.facilities.registry import register

BASE = "https://ztfweb.ipac.caltech.edu"
SUBMIT_URL = BASE + "/cgi-bin/requestForcedPhotometry.cgi"
STATUS_URL = BASE + "/cgi-bin/getForcedPhotometryRequests.cgi"
#: shared HTTP login of the public service (the per-user account is in the credential)
HTTP_USER = "ztffps"
HTTP_PASSWORD = "dontgocrazy!"
INSTRUMENT = "ZTF-Cam"
OBS_GROUP = "ZTF"
ZP_YSE = 27.5
JD_UNIX_EPOCH = 2440587.5
COORD_TOLERANCE = 2e-5  # degrees; the service echoes 6-decimal coordinates


def jd_now() -> float:
    return JD_UNIX_EPOCH + datetime.datetime.utcnow().timestamp() / 86400.0


def jd_to_datetime(jd: float) -> datetime.datetime:
    seconds = (float(jd) - JD_UNIX_EPOCH) * 86400.0
    return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)


def parse_lightcurve(text: str) -> List[Dict[str, Any]]:
    """Rows of a ZTF forced-photometry light-curve file as dicts keyed by column name."""
    columns: Optional[List[str]] = None
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            tokens = [c.strip() for c in stripped.lstrip("#").replace(",", " ").split() if c.strip()]
            if "jd" in tokens and "filter" in tokens and "forcediffimflux" in tokens:
                columns = tokens
            continue
        if columns is None:
            continue
        values = stripped.split()
        if len(values) < len(columns):
            continue
        rows.append(dict(zip(columns, values[:len(columns)])))
    return rows


def _float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def points_from_rows(rows: List[Dict[str, Any]], snr_limit: float = 3.0) -> List[Dict[str, Any]]:
    """Light-curve rows -> photometry points (mag None below ``snr_limit``; fluxes on zero point 27.5)."""
    points = []
    for row in rows:
        if str(row.get("procstatus", "0")).split(",")[0] not in ("0", "56", "57", "58"):
            continue
        jd, flux, unc, zp = (_float(row.get(k)) for k in ("jd", "forcediffimflux", "forcediffimfluxunc", "zpdiff"))
        if jd is None or flux is None or unc is None or zp is None:
            continue
        # ZTF_g -> g: YSE stores short band tokens scoped by instrument (common.tns_photometry_map)
        band = str(row.get("filter", "")).strip()
        band = band[-1] if band else "r"
        scale = 10 ** (-0.4 * (zp - ZP_YSE))
        mag = mag_err = None
        if unc > 0 and flux / unc >= snr_limit:
            mag = -2.5 * math.log10(flux) + zp
            mag_err = 1.0857 * unc / flux
        points.append({
            "mjd": jd - 2400000.5, "obs_date": jd_to_datetime(jd), "band": band, "mag": mag, "mag_err": mag_err,
            "flux": flux * scale, "flux_err": unc * scale, "flux_zero_point": ZP_YSE, "forced": True, "diffim": True,
        })
    return points


@register
class ZTFForcedPhotometry(FacilityAPI):
    slug = "ztf"
    name = "ZTF forced photometry"
    description = "Requests a forced-photometry light curve from the ZTF service and ingests it as ZTF-Cam photometry."
    kind = KIND_PHOTOMETRY
    capabilities = frozenset({"submit", "status", "results"})
    credential_keys = ["email", "userpass", "http_user", "http_password"]
    manual_status = False
    poll_interval_minutes = 10
    results_instrument = INSTRUMENT
    results_obs_group = OBS_GROUP
    setup_notes = ("credential {\"email\": \"<account e-mail>\", \"userpass\": \"<forced-photometry password>\"} "
                   "registered at ztfweb.ipac.caltech.edu; no proposal id needed. Requests are polled by the "
                   "facility poll cron and ingested when the light curve is ready.")

    def fields(self, allocation=None):
        return [
            Field("days", "integer", label="Days before now", default=60, minimum=1, maximum=3000,
                  help="Window length when jdstart is not given"),
            Field("jdstart", "number", label="JD start", minimum=2458000),
            Field("jdend", "number", label="JD end", minimum=2458000),
        ]

    def estimate_hours(self, params, allocation=None):
        return 0.0

    def window(self, params: Dict[str, Any]) -> Dict[str, float]:
        jdend = float(params.get("jdend") or jd_now())
        jdstart = float(params.get("jdstart") or (jdend - float(params.get("days") or 60)))
        return {"jdstart": round(jdstart, 6), "jdend": round(jdend, 6)}

    def account(self, allocation) -> Dict[str, str]:
        secret = allocation.secret(touch=True)
        email, userpass = secret.get("email"), secret.get("userpass") or secret.get("password")
        if not (email and userpass):
            raise FacilityError("the ZTF credential needs email and userpass")
        return {"email": email, "userpass": userpass,
                "auth": (secret.get("http_user") or HTTP_USER, secret.get("http_password") or HTTP_PASSWORD)}

    def build_payload(self, request) -> Dict[str, Any]:
        window = self.window(request.payload or {})
        return {"ra": round(float(request.transient.ra), 6), "dec": round(float(request.transient.dec), 6), **window}

    def submit(self, request) -> SubmitResult:
        account = self.account(request.allocation)
        payload = self.build_payload(request)
        params = dict(payload, email=account["email"], userpass=account["userpass"])
        response = http_request("GET", SUBMIT_URL, params=params, auth=account["auth"], what="the ZTF forced-photometry service")
        if response.status_code >= 300:
            raise FacilityError("ZTF answered %s: %s" % (response.status_code, short(response.text)))
        text = response.text or ""
        if "error" in text.lower() and "success" not in text.lower():
            raise FacilityError("ZTF rejected the request: %s" % short(text))
        # the window is frozen in the payload so the status query can match the job
        request.payload = dict(request.payload or {}, jdstart=payload["jdstart"], jdend=payload["jdend"])
        request.save(update_fields=["payload", "modified_date"])
        return SubmitResult("submitted", detail="forced photometry requested for JD %s-%s" % (payload["jdstart"], payload["jdend"]),
                            response={"status_code": response.status_code, "text": text[:500]}, hours=0.0)

    def find_job(self, request, jobs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        payload = self.build_payload(request)
        if request.external_id:
            for job in jobs:
                if str(job.get("reqid", "")) == request.external_id:
                    return job
        matches = []
        for job in jobs:
            ra, dec = _float(job.get("ra")), _float(job.get("dec"))
            if ra is None or dec is None:
                continue
            if abs(ra - payload["ra"]) > COORD_TOLERANCE or abs(dec - payload["dec"]) > COORD_TOLERANCE:
                continue
            jdstart, jdend = _float(job.get("jdstart")), _float(job.get("jdend"))
            if jdstart is not None and abs(jdstart - payload["jdstart"]) > 0.01:
                continue
            if jdend is not None and abs(jdend - payload["jdend"]) > 0.01:
                continue
            matches.append(job)
        if not matches:
            return None
        return sorted(matches, key=lambda j: str(j.get("created", "")))[-1]

    def get_status(self, request) -> StatusResult:
        account = self.account(request.allocation)
        response = http_request("GET", STATUS_URL, params={"email": account["email"], "userpass": account["userpass"],
                                                           "option": "All", "action": "Query"},
                                auth=account["auth"], what="the ZTF forced-photometry service")
        if response.status_code >= 300:
            raise FacilityError("ZTF status answered %s: %s" % (response.status_code, short(response.text)))
        body = json_or_text(response)
        if not isinstance(body, list):
            raise FacilityError("ZTF status did not answer with a job list: %s" % short(body))
        job = self.find_job(request, body)
        if job is None:
            return StatusResult("submitted", detail="not yet listed by the service", response=None)
        reqid = str(job.get("reqid", ""))
        if job.get("lightcurve"):
            state, detail = "complete", "light curve ready"
        elif str(job.get("exitcode", "")) not in ("", "0", "None"):
            state, detail = "failed", "service exit code %s" % job.get("exitcode")
        elif job.get("started"):
            state, detail = "running", "processing since %s" % job.get("started")
        else:
            state, detail = "accepted", "queued at the service"
        result = StatusResult(state, detail="%s (job %s)" % (detail, reqid), response=job)
        result.external_id = reqid
        return result

    def fetch_results(self, request) -> List[Dict[str, Any]]:
        account = self.account(request.allocation)
        job = None
        response = http_request("GET", STATUS_URL, params={"email": account["email"], "userpass": account["userpass"],
                                                           "option": "All", "action": "Query"},
                                auth=account["auth"], what="the ZTF forced-photometry service")
        body = json_or_text(response)
        if isinstance(body, list):
            job = self.find_job(request, body)
        path = (job or {}).get("lightcurve")
        if not path:
            raise FacilityError("the light curve is not available yet")
        url = path if str(path).startswith("http") else BASE + str(path)
        response = http_request("GET", url, auth=account["auth"], what="the ZTF light-curve file")
        if response.status_code >= 300:
            raise FacilityError("ZTF light-curve download answered %s" % response.status_code)
        return points_from_rows(parse_lightcurve(response.text))
