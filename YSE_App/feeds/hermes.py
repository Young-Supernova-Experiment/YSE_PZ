"""Hermes / SCiMMA feed (#281) and the Hermes publishing client (#326).

Hermes (hermes.lco.global) is the web front of the SCiMMA Hopskotch Kafka
broker: every message it publishes on a topic is also served by its REST API,
so the feed polls ``GET /api/v0/messages/?topic=<topic>`` from a queued job
(no long-lived consumer needed) and, when ``hop-client`` is installed and the
credential carries ``hop_username`` / ``hop_password``, can also read the
topic straight from Kafka (:meth:`HermesFeed.consume`). Publishing goes
through ``POST /api/v0/submit_message/`` with the Hermes API token.

Hermes messages carry a ``data`` document with ``targets`` (name, ra, dec,
discovery_info, redshift, aliases), ``photometry`` (per target: date_obs,
bandpass, brightness, brightness_error, brightness_unit, limiting_brightness)
and ``spectroscopy`` (classification, redshift); the TNS mirror topics use the
same layout. Each target becomes one :class:`FeedMessage`.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Dict, Iterable, List, Optional

import requests
from django.conf import settings

from YSE_App.brokers import register
from YSE_App.brokers.base import BrokerAlert, make_point
from YSE_App.feeds.base import (
    KIND_CLASSIFICATION,
    KIND_DISCOVERY,
    KIND_OTHER,
    KIND_PHOTOMETRY,
    USER_AGENT,
    FeedError,
    FeedMessage,
    FeedProvider,
    http_get_json,
    http_timeout,
    num,
    parse_coord,
    parse_time_mjd,
    since_default,
)

logger = logging.getLogger(__name__)

SLUG = "hermes"
DEFAULT_API_URL = "https://hermes.lco.global/api/v0"
DEFAULT_KAFKA_URL = "kafka://kafka.scimma.org/"
TEST_TOPIC = "hermes.test"
PAGE_SIZE = 100

# Bandpass names Hermes / TNS use -> YSE band names.
BAND_ALIASES = {"g-ztf": "g", "r-ztf": "r", "i-ztf": "i", "zg": "g", "zr": "r", "zi": "i", "orange": "o", "cyan": "c",
                "w": "w", "clear": "clear", "unfiltered": "clear", "g-sloan": "g", "r-sloan": "r", "i-sloan": "i",
                "z-sloan": "z", "u-sloan": "u"}


def api_url() -> str:
    return (getattr(settings, "FEEDS_HERMES_API_URL", "") or DEFAULT_API_URL).rstrip("/")


def kafka_url() -> str:
    return getattr(settings, "FEEDS_HERMES_KAFKA_URL", "") or DEFAULT_KAFKA_URL


def _token(credential: Optional[Dict]) -> str:
    credential = credential or {}
    return str(credential.get("hermes_token") or credential.get("token") or credential.get("api_token") or "").strip()


def message_url(message: Dict) -> str:
    uuid = message.get("uuid") or message.get("id")
    if not uuid:
        return ""
    base = api_url()
    base = base[: base.index("/api")] if "/api" in base else base
    return "%s/message/%s" % (base, uuid)


# --- REST client ------------------------------------------------------------------

class HermesClient:
    """Thin client of the Hermes REST API (list messages by topic, submit a message)."""

    def __init__(self, token: str = "", base_url: Optional[str] = None, timeout: Optional[float] = None):
        self.token = token or ""
        self.base_url = (base_url or api_url()).rstrip("/")
        self.timeout = timeout or http_timeout()

    def _headers(self) -> Dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Token %s" % self.token
        return headers

    def list_messages(self, topic: str, *, since: Optional[datetime.datetime] = None, limit: int = PAGE_SIZE,
                      max_pages: int = 10) -> List[Dict]:
        """Messages on ``topic`` published after ``since`` (newest first as Hermes returns them)."""
        params = {"topic": topic, "limit": int(limit), "ordering": "-published"}
        if since is not None:
            params["published_after"] = since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = "%s/messages/" % self.base_url
        out: List[Dict] = []
        for _ in range(max_pages):
            body = http_get_json(url, params=params, headers=self._headers(), timeout=self.timeout)
            if isinstance(body, list):
                out.extend(m for m in body if isinstance(m, dict))
                break
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list):
                raise FeedError("unexpected Hermes payload from %s (no results list)" % url)
            out.extend(m for m in results if isinstance(m, dict))
            url = body.get("next")
            params = None
            if not url:
                break
        return out

    def submit(self, message: Dict) -> Dict:
        """``POST submit_message/``; returns the created message (with its ``uuid``)."""
        if not self.token:
            raise FeedError("Hermes publishing needs an API token in the credential (hermes_token)")
        url = "%s/submit_message/" % self.base_url
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        try:
            response = requests.post(url, data=json.dumps(message, default=str), headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FeedError("%s: %s" % (url, exc.__class__.__name__ if not str(exc) else exc))
        if response.status_code >= 400:
            raise FeedError("HTTP %s from Hermes: %s" % (response.status_code, (response.text or "")[:500]))
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"result": body}
        return body


# --- message parsing --------------------------------------------------------------

def _band(name) -> str:
    name = str(name or "").strip()
    return BAND_ALIASES.get(name.lower(), name)


def _to_ab_mag(value, unit) -> Optional[float]:
    value = num(value)
    if value is None:
        return None
    unit = str(unit or "AB mag").lower()
    if "jy" in unit:
        if "mjy" in unit or "milli" in unit:
            value = value / 1000.0
        elif "ujy" in unit or "micro" in unit or "μjy" in unit:
            value = value / 1e6
        if value <= 0:
            return None
        import math

        return -2.5 * math.log10(value) + 8.90
    return value


def photometry_points(rows: Iterable[Dict], target_name: str) -> List[Dict]:
    points = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("target_name") or row.get("target") or "").strip()
        if name and target_name and name != target_name:
            continue
        mjd = parse_time_mjd(row.get("date_obs") or row.get("mjd") or row.get("date"))
        if mjd is None:
            continue
        band = _band(row.get("bandpass") or row.get("band") or row.get("filter"))
        unit = row.get("brightness_unit") or row.get("unit")
        mag = _to_ab_mag(row.get("brightness") or row.get("mag"), unit)
        err = num(row.get("brightness_error") or row.get("mag_err"))
        limit = _to_ab_mag(row.get("limiting_brightness") or row.get("limit"), row.get("limiting_brightness_unit") or unit)
        if mag is None and limit is None:
            continue
        point = make_point(mjd, band, mag, err, limit=limit, alert_id=row.get("id"))
        point["instrument"] = str(row.get("instrument") or "").strip() or None
        point["telescope"] = str(row.get("telescope") or "").strip() or None
        points.append(point)
    return points


def _classification(data: Dict, target: Dict, target_name: str):
    """``(class, redshift)`` from spectroscopy rows of this target, else the target's own fields."""
    for row in data.get("spectroscopy") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("target_name") or "").strip()
        if name and target_name and name != target_name:
            continue
        cls = str(row.get("classification") or row.get("class") or "").strip()
        if cls:
            return cls, num(row.get("redshift"))
    for row in data.get("classifications") or []:
        if isinstance(row, dict) and str(row.get("target_name") or target_name) == target_name:
            cls = str(row.get("classification") or row.get("class") or "").strip()
            if cls:
                return cls, num(row.get("redshift"))
    cls = str(target.get("classification") or target.get("class") or "").strip()
    disc = target.get("discovery_info") if isinstance(target.get("discovery_info"), dict) else {}
    ttype = str(disc.get("transient_type") or "").strip()
    if not cls and ttype and ttype.upper() not in ("AT", "PSN", "PNV", "OTHER", "TRANSIENT"):
        cls = ttype
    return cls, num(target.get("redshift"))


def _kind_for(topic: str, classification: str, has_discovery: bool, has_phot: bool) -> str:
    topic = (topic or "").lower()
    if "classif" in topic or (classification and not has_discovery):
        return KIND_CLASSIFICATION
    if "discover" in topic or "new-object" in topic or has_discovery or "test" in topic:
        return KIND_DISCOVERY
    if "phot" in topic or has_phot:
        return KIND_PHOTOMETRY
    return KIND_DISCOVERY if classification == "" else KIND_CLASSIFICATION


def parse_message(message: Dict) -> List[FeedMessage]:
    """One :class:`FeedMessage` per target of a Hermes message (``[]`` when it has none)."""
    if not isinstance(message, dict):
        return []
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    topic = str(message.get("topic") or "")
    message_id = str(message.get("uuid") or message.get("id") or "")
    title = str(message.get("title") or "")
    text = str(message.get("message_text") or "")
    submitter = str(message.get("submitter") or message.get("authors") or "")
    published = parse_time_mjd(message.get("published") or message.get("created"))
    out = []
    targets = data.get("targets") if isinstance(data.get("targets"), list) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        name = str(target.get("name") or target.get("target_name") or "").strip()
        if not name:
            continue
        ra, dec = parse_coord(target.get("ra"), target.get("dec"))
        disc = target.get("discovery_info") if isinstance(target.get("discovery_info"), dict) else {}
        reporter = str(disc.get("reporting_group") or disc.get("discovery_source") or "").strip() or submitter
        classification, redshift = _classification(data, target, name)
        points = photometry_points(data.get("photometry") or [], name)
        aliases = target.get("aliases") if isinstance(target.get("aliases"), list) else []
        aliases = [str(a).strip() for a in aliases if str(a).strip()]
        for key in ("tns_name", "internal_name", "group_name"):
            if target.get(key):
                aliases.append(str(target[key]).strip())
        error = num(target.get("ra_error"))
        units = str(target.get("ra_error_units") or "arcsec").lower()
        if error is not None:
            if units.startswith("deg"):
                error *= 3600.0
            elif units.startswith("arcmin"):
                error *= 60.0
        time_mjd = parse_time_mjd(disc.get("date")) if disc else None
        kind = _kind_for(topic, classification, bool(disc), bool(points))
        detections = [p for p in points if p.get("mag") is not None]
        latest = max(detections, key=lambda p: p["mjd"]) if detections else None
        out.append(FeedMessage(
            feed=SLUG, message_id=message_id or ("%s:%s" % (topic, name)), kind=kind, object_id=name,
            ra=ra, dec=dec, error_radius_arcsec=error, time_mjd=time_mjd or (published if kind == KIND_DISCOVERY else None),
            reporter=reporter[:64], classification=classification[:128], redshift=redshift,
            mag=latest["mag"] if latest else None, band=latest["band"] if latest else "",
            photometry=points, aliases=[a for a in aliases if a != name], url=message_url(message), title=title,
            text=text, raw={"topic": topic, "submitter": submitter, "authors": str(message.get("authors") or ""),
                            "published": message.get("published"), "event_id": data.get("event_id"),
                            "discovery_info": disc, "target": {k: v for k, v in target.items() if k != "aliases"}},
        ))
    if not out and message_id:
        out.append(FeedMessage(feed=SLUG, message_id=message_id, kind=KIND_OTHER, object_id=title[:128] or message_id,
                               title=title, text=text, url=message_url(message), raw={"topic": topic}))
    return out


# --- provider ---------------------------------------------------------------------

@register
class HermesFeed(FeedProvider):
    slug = SLUG
    name = "Hermes / SCiMMA"
    description = "Hermes topics (hermes.*, tns.*) polled through the Hermes REST API or read from Hopskotch."
    feed_kind = "hermes"
    information_source = "Hermes"
    default_auto_save = True
    default_obs_group = "Hermes"
    default_instrument = "Unknown"
    consumer_package = "hop"
    config_keys = tuple(FeedProvider.config_keys) + ("since_hours", "kinds", "classify", "update_redshift")

    def client(self) -> HermesClient:
        return HermesClient(token=_token(self.credential))

    def poll(self, source, *, since: Optional[datetime.datetime] = None) -> List[FeedMessage]:
        since = since or since_default(self.option("since_hours", 24))
        raw = self.client().list_messages(source.topic, since=since)
        return self.filter_kinds(m for message in reversed(raw) for m in parse_message(message))

    def filter_kinds(self, messages: Iterable[FeedMessage]) -> List[FeedMessage]:
        wanted = self.option("kinds")
        if isinstance(wanted, str):
            wanted = [w.strip() for w in wanted.split(",") if w.strip()]
        out = [m for m in messages if m.kind != KIND_OTHER]
        if wanted:
            out = [m for m in out if m.kind in set(wanted)]
        return out

    def consume(self, source, *, max_messages: int = 100, timeout_seconds: float = 30.0, stream=None) -> List[FeedMessage]:
        """Read ``source.topic`` from Hopskotch with hop-client (credential: hop_username / hop_password)."""
        if stream is None:
            if not self.can_consume():
                raise FeedError("hop-client is not installed; pip install hop-client to consume from Kafka")
            from hop import Stream
            from hop.auth import Auth

            user = self.credential.get("hop_username") or self.credential.get("username")
            password = self.credential.get("hop_password") or self.credential.get("password")
            if not user or not password:
                raise FeedError("consuming from Hopskotch needs hop_username / hop_password in the credential")
            stream = Stream(auth=Auth(user, password), until_eos=False)
        url = kafka_url().rstrip("/") + "/" + source.topic
        out: List[FeedMessage] = []
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=float(timeout_seconds))
        with stream.open(url, "r") as consumer:
            for item in consumer:
                payload = getattr(item, "content", item)
                if isinstance(payload, (bytes, bytearray, str)):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        continue
                if isinstance(payload, dict) and "topic" not in payload:
                    payload = dict(payload, topic=source.topic)
                out.extend(parse_message(payload))
                if len(out) >= max_messages or datetime.datetime.now() >= deadline:
                    break
        return self.filter_kinds(out)

    # -- hooks -----------------------------------------------------------------
    def on_linked(self, source, message: FeedMessage, transient, user) -> None:
        if message.classification and self.option("classify", True):
            self.apply_classification(message, transient, user)
        elif message.kind == KIND_DISCOVERY and message.reporter:
            self.add_comment(transient, "Hermes: %s reported by %s on %s%s" % (
                message.object_id, message.reporter, (message.raw or {}).get("topic") or "Hermes",
                " (%s)" % message.url if message.url else ""), user)
        if message.redshift is not None and transient.redshift is None and self.option("update_redshift", True):
            transient.redshift = message.redshift
            transient.save(update_fields=["redshift", "modified_date"])

    def apply_classification(self, message: FeedMessage, transient, user) -> None:
        """Set ``best_spec_class`` (matching a TransientClass by name) and leave a comment."""
        from YSE_App.models.enum_models import TransientClass

        label = message.classification.strip()
        cls = TransientClass.objects.filter(name__iexact=label).first()
        changed = []
        if cls is not None and transient.best_spec_class_id != cls.id:
            transient.best_spec_class = cls
            changed.append("best_spec_class")
        if cls is None and transient.TNS_spec_class != label:
            transient.TNS_spec_class = label[:64]
            changed.append("TNS_spec_class")
        if changed:
            transient.modified_by_id = user.id
            transient.save(update_fields=changed + ["modified_by", "modified_date"])
        z = " at z=%.4f" % message.redshift if message.redshift is not None else ""
        self.add_comment(transient, "Hermes classification: %s%s by %s%s" % (
            label, z, message.reporter or "unknown reporter", " (%s)" % message.url if message.url else ""), user)


# --- publishing (#326) ------------------------------------------------------------

def _iso(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime.datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return str(dt)


def build_message(transient, topic: str, *, kind: str = "discovery", reporting_group: str = "YSE",
                  authors: str = "", remarks: str = "", allowed_instruments=None, allowed_obs_groups=None,
                  classification: str = "", redshift: Optional[float] = None, max_points: int = 50,
                  submitter: str = "") -> Dict:
    """The Hermes ``submit_message`` document for ``transient`` (targets + photometry [+ spectroscopy])."""
    from YSE_App.models.phot_models import TransientPhotData

    target = {
        "name": transient.name,
        "ra": float(transient.ra),
        "dec": float(transient.dec),
        "ra_error": 0.5, "dec_error": 0.5, "ra_error_units": "arcsec", "dec_error_units": "arcsec",
        "discovery_info": {
            "reporting_group": reporting_group,
            "discovery_source": getattr(getattr(transient, "obs_group", None), "name", "") or reporting_group,
            "date": _iso(transient.disc_date),
            "transient_type": "AT",
        },
    }
    z = redshift if redshift is not None else transient.redshift
    if z is not None:
        target["redshift"] = float(z)
    aliases = list(transient.alternatetransientnames_set.values_list("name", flat=True)) \
        if hasattr(transient, "alternatetransientnames_set") else []
    if aliases:
        target["aliases"] = aliases
    qs = (TransientPhotData.objects.filter(photometry__transient=transient, mag__isnull=False)
          .select_related("band", "photometry__instrument", "photometry__instrument__telescope", "photometry__obs_group")
          .order_by("-obs_date"))
    if allowed_instruments:
        qs = qs.filter(photometry__instrument__in=allowed_instruments)
    if allowed_obs_groups:
        qs = qs.filter(photometry__obs_group__in=allowed_obs_groups)
    photometry = []
    for point in qs[: int(max_points)]:
        instrument = point.photometry.instrument
        photometry.append({
            "target_name": transient.name,
            "date_obs": _iso(point.obs_date),
            "telescope": getattr(getattr(instrument, "telescope", None), "name", "") or "",
            "instrument": getattr(instrument, "name", "") or "",
            "bandpass": getattr(point.band, "name", "") or "",
            "brightness": float(point.mag),
            "brightness_error": float(point.mag_err) if point.mag_err is not None else None,
            "brightness_unit": "AB mag",
        })
    photometry.reverse()
    data = {"targets": [target], "photometry": photometry, "references": []}
    label = classification or (getattr(transient.best_spec_class, "name", "") if transient.best_spec_class_id else "")
    if kind == "classification" and label:
        data["spectroscopy"] = [{"target_name": transient.name, "classification": label,
                                 "redshift": float(z) if z is not None else None,
                                 "date_obs": _iso(transient.modified_date)}]
        target["discovery_info"]["transient_type"] = label
    title = "%s: %s%s from %s" % (transient.name, kind, " (%s)" % label if kind == "classification" and label else "",
                                  reporting_group)
    text = remarks or "%s report for %s from YSE-PZ." % (kind.capitalize(), transient.name)
    return {"title": title[:200], "topic": topic, "submitter": submitter or reporting_group,
            "authors": authors or reporting_group, "message_text": text, "data": data}


def publish_submission(submission) -> Dict:
    """Publish a ``SharingSubmission`` to its service's Hermes topic; returns ``{"uuid": ..., "topic": ...}``."""
    service = submission.service
    topic = (service.hermes_topic or "").strip()
    if service.testing:
        topic = TEST_TOPIC
    if not topic:
        raise FeedError("sharing service %s has no hermes_topic" % service.slug)
    credential = {}
    if service.credential_id and service.credential.is_active and service.credential.has_secret:
        credential = service.credential.get_secret(touch=True) or {}
    token = _token(credential)
    if not token:
        raise FeedError("sharing service %s has no Hermes token (credential key hermes_token)" % service.slug)
    payload = submission.payload if isinstance(submission.payload, dict) else {}
    hermes = payload.get("hermes") if isinstance(payload.get("hermes"), dict) else {}
    kind = hermes.get("kind") or ("classification" if submission.transient.best_spec_class_id else "discovery")
    message = build_message(
        submission.transient, topic, kind=kind, reporting_group=service.tns_group_name or "YSE",
        authors=hermes.get("coauthors") or service.default_coauthors or "", remarks=hermes.get("remarks") or "",
        allowed_instruments=list(service.allowed_instruments.all()) or None,
        allowed_obs_groups=list(service.allowed_obs_groups.all()) or None,
        classification=hermes.get("classification") or "", redshift=num(hermes.get("redshift")),
        submitter=getattr(submission.created_by, "username", "") or "",
    )
    result = HermesClient(token=token).submit(message)
    uuid = str(result.get("uuid") or result.get("id") or "")
    return {"uuid": uuid, "topic": topic, "title": message["title"], "url": message_url({"uuid": uuid}) if uuid else "",
            "response": result, "message": message}


__all__ = ["HermesClient", "HermesFeed", "build_message", "parse_message", "photometry_points", "publish_submission",
           "BrokerAlert"]
