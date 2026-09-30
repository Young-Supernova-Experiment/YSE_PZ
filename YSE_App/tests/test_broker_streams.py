"""Broker stream ingest (#278), Lasair provider (#274), filter versions / preview (#277), per-group
rejection / default tags / notifications (#279), Brokers tab and cone search (#275)."""

from __future__ import annotations

import datetime
import io
import json
from unittest import mock

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.brokers import antares as antares_mod
from YSE_App.brokers import fink as fink_mod
from YSE_App.brokers import ingest, registry, streams
from YSE_App.brokers import lasair as lasair_mod
from YSE_App.brokers.base import (
    CONE_SEARCH,
    CUTOUTS,
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
from YSE_App.models import Job, Transient, TransientTag
from YSE_App.models.candidate_models import BrokerConnection, BrokerFilter, BrokerFilterVersion, Candidate, IngestHeartbeat
from YSE_App.models.notification_models import Notification
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user, ensure_transient_statuses

NOW_MJD = 61300.0


def record(object_id="ZTF26sssssss", ra=150.0, dec=40.0, mag=18.5, rb=0.9, **extra):
    d = {"object_id": object_id, "ra": ra, "dec": dec, "mag": mag, "rb": rb, "band": "r", "mjd": NOW_MJD - 0.2,
         "discovery_mjd": NOW_MJD - 2.0, "classification": "SN candidate", "candid": "c-%s" % object_id}
    d.update(extra)
    return d


class StreamFakeProvider(BrokerProvider):
    """In-memory provider with a stream parser: messages are JSON dicts of ``record()`` shape."""

    slug = "sfake"
    name = "Stream fake"
    capabilities = (QUERY_ALERTS, GET_ALERT, CUTOUTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT, STREAM)
    query_keys = ("classes",)
    alerts = []
    photometry = {}
    fail_cone = False
    parsed = []

    @staticmethod
    def to_alert(d):
        return BrokerAlert(broker="sfake", object_id=d["object_id"], alert_id=str(d.get("candid") or ""), ra=d["ra"], dec=d["dec"],
                           mjd=d.get("mjd"), discovery_mjd=d.get("discovery_mjd"), mag=d.get("mag"), mag_err=0.05,
                           band=d.get("band", "r"), rb=d.get("rb"), ndet=3, is_positive=True,
                           classification=d.get("classification", ""), url="https://example.org/%s" % d["object_id"],
                           cutout_urls={"science": "https://example.org/%s-sci.png" % d["object_id"]})

    def parse_stream_message(self, topic, message, *, key=None):
        if not isinstance(message, dict):
            return None
        if message.get("object_id") == "BAD":
            raise BrokerError("cannot parse BAD")
        if message.get("heartbeat"):
            return None
        StreamFakeProvider.parsed.append((topic, message["object_id"]))
        return self.to_alert(message)

    def query_alerts(self, query=None, *, since_mjd=None, limit=100):
        self.validate_query(query)
        return list(StreamFakeProvider.alerts)[:limit]

    def cone_search(self, ra, dec, radius_arcsec, *, limit=50):
        if StreamFakeProvider.fail_cone:
            raise BrokerError("broker is down")
        return [a for a in StreamFakeProvider.alerts if abs(a.ra - ra) < 0.01 and abs(a.dec - dec) < 0.01][:limit]

    def get_alert(self, object_id):
        return next((a for a in StreamFakeProvider.alerts if a.object_id == object_id), None)

    def get_cutouts(self, object_id, alert_id=""):
        a = self.get_alert(object_id)
        return dict(a.cutout_urls) if a else {}

    def get_photometry(self, object_id):
        return list(StreamFakeProvider.photometry.get(object_id, []))


registry.register(StreamFakeProvider)


def fake_points(n=3, start=NOW_MJD - 3.0):
    return [make_point(start + i, "r" if i % 2 == 0 else "g", 19.0 - 0.1 * i, 0.05, alert_id="c%d" % i) for i in range(n)]


class FakeClient(streams.StreamClient):
    """Replays recorded messages in batches; records commits."""

    def __init__(self, messages, fail_after=None):
        self.messages = list(messages)
        self.commits = 0
        self.closed = False
        self.polls = 0
        self.fail_after = fail_after

    def poll(self, max_messages, timeout_seconds):
        self.polls += 1
        if self.fail_after is not None and self.polls > self.fail_after:
            raise BrokerUnavailable("broker gone")
        batch, self.messages = self.messages[:max_messages], self.messages[max_messages:]
        return batch

    def commit(self):
        self.commits += 1

    def lag(self):
        return len(self.messages)

    def close(self):
        self.closed = True


def msg(d, topic="t1", offset=None, fmt="json"):
    value = json.dumps(d).encode("utf-8") if fmt == "json" else d
    return streams.StreamMessage(topic, value, offset=offset)


def make_connection(**kw):
    defaults = dict(name="Fake stream", slug="fake-stream", broker="sfake", kind=BrokerConnection.KIND_KAFKA,
                    bootstrap_servers="kafka.example.org:9092", topics=["t1", "t2"], group_id="yse-test", enabled=True)
    defaults.update(kw)
    return BrokerConnection.objects.create(**defaults)


# --- Lasair provider ------------------------------------------------------------------

LASAIR_OBJECT = {
    "objectId": "ZTF26lasair1",
    "objectData": {"ramean": 150.0, "decmean": 40.0, "ncand": 4, "jdmin": 2461300.5 - 3.0 + 0.0, "jdmax": 2461300.3,
                   "glatmean": 60.0},
    "candidates": [
        {"candid": 1001, "jd": 2461298.5, "fid": 1, "magpsf": 19.2, "sigmapsf": 0.05, "ra": 150.0, "dec": 40.0, "rb": 0.8,
         "drb": 0.95, "isdiffpos": "t", "sgscore1": 0.1, "distpsnr1": 2.0},
        {"jd": 2461297.5, "fid": 2, "diffmaglim": 20.4},
        {"candid": 1002, "jd": 2461300.3, "fid": 2, "magpsf": 18.6, "sigmapsf": 0.04, "ra": 150.0, "dec": 40.0, "rb": 0.9,
         "drb": 0.97, "isdiffpos": "t", "sgscore1": 0.1, "distpsnr1": 2.0},
    ],
    "sherlock": {"classification": "SN", "catalogue_table_name": "GLADE"},
}


def lasair_router(calls):
    def fake_request_json(method, url, *, params=None, json=None, headers=None, timeout=None):
        calls.append((method, url, dict(params or {}), dict(headers or {})))
        if "/streams/" in url:
            return [{"objectId": "ZTF26lasair1", "ramean": 150.0, "decmean": 40.0, "magpsf": 18.6, "rb": 0.9},
                    {"objectId": "ZTF26lasair2", "ramean": 10.0, "decmean": -5.0}]
        if "/objects/" in url:
            ids = (params or {}).get("objectIds", "").split(",")
            out = []
            if "ZTF26lasair1" in ids:
                out.append(LASAIR_OBJECT)
            if "ZTF26lasair2" in ids:
                out.append(dict(LASAIR_OBJECT, objectId="ZTF26lasair2", objectData={"ramean": 10.0, "decmean": -5.0, "ncand": 1},
                                candidates=[{"candid": 2001, "jd": 2461300.0, "fid": 1, "magpsf": 20.1, "sigmapsf": 0.1, "rb": 0.6}],
                                sherlock={}))
            return out
        if "/lightcurves/" in url:
            return [{"objectId": "ZTF26lasair1", "candidates": LASAIR_OBJECT["candidates"]}]
        if "/cone/" in url:
            return [{"object": "ZTF26lasair1", "separation": 0.4}]
        raise AssertionError("unexpected url %s" % url)
    return fake_request_json


class LasairProviderTests(TestCase):
    def test_unavailable_without_token_and_absent_from_enabled_providers(self):
        p = lasair_mod.LasairProvider()
        self.assertFalse(p.available())
        self.assertIn("service='lasair'", p.unavailable_reason())
        self.assertIn("lasair", registry.registered_slugs())
        self.assertNotIn("lasair", [q.slug for q in registry.enabled_providers()])
        self.assertIn("lasair", [q.slug for q in registry.all_providers()])
        with self.assertRaises(BrokerUnavailable):
            p.get_alert("ZTF26lasair1")
        with self.assertRaises(BrokerUnavailable):
            registry.get_provider("lasair", require_available=True)
        self.assertEqual(p.describe()["stream"]["bootstrap_servers"], "kafka.lsst.ac.uk:9092")

    def test_query_alerts_cone_photometry_with_token(self):
        calls = []
        p = lasair_mod.LasairProvider(credential={"token": "tok"})
        self.assertTrue(p.available())
        with mock.patch.object(lasair_mod, "request_json", side_effect=lasair_router(calls)):
            alerts = p.query_alerts({"topic": "lasair_2SN-likecandidates", "limit": 10})
            self.assertEqual([a.object_id for a in alerts], ["ZTF26lasair1", "ZTF26lasair2"])
            a = alerts[0]
            self.assertEqual((a.mag, a.band, a.rb, a.drb, a.ndet, a.classification), (18.6, "r", 0.9, 0.97, 4, "SN"))
            self.assertAlmostEqual(a.mjd, 2461300.3 - 2400000.5, places=5)
            self.assertAlmostEqual(a.discovery_mjd, LASAIR_OBJECT["objectData"]["jdmin"] - 2400000.5, places=5)
            self.assertTrue(a.is_positive)
            self.assertEqual(a.url, "https://lasair-ztf.lsst.ac.uk/objects/ZTF26lasair1/")
            self.assertEqual(calls[0][1], "https://lasair-ztf.lsst.ac.uk/api/streams/lasair_2SN-likecandidates/")
            self.assertEqual(calls[0][3]["Authorization"], "Token tok")
            self.assertEqual(calls[1][2]["objectIds"], "ZTF26lasair1,ZTF26lasair2")
            cone = p.cone_search(150.0, 40.0, 5.0)
            self.assertEqual([c.object_id for c in cone], ["ZTF26lasair1"])
            self.assertEqual(calls[-2][2]["requestType"], "all")
            pts = p.get_photometry("ZTF26lasair1")
            self.assertEqual(len(pts), 3)
            self.assertEqual([q["mag"] for q in pts], [None, 19.2, 18.6])
            self.assertEqual(pts[0]["limit"], 20.4)
            with self.assertRaises(ValueError):
                p.query_alerts({"bogus": 1})

    def test_stream_message_parsing(self):
        p = lasair_mod.LasairProvider(credential={"token": "tok"})
        full = {"objectId": "ZTF26lasair9", "ramean": 1.0, "decmean": 2.0, "magpsf": 18.0, "fid": 2, "rb": 0.7, "ncand": 2}
        with mock.patch.object(lasair_mod, "request_json", side_effect=AssertionError("no HTTP expected")):
            a = p.parse_stream_message("lasair_x", full)
            self.assertEqual((a.object_id, a.ra, a.dec, a.mag, a.band, a.ndet), ("ZTF26lasair9", 1.0, 2.0, 18.0, "r", 2))
            self.assertIsNone(p.parse_stream_message("lasair_x", {"nothing": 1}))
        calls = []
        with mock.patch.object(lasair_mod, "request_json", side_effect=lasair_router(calls)):
            a = p.parse_stream_message("lasair_x", {"objectId": "ZTF26lasair1"})
            self.assertEqual((a.object_id, a.mag), ("ZTF26lasair1", 18.6))
            self.assertIn("/objects/", calls[0][1])
        # no token: the row alone, or None when it has no position
        q = lasair_mod.LasairProvider()
        self.assertIsNone(q.parse_stream_message("t", {"objectId": "ZTF26lasair1"}))
        self.assertEqual(q.parse_stream_message("t", full).object_id, "ZTF26lasair9")


# --- stream parsers and decoding ------------------------------------------------------

class StreamParserTests(SimpleTestCase):
    def test_fink_packet(self):
        packet = {"objectId": "ZTF26fink001", "candidate": {"candid": 55, "jd": 2461300.1, "fid": 1, "magpsf": 18.9,
                                                              "sigmapsf": 0.06, "ra": 12.0, "dec": -3.0, "rb": 0.85, "drb": 0.99,
                                                              "isdiffpos": "t", "ndethist": 5, "jdstarthist": 2461298.0,
                                                              "sgscore1": 0.2, "distpsnr1": 1.5},
                  "cdsxmatch": "Unknown", "snn_snia_vs_nonia": 0.91, "rf_snia_vs_nonia": 0.7, "finkclass": "Early SN Ia candidate",
                  "cutoutScience": {"stampData": b"\x00\x01"}}
        p = fink_mod.FinkProvider()
        a = p.parse_stream_message("fink_early_sn_candidates_ztf", packet)
        self.assertEqual((a.object_id, a.alert_id, a.mag, a.band, a.rb, a.ndet), ("ZTF26fink001", "55", 18.9, "g", 0.85, 5))
        self.assertEqual(a.classification, "Early SN Ia candidate")
        self.assertEqual(a.class_probabilities["SN Ia (SNN)"], 0.91)
        self.assertIn("science", a.cutout_urls)
        self.assertNotIn("cutoutScience", a.properties)
        self.assertIsNone(p.parse_stream_message("t", {"objectId": "x"}))
        self.assertIsNone(p.parse_stream_message("t", "junk"))
        self.assertIn(STREAM, p.capability_set())

    def test_antares_dict_locus(self):
        p = antares_mod.AntaresProvider()
        a = p.parse_stream_message("extragalactic", {"locus_id": "ANT2026abc", "ra": 100.0, "dec": 10.0,
                                                     "properties": {"ztf_object_id": "ZTF26ant0001", "newest_alert_magnitude": 18.2,
                                                                    "newest_alert_observation_time": NOW_MJD, "ztf_rb": 0.8,
                                                                    "num_mag_values": 7},
                                                     "tags": ["extragalactic"]})
        self.assertEqual((a.object_id, a.mag, a.rb, a.ndet, a.classification), ("ZTF26ant0001", 18.2, 0.8, 7, "extragalactic"))
        self.assertIsNone(p.parse_stream_message("t", {"foo": 1}))
        self.assertIsNone(p.parse_stream_message("t", None))

    def test_decode_value(self):
        self.assertEqual(streams.decode_value(b'{"a": 1}', "json"), {"a": 1})
        self.assertEqual(streams.decode_value({"a": 2}, "json"), {"a": 2})
        self.assertEqual(streams.decode_value('{"a": 3}', "json"), {"a": 3})
        with self.assertRaises(BrokerError):
            streams.decode_value(b"not json", "json")
        try:
            import fastavro
        except ImportError:
            with self.assertRaises(BrokerUnavailable):
                streams.decode_value(b"\x00", "avro")
        else:
            schema = {"type": "record", "name": "T", "fields": [{"name": "objectId", "type": "string"}]}
            buf = io.BytesIO()
            fastavro.writer(buf, schema, [{"objectId": "ZTF1"}])
            self.assertEqual(streams.decode_value(buf.getvalue(), "avro"), {"objectId": "ZTF1"})

    def test_kafka_client_needs_confluent(self):
        try:
            import confluent_kafka  # noqa: F401
        except ImportError:
            conn = BrokerConnection(slug="x", broker="sfake", bootstrap_servers="k:9092", topics=["t"])
            with self.assertRaises(BrokerUnavailable):
                streams.KafkaStreamClient(conn)


# --- the consumer loop ------------------------------------------------------------------

class ConsumeBase(TestCase):
    def setUp(self):
        StreamFakeProvider.alerts = []
        StreamFakeProvider.photometry = {}
        StreamFakeProvider.fail_cone = False
        StreamFakeProvider.parsed = []
        self.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(self.admin)
        self.group = Group.objects.create(name="yse-scanners")
        self.member = create_test_user("scanner", is_staff=False, email="scanner@example.org")
        self.member.groups.add(self.group)
        self.connection = make_connection(created_by=self.admin)
        self.bf = BrokerFilter.objects.create(name="stream SNe", broker="sfake", group=self.group, created_by=self.admin,
                                              criteria={"mag_max": 20.0, "rb_min": 0.5})
        self.client = Client()


class ConsumeTests(ConsumeBase):
    def messages(self):
        return [msg(record("ZTF26aaaaaa1"), offset=1), msg(record("ZTF26aaaaaa2", ra=151.0, dec=41.0, rb=0.2), offset=2),
                streams.StreamMessage("t1", b"not json", offset=3), msg(record("ZTF26aaaaaa3", ra=152.0, dec=42.0, mag=19.9), offset=4),
                msg({"heartbeat": True}, offset=5), msg(record("BAD"), offset=6)]

    @override_settings(BROKER_STREAM_BATCH_SIZE=4)
    def test_consume_registers_candidates_heartbeat_and_errors(self):
        client = FakeClient(self.messages())
        result = streams.consume(self.connection, client=client, max_messages=6)
        self.assertEqual((result["messages"], result["parsed"], result["passed"], result["new"], result["errors"]), (6, 3, 2, 2, 2))
        self.assertEqual(sorted(Candidate.objects.values_list("alert_id", flat=True)), ["ZTF26aaaaaa1", "ZTF26aaaaaa3"])
        c = Candidate.objects.get(alert_id="ZTF26aaaaaa1")
        self.assertEqual(c.topic, "t1")
        self.assertEqual(list(c.filters.all()), [self.bf])
        self.assertEqual(client.commits, 2)  # two batches of 4 + 2
        self.assertFalse(client.closed)  # injected clients are the caller's
        hb = IngestHeartbeat.objects.get(connection=self.connection, topic="t1", worker=0)
        self.assertEqual((hb.messages, hb.candidates, hb.errors, hb.last_offset), (6, 2, 2, 6))
        self.assertIn("cannot parse BAD", hb.last_error)
        self.assertEqual(hb.status, IngestHeartbeat.STATUS_STOPPED)
        self.assertEqual(hb.last_alert_id, "ZTF26aaaaaa3")
        self.assertEqual(hb.lag, 0)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.last_message_at)
        self.assertIn("cannot parse BAD", self.connection.last_error)

    def test_dry_run_writes_and_commits_nothing(self):
        client = FakeClient(self.messages())
        result = streams.consume(self.connection, client=client, max_messages=6, dry_run=True)
        self.assertEqual(result["passed"], 2)
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(client.commits, 0)
        self.assertTrue(IngestHeartbeat.objects.filter(connection=self.connection).exists())

    def test_disabled_connection_is_refused_unless_forced(self):
        self.connection.enabled = False
        self.connection.save()
        with self.assertRaises(BrokerUnavailable):
            streams.consume(self.connection, client=FakeClient([]), max_messages=1)
        result = streams.consume(self.connection, client=FakeClient([msg(record())]), max_messages=1, force=True)
        self.assertEqual(result["new"], 1)

    def test_unknown_or_streamless_broker(self):
        conn = make_connection(slug="fink-stream", broker="alerce")
        with self.assertRaises(BrokerError):
            streams.consume(conn, client=FakeClient([]), max_messages=1)
        conn2 = make_connection(slug="nope", broker="nope")
        with self.assertRaises(BrokerError):
            streams.consume(conn2, client=FakeClient([]), max_messages=1)

    def test_filter_topic_restriction(self):
        self.bf.topics = ["t2"]
        self.bf.save()
        self.assertTrue(self.bf.applies_to_topic("t2"))
        self.assertFalse(self.bf.applies_to_topic("t1"))
        self.assertTrue(self.bf.applies_to_topic(""))
        streams.consume(self.connection, client=FakeClient([msg(record("ZTF26t1"), topic="t1"), msg(record("ZTF26t2"), topic="t2")]),
                        max_messages=2)
        self.assertEqual(list(Candidate.objects.values_list("alert_id", flat=True)), ["ZTF26t2"])
        self.assertEqual(IngestHeartbeat.objects.filter(connection=self.connection).count(), 2)

    def test_two_workers_match_one_worker(self):
        recorded = [msg(record("ZTF26w%06d" % i, ra=150.0 + i * 0.1, dec=40.0, rb=0.9 if i % 3 else 0.1), offset=i) for i in range(12)]
        streams.consume(self.connection, client=FakeClient(recorded), max_messages=12)
        one = set(Candidate.objects.values_list("alert_id", flat=True))
        Candidate.objects.all().delete()
        IngestHeartbeat.objects.all().delete()
        # a consumer group hands each worker a share of the partitions
        streams.consume(self.connection, worker=0, client=FakeClient(recorded[0::2]), max_messages=6)
        streams.consume(self.connection, worker=1, client=FakeClient(recorded[1::2]), max_messages=6)
        two = set(Candidate.objects.values_list("alert_id", flat=True))
        self.assertEqual(one, two)
        self.assertEqual(len(one), 8)
        self.assertEqual(sorted(IngestHeartbeat.objects.values_list("worker", flat=True)), [0, 1])

    def test_auto_save_default_tags_and_group_notification(self):
        tag = TransientTag.objects.create(name="ZTF stream", created_by=self.admin, modified_by=self.admin)
        self.bf.auto_save = True
        self.bf.notify_group = True
        self.bf.save()
        self.bf.default_tags.add(tag)
        StreamFakeProvider.photometry = {"ZTF26auto001": fake_points(3)}
        result = streams.consume(self.connection, client=FakeClient([msg(record("ZTF26auto001"))]), max_messages=1)
        self.assertEqual((result["new"], result["saved"], result["notified"]), (1, 1, 1))
        t = Transient.objects.get(name="ZTF26auto001")
        self.assertEqual(t.status.name, "New")
        self.assertEqual([x.name for x in t.tags.all()], ["ZTF stream"])
        c = Candidate.objects.get()
        self.assertEqual((c.status, c.transient_id), (Candidate.SAVED, t.pk))
        n = Notification.objects.get(recipient=self.member)
        self.assertEqual(n.kind, "candidate")
        self.assertIn("stream SNe", n.text)
        self.assertIn("filter=%d" % self.bf.pk, n.url)
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())
        # a second alert on the same object does not notify again
        streams.consume(self.connection, client=FakeClient([msg(record("ZTF26auto001", mag=18.0))]), max_messages=1)
        self.assertEqual(Notification.objects.count(), 1)

    def test_client_failure_marks_heartbeat_error(self):
        client = FakeClient([msg(record("ZTF26fail01"))] * 1, fail_after=1)
        with self.assertRaises(BrokerUnavailable):
            streams.consume(self.connection, client=client, max_messages=5)
        hb = IngestHeartbeat.objects.get(connection=self.connection, topic="t1")
        self.assertEqual(hb.status, IngestHeartbeat.STATUS_ERROR)
        self.assertIn("broker gone", hb.last_error)
        self.assertEqual(Candidate.objects.count(), 1)

    def test_idle_exit_and_stop_callback(self):
        result = streams.consume(self.connection, client=FakeClient([]), idle_exit=True)
        self.assertEqual(result["messages"], 0)
        result = streams.consume(self.connection, client=FakeClient([msg(record())]), stop=lambda: True)
        self.assertEqual(result["messages"], 0)

    def test_stale_heartbeats_and_dashboard_banner(self):
        hb = IngestHeartbeat.objects.create(connection=self.connection, topic="t1", worker=0,
                                            last_seen=timezone.now() - datetime.timedelta(minutes=40), last_error="kafka: timed out")
        fresh = IngestHeartbeat.objects.create(connection=self.connection, topic="t2", worker=0, last_seen=timezone.now())
        stale = streams.stale_heartbeats(15)
        self.assertEqual([h.pk for h in stale], [hb.pk])
        self.assertTrue(hb.is_stale(15))
        self.assertFalse(fresh.is_stale(15))
        status = streams.stream_status()
        self.assertEqual([s["stale"] for s in status], [True, False])
        self.client.force_login(self.admin)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "yse-broker-stale-banner")
        self.assertContains(r, "fake-stream/t1#0")
        # a stopped worker or a disabled connection is not a problem
        hb.status = IngestHeartbeat.STATUS_STOPPED
        hb.save()
        self.assertEqual(streams.stale_heartbeats(15), [])
        hb.status = IngestHeartbeat.STATUS_RUNNING
        hb.save()
        self.connection.enabled = False
        self.connection.save()
        self.assertEqual(streams.stale_heartbeats(15), [])
        r = self.client.get(reverse("dashboard"))
        self.assertNotContains(r, "yse-broker-stale-banner")

    def test_command_list_consume_and_workers(self):
        out = io.StringIO()
        call_command("broker_ingest", "--list", stdout=out)
        self.assertIn("fake-stream", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(streams, "open_client", return_value=FakeClient([msg(record("ZTF26cmd0001")), msg(record("ZTF26cmd0002", ra=1.0, dec=1.0))])):
            call_command("broker_ingest", "--connection", "fake-stream", "--max-messages", "2", "--since", "2026-09-01T00:00:00Z", stdout=out)
        result = json.loads(out.getvalue())
        self.assertEqual(result["new"], 2)
        out = io.StringIO()
        call_command("broker_ingest", "--list", "--json", stdout=out)
        listing = json.loads(out.getvalue())
        self.assertEqual(listing["heartbeats"][0]["messages"], 2)
        with self.assertRaises(Exception):
            call_command("broker_ingest", "--connection", "missing")

        class InlineProc:
            exitcode = 0

            def join(self):
                pass

        spawned = []

        def inline_spawn(connection, worker, kwargs):
            spawned.append(worker)
            streams.consume(connection, worker=worker, client=FakeClient([msg(record("ZTF26mp%03d" % worker, ra=5.0 + worker, dec=5.0))]), **kwargs)
            return InlineProc()

        out = io.StringIO()
        with mock.patch.object(streams, "spawn_worker", side_effect=inline_spawn):
            call_command("broker_ingest", "--connection", "fake-stream", "--workers", "2", "--max-messages", "1", stdout=out)
        self.assertEqual(spawned, [0, 1])
        self.assertIn("2 ok, 0 failed", out.getvalue())
        self.assertEqual(Candidate.objects.filter(alert_id__startswith="ZTF26mp").count(), 2)

    def test_connection_clean_and_api(self):
        conn = BrokerConnection(name="bad", slug="bad", broker="nope", topics=[])
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            conn.full_clean()
        conn = BrokerConnection(name="ok", slug="ok", broker="sfake", bootstrap_servers="k:9092", topics=["a"])
        conn.full_clean()
        self.client.force_login(self.member)
        r = self.client.get("/api/brokerconnections/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"][0]["slug"], "fake-stream") if "results" in r.json() else self.assertEqual(r.json()[0]["slug"], "fake-stream")
        IngestHeartbeat.objects.create(connection=self.connection, topic="t1", worker=0, last_seen=timezone.now() - datetime.timedelta(hours=1))
        r = self.client.get("/api/brokerconnections/heartbeats/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stale"], 1)
        r = self.client.get("/api/brokers/")
        sfake = next(b for b in r.json()["brokers"] if b["slug"] == "sfake")
        self.assertIn("stream", sfake["capabilities"])


# --- filter versions and preview (#277) --------------------------------------------------

class FilterVersionPreviewTests(ConsumeBase):
    def test_versions_recorded_on_api_edits(self):
        self.client.force_login(self.member)
        r = self.client.post("/api/brokerfilters/", data=json.dumps({
            "name": "api filter", "broker": "sfake", "group": self.group.pk, "criteria": {"mag_max": 19.0}, "query": {"classes": ["SN"]}}),
            content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        fid = r.json()["id"]
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(BrokerFilterVersion.objects.filter(filter_id=fid).count(), 1)
        r = self.client.patch("/api/brokerfilters/%d/" % fid, data=json.dumps({"criteria": {"mag_max": 18.5, "rb_min": 0.7}}),
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["version"], 2)
        r = self.client.patch("/api/brokerfilters/%d/" % fid, data=json.dumps({"description": "words only"}), content_type="application/json")
        self.assertEqual(r.json()["version"], 2)
        r = self.client.get("/api/brokerfilters/%d/versions/" % fid)
        rows = r.json()
        self.assertEqual([v["version"] for v in rows], [2, 1])
        self.assertEqual(rows[0]["criteria"], {"mag_max": 18.5, "rb_min": 0.7})
        self.assertEqual(rows[1]["criteria"], {"mag_max": 19.0})
        self.assertTrue(rows[0]["is_current"])
        self.assertFalse(rows[1]["is_current"])
        self.assertEqual(rows[0]["saved_by"], "scanner")
        r = self.client.patch("/api/brokerfilters/%d/" % fid, data=json.dumps({"criteria": {"nonsense": 1}}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/brokerfilters/%d/" % fid, data=json.dumps({"topics": ["t1"], "default_tags": []}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["version"], 3)

    def test_model_record_version_is_idempotent(self):
        self.assertIsNotNone(self.bf.record_version(self.admin, "created"))
        self.assertIsNone(self.bf.record_version(self.admin, "again"))
        self.bf.criteria = {"mag_max": 19.5}
        self.bf.save()
        v = self.bf.record_version(self.admin)
        self.assertEqual((v.version, self.bf.version), (2, 2))

    def test_preview_counts_without_writing(self):
        StreamFakeProvider.alerts = [StreamFakeProvider.to_alert(record("ZTF26p1")), StreamFakeProvider.to_alert(record("ZTF26p2", mag=21.0)),
                                     StreamFakeProvider.to_alert(record("ZTF26p3", rb=0.1))]
        self.client.force_login(self.member)
        r = self.client.get("/api/brokerfilters/%d/preview/?n=10" % self.bf.pk)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["fetched"], body["passed"], body["failed"]), (3, 1, 2))
        failed = {a["object_id"]: a["failed"] for a in body["alerts"]}
        self.assertEqual(failed, {"ZTF26p1": [], "ZTF26p2": ["mag_max"], "ZTF26p3": ["rb_min"]})
        self.assertEqual(Candidate.objects.count(), 0)
        r = self.client.post("/api/brokerfilters/%d/preview/" % self.bf.pk, data=json.dumps({"criteria": {"mag_max": 22.0}}),
                             content_type="application/json")
        self.assertEqual(r.json()["passed"], 3)
        self.assertEqual(self.bf.criteria, {"mag_max": 20.0, "rb_min": 0.5})  # not saved
        r = self.client.post("/api/brokerfilters/%d/preview/" % self.bf.pk, data=json.dumps({"criteria": {"bogus": 1}}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/brokerfilters/%d/preview/" % self.bf.pk, data=json.dumps({"query": {"nope": 1}}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.client.force_login(create_test_user("stranger", is_staff=False))
        r = self.client.get("/api/brokerfilters/%d/preview/" % self.bf.pk)
        self.assertEqual(r.status_code, 404)


# --- per-group rejection (#279) ------------------------------------------------------------

class GroupRejectionTests(ConsumeBase):
    def setUp(self):
        super().setUp()
        self.group_b = Group.objects.create(name="other-team")
        self.member_b = create_test_user("scanner_b", is_staff=False)
        self.member_b.groups.add(self.group_b)
        self.bf_b = BrokerFilter.objects.create(name="team B SNe", broker="sfake", group=self.group_b, criteria={})
        streams.consume(self.connection, client=FakeClient([msg(record("ZTF26both001"))]), max_messages=1)
        self.candidate = Candidate.objects.get()
        self.assertEqual(self.candidate.filters.count(), 2)

    def test_group_rejection_hides_only_for_that_group(self):
        self.client.force_login(self.member)
        r = self.client.post(reverse("candidate_reject", args=[self.candidate.pk]), {"note": "bogus"})
        self.assertEqual(r.status_code, 302)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, Candidate.NEW)
        self.assertEqual([g.name for g in self.candidate.rejected_by_groups.all()], ["yse-scanners"])
        self.assertEqual(self.candidate.note, "bogus")
        row = 'data-candidate-id="%d"' % self.candidate.pk
        r = self.client.get(reverse("candidate_list"))  # the flash message names it; the table must not
        self.assertNotContains(r, row)
        r = self.client.get("/api/candidates/")
        self.assertEqual(r.json()["count"] if isinstance(r.json(), dict) else len(r.json()), 0)
        r = self.client.get("/api/candidates/?include_rejected=1")
        self.assertEqual(r.json()["count"] if isinstance(r.json(), dict) else len(r.json()), 1)
        self.client.force_login(self.member_b)
        r = self.client.get(reverse("candidate_list"))
        self.assertContains(r, row)
        self.assertContains(r, "rejected by yse-scanners")
        r = self.client.post(reverse("candidate_reject", args=[self.candidate.pk]), {})
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, Candidate.REJECTED)
        self.assertEqual(self.candidate.rejected_by_groups.count(), 2)
        # staff still sees it (all statuses) and a re-open by A clears A only
        self.client.force_login(self.admin)
        r = self.client.get(reverse("candidate_list") + "?status=all")
        self.assertContains(r, row)
        self.client.force_login(self.member)
        self.client.post(reverse("candidate_reopen", args=[self.candidate.pk]), {})
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, Candidate.NEW)
        self.assertEqual([g.name for g in self.candidate.rejected_by_groups.all()], ["other-team"])
        r = self.client.get(reverse("candidate_list"))
        self.assertContains(r, row)

    def test_staff_and_scope_all_reject_globally(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("candidate_reject", args=[self.candidate.pk]), {})
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, Candidate.REJECTED)
        ingest.reopen_candidate(self.candidate, self.admin)
        self.candidate.refresh_from_db()
        self.assertEqual((self.candidate.status, self.candidate.rejected_by_groups.count()), (Candidate.NEW, 0))
        self.client.force_login(self.member)
        r = self.client.post("/api/candidates/%d/reject/" % self.candidate.pk, data=json.dumps({"scope": "all", "note": "junk"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], Candidate.REJECTED)

    def test_shared_filter_rejection_is_global(self):
        shared = BrokerFilter.objects.create(name="shared", broker="sfake", group=None, criteria={})
        streams.consume(self.connection, client=FakeClient([msg(record("ZTF26shared1", ra=10.0, dec=10.0))]), max_messages=1)
        c = Candidate.objects.get(alert_id="ZTF26shared1")
        self.assertIn(shared, c.filters.all())
        ingest.reject_candidate(c, self.member)
        c.refresh_from_db()
        self.assertEqual(c.status, Candidate.REJECTED)


# --- Brokers tab and cone search page (#275) ------------------------------------------------

@override_settings(BROKERS_ENABLED=["sfake", "lasair"])  # no live broker: the page must never reach the network in CI
class BrokersTabAndSearchTests(ConsumeBase):
    def setUp(self):
        super().setUp()
        self.transient = create_minimal_transient(self.admin, name="2026brk", status_name="Following", obs_group_name="YSE",
                                                  ra=150.0, dec=40.0)
        StreamFakeProvider.alerts = [StreamFakeProvider.to_alert(record("ZTF26tab0001", ra=150.0003, dec=40.0002)),
                                     StreamFakeProvider.to_alert(record("ZTF26far0001", ra=10.0, dec=10.0))]
        StreamFakeProvider.photometry = {"ZTF26tab0001": fake_points(2), "ZTF26far0001": fake_points(2)}

    def test_fragment_lists_matches_and_inline_errors(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse("transient_detail_brokers_fragment", args=[self.transient.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "ZTF26tab0001")
        self.assertNotContains(r, "ZTF26far0001")
        self.assertContains(r, "yse-broker-import")
        self.assertContains(r, "ZTF26tab0001-sci.png")
        self.assertContains(r, 'data-broker="sfake" data-error="0"')
        self.assertContains(r, "unavailable: no active EncryptedCredential")  # lasair, inline, no 500
        StreamFakeProvider.fail_cone = True
        r = self.client.get(reverse("transient_detail_brokers_fragment", args=[self.transient.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "yse-broker-error")
        self.assertContains(r, "broker is down")
        # the tab shell is on the detail page
        r = self.client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertContains(r, 'href="#brokers_tab"')
        self.assertContains(r, reverse("transient_detail_brokers_fragment", args=[self.transient.pk]))
        r = self.client.get(reverse("transient_detail_brokers_fragment", args=[self.transient.pk]).replace("/brokers_fragment", "/brokers_fragment"), HTTP_ACCEPT="text/html")
        self.assertEqual(r.status_code, 200)

    def test_import_photometry_button_queues_job_and_alias(self):
        self.client.force_login(self.member)
        r = self.client.post(reverse("transient_broker_import", args=[self.transient.pk]),
                             {"broker": "sfake", "object_id": "ZTF26tab0001", "alias": "1"})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["ok"])
        job = Job.objects.get(pk=body["job"])
        self.assertEqual(job.kind, "brokers.import_photometry")
        self.assertEqual(job.payload["object_id"], "ZTF26tab0001")
        self.assertTrue(self.transient.alternatetransientnames_set.filter(name="ZTF26tab0001").exists())
        from YSE_App.jobs import run_pass
        run_pass()
        job.refresh_from_db()
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual(job.result["points"], 2)
        r = self.client.post(reverse("transient_broker_import", args=[self.transient.pk]), {"broker": "nope", "object_id": "x"})
        self.assertEqual(r.status_code, 400)

    def test_search_page_and_save_as_transient(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse("broker_search"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "yse-broker-search-form")
        r = self.client.get(reverse("broker_search"), {"ra": "10.0", "dec": "10.0", "radius": "5"})
        self.assertContains(r, "ZTF26far0001")
        self.assertContains(r, "Save as transient")
        self.assertNotContains(r, "ZTF26tab0001")
        r = self.client.get(reverse("broker_search"), {"ra": "00:40:00", "dec": "+10:00:00"})
        self.assertContains(r, "ZTF26far0001")
        r = self.client.get(reverse("broker_search"), {"name": "2026brk"})
        self.assertContains(r, "ZTF26tab0001")
        self.assertContains(r, "Already in YSE-PZ")
        r = self.client.get(reverse("broker_search"), {"name": "ZTF26far0001"})
        self.assertContains(r, "ZTF26far0001")
        r = self.client.get(reverse("broker_search"), {"name": "nothing-here"})
        self.assertContains(r, "no transient or broker object named")
        r = self.client.get(reverse("broker_search"), {"ra": "abc", "dec": "def"})
        self.assertContains(r, "could not parse")
        r = self.client.post(reverse("broker_search_save"), {"broker": "sfake", "object_id": "ZTF26far0001", "status": "New",
                                                            "next": reverse("broker_search") + "?ra=10&dec=10"})
        self.assertEqual(r.status_code, 302)
        t = Transient.objects.get(name="ZTF26far0001")
        self.assertEqual((t.status.name, t.obs_group.name), ("New", "ZTF"))
        self.assertEqual(t.ra, 10.0)
        c = Candidate.objects.get(alert_id="ZTF26far0001")
        self.assertEqual((c.status, c.transient_id), (Candidate.SAVED, t.pk))
        r = self.client.get(reverse("broker_search"), {"ra": "10.0", "dec": "10.0"})
        self.assertContains(r, "In YSE-PZ: ZTF26far0001")
        r = self.client.post(reverse("broker_search_save"), {"broker": "sfake", "object_id": "ZTF26missing"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 400)
        self.assertIn("knows no object", r.json()["error"])
        r = self.client.get(reverse("candidate_list"))
        self.assertContains(r, reverse("broker_search"))

    def test_login_required(self):
        for url in (reverse("broker_search"), reverse("transient_detail_brokers_fragment", args=[self.transient.pk])):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])
