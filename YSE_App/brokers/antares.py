"""ANTARES provider (issue #273): the client code behind ``Query_LSST.AntaresLSST``
and ``Query_ZTF.AntaresZTF`` moved behind the :class:`BrokerProvider` contract.

The ``antares_client`` import is guarded as before: the web venv can import
this module without the package; ``available()`` is then False, the crons log
that they are disabled, and the UI hides the broker's actions.

Field names follow the ``antares-client`` API fixtures (see
``docs/rubin-antares-ingest.md``): a ``Locus`` has ``locus_id``, ``ra``,
``dec``, ``properties`` (``ztf_object_id``, ``num_mag_values``,
``newest_alert_*``, ``oldest_alert_*``, ``ztf_rb`` ...), ``tags`` and a pandas
``lightcurve`` with ``alert_id, ant_mjd, ant_survey, ant_passband, ant_mag,
ant_magerr, ant_maglim`` where ``ant_survey`` is 1 (ZTF detection), 2 (ZTF
upper limit) or 4 (LSST alert).
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

from astropy.coordinates import Angle, SkyCoord

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
    mjd_now,
)

try:
    from antares_client.search import cone_search, get_by_id, search

    HAS_ANTARES = True
except ImportError:  # web venv without the broker client
    cone_search = None
    get_by_id = None
    search = None
    HAS_ANTARES = False

ZTF_SURVEY_ID = 1
ZTF_LIMIT_SURVEY_ID = 2
LSST_SURVEY_ID = 4
ZTF_BANDS = {"g": "g", "r": "r", "i": "i"}  # ANTARES writes ZTF passbands as g / R / i
LSST_BANDS = ("u", "g", "r", "i", "z", "y")
LSST_INSTRUMENT = "LSSTCam"
LSST_OBS_GROUP = "LSST"
WEB_URL = "https://antares.noirlab.edu/loci/{locus_id}"


def _finite(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _survey(value) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _rows(locus) -> List[dict]:
    lc = getattr(locus, "lightcurve", None)
    if lc is None or not len(lc):
        return []
    return lc.to_dict("records")


def lsst_loci_near(ra: float, dec: float, cfg):
    """ANTARES cone search around a position, LSST loci only (``Query_LSST`` API)."""
    from YSE_App.data_ingest.Query_LSST import is_lsst_locus

    if not HAS_ANTARES:
        return []
    sc = SkyCoord(float(ra), float(dec), unit="deg")
    return [
        locus
        for locus in cone_search(sc, Angle(cfg.cone_radius_arcsec, unit="arcsec"))
        if is_lsst_locus(locus, cfg.survey_id)
    ]


def locus_points(locus, *, lsst_cfg=None) -> List[Dict]:
    """YSE point dicts for every survey on the locus: ZTF detections and limits
    (``instrument`` ZTF-Cam) and LSST detections through
    ``Query_LSST.parse_locus_lightcurve`` (``instrument`` LSSTCam)."""
    from YSE_App.data_ingest import Query_LSST

    points = []
    for row in _rows(locus):
        survey = _survey(row.get("ant_survey"))
        mjd = _finite(row.get("ant_mjd"))
        if mjd is None or survey not in (ZTF_SURVEY_ID, ZTF_LIMIT_SURVEY_ID):
            continue
        band = ZTF_BANDS.get(str(row.get("ant_passband") or "").strip().lower())
        if band is None:
            continue
        if survey == ZTF_SURVEY_ID and _finite(row.get("ant_mag")) is not None:
            p = make_point(mjd, band, row.get("ant_mag"), row.get("ant_magerr"), alert_id=row.get("alert_id"))
        else:
            p = make_point(mjd, band, None, None, limit=row.get("ant_maglim"), alert_id=row.get("alert_id"))
        p["instrument"] = "ZTF-Cam"
        p["obs_group"] = "ZTF"
        points.append(p)
    cfg = lsst_cfg or Query_LSST.LSSTIngestConfig()
    for p in Query_LSST.parse_locus_lightcurve(locus, cfg):
        p = dict(p)
        p.setdefault("limit", None)
        p["instrument"] = LSST_INSTRUMENT
        p["obs_group"] = LSST_OBS_GROUP
        points.append(p)
    points.sort(key=lambda p: p["mjd"])
    return points


def locus_to_alert(locus, *, points: Optional[List[Dict]] = None) -> BrokerAlert:
    """Normalise one ``Locus`` (latest detection of any survey) to a :class:`BrokerAlert`."""
    props = dict(getattr(locus, "properties", None) or {})
    tags = list(getattr(locus, "tags", None) or [])
    points = points if points is not None else locus_points(locus)
    detections = [p for p in points if p.get("mag") is not None]
    latest = detections[-1] if detections else None
    first_mjd = min((p["mjd"] for p in detections), default=_finite(props.get("oldest_alert_observation_time")))
    ztf_id = props.get("ztf_object_id")
    has_lsst = any(p.get("instrument") == LSST_INSTRUMENT for p in detections)
    instrument = LSST_INSTRUMENT if has_lsst and not ztf_id else "ZTF-Cam"
    obs_group = LSST_OBS_GROUP if instrument == LSST_INSTRUMENT else "ZTF"
    isdiffpos = props.get("ztf_isdiffpos")
    return BrokerAlert(
        broker=AntaresProvider.slug,
        object_id=str(ztf_id or locus.locus_id),
        alert_id=str(props.get("newest_alert_id") or (latest or {}).get("alert_id") or ""),
        ra=float(locus.ra),
        dec=float(locus.dec),
        mjd=latest["mjd"] if latest else _finite(props.get("newest_alert_observation_time")),
        discovery_mjd=first_mjd,
        mag=latest["mag"] if latest else _finite(props.get("newest_alert_magnitude")),
        mag_err=latest["mag_err"] if latest else None,
        band=latest["band"] if latest else "",
        rb=_finite(props.get("ztf_rb")),
        drb=_finite(props.get("ztf_drb")),
        ndet=int(props["num_mag_values"]) if _finite(props.get("num_mag_values")) is not None else (len(detections) or None),
        is_positive=(str(isdiffpos).lower() in ("t", "1", "true")) if isdiffpos is not None else None,
        sgscore=_finite(props.get("ztf_sgscore1")),
        distpsnr1=_finite(props.get("ztf_distpsnr1")),
        classification=tags[0] if tags else "",
        class_probabilities={},
        instrument=instrument,
        obs_group=obs_group,
        url=WEB_URL.format(locus_id=locus.locus_id),
        properties={"locus_id": locus.locus_id, "tags": tags, "properties": _json_safe(props)},
    )


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@registry.register
class AntaresProvider(BrokerProvider):
    slug = "antares"
    name = "ANTARES"
    description = "NOIRLab ANTARES broker (ZTF and Rubin/LSST alerts) through antares-client."
    capabilities = (QUERY_ALERTS, GET_ALERT, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT, STREAM)
    requires_package = "antares_client"
    stream_format = "antares"
    stream_topics = ("extragalactic",)
    default_obs_group = "ZTF"
    default_instrument = "ZTF-Cam"
    query_keys = ("tags", "days", "rb_min", "survey", "ra_min", "ra_max", "dec_min", "dec_max")

    def available(self) -> bool:
        return HAS_ANTARES

    def unavailable_reason(self) -> str:
        return "" if HAS_ANTARES else "python package 'antares_client' is not installed"

    def _require(self):
        if not HAS_ANTARES:
            raise BrokerUnavailable("antares_client is not installed in this environment")

    # -- queries -------------------------------------------------------------
    @staticmethod
    def build_query(query: Optional[Dict], since_mjd: Optional[float] = None) -> Dict:
        """ElasticSearch document for ``antares_client.search.search`` from the
        documented ``BrokerFilter.query`` keys (``tags``, ``days``, ``rb_min``,
        ``ra_min``.. ``dec_max``)."""
        query = dict(query or {})
        must: List[Dict] = []
        days = query.get("days")
        if since_mjd is None and days is not None:
            since_mjd = mjd_now() - float(days)
        if since_mjd is not None:
            must.append({"range": {"properties.newest_alert_observation_time": {"gte": float(since_mjd)}}})
        tags = query.get("tags")
        if tags:
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            must.append({"terms": {"tags": list(tags)}})
        if query.get("rb_min") is not None:
            must.append({"range": {"properties.ztf_rb": {"gte": float(query["rb_min"])}}})
        for key, field in (("ra_min", "ra"), ("ra_max", "ra"), ("dec_min", "dec"), ("dec_max", "dec")):
            if query.get(key) is not None:
                op = "gte" if key.endswith("_min") else "lte"
                must.append({"range": {field: {op: float(query[key])}}})
        return {"query": {"bool": {"must": must}}} if must else {"query": {"match_all": {}}}

    def query_alerts(self, query=None, *, since_mjd=None, limit=100) -> List[BrokerAlert]:
        self._require()
        es_query = self.build_query(query, since_mjd)
        out = []
        try:
            for locus in search(es_query):
                out.append(locus_to_alert(locus))
                if len(out) >= int(limit):
                    break
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001 - antares_client raises its own types
            raise BrokerError("ANTARES search failed: %s" % exc) from exc
        return out

    def cone_search(self, ra, dec, radius_arcsec, *, limit=50) -> List[BrokerAlert]:
        self._require()
        sc = SkyCoord(float(ra), float(dec), unit="deg")
        out = []
        try:
            for locus in cone_search(sc, Angle(float(radius_arcsec), unit="arcsec")):
                out.append(locus_to_alert(locus))
                if len(out) >= int(limit):
                    break
        except Exception as exc:  # noqa: BLE001
            raise BrokerError("ANTARES cone search failed: %s" % exc) from exc
        return out

    def _locus(self, object_id: str):
        self._require()
        object_id = str(object_id).strip()
        try:
            if object_id.upper().startswith("ZTF"):
                loci = list(search({"query": {"term": {"properties.ztf_object_id": object_id}}}))
                return loci[0] if loci else None
            return get_by_id(object_id)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError("ANTARES lookup of %s failed: %s" % (object_id, exc)) from exc

    def get_alert(self, object_id) -> Optional[BrokerAlert]:
        locus = self._locus(object_id)
        return locus_to_alert(locus) if locus is not None else None

    def get_photometry(self, object_id) -> List[Dict]:
        locus = self._locus(object_id)
        return locus_points(locus) if locus is not None else []

    def parse_stream_message(self, topic, message, *, key=None) -> Optional[BrokerAlert]:
        """``antares_client.StreamingClient`` yields ``Locus`` objects; a JSON mirror of a
        locus (``locus_id``, ``ra``, ``dec``, ``properties``, ``tags``) is accepted too."""
        if message is None:
            return None
        if isinstance(message, dict):
            if message.get("locus_id") is None or message.get("ra") is None:
                return None
            message = _DictLocus(message)
        return locus_to_alert(message)


class _DictLocus:
    """Attribute view of a locus serialised as JSON (no lightcurve)."""

    def __init__(self, data: Dict):
        self.locus_id = data.get("locus_id")
        self.ra = data.get("ra")
        self.dec = data.get("dec")
        self.properties = dict(data.get("properties") or {})
        self.tags = list(data.get("tags") or [])
        self.lightcurve = None


def loci_to_alerts(loci: Iterable) -> List[BrokerAlert]:
    return [locus_to_alert(locus) for locus in loci]
