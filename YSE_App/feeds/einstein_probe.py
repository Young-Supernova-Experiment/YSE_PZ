"""Einstein Probe WXT alert feed (#282).

EP alerts reach the community as GCN notices on the Kafka topic
``gcn.notices.einstein_probe.wxt.alert`` (unified JSON schema: ``id``, ``ra``,
``dec``, ``ra_dec_error`` in degrees, ``trigger_time``, ``image_snr``,
``net_count_rate``, ``image_energy_range``, ``instrument``). The feed reads
them either from Kafka with ``gcn-kafka`` (credential: ``client_id`` /
``client_secret``; :meth:`EinsteinProbeFeed.consume`) or, for the queued poll,
from a JSON URL (``config.url``: a list of notices, or an object with a
``notices`` / ``results`` list) such as an institutional mirror of the notice
archive. Each notice becomes a candidate with the X-ray error circle
(``broker='ep'``); a second notice for the same event updates that candidate.
Transients inside the error circle get an ``einstein_probe`` annotation.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Dict, Iterable, List, Optional

from django.conf import settings

from YSE_App.brokers import register
from YSE_App.feeds.base import (
    KIND_ALERT,
    FeedError,
    FeedMessage,
    FeedProvider,
    http_get_json,
    mjd_to_iso,
    num,
    parse_coord,
    parse_time_mjd,
    separation_arcsec,
    since_default,
)

logger = logging.getLogger(__name__)

SLUG = "ep"
GCN_TOPIC = "gcn.notices.einstein_probe.wxt.alert"
ORIGIN = "einstein_probe"
DEFAULT_ERROR_ARCSEC = 180.0  # WXT localisation when a notice has none (~3')
MAX_MATCH_ARCSEC = 1800.0     # never cross-match beyond 30'


def gcn_domain() -> str:
    return getattr(settings, "FEEDS_GCN_KAFKA_DOMAIN", "") or "gcn.nasa.gov"


def _notice_id(notice: Dict) -> str:
    ident = notice.get("id")
    if isinstance(ident, (list, tuple)):
        ident = ident[0] if ident else ""
    ident = str(ident or notice.get("trigger_id") or notice.get("event_id") or "").strip()
    return ident


def parse_notice(notice: Dict) -> Optional[FeedMessage]:
    """The :class:`FeedMessage` of one EP WXT notice (``None`` without id or position)."""
    if not isinstance(notice, dict):
        return None
    ident = _notice_id(notice)
    ra, dec = parse_coord(notice.get("ra"), notice.get("dec"))
    if not ident or ra is None or dec is None:
        return None
    error = num(notice.get("ra_dec_error"))
    error_arcsec = error * 3600.0 if error is not None else DEFAULT_ERROR_ARCSEC
    trigger = notice.get("trigger_time") or notice.get("alert_datetime")
    snr = num(notice.get("image_snr"))
    rate = num(notice.get("net_count_rate"))
    instrument = str(notice.get("instrument") or "WXT")
    energy = notice.get("image_energy_range")
    name = "EP%s" % ident if not ident.upper().startswith("EP") else ident
    label = "X-ray transient (%s)" % instrument
    text = "Einstein Probe %s alert %s: %.4f %+.4f, error %.1f arcsec" % (instrument, ident, ra, dec, error_arcsec)
    if snr is not None:
        text += ", SNR %.1f" % snr
    if rate is not None:
        text += ", %.3g counts/s" % rate
    info = str(notice.get("additional_info") or "").strip()
    return FeedMessage(
        feed=SLUG, message_id="%s:%s" % (ident, trigger or ""), kind=KIND_ALERT, object_id=name, ra=ra, dec=dec,
        error_radius_arcsec=error_arcsec, time_mjd=parse_time_mjd(trigger), reporter="Einstein Probe",
        classification=label, score=None, url=str(notice.get("url") or ""), title=text, text=info,
        raw={"notice_id": ident, "trigger_time": trigger, "image_snr": snr, "net_count_rate": rate,
             "image_energy_range": energy, "instrument": instrument, "additional_info": info[:1000]},
    )


def _notices_from(body) -> List[Dict]:
    if isinstance(body, list):
        return [n for n in body if isinstance(n, dict)]
    if isinstance(body, dict):
        for key in ("notices", "results", "data", "alerts"):
            if isinstance(body.get(key), list):
                return [n for n in body[key] if isinstance(n, dict)]
        if "ra" in body and "dec" in body:
            return [body]
    raise FeedError("unexpected Einstein Probe payload (expected a list of notices)")


@register
class EinsteinProbeFeed(FeedProvider):
    slug = SLUG
    name = "Einstein Probe"
    description = "Einstein Probe WXT alerts (GCN notices) as candidates with their X-ray error circle."
    feed_kind = "ep"
    information_source = "Einstein Probe"
    default_auto_save = False
    default_obs_group = "Einstein Probe"
    default_instrument = "Unknown"
    consumer_package = "gcn_kafka"
    config_keys = tuple(FeedProvider.config_keys) + ("url", "topic", "since_hours", "max_error_arcsec", "annotate")

    def poll(self, source, *, since: Optional[datetime.datetime] = None) -> List[FeedMessage]:
        url = str(self.option("url") or "").strip()
        if not url:
            raise FeedError("Einstein Probe source %s has no config.url (notice archive / mirror) and no consumer;"
                            " set config.url or run `manage.py feeds --consume %s`" % (source.slug, source.slug))
        body = http_get_json(url)
        messages = [m for m in (parse_notice(n) for n in _notices_from(body)) if m is not None]
        # Refined notices repeat the trigger time, so the window is config.since_hours (unset: everything
        # the mirror returns); the candidate upsert de-duplicates by event id.
        hours = num(self.option("since_hours"))
        if hours is not None:
            since_mjd = parse_time_mjd(since_default(hours))
            messages = [m for m in messages if m.time_mjd is None or m.time_mjd >= since_mjd]
        return self.filter_error(messages)

    def filter_error(self, messages: Iterable[FeedMessage]) -> List[FeedMessage]:
        cap = num(self.option("max_error_arcsec"))
        out = list(messages)
        if cap is not None:
            out = [m for m in out if m.error_radius_arcsec is None or m.error_radius_arcsec <= cap]
        return out

    def consume(self, source, *, max_messages: int = 100, timeout_seconds: float = 30.0, consumer=None) -> List[FeedMessage]:
        """Read notices from GCN Kafka (credential: client_id / client_secret)."""
        topic = str(self.option("topic") or source.topic or GCN_TOPIC)
        if consumer is None:
            if not self.can_consume():
                raise FeedError("gcn-kafka is not installed; pip install gcn-kafka to consume GCN notices")
            from gcn_kafka import Consumer

            client_id = self.credential.get("client_id")
            client_secret = self.credential.get("client_secret")
            if not client_id or not client_secret:
                raise FeedError("consuming GCN notices needs client_id / client_secret in the credential")
            consumer = Consumer(client_id=client_id, client_secret=client_secret, domain=gcn_domain())
            consumer.subscribe([topic])
        out: List[FeedMessage] = []
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=float(timeout_seconds))
        while len(out) < max_messages and datetime.datetime.now() < deadline:
            batch = consumer.consume(max(1, max_messages - len(out)), timeout=1)
            if not batch:
                if getattr(consumer, "_yse_fake", False):
                    break
                continue
            for record in batch:
                if callable(getattr(record, "error", None)) and record.error():
                    logger.warning("gcn kafka error: %s", record.error())
                    continue
                value = record.value() if callable(getattr(record, "value", None)) else record
                try:
                    payload = json.loads(value) if isinstance(value, (bytes, bytearray, str)) else value
                except ValueError:
                    continue
                message = parse_notice(payload)
                if message is not None:
                    out.append(message)
        return self.filter_error(out)

    # -- hooks -----------------------------------------------------------------
    def on_candidate(self, source, message: FeedMessage, candidate, user) -> None:
        self.cross_match(source, message, user)

    def on_linked(self, source, message: FeedMessage, transient, user) -> None:
        self.cross_match(source, message, user)

    def cross_match(self, source, message: FeedMessage, user) -> int:
        """Annotate every transient inside the error circle (origin ``einstein_probe``); returns the count."""
        if not self.option("annotate", True):
            return 0
        from YSE_App.brokers.ingest import nearby_transients
        from YSE_App.services import annotations as annotations_svc

        radius = min(float(message.error_radius_arcsec or DEFAULT_ERROR_ARCSEC), MAX_MATCH_ARCSEC)
        n = 0
        for transient in nearby_transients(message.ra, message.dec, radius):
            sep = separation_arcsec(message.ra, message.dec, transient.ra, transient.dec)
            data = {
                "event": message.object_id,
                "separation_arcsec": sep,
                "error_radius_arcsec": round(radius, 1),
                "trigger_time": mjd_to_iso(message.time_mjd) if message.time_mjd is not None else "",
                "image_snr": (message.raw or {}).get("image_snr"),
                "net_count_rate": (message.raw or {}).get("net_count_rate"),
                "instrument": (message.raw or {}).get("instrument") or "WXT",
                "feed_source": source.slug,
                "summary": "Inside the %.0f arcsec error circle of %s (%.0f arcsec away)." % (radius, message.object_id, sep or 0),
            }
            if message.url:
                data["url"] = message.url
            annotations_svc.upsert(transient, ORIGIN, data, user=user, merge=True)
            if self.option("comment", False):
                self.add_comment(transient, "Einstein Probe: %s lies %.0f arcsec from this transient (error circle %.0f arcsec)."
                                 % (message.object_id, sep or 0, radius), user)
            n += 1
        return n


__all__ = ["EinsteinProbeFeed", "GCN_TOPIC", "ORIGIN", "parse_notice"]
