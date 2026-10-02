"""Feed providers: non-optical report streams read on a schedule (#280).

A *feed provider* is a :class:`~YSE_App.brokers.base.BrokerProvider` whose
input is a configured ``FeedSource`` row instead of a broker query: a Hermes /
SCiMMA topic (#281), the Einstein Probe WXT alert stream (#282) or the JPL
Scout NEO list (#283). Registering the providers with the broker registry
means the candidate page, its save / reject actions and the API treat a feed
candidate like any broker candidate: ``Candidate.broker`` is the feed slug and
``save_candidate`` goes through the provider's ``save_as_transient``.

Every message is normalised into :class:`FeedMessage`; :meth:`FeedProvider.run`
polls a source, evaluates the source's optional ``criteria`` on each message
and either saves it as a transient, upserts a candidate, or, for a known
object, links the name and imports what the message carries. The concrete
providers only implement ``poll`` (and ``consume`` for the Kafka-backed ones)
plus the per-kind hooks.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from django.conf import settings
from django.utils import timezone

from YSE_App.brokers.base import (
    GET_ALERT,
    PHOTOMETRY,
    SAVE_AS_TRANSIENT,
    BrokerAlert,
    BrokerError,
    BrokerProvider,
    make_point,
    mjd_now,
)

logger = logging.getLogger(__name__)

USER_AGENT = "YSE-PZ feeds/1.0"

KIND_DISCOVERY = "discovery"
KIND_CLASSIFICATION = "classification"
KIND_PHOTOMETRY = "photometry"
KIND_ALERT = "alert"          # high-energy alert with an error circle (Einstein Probe)
KIND_NEO = "neo"              # moving-object candidate (Scout)
KIND_OTHER = "other"


class FeedError(BrokerError):
    """A feed call failed (network, HTTP status, unexpected payload)."""


# --- helpers ----------------------------------------------------------------------

def http_timeout() -> float:
    return float(getattr(settings, "FEEDS_HTTP_TIMEOUT_SECONDS", 30) or 30)


def num(value) -> Optional[float]:
    """A finite float, or None for null / non-numeric / NaN values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


_SEXAGESIMAL = re.compile(r"^\s*[+-]?\d{1,3}[:\s]\d{1,2}[:\s]\d{1,2}(\.\d*)?\s*$")


def parse_coord(ra, dec) -> Tuple[Optional[float], Optional[float]]:
    """``(ra_deg, dec_deg)`` from decimal degrees or sexagesimal strings (RA in hours)."""
    ra_f, dec_f = num(ra), num(dec)
    if ra_f is not None and dec_f is not None:
        return ra_f, dec_f
    if isinstance(ra, str) and isinstance(dec, str) and _SEXAGESIMAL.match(ra) and _SEXAGESIMAL.match(dec):
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        try:
            coord = SkyCoord(ra.strip().replace(":", " "), dec.strip().replace(":", " "), unit=(u.hourangle, u.deg))
        except (ValueError, TypeError):
            return None, None
        return float(coord.ra.deg), float(coord.dec.deg)
    return None, None


def parse_time_mjd(value) -> Optional[float]:
    """MJD from an ISO string, a datetime, a JD (> 2400000) or an MJD."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        from astropy.time import Time

        dt = value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
        return float(Time(dt).mjd)
    f = num(value)
    if f is not None:
        return f - 2400000.5 if f > 2400000.0 else f
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    except ValueError:
        try:
            from astropy.time import Time

            return float(Time(str(value).strip()).mjd)
        except Exception:  # noqa: BLE001 - not a date
            return None
    return parse_time_mjd(dt)


def mjd_to_iso(mjd) -> str:
    from YSE_App.brokers.base import mjd_to_datetime

    dt = mjd_to_datetime(mjd)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


def separation_arcsec(ra1, dec1, ra2, dec2) -> Optional[float]:
    ra1, dec1, ra2, dec2 = (num(ra1), num(dec1), num(ra2), num(dec2))
    if None in (ra1, dec1, ra2, dec2):
        return None
    r1, d1, r2, d2 = (math.radians(v) for v in (ra1, dec1, ra2, dec2))
    cos_sep = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return round(math.degrees(math.acos(max(-1.0, min(1.0, cos_sep)))) * 3600.0, 3)


def http_get_json(url: str, *, params: Optional[Dict] = None, headers: Optional[Dict] = None,
                  timeout: Optional[float] = None):
    """GET ``url`` and return the decoded JSON body; :class:`FeedError` on any failure."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    try:
        response = requests.get(url, params=params or None, headers=hdrs, timeout=timeout or http_timeout())
    except requests.RequestException as exc:
        raise FeedError("%s: %s" % (url, exc.__class__.__name__ if not str(exc) else exc))
    if response.status_code >= 400:
        raise FeedError("HTTP %s from %s: %s" % (response.status_code, url, (response.text or "")[:200]))
    try:
        return response.json()
    except ValueError:
        raise FeedError("%s did not answer with JSON: %s" % (url, (response.text or "")[:200]))


# --- normalised message -----------------------------------------------------------

@dataclass
class FeedMessage:
    """One report from a feed about one object, in YSE-PZ terms."""

    feed: str                                   # provider slug: hermes, ep, scout
    message_id: str                             # stable id of the message / notice
    kind: str                                   # KIND_* above
    object_id: str                              # object name (target, EP id, temporary designation)
    ra: Optional[float] = None
    dec: Optional[float] = None
    error_radius_arcsec: Optional[float] = None
    time_mjd: Optional[float] = None            # discovery / trigger time
    reporter: str = ""                          # reporting group or submitter
    classification: str = ""
    redshift: Optional[float] = None
    mag: Optional[float] = None                 # latest reported brightness
    band: str = ""
    score: Optional[float] = None               # 0-1 confidence-like score (Scout neoScore / 100)
    photometry: List[Dict] = field(default_factory=list)   # make_point dicts
    aliases: List[str] = field(default_factory=list)
    url: str = ""
    title: str = ""
    text: str = ""
    raw: Dict = field(default_factory=dict)

    @property
    def has_position(self) -> bool:
        return self.ra is not None and self.dec is not None

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_alert(self, *, obs_group: str, instrument: str) -> BrokerAlert:
        """The :class:`BrokerAlert` the candidate table and the save path understand."""
        detections = sorted((p for p in self.photometry if p.get("mag") is not None), key=lambda p: p["mjd"] or 0)
        latest = detections[-1] if detections else None
        first_mjd = detections[0]["mjd"] if detections else None
        props = dict(self.raw or {})
        props.update({
            "feed_kind": self.kind,
            "message_id": self.message_id,
            "reporter": self.reporter,
            "error_radius_arcsec": self.error_radius_arcsec,
            "redshift": self.redshift,
            "aliases": list(self.aliases),
            "title": self.title,
            "text": (self.text or "")[:2000],
            "photometry": [
                {k: (v.isoformat() if isinstance(v, datetime.datetime) else v) for k, v in p.items()}
                for p in self.photometry
            ],
        })
        return BrokerAlert(
            broker=self.feed,
            object_id=self.object_id,
            ra=float(self.ra) if self.ra is not None else 0.0,
            dec=float(self.dec) if self.dec is not None else 0.0,
            alert_id=self.message_id,
            mjd=(latest["mjd"] if latest else self.time_mjd),
            discovery_mjd=self.time_mjd if self.time_mjd is not None else first_mjd,
            mag=self.mag if self.mag is not None else (latest["mag"] if latest else None),
            mag_err=(latest["mag_err"] if latest else None),
            band=self.band or (latest["band"] if latest else ""),
            rb=self.score,
            ndet=len(detections) or None,
            classification=self.classification or "",
            instrument=instrument,
            obs_group=obs_group,
            url=self.url,
            properties=props,
        )


def points_from_payload(payload: Dict) -> List[Dict]:
    """Rebuild ``make_point`` dicts from the JSON-safe copy stored on a candidate."""
    out = []
    for p in (payload or {}).get("photometry") or []:
        out.append(make_point(p.get("mjd"), p.get("band") or "", p.get("mag"), p.get("mag_err"),
                              limit=p.get("limit"), alert_id=p.get("alert_id"), forced=p.get("forced", False),
                              diffim=p.get("diffim", True)))
    return out


# --- provider ---------------------------------------------------------------------

class FeedProvider(BrokerProvider):
    """Base class of the feed providers; subclasses implement :meth:`poll`.

    ``options`` is the ``FeedSource.config`` dict; ``credential`` the decrypted
    secret of the source's ``EncryptedCredential`` (``{}`` when there is none).
    """

    capabilities = (GET_ALERT, PHOTOMETRY, SAVE_AS_TRANSIENT)
    #: FeedSource.kind this provider serves.
    feed_kind: str = ""
    #: InformationSource name for provenance rows (TransientWebResource).
    information_source: str = ""
    #: Save passing messages as transients at once (else they become candidates).
    default_auto_save: bool = False
    #: Keys accepted in ``FeedSource.config`` (documentation + admin validation).
    config_keys: Iterable[str] = ("criteria", "auto_save", "save_status", "save_obs_group", "match_radius_arcsec",
                                  "import_photometry", "max_per_run", "comment")
    #: Optional Kafka client package for :meth:`consume`.
    consumer_package: Optional[str] = None

    def __init__(self, credential: Optional[Dict] = None, options: Optional[Dict] = None, source=None):
        super().__init__(credential=credential, options=options)
        self.source = source
        self._photometry: Dict[str, List[Dict]] = {}
        self._alerts: Dict[str, BrokerAlert] = {}

    @classmethod
    def for_source(cls, source) -> "FeedProvider":
        return cls(credential=source.secret(), options=source.config_dict(), source=source)

    # -- configuration -------------------------------------------------------
    def option(self, key, default=None):
        value = (self.options or {}).get(key)
        return default if value is None else value

    def auto_save(self) -> bool:
        return bool(self.option("auto_save", self.default_auto_save))

    def match_radius(self) -> float:
        from YSE_App.brokers.ingest import match_radius_arcsec

        return float(self.option("match_radius_arcsec", match_radius_arcsec()))

    def max_per_run(self) -> int:
        return int(self.option("max_per_run", 200) or 200)

    def validate_config(self, config: Optional[Dict]) -> Dict:
        """Reject unknown keys and malformed criteria; returns a normalised copy."""
        from YSE_App.brokers.filters import validate_criteria

        config = dict(config or {})
        unknown = sorted(set(config) - set(self.config_keys))
        if unknown:
            raise ValueError("unknown %s config key(s): %s (known: %s)" % (
                self.slug, ", ".join(unknown), ", ".join(sorted(self.config_keys))))
        if "criteria" in config:
            config["criteria"] = validate_criteria(config.get("criteria"))
        return config

    def can_consume(self) -> bool:
        if not self.consumer_package:
            return False
        import importlib.util

        return importlib.util.find_spec(self.consumer_package) is not None

    def describe(self) -> Dict:
        d = super().describe()
        d.update({"feed_kind": self.feed_kind, "config_keys": sorted(self.config_keys),
                  "can_consume": self.can_consume()})
        return d

    # -- feed-specific hooks ---------------------------------------------------
    def poll(self, source, *, since: Optional[datetime.datetime] = None) -> List[FeedMessage]:
        """Messages published since ``since`` (or the provider's default window)."""
        raise NotImplementedError

    def consume(self, source, *, max_messages: int = 100, timeout_seconds: float = 30.0) -> List[FeedMessage]:
        """Read from the stream (Kafka) until ``max_messages`` or ``timeout_seconds``."""
        raise FeedError("%s has no stream consumer" % self.slug)

    def on_linked(self, source, message: FeedMessage, transient, user) -> None:
        """Hook after a message was matched to an existing transient (classification, annotations)."""

    def on_created(self, source, message: FeedMessage, transient, user) -> None:
        """Hook after a message created a new transient."""

    def on_candidate(self, source, message: FeedMessage, candidate, user) -> None:
        """Hook after a message was recorded as a candidate (cross-matching, annotations)."""

    # -- BrokerProvider capabilities -------------------------------------------
    def get_alert(self, object_id: str) -> Optional[BrokerAlert]:
        alert = self._alerts.get(object_id)
        if alert is None:
            from YSE_App.models.candidate_models import Candidate

            candidate = Candidate.objects.filter(broker=self.slug, alert_id=object_id).first()
            alert = candidate.alert if candidate is not None else None
        return alert

    def get_photometry(self, object_id: str) -> List[Dict]:
        """Photometry the message carried (kept in memory for this run, else from the candidate payload)."""
        if object_id in self._photometry:
            return list(self._photometry[object_id])
        alert = self.get_alert(object_id)
        return points_from_payload(alert.properties) if alert is not None else []

    def remember(self, message: FeedMessage, alert: BrokerAlert) -> None:
        self._alerts[alert.object_id] = alert
        self._photometry[alert.object_id] = list(message.photometry)

    # -- the poll loop ---------------------------------------------------------
    def run(self, source, *, user=None, dry_run: bool = False, messages: Optional[List[FeedMessage]] = None) -> Dict:
        """Poll ``source`` (or process ``messages``), handle each, record the outcome on the source."""
        from YSE_App.brokers.filters import evaluate
        from YSE_App.brokers.ingest import audit_user

        user = audit_user(user or getattr(source, "created_by", None))
        result = {"source": source.slug, "fetched": 0, "passed": 0, "created": 0, "linked": 0, "candidates": 0,
                  "skipped": 0, "errors": 0}
        error = ""
        try:
            if messages is None:
                messages = self.poll(source, since=source.last_polled)
            messages = list(messages)[: self.max_per_run()]
            result["fetched"] = len(messages)
            criteria = self.option("criteria") or {}
            now_mjd = mjd_now()
            for message in messages:
                try:
                    if not message.has_position:
                        result["skipped"] += 1
                        continue
                    alert = message.to_alert(obs_group=self.obs_group_for(message), instrument=self.default_instrument)
                    if criteria:
                        passed, _failed = evaluate(criteria, alert, now_mjd=now_mjd)
                        if not passed:
                            result["skipped"] += 1
                            continue
                    result["passed"] += 1
                    if dry_run:
                        continue
                    outcome = self.handle(source, message, alert, user)
                    result[outcome] += 1
                except Exception as exc:  # noqa: BLE001 - one bad message must not stop the poll
                    result["errors"] += 1
                    logger.exception("feed %s: message %s failed: %s", source.slug, message.message_id, exc)
        except FeedError as exc:
            error = str(exc)
            logger.warning("feed %s poll failed: %s", source.slug, exc)
        summary = ("fetched %(fetched)d, passed %(passed)d, created %(created)d, linked %(linked)d, "
                   "candidates %(candidates)d, skipped %(skipped)d, errors %(errors)d" % result)
        if error:
            summary = "failed: %s" % error[:200]
        result["summary"] = summary
        result["error"] = error
        if not dry_run:
            source.record_poll(summary, error)
        return result

    def obs_group_for(self, message: FeedMessage) -> str:
        return self.option("save_obs_group") or message.reporter or self.default_obs_group

    def handle(self, source, message: FeedMessage, alert: BrokerAlert, user) -> str:
        """Route one message; returns the counter it belongs to."""
        from YSE_App.brokers import ingest

        self.remember(message, alert)
        transient = self.known_transient(message, alert)
        if transient is not None:
            ingest.link_existing_transient(alert, transient, user, obs_group=alert.obs_group)
            self.add_web_resource(transient, message, user)
            if message.photometry and self.option("import_photometry", True):
                try:
                    ingest.import_photometry_for(self, transient, alert, user)
                except BrokerError as exc:
                    logger.warning("feed %s: photometry for %s not imported: %s", source.slug, transient.name, exc)
            self.on_linked(source, message, transient, user)
            self.mark_candidate_saved(alert, transient, user)
            return "linked"
        if self.auto_save() and message.kind in (KIND_DISCOVERY, KIND_CLASSIFICATION, KIND_PHOTOMETRY):
            transient = self.save_as_transient(
                alert, user, status=self.option("save_status", "New"), obs_group=alert.obs_group,
                import_photometry=bool(self.option("import_photometry", True)))
            self.add_web_resource(transient, message, user)
            self.on_created(source, message, transient, user)
            self.on_linked(source, message, transient, user)
            return "created"
        candidate = ingest.upsert_candidate(alert, [])
        self.on_candidate(source, message, candidate, user)
        return "candidates"

    def known_transient(self, message: FeedMessage, alert: BrokerAlert):
        """Existing transient by name / alias (message aliases included), else by position."""
        from YSE_App.brokers.ingest import nearby_transients
        from YSE_App.models.transient_models import AlternateTransientNames, Transient

        names = [message.object_id] + [a for a in message.aliases if a]
        t = Transient.objects.filter(name__in=names).first()
        if t is None:
            alias = AlternateTransientNames.objects.filter(name__in=names).select_related("transient").first()
            if alias is not None:
                t = alias.transient
        if t is None and self.match_radius() > 0:
            near = nearby_transients(alert.ra, alert.dec, self.match_radius())
            t = near[0] if near else None
        return t

    def mark_candidate_saved(self, alert: BrokerAlert, transient, user) -> None:
        from YSE_App.models.candidate_models import Candidate

        candidate = Candidate.objects.filter(broker=self.slug, alert_id=alert.object_id).first()
        if candidate is not None and candidate.status == Candidate.NEW:
            candidate.set_status(Candidate.SAVED, user, transient=transient, note="matched by feed %s" % self.slug)

    def add_web_resource(self, transient, message: FeedMessage, user) -> None:
        """Provenance: an InformationSource row for the feed and a link back to the message."""
        if not message.url or not self.information_source:
            return
        from YSE_App.models.additional_info_models import TransientWebResource
        from YSE_App.models.enum_models import InformationSource

        source, _ = InformationSource.objects.get_or_create(
            name=self.information_source, defaults={"created_by_id": user.id, "modified_by_id": user.id})
        if TransientWebResource.objects.filter(transient=transient, resource_url=message.url).exists():
            return
        TransientWebResource.objects.create(
            transient=transient, information_source=source, resource_url=message.url[:512],
            information_text=(message.title or "%s message %s" % (self.name, message.message_id))[:1000],
            created_by_id=user.id, modified_by_id=user.id)

    def add_comment(self, transient, text: str, user) -> None:
        from YSE_App.models.log_models import Log

        if not Log.objects.filter(transient=transient, comment=text).exists():
            Log.objects.create(transient=transient, comment=text, created_by_id=user.id, modified_by_id=user.id)


def since_default(hours: float) -> datetime.datetime:
    return timezone.now() - datetime.timedelta(hours=float(hours))
