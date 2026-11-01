"""Blast host-galaxy properties as a transient annotation (#439).

Blast (https://blast.scimma.org, SCiMMA) associates a host with each TNS transient and fits
its SED. Its REST API (Django REST framework, read-only) needs an account token
(``Authorization: Token <key>``, ``[enrichment] BLAST_API_TOKEN``) and pages results
(``LimitOffsetPagination``: ``{"count", "next", "results": [...]}``). Two requests per
transient:

* ``GET /api/transient/?name=<TNS name>`` -- the transient and its host (name, position,
  redshift, catalogue);
* ``GET /api/sedfittingresult/?transient=<TNS name>`` -- Prospector fits per aperture
  (``local`` / ``global``): stellar mass, SFR, sSFR, age, metallicity, dust, as 16th / 50th /
  84th percentiles.

The result is stored as the transient's ``TransientAnnotation`` with origin ``blast`` (no
groups: every logged-in user, like the other catalogue annotations), so it shows in the
annotations panel and the search filters (``blast.global_log_mass`` ...). Nothing on the
Host row is changed; in particular no redshift is written. Off unless ``BLAST_ENABLED``.
We never submit transients to Blast.
"""

from __future__ import annotations

import time
from typing import Optional

import requests
from django.conf import settings

ORIGIN = "blast"
USER_AGENT = "YSE-PZ enrichment/1.0"
SED_FIELDS = ("log_mass", "log_sfr", "log_ssfr", "log_age", "logzsol", "dust2")
HOST_FIELDS = ("name", "ra_deg", "dec_deg", "redshift", "catalog_name", "object_id")


class BlastError(Exception):
    """Blast could not be queried (network, HTTP status, not JSON, no token)."""


class BlastRateLimited(BlastError):
    def __init__(self, message, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class BlastClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, *, session=None,
                 timeout: Optional[float] = None, interval: Optional[float] = None):
        self.base_url = (base_url or getattr(settings, "BLAST_API_URL", "") or "https://blast.scimma.org/api").rstrip("/")
        self.token = token if token is not None else getattr(settings, "BLAST_API_TOKEN", "")
        self.session = session or requests.Session()
        self.timeout = float(timeout if timeout is not None else getattr(settings, "BLAST_TIMEOUT_SECONDS", 30) or 30)
        self.interval = float(interval if interval is not None
                              else getattr(settings, "BLAST_REQUEST_INTERVAL_SECONDS", 1.0) or 0)
        self.requests = 0
        self._last = None

    def _wait(self):
        if self._last is not None and self.interval:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
        self._last = time.monotonic()

    def get(self, path: str, params: dict) -> dict:
        if not self.token:
            raise BlastError("no Blast API token: set [enrichment] BLAST_API_TOKEN")
        self._wait()
        self.requests += 1
        url = "%s/%s/" % (self.base_url, path.strip("/"))
        headers = {"Authorization": "Token %s" % self.token, "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        try:
            response = self.session.get(url, params=dict(params, format="json"), headers=headers,
                                        timeout=self.timeout)
        except requests.RequestException as exc:
            raise BlastError("%s: %s" % (path, type(exc).__name__))
        if response.status_code == 429:
            retry = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
            try:
                retry = float(retry) if retry else None
            except ValueError:
                retry = None
            raise BlastRateLimited("Blast rate limit", retry_after=retry)
        if response.status_code >= 400:
            raise BlastError("HTTP %s from Blast %s" % (response.status_code, path))
        try:
            return response.json()
        except ValueError:
            raise BlastError("Blast %s did not return JSON" % path)

    @staticmethod
    def results(doc) -> list:
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict):
            return list(doc.get("results") or [])
        return []

    def transient(self, name: str) -> Optional[dict]:
        rows = self.results(self.get("transient", {"name": name}))
        return rows[0] if rows else None

    def sed_results(self, name: str) -> list:
        return self.results(self.get("sedfittingresult", {"transient": name}))


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def annotation_document(record: dict, seds: list) -> dict:
    """The flat annotation document from Blast's transient record and its SED fits."""
    doc = {}
    host = record.get("host") if isinstance(record, dict) else None
    if isinstance(host, dict):
        for key in HOST_FIELDS:
            value = host.get(key)
            if value in (None, ""):
                continue
            doc["host_" + key.replace("_deg", "")] = _num(value) if key in ("ra_deg", "dec_deg", "redshift") else str(value)
    for sed in seds or []:
        aperture = sed.get("aperture") if isinstance(sed, dict) else None
        kind = str((aperture or {}).get("type") or "").strip().lower() if isinstance(aperture, dict) else ""
        if kind not in ("local", "global"):
            continue
        for name in SED_FIELDS:
            mid = _num(sed.get(name + "_50"))
            if mid is None:
                continue
            doc["%s_%s" % (kind, name)] = mid
            for pct in ("16", "84"):
                value = _num(sed.get("%s_%s" % (name, pct)))
                if value is not None:
                    doc["%s_%s_%s" % (kind, name, pct)] = value
    parts = []
    if doc.get("host_name"):
        parts.append("host %s" % doc["host_name"])
    for kind in ("global", "local"):
        if ("%s_log_mass" % kind) in doc:
            parts.append("%s log M* %.2f" % (kind, doc["%s_log_mass" % kind]))
            break
    doc["summary"] = "; ".join(parts) or "in Blast, no host yet"
    return doc


def enabled() -> bool:
    return bool(getattr(settings, "BLAST_ENABLED", False))


def make_context() -> dict:
    return {"client": BlastClient()}


def blast_one(transient, ctx) -> "object":
    """Enrichment ``run_one`` for ``blast``: fetch and store one transient's Blast annotation."""
    from YSE_App.services import annotations
    from YSE_App.services.enrichment import (ERROR, NO_MATCH, UNCHANGED, UPDATED, EnrichmentError, Outcome,
                                             RateLimited)
    from YSE_App.sharing.tns import tns_name_for

    name = tns_name_for(transient)
    if not name:
        return Outcome(NO_MATCH, "no TNS designation")
    client = ctx.get("client") or BlastClient()
    ctx["client"] = client
    if not client.token:
        raise EnrichmentError("no Blast API token: set [enrichment] BLAST_API_TOKEN")
    try:
        record = client.transient(name)
        if record is None:
            return Outcome(NO_MATCH, "%s is not in Blast" % name)
        seds = client.sed_results(name)
    except BlastRateLimited as exc:
        raise RateLimited(str(exc), retry_after=exc.retry_after)
    except BlastError as exc:
        return Outcome(ERROR, error=str(exc)[:500])
    doc = annotation_document(record, seds)
    doc["blast_name"] = name
    existing = transient.annotations.filter(origin=ORIGIN).first()
    if existing is not None and existing.data == doc:
        return Outcome(UNCHANGED, doc["summary"])
    from YSE_App.services.enrichment import external_service, get

    service = external_service(get("blast"), ctx.get("user"))
    annotations.upsert(transient, ORIGIN, doc, user=ctx.get("user"), service=service)
    return Outcome(UPDATED, doc["summary"], data={"keys": len(doc)})
