"""ALeRCE provider (issue #274) against the public ALeRCE ZTF REST API.

Plain ``requests`` calls against https://api.alerce.online/ztf/v1 (object
search with classifier / class / probability / mjd cuts, one object, its
light curve and classifier probabilities); stamps come from the ALeRCE
``avro`` service. The pinned ``alerce`` client package is not used so the web
venv needs nothing new. Field names follow the ALeRCE API documentation
(``oid, meanra, meandec, firstmjd, lastmjd, ndet, class, classifier,
probability``; detections ``mjd, fid, magpsf, sigmapsf, rb, drb, isdiffpos,
candid``; non-detections ``mjd, fid, diffmaglim``).
"""

from __future__ import annotations

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
    BrokerAlert,
    BrokerProvider,
    make_point,
    mjd_now,
)
from YSE_App.brokers.http import request_json

DEFAULT_API = "https://api.alerce.online/ztf/v1"
STAMP_URL = "https://avro.alerce.online/get_stamp?oid={oid}&candid={candid}&type={kind}&format=png"
WEB_URL = "https://alerce.online/object/{oid}"
FID_TO_BAND = {1: "g", 2: "r", 3: "i"}
DEFAULT_CLASSIFIER = "stamp_classifier"
DEFAULT_CLASSES = ("SN",)


def _f(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _band(fid) -> str:
    try:
        return FID_TO_BAND.get(int(fid), str(fid or ""))
    except (TypeError, ValueError):
        return str(fid or "")


def stamp_urls(oid: str, candid: str) -> Dict[str, str]:
    if not candid:
        return {}
    return {kind: STAMP_URL.format(oid=oid, candid=candid, kind=kind) for kind in ("science", "template", "difference")}


def object_to_alert(obj: Dict, *, detection: Optional[Dict] = None,
                    probabilities: Optional[List[Dict]] = None) -> BrokerAlert:
    """``/objects`` item (+ optional latest detection and probability rows)."""
    oid = str(obj.get("oid") or "")
    det = detection or {}
    probs = {}
    for p in probabilities or ():
        if p.get("classifier_name") in (None, obj.get("classifier"), DEFAULT_CLASSIFIER, "lc_classifier"):
            v = _f(p.get("probability"))
            if v is not None and p.get("class_name"):
                probs[str(p["class_name"])] = max(v, probs.get(str(p["class_name"]), 0.0))
    if not probs and obj.get("class") and _f(obj.get("probability")) is not None:
        probs[str(obj["class"])] = float(obj["probability"])
    isdiffpos = det.get("isdiffpos")
    return BrokerAlert(
        broker=AlerceProvider.slug,
        object_id=oid,
        alert_id=str(det.get("candid") or ""),
        ra=float(obj.get("meanra") if obj.get("meanra") is not None else det.get("ra")),
        dec=float(obj.get("meandec") if obj.get("meandec") is not None else det.get("dec")),
        mjd=_f(det.get("mjd")) if det else _f(obj.get("lastmjd")),
        discovery_mjd=_f(obj.get("firstmjd")),
        mag=_f(det.get("magpsf")),
        mag_err=_f(det.get("sigmapsf")),
        band=_band(det.get("fid")) if det else "",
        rb=_f(det.get("rb")),
        drb=_f(det.get("drb")),
        ndet=int(obj["ndet"]) if _f(obj.get("ndet")) is not None else None,
        is_positive=(int(isdiffpos) > 0) if _f(isdiffpos) is not None else None,
        sgscore=None,
        distpsnr1=None,
        classification=str(obj.get("class") or ""),
        class_probabilities=probs,
        instrument="ZTF-Cam",
        obs_group="ZTF",
        url=WEB_URL.format(oid=oid),
        cutout_urls=stamp_urls(oid, str(det.get("candid") or "")),
        properties={"object": obj, "latest_detection": det},
    )


@registry.register
class AlerceProvider(BrokerProvider):
    slug = "alerce"
    name = "ALeRCE"
    description = "ALeRCE broker (ZTF) through its public REST API; no credential needed."
    capabilities = (QUERY_ALERTS, GET_ALERT, CUTOUTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT)
    query_keys = ("classifier", "classes", "probability_min", "days", "ndet_min", "page_size")

    @property
    def api(self) -> str:
        return (self.options.get("api_url") or getattr(settings, "BROKER_ALERCE_API_URL", None) or DEFAULT_API).rstrip("/")

    def _get(self, path: str, params: Optional[Dict] = None):
        return request_json("GET", "%s/%s" % (self.api, path.lstrip("/")), params=params)

    def _items(self, params: Dict) -> List[Dict]:
        data = self._get("objects", params) or {}
        items = data.get("items") if isinstance(data, dict) else data
        return list(items or [])

    def _enrich(self, obj: Dict, *, with_lightcurve: bool = True) -> BrokerAlert:
        """Attach the latest detection and probabilities to an ``/objects`` item."""
        oid = str(obj.get("oid") or "")
        detection = None
        if with_lightcurve and oid:
            lc = self._get("objects/%s/lightcurve" % oid) or {}
            dets = [d for d in (lc.get("detections") or []) if _f(d.get("mjd")) is not None and _f(d.get("magpsf")) is not None]
            if dets:
                detection = max(dets, key=lambda d: float(d["mjd"]))
        probabilities = self._get("objects/%s/probabilities" % oid) if oid else None
        return object_to_alert(obj, detection=detection, probabilities=probabilities if isinstance(probabilities, list) else None)

    def query_alerts(self, query=None, *, since_mjd=None, limit=100) -> List[BrokerAlert]:
        query = self.validate_query(query)
        classes = query.get("classes") or list(DEFAULT_CLASSES)
        if isinstance(classes, str):
            classes = [c.strip() for c in classes.split(",") if c.strip()]
        params = {
            "classifier": query.get("classifier") or DEFAULT_CLASSIFIER,
            "class_name": list(classes),
            "page_size": int(query.get("page_size") or limit),
            "order_by": "lastmjd",
            "order_mode": "DESC",
        }
        if query.get("probability_min") is not None:
            params["probability"] = float(query["probability_min"])
        if query.get("ndet_min") is not None:
            params["ndet"] = [int(query["ndet_min"])]
        days = query.get("days")
        if since_mjd is None and days is not None:
            since_mjd = mjd_now() - float(days)
        if since_mjd is not None:
            params["lastmjd"] = [float(since_mjd)]
        return [self._enrich(obj) for obj in self._items(params)[: int(limit)]]

    def cone_search(self, ra, dec, radius_arcsec, *, limit=50) -> List[BrokerAlert]:
        params = {"ra": float(ra), "dec": float(dec), "radius": float(radius_arcsec), "page_size": int(limit)}
        return [self._enrich(obj) for obj in self._items(params)[: int(limit)]]

    def get_alert(self, object_id) -> Optional[BrokerAlert]:
        obj = self._get("objects/%s" % str(object_id).strip())
        if not obj or not isinstance(obj, dict) or not obj.get("oid"):
            return None
        return self._enrich(obj)

    def get_cutouts(self, object_id, alert_id="") -> Dict[str, str]:
        if not alert_id:
            alert = self.get_alert(object_id)
            alert_id = alert.alert_id if alert else ""
        return stamp_urls(str(object_id), str(alert_id or ""))

    def get_photometry(self, object_id) -> List[Dict]:
        lc = self._get("objects/%s/lightcurve" % str(object_id).strip()) or {}
        points = []
        for d in lc.get("detections") or []:
            band = _band(d.get("fid"))
            if band not in FID_TO_BAND.values():
                continue
            points.append(make_point(d.get("mjd"), band, d.get("magpsf"), d.get("sigmapsf"),
                                     alert_id=d.get("candid"), bad=bool(d.get("dubious"))))
        for nd in lc.get("non_detections") or []:
            band = _band(nd.get("fid"))
            if band in FID_TO_BAND.values():
                points.append(make_point(nd.get("mjd"), band, None, None, limit=nd.get("diffmaglim")))
        points = [p for p in points if p["mjd"] is not None]
        points.sort(key=lambda p: p["mjd"])
        return points
