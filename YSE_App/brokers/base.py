"""Broker provider contract (issue #273, part of #272).

A *provider* wraps one alert broker (ANTARES, Fink, ALeRCE, ...) behind the
SkyPortal-style capability set so the rest of YSE-PZ, the ingest job, the
candidate page and the API, can talk to any broker through one interface and
show only the actions a broker implements. The design follows SkyPortal's
``doc/broker_plugins.md`` in shape only; no code is copied.

Every provider normalises broker payloads into :class:`BrokerAlert`, a flat
record with the handful of fields the filter evaluator
(:mod:`YSE_App.brokers.filters`) understands, plus the raw payload.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional

from astropy.coordinates import SkyCoord
from astropy.time import Time

# --- capabilities -----------------------------------------------------------------

QUERY_ALERTS = "query_alerts"          # recent alerts matching a broker-side query (polling)
GET_ALERT = "get_alert"                # one object / alert by id
CUTOUTS = "cutouts"                    # science / template / difference stamps
CONE_SEARCH = "cone_search"            # objects near a position
PHOTOMETRY = "photometry"              # light curve as YSE point dicts
SAVE_AS_TRANSIENT = "save_as_transient"  # create a YSE-PZ Transient from an object
FILTER_CRUD = "filter_crud"            # broker-side saved filters (streams / topics)

ALL_CAPABILITIES = (
    QUERY_ALERTS, GET_ALERT, CUTOUTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT, FILTER_CRUD,
)

CAPABILITY_LABELS = {
    QUERY_ALERTS: "Query alerts",
    GET_ALERT: "Fetch alert",
    CUTOUTS: "Cutouts",
    CONE_SEARCH: "Cone search",
    PHOTOMETRY: "Photometry",
    SAVE_AS_TRANSIENT: "Save as transient",
    FILTER_CRUD: "Filter CRUD",
}


class BrokerError(Exception):
    """A broker call failed (network, HTTP status, unexpected payload)."""


class BrokerUnavailable(BrokerError):
    """The provider cannot run here (missing client package or credential)."""


class CapabilityNotSupported(BrokerError):
    """The provider does not implement the requested capability."""


# --- helpers ----------------------------------------------------------------------

def _finite(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mjd_now() -> float:
    return float(Time(datetime.datetime.now(datetime.timezone.utc)).mjd)


def galactic_latitude(ra, dec) -> Optional[float]:
    """Galactic latitude b in degrees, or None when the position is missing."""
    ra, dec = _finite(ra), _finite(dec)
    if ra is None or dec is None:
        return None
    return float(SkyCoord(ra, dec, unit="deg", frame="icrs").galactic.b.deg)


def mjd_to_datetime(mjd) -> Optional[datetime.datetime]:
    mjd = _finite(mjd)
    if mjd is None:
        return None
    dt = Time(mjd, format="mjd", scale="utc").to_datetime()
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


# --- normalised alert -------------------------------------------------------------

@dataclass
class BrokerAlert:
    """One broker object at the time of its latest alert, in YSE-PZ terms.

    ``object_id`` is the broker's stable object name (ZTF name, ANTARES locus
    id); ``alert_id`` the latest alert / candidate id when the broker has one.
    Magnitudes are AB PSF magnitudes of the latest detection. ``properties``
    keeps the raw broker record for the ``Candidate.payload`` column.
    """

    broker: str
    object_id: str
    ra: float
    dec: float
    alert_id: str = ""
    mjd: Optional[float] = None               # time of the latest detection
    discovery_mjd: Optional[float] = None     # first detection on the broker
    mag: Optional[float] = None
    mag_err: Optional[float] = None
    band: str = ""                            # short YSE band name: g, r, i, ...
    rb: Optional[float] = None                # real/bogus score (ZTF rb)
    drb: Optional[float] = None               # deep-learning real/bogus (ZTF drb)
    ndet: Optional[int] = None                # number of detections
    is_positive: Optional[bool] = None        # positive difference flux
    sgscore: Optional[float] = None           # star/galaxy score of the nearest PS1 source
    distpsnr1: Optional[float] = None         # distance to the nearest PS1 source, arcsec
    classification: str = ""                  # broker's best class label
    class_probabilities: Dict[str, float] = field(default_factory=dict)
    instrument: str = "ZTF-Cam"               # YSE Instrument name for photometry passthrough
    obs_group: str = "ZTF"                    # YSE ObservationGroup name for the saved transient
    url: str = ""                             # broker web page for the object
    cutout_urls: Dict[str, str] = field(default_factory=dict)  # science/template/difference
    properties: Dict = field(default_factory=dict)

    # -- derived -----------------------------------------------------------
    @property
    def gal_b(self) -> Optional[float]:
        return galactic_latitude(self.ra, self.dec)

    def age_days(self, now_mjd: Optional[float] = None) -> Optional[float]:
        if self.discovery_mjd is None:
            return None
        return (now_mjd if now_mjd is not None else mjd_now()) - float(self.discovery_mjd)

    @property
    def discovery_date(self) -> Optional[datetime.datetime]:
        return mjd_to_datetime(self.discovery_mjd)

    def to_dict(self) -> Dict:
        """JSON-safe dict (``Candidate.payload``): dataclass fields plus derived values."""
        d = asdict(self)
        d["gal_b"] = self.gal_b
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "BrokerAlert":
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})

    def filter_values(self, now_mjd: Optional[float] = None) -> Dict:
        """The fields the rule evaluator sees (see :mod:`YSE_App.brokers.filters`)."""
        return {
            "mag": self.mag,
            "mag_err": self.mag_err,
            "band": self.band,
            "rb": self.rb,
            "drb": self.drb,
            "ndet": self.ndet,
            "gal_b": self.gal_b,
            "ra": self.ra,
            "dec": self.dec,
            "age_days": self.age_days(now_mjd),
            "is_positive": self.is_positive,
            "sgscore": self.sgscore,
            "distpsnr1": self.distpsnr1,
            "classification": self.classification,
            "class_probabilities": dict(self.class_probabilities or {}),
        }


# --- provider ---------------------------------------------------------------------

class BrokerProvider:
    """Base class: subclasses set ``slug``/``name``, list ``capabilities`` and
    implement the matching methods. Unimplemented capability methods raise
    :class:`CapabilityNotSupported` so callers can rely on ``capabilities()``.
    """

    slug: str = ""
    name: str = ""
    description: str = ""
    capabilities: Iterable[str] = ()
    #: Optional[str]: pip package the provider needs; ``available()`` checks it.
    requires_package: Optional[str] = None
    #: bool: the provider needs an EncryptedCredential(service=slug) to work.
    requires_credential: bool = False
    #: Default YSE ObservationGroup / Instrument for objects from this broker.
    default_obs_group: str = "ZTF"
    default_instrument: str = "ZTF-Cam"
    #: Keys allowed in ``BrokerFilter.query`` for this provider (documentation + validation).
    query_keys: Iterable[str] = ()

    def __init__(self, credential: Optional[Dict] = None, options: Optional[Dict] = None):
        self.credential = credential or {}
        self.options = options or {}

    # -- introspection -----------------------------------------------------
    @classmethod
    def capability_set(cls) -> List[str]:
        return [c for c in ALL_CAPABILITIES if c in set(cls.capabilities)]

    def capabilities_list(self) -> List[str]:
        return self.capability_set()

    def has(self, capability: str) -> bool:
        return capability in set(self.capabilities)

    def available(self) -> bool:
        """True when the provider can run in this process (deps + credential)."""
        if self.requires_package:
            import importlib.util
            if importlib.util.find_spec(self.requires_package) is None:
                return False
        if self.requires_credential and not self.credential:
            return False
        return True

    def unavailable_reason(self) -> str:
        if self.requires_package:
            import importlib.util
            if importlib.util.find_spec(self.requires_package) is None:
                return "python package %r is not installed" % self.requires_package
        if self.requires_credential and not self.credential:
            return "no active EncryptedCredential with service=%r" % self.slug
        return ""

    def describe(self) -> Dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capability_set(),
            "available": self.available(),
            "unavailable_reason": self.unavailable_reason(),
            "query_keys": list(self.query_keys),
        }

    def _unsupported(self, capability):
        raise CapabilityNotSupported("%s does not support %s" % (self.slug, capability))

    # -- capability methods (override the ones listed in ``capabilities``) -
    def query_alerts(self, query: Optional[Dict] = None, *, since_mjd: Optional[float] = None,
                     limit: int = 100) -> List[BrokerAlert]:
        """Recent objects matching a broker-side ``query`` (see ``query_keys``)."""
        self._unsupported(QUERY_ALERTS)

    def get_alert(self, object_id: str) -> Optional[BrokerAlert]:
        self._unsupported(GET_ALERT)

    def get_cutouts(self, object_id: str, alert_id: str = "") -> Dict[str, str]:
        """``{'science': url, 'template': url, 'difference': url}`` (any subset)."""
        self._unsupported(CUTOUTS)

    def cone_search(self, ra: float, dec: float, radius_arcsec: float, *,
                    limit: int = 50) -> List[BrokerAlert]:
        self._unsupported(CONE_SEARCH)

    def get_photometry(self, object_id: str) -> List[Dict]:
        """Light curve as the point dicts ``Query_LSST.parse_locus_lightcurve`` produces
        (``mjd, obs_date, band, mag, mag_err, flux, flux_err, flux_zero_point, forced,
        diffim, bad, alert_id``) plus ``limit`` for non-detections (``mag`` None)."""
        self._unsupported(PHOTOMETRY)

    def save_as_transient(self, alert: BrokerAlert, user, *, status: str = "New",
                          obs_group: Optional[str] = None, import_photometry: bool = True):
        """Create (or find) the YSE-PZ ``Transient`` for ``alert``; see
        :func:`YSE_App.brokers.ingest.save_alert_as_transient`."""
        if not self.has(SAVE_AS_TRANSIENT):
            self._unsupported(SAVE_AS_TRANSIENT)
        from YSE_App.brokers.ingest import save_alert_as_transient

        return save_alert_as_transient(
            self, alert, user, status=status, obs_group=obs_group, import_photometry=import_photometry
        )

    def list_filters(self) -> List[Dict]:
        self._unsupported(FILTER_CRUD)

    def create_filter(self, definition: Dict) -> Dict:
        self._unsupported(FILTER_CRUD)

    def update_filter(self, filter_id: str, definition: Dict) -> Dict:
        self._unsupported(FILTER_CRUD)

    def delete_filter(self, filter_id: str) -> None:
        self._unsupported(FILTER_CRUD)

    def validate_query(self, query: Optional[Dict]) -> Dict:
        """Reject unknown keys in a ``BrokerFilter.query`` for this provider."""
        query = dict(query or {})
        unknown = sorted(set(query) - set(self.query_keys))
        if unknown:
            raise ValueError("unknown %s query key(s): %s" % (self.slug, ", ".join(unknown)))
        return query

    def __repr__(self):
        return "<%s %s %s>" % (type(self).__name__, self.slug, self.capability_set())


# --- shared photometry helpers ------------------------------------------------------

FLUX_ZERO_POINT = 27.5  # the AB zero point the ZTF / LSST ingests store flux at


def make_point(mjd, band, mag, mag_err, *, limit=None, alert_id=None, bad=False,
               forced=False, diffim=True) -> Dict:
    """One YSE photometry point dict (detection when ``mag`` is finite, else a limit)."""
    mjd = _finite(mjd)
    mag = _finite(mag)
    mag_err = _finite(mag_err)
    limit = _finite(limit)
    flux = flux_err = None
    if mag is not None:
        flux = 10 ** (-0.4 * (mag - FLUX_ZERO_POINT))
        if mag_err is not None:
            flux_err = 0.4 * math.log(10) * flux * mag_err
    return {
        "mjd": mjd,
        "obs_date": mjd_to_datetime(mjd),
        "band": band,
        "mag": mag,
        "mag_err": mag_err,
        "limit": limit,
        "flux": flux,
        "flux_err": flux_err,
        "flux_zero_point": FLUX_ZERO_POINT,
        "forced": bool(forced),
        "diffim": bool(diffim),
        "bad": bool(bad),
        "alert_id": str(alert_id) if alert_id is not None else None,
    }


def points_to_upload_blocks(points: Iterable[Dict], instrument: str, obs_group: str,
                            *, bad_quality_name: str = "Bad") -> Dict[str, Dict]:
    """``/add_transient``-shaped ``transientphotometry`` value for ``points``.

    One block per instrument (a point may carry its own ``instrument`` /
    ``obs_group`` keys, e.g. an ANTARES locus with both ZTF and LSST rows;
    otherwise the defaults apply). Detections only: limits carry no ``mag``
    and the upload path does not store them.
    """
    blocks: Dict[str, Dict] = {}
    detections = sorted(
        (p for p in points if p.get("mag") is not None and p.get("obs_date")), key=lambda p: p["mjd"]
    )
    for i, p in enumerate(detections):
        inst = p.get("instrument") or instrument
        block = blocks.setdefault(inst, {"instrument": inst, "obs_group": p.get("obs_group") or obs_group,
                                         "photdata": {}})
        first = not block["photdata"]
        block["photdata"]["%s_%i" % (p["obs_date"].strftime("%Y-%m-%dT%H:%M:%S"), i)] = {
            "obs_date": p["obs_date"].strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "band": p["band"],
            "groups": [],
            "mag": p["mag"],
            "mag_err": p["mag_err"],
            "flux": p["flux"],
            "flux_err": p["flux_err"],
            "data_quality": bad_quality_name if p.get("bad") else 0,
            "forced": 1 if p.get("forced") else 0,
            "flux_zero_point": p["flux_zero_point"],
            "discovery_point": 1 if first else 0,
            "diffim": 1 if p.get("diffim", True) else 0,
        }
    return blocks
