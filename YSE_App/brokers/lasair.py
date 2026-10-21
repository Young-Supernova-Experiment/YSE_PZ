"""Lasair provider (issue #274) against the Lasair ZTF REST API and its Kafka streams.

Lasair (https://lasair-ztf.lsst.ac.uk) needs an API token for every REST call,
so the provider declares ``requires_credential``: without an active
``EncryptedCredential(service="lasair")`` holding ``{"token": ...}`` it is
listed as unavailable and left out of ``enabled_providers()``, never erroring.
Endpoints used (all documented at ``/api/``): ``/streams/<topic>/`` (rows of a
Lasair filter's output stream, the polling ``query_alerts``), ``/objects/``
(object record with candidates and Sherlock classification), ``/lightcurves/``
(candidates and non-detections) and ``/cone/`` (cone search). The public
Kafka stream (``kafka.lsst.ac.uk``) carries one JSON message per filter hit
with the filter's selected columns; :meth:`parse_stream_message` enriches it
through ``/objects/`` when the position or magnitude is missing.
The broker is not reachable from the build container; tests mock the HTTP layer.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from django.conf import settings

from YSE_App.brokers import registry
from YSE_App.brokers.base import (
    CONE_SEARCH,
    GET_ALERT,
    PHOTOMETRY,
    QUERY_ALERTS,
    SAVE_AS_TRANSIENT,
    STREAM,
    BrokerAlert,
    BrokerError,
    BrokerProvider,
    BrokerUnavailable,
    make_point,
)
from YSE_App.brokers.http import request_json

DEFAULT_API = "https://lasair-ztf.lsst.ac.uk/api"
WEB_URL = "https://lasair-ztf.lsst.ac.uk/objects/{object_id}/"
DEFAULT_STREAM = "lasair_2SN-likecandidates"
FID_TO_BAND = {1: "g", 2: "r", 3: "i"}
OBJECT_BATCH = 50  # ids per /objects/ call


def _f(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def jd_to_mjd(jd) -> Optional[float]:
    jd = _f(jd)
    return jd - 2400000.5 if jd is not None else None


def _band(fid) -> str:
    try:
        return FID_TO_BAND.get(int(fid), str(fid or ""))
    except (TypeError, ValueError):
        return str(fid or "")


def _isdiffpos(value) -> Optional[bool]:
    if value is None:
        return None
    return str(value).strip().lower() in ("t", "1", "true")


def _sherlock(record: Dict) -> Dict:
    sherlock = record.get("sherlock") or {}
    if not isinstance(sherlock, dict):
        return {}
    return sherlock


def candidates_to_points(candidates: List[Dict]) -> List[Dict]:
    """YSE point dicts from Lasair ``candidates`` rows (detections have ``candid``,
    non-detections only ``diffmaglim``)."""
    points = []
    for row in candidates or []:
        mjd = jd_to_mjd(row.get("jd")) if row.get("jd") is not None else _f(row.get("mjd"))
        band = _band(row.get("fid"))
        if mjd is None or band not in FID_TO_BAND.values():
            continue
        if row.get("candid") and _f(row.get("magpsf")) is not None:
            points.append(make_point(mjd, band, row.get("magpsf"), row.get("sigmapsf"), alert_id=row.get("candid")))
        else:
            points.append(make_point(mjd, band, None, None, limit=row.get("diffmaglim")))
    points.sort(key=lambda p: p["mjd"])
    return points


def object_to_alert(record: Dict, *, stream_row: Optional[Dict] = None) -> Optional[BrokerAlert]:
    """One ``/objects/`` record (``objectId``, ``objectData``, ``candidates``, ``sherlock``)."""
    object_id = str(record.get("objectId") or (stream_row or {}).get("objectId") or "")
    data = record.get("objectData") or {}
    candidates = [c for c in (record.get("candidates") or []) if c.get("candid") and _f(c.get("magpsf")) is not None]
    candidates.sort(key=lambda c: _f(c.get("jd")) or 0)
    latest = candidates[-1] if candidates else {}
    ra = _f(data.get("ramean")) or _f(latest.get("ra")) or _f((stream_row or {}).get("ramean"))
    dec = _f(data.get("decmean")) or _f(latest.get("dec")) or _f((stream_row or {}).get("decmean"))
    if not object_id or ra is None or dec is None:
        return None
    sherlock = _sherlock(record)
    classification = sherlock.get("classification") or data.get("sherlock_classification") or (stream_row or {}).get("classification") or ""
    first_mjd = jd_to_mjd(data.get("jdmin")) if data.get("jdmin") else (jd_to_mjd(candidates[0].get("jd")) if candidates else None)
    props = {"objectData": data, "sherlock": sherlock}
    if stream_row:
        props["stream"] = stream_row
    return BrokerAlert(
        broker=LasairProvider.slug,
        object_id=object_id,
        alert_id=str(latest.get("candid") or ""),
        ra=float(ra),
        dec=float(dec),
        mjd=jd_to_mjd(latest.get("jd")) if latest else jd_to_mjd(data.get("jdmax")),
        discovery_mjd=first_mjd,
        mag=_f(latest.get("magpsf")) if latest else _f((stream_row or {}).get("magpsf")),
        mag_err=_f(latest.get("sigmapsf")) if latest else None,
        band=_band(latest.get("fid")) if latest else "",
        rb=_f(latest.get("rb")) if latest else _f((stream_row or {}).get("rb")),
        drb=_f(latest.get("drb")) if latest else _f((stream_row or {}).get("drb")),
        ndet=int(data["ncand"]) if _f(data.get("ncand")) is not None else (len(candidates) or None),
        is_positive=_isdiffpos(latest.get("isdiffpos")) if latest else None,
        sgscore=_f(latest.get("sgscore1")) if latest else _f((stream_row or {}).get("sgscore1")),
        distpsnr1=_f(latest.get("distpsnr1")) if latest else _f((stream_row or {}).get("distpsnr1")),
        classification=str(classification),
        class_probabilities={},
        instrument="ZTF-Cam",
        obs_group="ZTF",
        url=WEB_URL.format(object_id=object_id),
        properties=props,
    )


def stream_row_to_alert(row: Dict) -> Optional[BrokerAlert]:
    """A filter-stream row on its own (``objectId`` plus the filter's selected columns)."""
    object_id = str(row.get("objectId") or "")
    ra = _f(row.get("ramean") if row.get("ramean") is not None else row.get("ra"))
    dec = _f(row.get("decmean") if row.get("decmean") is not None else row.get("dec"))
    if not object_id or ra is None or dec is None:
        return None
    mjd = _f(row.get("mjdmax")) if row.get("mjdmax") is not None else jd_to_mjd(row.get("jdmax"))
    first = _f(row.get("mjdmin")) if row.get("mjdmin") is not None else jd_to_mjd(row.get("jdmin"))
    return BrokerAlert(
        broker=LasairProvider.slug,
        object_id=object_id,
        alert_id=str(row.get("candid") or ""),
        ra=float(ra),
        dec=float(dec),
        mjd=mjd,
        discovery_mjd=first,
        mag=_f(row.get("magpsf")) if row.get("magpsf") is not None else _f(row.get("latestmag")),
        mag_err=_f(row.get("sigmapsf")),
        band=_band(row.get("fid")) if row.get("fid") is not None else "",
        rb=_f(row.get("rb")),
        drb=_f(row.get("drb")),
        ndet=int(row["ncand"]) if _f(row.get("ncand")) is not None else None,
        is_positive=None,
        sgscore=_f(row.get("sgscore1")),
        distpsnr1=_f(row.get("distpsnr1")),
        classification=str(row.get("classification") or row.get("sherlock_classification") or ""),
        instrument="ZTF-Cam",
        obs_group="ZTF",
        url=WEB_URL.format(object_id=object_id),
        properties={"stream": row},
    )


@registry.register
class LasairProvider(BrokerProvider):
    slug = "lasair"
    name = "Lasair"
    description = "Lasair broker (ZTF) through its REST API (token) and public Kafka filter streams."
    capabilities = (QUERY_ALERTS, GET_ALERT, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT, STREAM)
    requires_credential = True
    query_keys = ("topic", "limit", "regex")
    stream_format = "json"
    stream_bootstrap_servers = "kafka.lsst.ac.uk:9092"
    stream_topics = (DEFAULT_STREAM,)

    @property
    def api(self) -> str:
        return (self.options.get("api_url") or getattr(settings, "BROKER_LASAIR_API_URL", None) or DEFAULT_API).rstrip("/")

    def token(self) -> str:
        return str(self.credential.get("token") or self.credential.get("api_token") or "")

    def available(self) -> bool:
        return bool(self.token())

    def unavailable_reason(self) -> str:
        return "" if self.token() else "no active EncryptedCredential with service='lasair' holding a token"

    def _get(self, path: str, params: Optional[Dict] = None):
        if not self.token():
            raise BrokerUnavailable("lasair: %s" % self.unavailable_reason())
        params = dict(params or {})
        params.setdefault("format", "json")
        return request_json("GET", "%s/%s" % (self.api, path.strip("/") + "/"), params=params,
                            headers={"Authorization": "Token %s" % self.token()})

    # -- objects --------------------------------------------------------------
    def _objects(self, object_ids: List[str]) -> List[Dict]:
        out = []
        ids = [str(o) for o in object_ids if o]
        for i in range(0, len(ids), OBJECT_BATCH):
            data = self._get("objects", {"objectIds": ",".join(ids[i:i + OBJECT_BATCH])})
            if isinstance(data, list):
                out.extend(d for d in data if isinstance(d, dict))
            elif isinstance(data, dict) and data.get("objectId"):
                out.append(data)
        return out

    def get_alert(self, object_id) -> Optional[BrokerAlert]:
        records = self._objects([object_id])
        return object_to_alert(records[0]) if records else None

    def get_photometry(self, object_id) -> List[Dict]:
        data = self._get("lightcurves", {"objectIds": str(object_id)})
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        candidates = []
        for rec in rows:
            if isinstance(rec, dict):
                candidates.extend(rec.get("candidates") or [])
        return candidates_to_points(candidates)

    # -- search ----------------------------------------------------------------
    def query_alerts(self, query=None, *, since_mjd=None, limit=100) -> List[BrokerAlert]:
        query = self.validate_query(query)
        topic = str(query.get("topic") or DEFAULT_STREAM)
        params = {"limit": int(query.get("limit") or limit)}
        if query.get("regex"):
            params["regex"] = str(query["regex"])
        rows = self._get("streams/%s" % topic, params)
        if isinstance(rows, dict):
            rows = rows.get("results") or rows.get("digest") or []
        rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("objectId")]
        by_id: Dict[str, Dict] = {}
        for row in rows:
            by_id.setdefault(str(row["objectId"]), row)
        records = {str(r.get("objectId")): r for r in self._objects(list(by_id))}
        alerts = []
        for oid, row in by_id.items():
            rec = records.get(oid)
            alert = object_to_alert(rec, stream_row=row) if rec else stream_row_to_alert(row)
            if alert is not None:
                if since_mjd is not None and alert.mjd is not None and alert.mjd < float(since_mjd):
                    continue
                alerts.append(alert)
        return alerts[: int(limit)]

    def cone_search(self, ra, dec, radius_arcsec, *, limit=50) -> List[BrokerAlert]:
        data = self._get("cone", {"ra": float(ra), "dec": float(dec), "radius": float(radius_arcsec), "requestType": "all"})
        hits = data if isinstance(data, list) else ((data or {}).get("results") or [])
        ids = [str(h.get("object")) for h in hits if isinstance(h, dict) and h.get("object")][: int(limit)]
        if not ids:
            return []
        records = {str(r.get("objectId")): r for r in self._objects(ids)}
        return [a for a in (object_to_alert(records[i]) for i in ids if i in records) if a is not None]

    # -- stream ----------------------------------------------------------------
    def parse_stream_message(self, topic, message, *, key=None) -> Optional[BrokerAlert]:
        if not isinstance(message, dict) or not message.get("objectId"):
            return None
        alert = stream_row_to_alert(message)
        if (alert is None or alert.mag is None) and self.token():
            try:
                records = self._objects([str(message["objectId"])])
            except BrokerError:
                records = []
            if records:
                return object_to_alert(records[0], stream_row=message)
        return alert
