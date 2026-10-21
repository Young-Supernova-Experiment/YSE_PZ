"""Kafka / streaming ingest of broker alerts to candidates (issue #278).

``consume(connection, ...)`` is the worker loop behind ``manage.py
broker_ingest``: it opens the connection's stream (``confluent_kafka.Consumer``
for a Kafka connection, ``antares_client.StreamingClient`` for ANTARES; both
imported lazily so the web venv needs neither), decodes each message (JSON or
Avro through ``fastavro``), asks the provider to normalise it
(``parse_stream_message``) and runs :func:`YSE_App.brokers.ingest.process_alert`
with the enabled ``BrokerFilter`` rows of that broker and topic: candidates,
auto-save, notifications, all as for a poll. Offsets are committed per batch
and an ``IngestHeartbeat`` row per (connection, topic, worker) is updated every
batch with counts, lag when the client reports it, and the last error. One
bad message is logged and counted; the loop goes on.

Nothing here runs by itself: a ``BrokerConnection`` must be ``enabled`` and an
operator must start the command (docker ``brokers`` profile or the systemd
unit in docs/broker-streams.md). Tests inject a fake consumer.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import os
import signal
import socket
import time
from typing import Callable, Dict, List, Optional

from django.conf import settings
from django.db import connections
from django.utils import timezone

from YSE_App.brokers import registry
from YSE_App.brokers.base import STREAM, BrokerError, BrokerProvider, BrokerUnavailable, mjd_now
from YSE_App.brokers.ingest import process_alert
from YSE_App.models.candidate_models import BrokerConnection, BrokerFilter, IngestHeartbeat

logger = logging.getLogger(__name__)


def batch_size(connection: Optional[BrokerConnection] = None) -> int:
    cfg = connection.config_dict() if connection is not None else {}
    return int(cfg.get("batch_size") or getattr(settings, "BROKER_STREAM_BATCH_SIZE", 100) or 100)


def stale_minutes() -> float:
    return float(getattr(settings, "BROKER_STREAM_STALE_MINUTES", 15) or 15)


# --- message records ----------------------------------------------------------------

class StreamMessage:
    """One record from any stream client: decoded ``value`` (dict) or raw bytes."""

    __slots__ = ("topic", "value", "key", "offset", "partition", "timestamp", "error")

    def __init__(self, topic, value, *, key=None, offset=None, partition=None, timestamp=None, error=None):
        self.topic = topic
        self.value = value
        self.key = key
        self.offset = offset
        self.partition = partition
        self.timestamp = timestamp
        self.error = error


def decode_value(value, fmt: str):
    """Bytes -> dict for the connection's wire format (``json`` or ``avro``); dicts pass through."""
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    else:
        raw = str(value).encode("utf-8")
    if fmt == BrokerConnection.FORMAT_AVRO:
        try:
            import fastavro
        except ImportError as exc:
            raise BrokerUnavailable("fastavro is not installed; pip install fastavro to decode Avro topics") from exc
        records = list(fastavro.reader(io.BytesIO(raw)))
        return records[0] if len(records) == 1 else {"records": records}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BrokerError("message is not valid JSON: %s" % exc) from exc


# --- stream clients ----------------------------------------------------------------

class StreamClient:
    """What the loop needs from a client: ``poll(n, timeout)`` -> messages, ``commit()``,
    ``lag()`` (or None) and ``close()``. The fake used in tests implements the same."""

    def poll(self, max_messages: int, timeout_seconds: float) -> List[StreamMessage]:
        raise NotImplementedError

    def commit(self) -> None:
        pass

    def lag(self) -> Optional[int]:
        return None

    def close(self) -> None:
        pass


class KafkaStreamClient(StreamClient):
    """``confluent_kafka.Consumer`` over the connection's topics (one consumer group;
    several workers with the same ``group_id`` share the partitions)."""

    def __init__(self, connection: BrokerConnection, *, from_beginning: bool = False,
                 since: Optional[datetime.datetime] = None, worker: int = 0):
        try:
            from confluent_kafka import Consumer, KafkaException, TopicPartition
        except ImportError as exc:
            raise BrokerUnavailable("confluent-kafka is not installed in this environment") from exc
        self._KafkaException = KafkaException
        self._TopicPartition = TopicPartition
        cfg = connection.config_dict()
        secret = connection.secret()
        conf = {
            "bootstrap.servers": connection.bootstrap_servers,
            "group.id": connection.group_id or "yse-pz-%s" % connection.slug,
            "auto.offset.reset": "earliest" if from_beginning else str(cfg.get("auto_offset_reset") or "latest"),
            "enable.auto.commit": False,
            "client.id": "yse-pz-%s-%d" % (connection.slug, worker),
        }
        if cfg.get("security_protocol"):
            conf["security.protocol"] = cfg["security_protocol"]
        if cfg.get("sasl_mechanism"):
            conf["sasl.mechanism"] = cfg["sasl_mechanism"]
        if secret.get("username") and secret.get("password"):
            conf.setdefault("security.protocol", "SASL_SSL")
            conf.setdefault("sasl.mechanism", "SCRAM-SHA-512")
            conf["sasl.username"] = secret["username"]
            conf["sasl.password"] = secret["password"]
        conf.update(cfg.get("kafka") or {})
        self.consumer = Consumer(conf)
        self.topics = connection.topic_list()
        self.since = since
        self.fmt = connection.message_format
        self._assigned = []
        self.consumer.subscribe(self.topics, on_assign=self._on_assign)

    def _on_assign(self, consumer, partitions):
        self._assigned = list(partitions)
        if self.since is not None:
            ts_ms = int(self.since.timestamp() * 1000)
            for p in partitions:
                p.offset = ts_ms
            try:
                partitions = consumer.offsets_for_times(partitions, timeout=30)
            except self._KafkaException as exc:
                logger.warning("offsets_for_times failed: %s", exc)
        consumer.assign(partitions)

    def poll(self, max_messages, timeout_seconds):
        raw = self.consumer.consume(num_messages=int(max_messages), timeout=float(timeout_seconds))
        out = []
        for m in raw or []:
            if m.error():
                out.append(StreamMessage(m.topic(), None, offset=m.offset(), partition=m.partition(), error=str(m.error())))
                continue
            ts = m.timestamp()[1] if m.timestamp() else None
            out.append(StreamMessage(m.topic(), m.value(), key=m.key(), offset=m.offset(), partition=m.partition(),
                                     timestamp=ts))
        return out

    def commit(self):
        try:
            self.consumer.commit(asynchronous=False)
        except self._KafkaException as exc:
            if "No offset stored" not in str(exc):
                raise

    def lag(self):
        total = 0
        found = False
        for p in self._assigned:
            try:
                _low, high = self.consumer.get_watermark_offsets(p, timeout=5, cached=True)
                pos = self.consumer.position([p])[0].offset
            except self._KafkaException:
                continue
            if pos is not None and pos >= 0 and high is not None and high >= 0:
                total += max(0, high - pos)
                found = True
        return total if found else None

    def close(self):
        self.consumer.close()


class AntaresStreamClient(StreamClient):
    """``antares_client.StreamingClient`` over the connection's topics (credential: api_key / api_secret)."""

    def __init__(self, connection: BrokerConnection, **_kw):
        try:
            from antares_client import StreamingClient
        except ImportError as exc:
            raise BrokerUnavailable("antares_client is not installed in this environment") from exc
        secret = connection.secret()
        if not secret.get("api_key") or not secret.get("api_secret"):
            raise BrokerUnavailable("an ANTARES stream needs api_key / api_secret in the connection's credential")
        cfg = connection.config_dict()
        self.client = StreamingClient(connection.topic_list(), api_key=secret["api_key"], api_secret=secret["api_secret"],
                                      group=connection.group_id or None, **(cfg.get("antares") or {}))
        self._iter = None

    def poll(self, max_messages, timeout_seconds):
        if self._iter is None:
            self._iter = self.client.iter()
        out = []
        deadline = time.monotonic() + float(timeout_seconds)
        while len(out) < int(max_messages) and time.monotonic() < deadline:
            try:
                topic, locus = next(self._iter)
            except StopIteration:
                break
            out.append(StreamMessage(topic, locus))
        return out

    def commit(self):
        try:
            self.client.commit()
        except Exception as exc:  # noqa: BLE001 - older clients have no commit
            logger.debug("antares commit: %s", exc)

    def close(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def open_client(connection: BrokerConnection, **kw) -> StreamClient:
    if connection.kind == BrokerConnection.KIND_ANTARES:
        return AntaresStreamClient(connection, **kw)
    return KafkaStreamClient(connection, **kw)


# --- the loop -------------------------------------------------------------------------

class StopConsuming(Exception):
    """Raised inside the loop by the signal handler / limits to stop cleanly."""


def filters_for(broker: str, topic: str = "") -> List[BrokerFilter]:
    return [bf for bf in BrokerFilter.objects.filter(broker=broker, enabled=True).order_by("pk")
            if bf.applies_to_topic(topic)]


def heartbeat_for(connection: BrokerConnection, topic: str, worker: int) -> IngestHeartbeat:
    hb, _ = IngestHeartbeat.objects.get_or_create(connection=connection, topic=topic or "", worker=int(worker))
    hb.hostname = socket.gethostname()[:128]
    hb.pid = os.getpid()
    hb.status = IngestHeartbeat.STATUS_RUNNING
    hb.started_at = timezone.now()
    hb.last_seen = hb.started_at
    hb.save()
    return hb


def _record_error(hb: IngestHeartbeat, connection: BrokerConnection, error: str):
    hb.errors += 1
    hb.last_error = (error or "")[:10000]
    hb.last_error_at = timezone.now()
    connection.record_error(error)


def consume(connection: BrokerConnection, *, worker: int = 0, client: Optional[StreamClient] = None,
            max_messages: Optional[int] = None, timeout_seconds: Optional[float] = None,
            from_beginning: bool = False, since: Optional[datetime.datetime] = None,
            dry_run: bool = False, poll_timeout: float = 5.0, idle_exit: bool = False,
            stop: Optional[Callable[[], bool]] = None, force: bool = False) -> Dict:
    """Run one worker until ``max_messages`` were read, ``timeout_seconds`` passed,
    ``stop()`` returns True or (with ``idle_exit``) a poll came back empty.

    Returns counters: ``messages, parsed, passed, new, saved, errors, batches``.
    """
    if not connection.enabled and not force:
        raise BrokerUnavailable("connection %s is disabled; enable it in the admin first" % connection.slug)
    provider = registry.get_provider(connection.broker, require_available=True)
    if provider is None:
        raise BrokerError("broker %r is unknown or disabled (BROKERS_ENABLED)" % connection.broker)
    if not provider.has(STREAM):
        raise BrokerError("%s has no stream parser" % connection.broker)
    own_client = client is None
    if own_client:
        client = open_client(connection, from_beginning=from_beginning, since=since, worker=worker)
    result = {"connection": connection.slug, "worker": int(worker), "messages": 0, "parsed": 0, "passed": 0, "new": 0,
              "saved": 0, "errors": 0, "notified": 0, "batches": 0, "dry_run": bool(dry_run)}
    heartbeats: Dict[str, IngestHeartbeat] = {}
    filters_cache: Dict[str, List[BrokerFilter]] = {}
    deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds else None
    size = batch_size(connection)
    try:
        while True:
            if stop is not None and stop():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            want = size if max_messages is None else max(1, min(size, int(max_messages) - result["messages"]))
            messages = client.poll(want, poll_timeout)
            result["batches"] += 1
            if not messages:
                if idle_exit or (max_messages is not None and result["messages"] >= int(max_messages)):
                    break
                for hb in heartbeats.values():
                    hb.last_seen = timezone.now()
                    hb.lag = client.lag()
                    hb.save(update_fields=["last_seen", "lag"])
                if not heartbeats:
                    heartbeats[""] = heartbeat_for(connection, "", worker)
                continue
            now_mjd = mjd_now()
            for message in messages:
                topic = str(message.topic or "")
                hb = heartbeats.get(topic)
                if hb is None:
                    hb = heartbeats[topic] = heartbeat_for(connection, topic, worker)
                    heartbeats.pop("", None)
                result["messages"] += 1
                hb.messages += 1
                if message.offset is not None:
                    hb.last_offset = int(message.offset)
                if message.error:
                    result["errors"] += 1
                    _record_error(hb, connection, "kafka: %s" % message.error)
                    continue
                try:
                    value = decode_value(message.value, connection.message_format)
                    alert = provider.parse_stream_message(topic, value, key=message.key)
                    if alert is None:
                        continue
                    result["parsed"] += 1
                    hb.last_alert_id = (alert.object_id or "")[:128]
                    if topic not in filters_cache:
                        filters_cache[topic] = filters_for(connection.broker, topic)
                    counters: Dict[str, int] = {}
                    candidate = process_alert(alert, filters_cache[topic], provider, now_mjd=now_mjd, dry_run=dry_run,
                                              topic=topic, result=counters)
                    for key in ("passed", "new", "saved", "errors", "notified"):
                        result[key] += counters.get(key, 0)
                    if candidate is not None:
                        hb.candidates += counters.get("new", 0)
                        hb.saved += counters.get("saved", 0)
                    if counters.get("errors"):
                        hb.errors += counters["errors"]
                except BrokerUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad message must not stop the consumer
                    result["errors"] += 1
                    _record_error(hb, connection, "%s: %s" % (type(exc).__name__, exc))
                    logger.exception("%s worker %d: message on %s failed", connection.slug, worker, topic)
            if not dry_run:
                client.commit()
            now = timezone.now()
            lag = client.lag()
            for hb in heartbeats.values():
                hb.last_seen = now
                hb.lag = lag
                hb.save()
            BrokerConnection.objects.filter(pk=connection.pk).update(last_message_at=now)
            filters_cache.clear()  # pick up filter edits between batches
            if max_messages is not None and result["messages"] >= int(max_messages):
                break
    except StopConsuming:
        pass
    except BrokerUnavailable as exc:
        for hb in heartbeats.values():
            hb.status = IngestHeartbeat.STATUS_ERROR
            _record_error(hb, connection, str(exc))
            hb.save()
        raise
    finally:
        for hb in heartbeats.values():
            if hb.status == IngestHeartbeat.STATUS_RUNNING:
                hb.status = IngestHeartbeat.STATUS_STOPPED
            hb.last_seen = timezone.now()
            hb.save()
        if own_client:
            client.close()
    return result


# --- workers -----------------------------------------------------------------------------

def _worker_main(connection_id: int, worker: int, kwargs: Dict):
    """Entry point of a forked worker process."""
    connections.close_all()
    stop_flag = {"stop": False}

    def _handler(signum, frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    connection = BrokerConnection.objects.get(pk=connection_id)
    try:
        result = consume(connection, worker=worker, stop=lambda: stop_flag["stop"], **kwargs)
        logger.info("%s worker %d finished: %s", connection.slug, worker, result)
    except (BrokerError, BrokerUnavailable) as exc:
        logger.error("%s worker %d stopped: %s", connection.slug, worker, exc)
        connection.record_error(str(exc))
        raise SystemExit(1)


def spawn_worker(connection: BrokerConnection, worker: int, kwargs: Dict):
    """Start one worker process (fork). Tests patch this to run inline."""
    import multiprocessing

    connections.close_all()  # every child opens its own database connection
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=_worker_main, args=(connection.pk, worker, kwargs), name="broker-ingest-%s-%d" % (connection.slug, worker))
    proc.start()
    return proc


def run_workers(connection: BrokerConnection, workers: int = 1, **kwargs) -> List:
    """Fork ``workers`` consumers in the same consumer group and wait for them.

    The database connections are closed before each fork so every child opens
    its own. Returns the finished process objects (``exitcode`` per worker).
    """
    procs = [spawn_worker(connection, i, kwargs) for i in range(max(1, int(workers)))]
    for proc in procs:
        proc.join()
    return procs


# --- status -------------------------------------------------------------------------------

def stale_heartbeats(threshold_minutes: Optional[float] = None, now=None) -> List[IngestHeartbeat]:
    """Running heartbeats of enabled connections older than the threshold (dashboard banner)."""
    now = now or timezone.now()
    threshold = float(threshold_minutes if threshold_minutes is not None else stale_minutes())
    cutoff = now - datetime.timedelta(minutes=threshold)
    return list(IngestHeartbeat.objects.filter(connection__enabled=True, status=IngestHeartbeat.STATUS_RUNNING,
                                               last_seen__lt=cutoff).select_related("connection"))


def stream_status(now=None) -> List[Dict]:
    """One row per heartbeat with age / lag / errors for the admin and the API."""
    now = now or timezone.now()
    threshold = stale_minutes()
    out = []
    for hb in IngestHeartbeat.objects.select_related("connection").order_by("connection__slug", "topic", "worker"):
        out.append({
            "connection": hb.connection.slug, "broker": hb.connection.broker, "enabled": hb.connection.enabled,
            "topic": hb.topic, "worker": hb.worker, "status": hb.status, "hostname": hb.hostname, "pid": hb.pid,
            "last_seen": hb.last_seen, "age_seconds": hb.age_seconds(now), "stale": hb.is_stale(threshold, now),
            "messages": hb.messages, "candidates": hb.candidates, "saved": hb.saved, "errors": hb.errors,
            "lag": hb.lag, "last_offset": hb.last_offset, "last_alert_id": hb.last_alert_id,
            "last_error": hb.last_error[:300], "last_error_at": hb.last_error_at,
        })
    return out


__all__ = ["AntaresStreamClient", "KafkaStreamClient", "StreamClient", "StreamMessage", "consume", "decode_value",
           "filters_for", "open_client", "run_workers", "stale_heartbeats", "stream_status"]
