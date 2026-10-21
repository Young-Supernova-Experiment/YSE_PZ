"""Fink provider (issue #274) against the public Fink REST API.

No credential and no Kafka: the polling ingest asks ``/api/v1/latests`` for
the most recent alerts of one or more Fink classes and ``/api/v1/objects``
for a full light curve. Field names are the documented Fink column names
(``i:`` = ZTF alert field, ``d:`` = Fink added value, ``v:`` = derived), see
https://api.fink-portal.org. The broker is not reachable from the CI runner,
so the tests mock the HTTP layer with recorded-shape rows.
"""

from __future__ import annotations

import datetime
import math
from typing import Dict, List, Optional

from django.conf import settings

from YSE_App.brokers import registry
from YSE_App.brokers.base import (
    CONE_SEARCH,
    CUTOUTS,
    GET_ALERT,
    PHOTOMETRY,
    QUERY_ALERTS,
    SAVE_AS_TRANSIENT,
    STREAM,
    BrokerAlert,
    BrokerProvider,
    make_point,
)
from YSE_App.brokers.http import request_json

DEFAULT_API = "https://api.fink-portal.org/api/v1"
WEB_URL = "https://fink-portal.org/{object_id}"
FID_TO_BAND = {1: "g", 2: "r", 3: "i"}
DEFAULT_CLASSES = ("SN candidate", "Early SN Ia candidate")
CUTOUT_KINDS = {"science": "Science", "template": "Template", "difference": "Difference"}


def _f(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _band(row) -> str:
    fid = row.get("i:fid")
    try:
        return FID_TO_BAND.get(int(fid), str(fid or ""))
    except (TypeError, ValueError):
        return str(fid or "")


def jd_to_mjd(jd) -> Optional[float]:
    jd = _f(jd)
    return jd - 2400000.5 if jd is not None else None


def _isdiffpos(row) -> Optional[bool]:
    v = row.get("i:isdiffpos")
    if v is None:
        return None
    return str(v).strip().lower() in ("t", "1", "true")


def _probabilities(row) -> Dict[str, float]:
    """Fink classifier scores with human labels (the ``d:`` columns present)."""
    out = {}
    for key, label in (("d:snn_snia_vs_nonia", "SN Ia (SNN)"), ("d:snn_sn_vs_all", "SN (SNN)"),
                       ("d:rf_snia_vs_nonia", "SN Ia (RF)"), ("d:rf_kn_vs_nonkn", "Kilonova (RF)")):
        v = _f(row.get(key))
        if v is not None:
            out[label] = v
    return out


def row_to_alert(row: Dict, cutout_urls: Optional[Dict[str, str]] = None, *,
                 first_mjd: Optional[float] = None, ndet: Optional[int] = None) -> BrokerAlert:
    """One Fink alert row (``/latests``, ``/conesearch`` or ``/objects`` shape)."""
    object_id = str(row.get("i:objectId") or "")
    jdstart = jd_to_mjd(row.get("i:jdstarthist"))
    classification = row.get("v:classification") or row.get("d:cdsxmatch") or ""
    return BrokerAlert(
        broker=FinkProvider.slug,
        object_id=object_id,
        alert_id=str(row.get("i:candid") or ""),
        ra=float(row.get("i:ra")),
        dec=float(row.get("i:dec")),
        mjd=jd_to_mjd(row.get("i:jd")),
        discovery_mjd=first_mjd if first_mjd is not None else jdstart,
        mag=_f(row.get("i:magpsf")),
        mag_err=_f(row.get("i:sigmapsf")),
        band=_band(row),
        rb=_f(row.get("i:rb")),
        drb=_f(row.get("i:drb")),
        ndet=ndet if ndet is not None else (int(row["i:ndethist"]) if _f(row.get("i:ndethist")) is not None else None),
        is_positive=_isdiffpos(row),
        sgscore=_f(row.get("i:sgscore1")),
        distpsnr1=_f(row.get("i:distpsnr1")),
        classification=str(classification),
        class_probabilities=_probabilities(row),
        instrument="ZTF-Cam",
        obs_group="ZTF",
        url=WEB_URL.format(object_id=object_id),
        cutout_urls=cutout_urls or {},
        properties={k: v for k, v in row.items() if not str(k).startswith("b:")},  # drop binary stamps
    )


@registry.register
class FinkProvider(BrokerProvider):
    slug = "fink"
    name = "Fink"
    description = "Fink broker (ZTF) through its public REST API; no credential needed."
    capabilities = (QUERY_ALERTS, GET_ALERT, CUTOUTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT, STREAM)
    query_keys = ("classes", "days", "n")
    stream_format = "avro"
    stream_bootstrap_servers = "kafka-ztf.fink-broker.org:24499"
    stream_topics = ("fink_sn_candidates_ztf", "fink_early_sn_candidates_ztf")

    @property
    def api(self) -> str:
        return (self.options.get("api_url") or getattr(settings, "BROKER_FINK_API_URL", None) or DEFAULT_API).rstrip("/")

    def _post(self, path: str, payload: Dict):
        payload = dict(payload)
        payload.setdefault("output-format", "json")
        data = request_json("POST", "%s/%s" % (self.api, path.lstrip("/")), json=payload)
        return data if isinstance(data, list) else []

    def cutout_urls(self, object_id: str, candid: str = "") -> Dict[str, str]:
        base = "%s/cutouts?objectId=%s&output-format=PNG" % (self.api, object_id)
        if candid:
            base += "&candid=%s" % candid
        return {key: "%s&kind=%s" % (base, kind) for key, kind in CUTOUT_KINDS.items()}

    def get_cutouts(self, object_id, alert_id="") -> Dict[str, str]:
        return self.cutout_urls(object_id, alert_id)

    def _alerts_from_rows(self, rows: List[Dict]) -> List[BrokerAlert]:
        """Latest row per object, with cutout links."""
        latest: Dict[str, Dict] = {}
        for row in rows:
            oid = str(row.get("i:objectId") or "")
            if not oid or _f(row.get("i:ra")) is None or _f(row.get("i:dec")) is None:
                continue
            if oid not in latest or (_f(row.get("i:jd")) or 0) > (_f(latest[oid].get("i:jd")) or 0):
                latest[oid] = row
        return [row_to_alert(r, self.cutout_urls(oid, str(r.get("i:candid") or ""))) for oid, r in latest.items()]

    def query_alerts(self, query=None, *, since_mjd=None, limit=100) -> List[BrokerAlert]:
        query = self.validate_query(query)
        classes = query.get("classes") or list(DEFAULT_CLASSES)
        if isinstance(classes, str):
            classes = [c.strip() for c in classes.split(",") if c.strip()]
        n = int(query.get("n") or limit)
        payload = {"n": n}
        days = query.get("days")
        if since_mjd is None and days is not None:
            since_mjd = datetime_mjd(datetime.datetime.now(datetime.timezone.utc)) - float(days)
        if since_mjd is not None:
            payload["startdate"] = mjd_to_isodate(since_mjd)
        rows = []
        for cls in classes:
            rows.extend(self._post("latests", dict(payload, **{"class": cls})))
        return self._alerts_from_rows(rows)[: int(limit)]

    def cone_search(self, ra, dec, radius_arcsec, *, limit=50) -> List[BrokerAlert]:
        rows = self._post("conesearch", {"ra": float(ra), "dec": float(dec), "radius": float(radius_arcsec)})
        return self._alerts_from_rows(rows)[: int(limit)]

    def _object_rows(self, object_id: str, with_limits: bool) -> List[Dict]:
        return self._post("objects", {"objectId": str(object_id), "withupperlim": bool(with_limits)})

    def get_alert(self, object_id) -> Optional[BrokerAlert]:
        rows = [r for r in self._object_rows(object_id, False) if _f(r.get("i:magpsf")) is not None]
        if not rows:
            return None
        rows.sort(key=lambda r: _f(r.get("i:jd")) or 0)
        latest = rows[-1]
        alert = row_to_alert(latest, self.cutout_urls(str(object_id), str(latest.get("i:candid") or "")),
                             first_mjd=jd_to_mjd(rows[0].get("i:jd")), ndet=len(rows))
        return alert

    def get_photometry(self, object_id) -> List[Dict]:
        points = []
        for row in self._object_rows(object_id, True):
            tag = str(row.get("d:tag") or "valid").lower()
            mjd = jd_to_mjd(row.get("i:jd"))
            band = _band(row)
            if mjd is None or band not in FID_TO_BAND.values():
                continue
            if tag == "upperlim" or _f(row.get("i:magpsf")) is None:
                points.append(make_point(mjd, band, None, None, limit=row.get("i:diffmaglim")))
            else:
                points.append(make_point(mjd, band, row.get("i:magpsf"), row.get("i:sigmapsf"),
                                         alert_id=row.get("i:candid"), bad=(tag == "badquality")))
        points.sort(key=lambda p: p["mjd"])
        return points


# Fink added values that arrive at the top level of a stream packet (``d:`` columns in the REST rows).
STREAM_ADDED_VALUES = ("cdsxmatch", "rf_snia_vs_nonia", "snn_snia_vs_nonia", "snn_sn_vs_all", "rf_kn_vs_nonkn",
                       "roid", "mulens", "nalerthist", "tag", "tracklet", "DR3Name", "Plx", "e_Plx", "gcvs", "vsx")


def stream_packet_to_row(packet: Dict) -> Optional[Dict]:
    """Flatten one Fink Kafka packet (Avro: ``objectId``, ``candidate``, Fink values at the top
    level) into the ``i:`` / ``d:`` keyed row the REST parser understands."""
    if not isinstance(packet, dict):
        return None
    candidate = packet.get("candidate")
    if not isinstance(candidate, dict):
        return None
    row = {"i:%s" % k: v for k, v in candidate.items() if not isinstance(v, (bytes, bytearray))}
    row["i:objectId"] = packet.get("objectId") or candidate.get("objectId")
    for key in STREAM_ADDED_VALUES:
        if key in packet:
            row["d:%s" % key] = packet[key]
    if "v:classification" not in row:
        label = packet.get("finkclass") or packet.get("classification") or packet.get("cdsxmatch")
        if label:
            row["v:classification"] = label
    if not row.get("i:objectId") or _f(row.get("i:ra")) is None or _f(row.get("i:dec")) is None:
        return None
    return row


def _fink_parse_stream(self, topic, message, *, key=None):
    row = stream_packet_to_row(message)
    if row is None:
        return None
    return row_to_alert(row, self.cutout_urls(str(row["i:objectId"]), str(row.get("i:candid") or "")))


FinkProvider.parse_stream_message = _fink_parse_stream


def datetime_mjd(dt: datetime.datetime) -> float:
    from astropy.time import Time

    return float(Time(dt).mjd)


def mjd_to_isodate(mjd: float) -> str:
    from astropy.time import Time

    return Time(float(mjd), format="mjd", scale="utc").to_datetime().strftime("%Y-%m-%d %H:%M:%S")
