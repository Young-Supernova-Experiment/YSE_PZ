"""Shared pieces of the built-in annotation checks (#318).

Each check is a function ``check(ra_deg, dec_deg, radius_arcsec) -> (data, verdict, summary)``
that talks to one catalogue through a TAP ``sync`` endpoint (Gaia's at ESA,
VizieR's at CDS) with ``FORMAT=json``; :func:`tap_query` returns the rows as
dicts. :func:`execute_check` is the glue ``execute_run`` calls: it marks the run
running, performs the check, writes the annotation with
``services.annotations.upsert`` and completes the run (failed, with the
message, when the catalogue could not be reached or answered with an error).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Callable, Dict, List, Optional, Tuple

import requests
from django.conf import settings

from YSE_App.models.annotation_models import (
    SUMMARY_KEY,
    VERDICT_AGN,
    VERDICT_CLEAN,
    VERDICT_KEY,
    VERDICT_STELLAR,
    VERDICT_UNKNOWN,
)
from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.services import annotations as annotations_svc
from YSE_App.services import external_services as runs

log = logging.getLogger(__name__)

USER_AGENT = "YSE-PZ annotation-services/1.0"
GAIA_TAP_URL_DEFAULT = "https://gea.esac.esa.int/tap-server/tap/sync"
VIZIER_TAP_URL_DEFAULT = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"

__all__ = [
    "AnnotationCheckError", "CheckResult", "VERDICT_AGN", "VERDICT_CLEAN", "VERDICT_STELLAR", "VERDICT_UNKNOWN",
    "execute_check", "gaia_tap_url", "http_timeout", "num", "round_or_none", "tap_query", "vizier_tap_url",
]

CheckResult = Tuple[Dict, str, str]


class AnnotationCheckError(Exception):
    """The catalogue could not be queried (network, HTTP status, TAP error, bad payload)."""


def http_timeout() -> float:
    return float(getattr(settings, "ANNOTATION_HTTP_TIMEOUT_SECONDS", 30) or 30)


def gaia_tap_url() -> str:
    return getattr(settings, "GAIA_TAP_URL", "") or GAIA_TAP_URL_DEFAULT


def vizier_tap_url() -> str:
    return getattr(settings, "VIZIER_TAP_URL", "") or VIZIER_TAP_URL_DEFAULT


def tap_query(url: str, adql: str, timeout: Optional[float] = None) -> List[Dict]:
    """POST an ADQL query to a TAP ``sync`` endpoint and return the rows as dicts.

    Both ESA's and CDS's services answer ``FORMAT=json`` with the VOTable-like
    ``{"metadata": [{"name": ...}], "data": [[...], ...]}``; a TAP error comes
    back as a VOTable (HTTP 200 or 400) whose text is not JSON, and is reported
    with its ``QUERY_STATUS`` message when one can be found.
    """
    payload = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": adql}
    try:
        response = requests.post(url, data=payload, timeout=timeout or http_timeout(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except requests.RequestException as exc:
        raise AnnotationCheckError("%s: %s" % (url, exc.__class__.__name__ if not str(exc) else exc))
    text = response.text or ""
    if response.status_code >= 400:
        raise AnnotationCheckError("HTTP %s from %s: %s" % (response.status_code, url, _tap_error(text)))
    try:
        body = json.loads(text)
    except ValueError:
        raise AnnotationCheckError("TAP service did not answer with JSON: %s" % _tap_error(text))
    if not isinstance(body, dict) or "data" not in body:
        raise AnnotationCheckError("unexpected TAP payload (no 'data' member)")
    columns = [str(c.get("name")) for c in body.get("metadata") or []]
    rows = []
    for values in body.get("data") or []:
        if isinstance(values, dict):
            rows.append(values)
        else:
            rows.append(dict(zip(columns, values)))
    return rows


def _tap_error(text: str) -> str:
    """The message of a VOTable ``QUERY_STATUS="ERROR"`` INFO, else the start of the body."""
    marker = 'QUERY_STATUS" value="ERROR"'
    idx = text.find(marker)
    if idx >= 0:
        start = text.find(">", idx)
        end = text.find("<", start + 1)
        if 0 <= start < end:
            message = text[start + 1:end].strip()
            if message:
                return message[:500]
    return (text.strip()[:200] or "empty body")


def num(value) -> Optional[float]:
    """A finite float, or None for null / non-numeric / NaN catalogue cells."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def round_or_none(value, digits: int = 4) -> Optional[float]:
    value = num(value)
    return None if value is None else round(value, digits)


def separation_arcsec(ra1, dec1, ra2, dec2) -> Optional[float]:
    """Great-circle separation in arcsec (spherical cosine, small angles fine at 3'')."""
    ra1, dec1, ra2, dec2 = (num(ra1), num(dec1), num(ra2), num(dec2))
    if None in (ra1, dec1, ra2, dec2):
        return None
    r1, d1, r2, d2 = (math.radians(v) for v in (ra1, dec1, ra2, dec2))
    cos_sep = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    cos_sep = max(-1.0, min(1.0, cos_sep))
    return round(math.degrees(math.acos(cos_sep)) * 3600.0, 3)


def nearest(rows: List[Dict], ra: float, dec: float, ra_key: str, dec_key: str) -> Tuple[Optional[Dict], Optional[float]]:
    """The row closest to ``(ra, dec)`` and its separation; ``(None, None)`` for no rows."""
    best, best_sep = None, None
    for row in rows:
        sep = separation_arcsec(ra, dec, row.get(ra_key), row.get(dec_key))
        if sep is None:
            continue
        if best_sep is None or sep < best_sep:
            best, best_sep = row, sep
    if best is None and rows:
        best = rows[0]
    return best, best_sep


def cone_params(run: ExternalServiceRun) -> Tuple[float, float, float]:
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    ra = num(payload.get("ra"))
    dec = num(payload.get("dec"))
    if (ra is None or dec is None) and run.transient_id:
        ra, dec = float(run.transient.ra), float(run.transient.dec)
    if ra is None or dec is None:
        raise AnnotationCheckError("the run has no coordinates (no transient and no ra/dec in the payload)")
    radius = num(payload.get("radius_arcsec")) or annotations_svc.default_radius_arcsec()
    return ra, dec, float(radius)


def execute_check(run: ExternalServiceRun, check: Callable[[float, float, float], CheckResult],
                  origin: Optional[str] = None) -> Dict:
    """Run ``check`` for ``run``; write the annotation; complete the run. Returns the job result."""
    origin = origin or run.service.slug
    run.mark_running()
    if run.transient_id is None:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="annotation runs need a transient")
        return {"status": run.status, "origin": origin}
    try:
        ra, dec, radius = cone_params(run)
        data, verdict, summary = check(ra, dec, radius)
    except AnnotationCheckError as exc:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=str(exc))
        return {"status": run.status, "origin": origin, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported on the run, never raised into the queue
        log.exception("annotation check %s for run %s raised", origin, run.uuid)
        error = "%s: %s" % (type(exc).__name__, exc)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
        return {"status": run.status, "origin": origin, "error": error}
    document = dict(data)
    document[VERDICT_KEY] = verdict
    document[SUMMARY_KEY] = summary
    document["radius_arcsec"] = radius
    annotation, _created = annotations_svc.upsert(
        run.transient, origin, document, user=run.created_by, run=run, service=run.service,
    )
    runs.record_completion(run, ExternalServiceRun.STATUS_SUCCEEDED, result=document)
    return {"status": run.status, "origin": origin, "verdict": verdict, "annotation_id": annotation.pk}
