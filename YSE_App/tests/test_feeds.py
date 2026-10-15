"""Other feeds (#280): Hermes / SCiMMA (#281, publishing from #326), Einstein Probe (#282), JPL Scout (#283).

Every external call (Hermes REST, Kafka, GCN, Scout, sb_ident) is mocked; the tests run on sqlite and MySQL.
"""

from __future__ import annotations

import datetime
import json
from unittest import mock

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App.brokers import ingest, registry
from YSE_App.data_ingest.Feeds import FeedPoll, MinorPlanetScreen, enqueue_polls
from YSE_App.feeds import base as feeds_base
from YSE_App.feeds import einstein_probe as ep_mod
from YSE_App.feeds import hermes as hermes_mod
from YSE_App.feeds import scout as scout_mod
from YSE_App.feeds import registry as feed_registry
from YSE_App.feeds.jobs import POLL_KIND, SCREEN_KIND, enqueue_poll, enqueue_screen, run_source
from YSE_App.jobs import run_pass
from YSE_App.models import (
    AlternateTransientNames,
    Candidate,
    EncryptedCredential,
    FeedSource,
    InformationSource,
    Job,
    Log,
    SharingService,
    SharingSubmission,
    Transient,
    TransientAnnotation,
    TransientClass,
    TransientPhotData,
    TransientWebResource,
)
from YSE_App.sharing import tns
from YSE_App.tests.fixtures_minimal import (
    create_minimal_transient,
    create_test_user,
    create_transient_with_synthetic_data,
    ensure_transient_statuses,
)

KEY = "u2JX5JY4q0Z5m8N6pQ9rS1tU3vW7xY0zA2bC4dE6fG8="
UTC = datetime.timezone.utc


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def fake_get(routes):
    """``requests.get`` replacement: routes maps a URL substring to a payload (or a FakeResponse)."""
    calls = []

    def _get(url, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        for needle, payload in routes.items():
            if needle in url:
                return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)
        return FakeResponse({"detail": "not found"}, status_code=404)

    _get.calls = calls
    return _get


# --- recorded messages ------------------------------------------------------------------

def hermes_message(name="2026hrm", topic="hermes.discovery", uuid="11111111-2222-3333-4444-555555555555", *, ra=150.0,
                   dec=40.0, group="YSE", classification=None, redshift=None, phot=True, aliases=None,
                   published="2026-09-28T10:00:00Z"):
    data = {
        "targets": [{
            "name": name, "ra": ra, "dec": dec, "ra_error": 0.5, "ra_error_units": "arcsec",
            "discovery_info": {"reporting_group": group, "discovery_source": "YSE", "date": "2026-09-27T08:30:00",
                               "transient_type": "AT"},
        }],
        "photometry": [],
        "references": [],
    }
    if aliases:
        data["targets"][0]["aliases"] = list(aliases)
    if redshift is not None:
        data["targets"][0]["redshift"] = redshift
    if phot:
        data["photometry"] = [
            {"target_name": name, "date_obs": "2026-09-26T08:00:00", "telescope": "PS1", "instrument": "GPC1",
             "bandpass": "r", "limiting_brightness": 21.5, "brightness_unit": "AB mag"},
            {"target_name": name, "date_obs": "2026-09-27T08:30:00", "telescope": "PS1", "instrument": "GPC1",
             "bandpass": "g", "brightness": 19.2, "brightness_error": 0.05, "brightness_unit": "AB mag"},
            {"target_name": name, "date_obs": "2026-09-28T08:30:00", "telescope": "PS1", "instrument": "GPC1",
             "bandpass": "r", "brightness": 18.9, "brightness_error": 0.04, "brightness_unit": "AB mag"},
        ]
    if classification:
        data["spectroscopy"] = [{"target_name": name, "date_obs": "2026-09-28T12:00:00", "telescope": "Keck I",
                                 "instrument": "LRIS", "classification": classification, "redshift": redshift}]
    return {"id": 4242, "uuid": uuid, "topic": topic, "title": "%s report" % name, "submitter": "yse-bot",
            "authors": "YSE team", "message_text": "Discovery of %s." % name, "published": published, "data": data}


def ep_notice(ident="01709131705", ra=120.5, dec=-30.25, error_deg=0.05, trigger="2026-09-28T02:15:00Z", snr=8.5):
    return {"$schema": "https://gcn.nasa.gov/schema/main/gcn/notices/einstein_probe/wxt/alert.schema.json",
            "id": [ident], "ra": ra, "dec": dec, "ra_dec_error": error_deg, "trigger_time": trigger,
            "instrument": "WXT", "image_energy_range": [0.5, 4.0], "net_count_rate": 1.7, "image_snr": snr,
            "additional_info": "Fast X-ray transient candidate."}


SCOUT_LIST = {"count": "3", "data": [
    {"objectName": "P22abcD", "ra": "10:30:00.0", "dec": "+15:00:00", "neoScore": "100", "rating": "4", "Vmag": "19.8",
     "unc": "2.5", "rate": "1.20", "nObs": "12", "arc": "3.1", "lastRun": "2026-09-28 06:00", "H": "24.1", "moid": "0.01",
     "tisserandScore": "100", "geocentricScore": "0", "phaScore": "35", "ieoScore": "0", "caDist": "0.02", "vInf": "8.1",
     "elong": "150"},
    {"objectName": "C55xyzQ", "ra": "22:10:00.0", "dec": "-05:30:00", "neoScore": "12", "rating": "1", "Vmag": "21.2",
     "unc": "40", "rate": "0.05", "nObs": "4", "arc": "0.5", "lastRun": "2026-09-28 06:00"},
    {"objectName": "bad", "ra": "nope", "dec": "x"},
]}


def sbident_body(rows):
    return {"signature": {"source": "NASA/JPL Small-Body Identification API", "version": "1.0"},
            "n_second_pass": len(rows),
            "fields_second": ["Object name", "Astrometric RA (hh:mm:ss)", "Astrometric Dec (dd mm'ss\")",
                              "Dist. from center RA (\")", "Dist. from center Dec (\")", "Dist. from center Norm (\")",
                              "Visual magnitude (V)", "RA rate (\"/h)", "Dec rate (\"/h)"],
            "data_second_pass": rows}


SBIDENT_HIT = sbident_body([["12345 Example (2001 AB1)", "10:00:00.01", "+20 00'01\"", "1.2", "-2.9", "3.14", "18.6", "35.2", "-12.1"],
                            ["(2020 XY99)", "10:00:04.00", "+20 00'40\"", "55.0", "40.0", "68.0", "21.3", "5.0", "1.0"]])
SBIDENT_CLEAN = sbident_body([["(2020 XY99)", "10:00:04.00", "+20 00'40\"", "55.0", "40.0", "68.0", "21.3", "5.0", "1.0"]])
SBIDENT_EMPTY = {"signature": {}, "n_second_pass": 0}


# --- unit tests without a database ---------------------------------------------------------

class ParsingTests(SimpleTestCase):
    def test_parse_coord_accepts_degrees_and_sexagesimal(self):
        self.assertEqual(feeds_base.parse_coord(150.0, -20.5), (150.0, -20.5))
        ra, dec = feeds_base.parse_coord("10:30:00.0", "+15:00:00")
        self.assertAlmostEqual(ra, 157.5, places=6)
        self.assertAlmostEqual(dec, 15.0, places=6)
        self.assertEqual(feeds_base.parse_coord("nope", "x"), (None, None))

    def test_parse_time_mjd(self):
        self.assertAlmostEqual(feeds_base.parse_time_mjd("2026-09-28T00:00:00Z"), 61311.0, places=6)
        self.assertAlmostEqual(feeds_base.parse_time_mjd(2461311.5), 61311.0, places=6)
        self.assertAlmostEqual(feeds_base.parse_time_mjd(61311.25), 61311.25)
        self.assertIsNone(feeds_base.parse_time_mjd("not a date"))

    def test_hermes_message_to_feed_messages(self):
        msgs = hermes_mod.parse_message(hermes_message(classification="SN Ia", redshift=0.031, aliases=["ZTF26abc"]))
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual((m.feed, m.object_id, m.kind), ("hermes", "2026hrm", feeds_base.KIND_DISCOVERY))
        self.assertEqual((m.ra, m.dec, m.reporter, m.classification, m.redshift), (150.0, 40.0, "YSE", "SN Ia", 0.031))
        self.assertEqual(m.aliases, ["ZTF26abc"])
        self.assertEqual(m.error_radius_arcsec, 0.5)
        self.assertEqual(len(m.photometry), 3)
        detections = [p for p in m.photometry if p["mag"] is not None]
        self.assertEqual([p["band"] for p in detections], ["g", "r"])
        self.assertEqual(detections[0]["instrument"], "GPC1")
        self.assertEqual(m.mag, 18.9)
        self.assertTrue(m.url.endswith("/message/11111111-2222-3333-4444-555555555555"))
        alert = m.to_alert(obs_group="YSE", instrument="Unknown")
        self.assertEqual((alert.broker, alert.object_id, alert.ndet, alert.band), ("hermes", "2026hrm", 2, "r"))
        self.assertAlmostEqual(alert.discovery_mjd, feeds_base.parse_time_mjd("2026-09-27T08:30:00"))
        self.assertEqual(len(feeds_base.points_from_payload(alert.properties)), 3)

    def test_hermes_classification_topic_kind_and_flux_units(self):
        message = hermes_message(topic="hermes.classification", classification="SN II", phot=False)
        del message["data"]["targets"][0]["discovery_info"]
        m = hermes_mod.parse_message(message)[0]
        self.assertEqual(m.kind, feeds_base.KIND_CLASSIFICATION)
        pts = hermes_mod.photometry_points([{"date_obs": "2026-09-28T00:00:00", "bandpass": "g-ztf", "brightness": 1000.0,
                                             "brightness_unit": "uJy"}], "")
        self.assertEqual(pts[0]["band"], "g")
        self.assertAlmostEqual(pts[0]["mag"], 16.4, places=3)
        self.assertEqual(hermes_mod.parse_message({"uuid": "x", "topic": "hermes.test", "title": "hello"})[0].kind,
                         feeds_base.KIND_OTHER)
        self.assertEqual(hermes_mod.parse_message("junk"), [])

    def test_ep_notice_parsing(self):
        m = ep_mod.parse_notice(ep_notice())
        self.assertEqual((m.feed, m.object_id, m.kind), ("ep", "EP01709131705", feeds_base.KIND_ALERT))
        self.assertAlmostEqual(m.error_radius_arcsec, 180.0)
        self.assertAlmostEqual(m.time_mjd, feeds_base.parse_time_mjd("2026-09-28T02:15:00Z"))
        self.assertIn("SNR 8.5", m.title)
        self.assertIsNone(ep_mod.parse_notice({"id": ["1"]}))
        self.assertIsNone(ep_mod.parse_notice({"ra": 1, "dec": 2}))

    def test_scout_row_parsing_and_cuts(self):
        m = scout_mod.parse_scout_row(SCOUT_LIST["data"][0])
        self.assertEqual((m.feed, m.object_id, m.kind, m.band), ("scout", "P22abcD", feeds_base.KIND_NEO, "V"))
        self.assertAlmostEqual(m.ra, 157.5)
        self.assertEqual((m.score, m.mag, m.error_radius_arcsec), (1.0, 19.8, 150.0))
        self.assertEqual(m.raw["tag"], "NEO")
        self.assertIsNone(scout_mod.parse_scout_row(SCOUT_LIST["data"][2]))
        provider = scout_mod.ScoutFeed(options={"min_neo_score": 50})
        msgs = [scout_mod.parse_scout_row(r) for r in SCOUT_LIST["data"][:2]]
        self.assertEqual([m.object_id for m in provider.apply_cuts(msgs)], ["P22abcD"])
        self.assertEqual([m.object_id for m in scout_mod.ScoutFeed(options={"max_vmag": 21.0}).apply_cuts(msgs)], ["P22abcD"])

    def test_sbident_params_and_parsing(self):
        params = scout_mod.sbident_params(150.0, -20.5, 61311.0, hwidth_arcmin=1.0, obs_code="T05")
        self.assertEqual(params["fov-ra-center"], "10-00-00.00")
        self.assertEqual(params["fov-dec-center"], "M20-30-00.0")
        self.assertEqual(params["obs-time"], "2461311.500000")
        self.assertEqual(params["mpc-code"], "T05")
        rows = scout_mod.parse_sbident(SBIDENT_HIT)
        self.assertEqual(rows[0]["name"], "12345 Example (2001 AB1)")
        self.assertAlmostEqual(rows[0]["separation_arcsec"], 3.14)
        self.assertEqual(rows[0]["vmag"], 18.6)
        self.assertEqual(scout_mod.parse_sbident(SBIDENT_EMPTY), [])
        with self.assertRaises(feeds_base.FeedError):
            scout_mod.parse_sbident([])

    def test_feed_providers_are_brokers_and_validate_config(self):
        self.assertEqual(set(feed_registry.feed_classes()), {"hermes", "ep", "scout"})
        for slug in ("hermes", "ep", "scout"):
            self.assertIn(slug, registry.registered_slugs())
            cls = registry.provider_class(slug)
            self.assertTrue(issubclass(cls, feeds_base.FeedProvider))
            self.assertIn("save_as_transient", cls.capability_set())
        provider = hermes_mod.HermesFeed()
        self.assertEqual(provider.validate_config({"criteria": {"mag_max": 20}, "since_hours": 6})["criteria"], {"mag_max": 20.0})
        with self.assertRaises(ValueError):
            provider.validate_config({"nope": 1})
        with self.assertRaises(ValueError):
            provider.validate_config({"criteria": {"unknown_key": 1}})
        described = {d["slug"]: d for d in feed_registry.describe_all()}
        self.assertEqual(described["ep"]["feed_kind"], "ep")
        self.assertIn("url", described["ep"]["config_keys"])

    def test_hermes_client_pages_and_submits(self):
        page1 = {"count": 3, "next": "https://hermes.example/api/v0/messages/?limit=2&offset=2",
                 "results": [hermes_message(uuid="a"), hermes_message(uuid="b")]}
        page2 = {"count": 3, "next": None, "results": [hermes_message(uuid="c")]}
        responses = iter([FakeResponse(page1), FakeResponse(page2)])

        def _get(url, params=None, headers=None, timeout=None):
            _get.calls.append((url, dict(params or {}), dict(headers or {})))
            return next(responses)
        _get.calls = []
        client = hermes_mod.HermesClient(token="tok", base_url="https://hermes.example/api/v0")
        with mock.patch.object(feeds_base.requests, "get", _get):
            messages = client.list_messages("hermes.test", since=datetime.datetime(2026, 9, 27, tzinfo=UTC))
        self.assertEqual([m["uuid"] for m in messages], ["a", "b", "c"])
        self.assertEqual(_get.calls[0][1]["topic"], "hermes.test")
        self.assertEqual(_get.calls[0][1]["published_after"], "2026-09-27T00:00:00Z")
        self.assertEqual(_get.calls[0][2]["Authorization"], "Token tok")
        self.assertEqual(_get.calls[1][1], {})
        with mock.patch.object(hermes_mod.requests, "post", return_value=FakeResponse({"uuid": "new-uuid"})) as post:
            self.assertEqual(client.submit({"title": "t"})["uuid"], "new-uuid")
        self.assertTrue(post.call_args[0][0].endswith("/submit_message/"))
        with mock.patch.object(hermes_mod.requests, "post", return_value=FakeResponse({"detail": "bad"}, status_code=400)):
            with self.assertRaises(feeds_base.FeedError):
                client.submit({})
        with self.assertRaises(feeds_base.FeedError):
            hermes_mod.HermesClient(token="").submit({})


# --- database tests --------------------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False,
                   BROKER_AUTO_SAVE_USERNAME="feed_admin")
class FeedBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("feed_admin", is_staff=True, is_superuser=True)
        cls.user = create_test_user("feed_user", is_staff=False)
        ensure_transient_statuses(cls.admin)
        cls.cred = EncryptedCredential(name="Hermes token", service="hermes", kind=EncryptedCredential.KIND_HERMES,
                                       created_by=cls.admin, modified_by=cls.admin)
        cls.cred.set_secret({"hermes_token": "secret-token", "hop_username": "u", "hop_password": "p"})
        cls.cred.save()

    def make_source(self, kind="hermes", slug=None, topic="hermes.discovery", config=None, credential="default", **kw):
        defaults = dict(name="%s feed" % kind, slug=slug or "%s-feed" % kind, kind=kind, topic=topic if kind == "hermes" else "",
                        config=config or {}, created_by=self.admin, credential=self.cred if credential == "default" else credential)
        defaults.update(kw)
        source = FeedSource(**defaults)
        source.full_clean()
        source.save()
        return source


class FeedSourceModelTests(FeedBase):
    def test_hermes_source_needs_topic_and_valid_config(self):
        with self.assertRaises(ValidationError):
            self.make_source(topic="")
        with self.assertRaises(ValidationError):
            self.make_source(config={"bogus": True})
        with self.assertRaises(ValidationError):
            self.make_source(config={"criteria": {"mag_max": "bright"}})
        source = self.make_source(config={"criteria": {"mag_max": 20}, "since_hours": 12})
        self.assertEqual(source.config, {"criteria": {"mag_max": 20.0}, "since_hours": 12})
        self.assertTrue(source.has_credential)
        self.assertEqual(source.secret()["hermes_token"], "secret-token")
        self.assertIn("hermes.discovery", str(source))
        self.assertTrue(source.visible_to(self.user))
        source.enabled = False
        self.assertFalse(source.visible_to(self.user))
        self.assertTrue(source.visible_to(self.admin))

    def test_record_poll_and_secret_without_credential(self):
        source = self.make_source(kind="scout", credential=None)
        self.assertEqual(source.secret(), {})
        source.record_poll("fetched 3", "boom")
        source.refresh_from_db()
        self.assertEqual((source.last_summary, source.last_error), ("fetched 3", "boom"))
        self.assertIsNotNone(source.last_polled)


class HermesFeedTests(FeedBase):
    def run_hermes(self, source, *messages, **kw):
        provider = feed_registry.provider_for(source)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"/messages/": {"count": len(messages), "next": None,
                                                                                    "results": list(messages)}})) as get:
            result = provider.run(source, **kw)
        return result, get

    def test_discovery_for_unknown_object_creates_transient_with_reporter_group(self):
        source = self.make_source()
        result, get = self.run_hermes(source, hermes_message(group="ZTF SNe"))
        self.assertEqual((result["fetched"], result["created"], result["linked"], result["candidates"]), (1, 1, 0, 0))
        self.assertEqual(get.calls[0]["params"]["topic"], "hermes.discovery")
        self.assertEqual(get.calls[0]["headers"]["Authorization"], "Token secret-token")
        t = Transient.objects.get(name="2026hrm")
        self.assertEqual(t.status.name, "New")
        self.assertEqual(t.obs_group.name, "ZTF SNe")
        self.assertAlmostEqual(t.ra, 150.0)
        self.assertIsNotNone(t.disc_date)
        rows = TransientPhotData.objects.filter(photometry__transient=t)
        self.assertEqual(rows.count(), 2)  # the limit is not a row
        self.assertEqual({r.photometry.instrument.name for r in rows}, {"GPC1"})
        self.assertEqual({r.band.name for r in rows}, {"g", "r"})
        resource = TransientWebResource.objects.get(transient=t)
        self.assertEqual(resource.information_source.name, "Hermes")
        self.assertIn("11111111-2222", resource.resource_url)
        self.assertTrue(InformationSource.objects.filter(name="Hermes").exists())
        source.refresh_from_db()
        self.assertIn("created 1", source.last_summary)
        self.assertEqual(source.last_error, "")
        self.assertIsNotNone(source.last_polled)
        self.assertTrue(Log.objects.filter(transient=t, comment__contains="reported by ZTF SNe").exists())
        # Replaying the same message links instead of creating a second transient.
        result2, _ = self.run_hermes(source, hermes_message(group="ZTF SNe"))
        self.assertEqual((result2["created"], result2["linked"]), (0, 1))
        self.assertEqual(Transient.objects.filter(name="2026hrm").count(), 1)
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=t).count(), 2)

    def test_classification_for_known_object_sets_class_and_comments(self):
        source = self.make_source(topic="hermes.classification")
        known = create_minimal_transient(self.admin, name="2026hrm", obs_group_name="YSE", ra=150.0, dec=40.0)
        TransientClass.objects.get_or_create(name="SN Ia", defaults={"created_by": self.admin, "modified_by": self.admin})
        message = hermes_message(topic="hermes.classification", classification="SN Ia", redshift=0.031, phot=False)
        del message["data"]["targets"][0]["discovery_info"]
        result, _ = self.run_hermes(source, message)
        self.assertEqual((result["linked"], result["created"]), (1, 0))
        known.refresh_from_db()
        self.assertEqual(known.best_spec_class.name, "SN Ia")
        self.assertAlmostEqual(known.redshift, 0.031)
        comment = Log.objects.get(transient=known)
        self.assertIn("Hermes classification: SN Ia at z=0.0310 by yse-bot", comment.comment)
        # An unknown class label lands in TNS_spec_class instead.
        message2 = hermes_message(topic="hermes.classification", classification="SLSN-II (weird)", phot=False,
                                  uuid="99999999-0000-0000-0000-000000000000")
        del message2["data"]["targets"][0]["discovery_info"]
        self.run_hermes(source, message2)
        known.refresh_from_db()
        self.assertEqual(known.best_spec_class.name, "SN Ia")
        self.assertEqual(known.TNS_spec_class, "SLSN-II (weird)")
        self.assertEqual(Log.objects.filter(transient=known).count(), 2)

    def test_matching_by_alias_and_cone(self):
        source = self.make_source()
        known = create_minimal_transient(self.admin, name="2026zzz", obs_group_name="YSE", ra=150.0 + 1.0 / 3600, dec=40.0)
        result, _ = self.run_hermes(source, hermes_message(name="ZTF26hermes", phot=False))
        self.assertEqual(result["linked"], 1)
        alias = AlternateTransientNames.objects.get(name="ZTF26hermes")
        self.assertEqual(alias.transient, known)
        self.assertEqual(alias.obs_group.name, "YSE")
        # A later message naming only the alias resolves to the same transient.
        result2, _ = self.run_hermes(source, hermes_message(name="AT 2026zzz", aliases=["ZTF26hermes"], phot=False, ra=10, dec=10))
        self.assertEqual((result2["linked"], result2["created"]), (1, 0))
        self.assertEqual(Transient.objects.count(), 1)
        # Outside the cone and unknown name: a new transient.
        far = self.make_source(slug="far", config={"match_radius_arcsec": 1.0})
        result3, _ = self.run_hermes(far, hermes_message(name="2026far", ra=150.0 + 3.0 / 3600, dec=40.0, phot=False))
        self.assertEqual(result3["created"], 1)

    def test_candidate_mode_criteria_and_save_from_candidate_page(self):
        source = self.make_source(config={"auto_save": False, "criteria": {"mag_max": 19.0}})
        bright = hermes_message(name="2026brt")
        faint = hermes_message(name="2026fnt", uuid="22222222-0000-0000-0000-000000000000")
        for row in faint["data"]["photometry"]:
            if "brightness" in row:
                row["brightness"] += 2.0
        result, _ = self.run_hermes(source, bright, faint)
        self.assertEqual((result["fetched"], result["passed"], result["candidates"], result["skipped"]), (2, 1, 1, 1))
        c = Candidate.objects.get()
        self.assertEqual((c.broker, c.alert_id, c.status, c.last_mag), ("hermes", "2026brt", Candidate.NEW, 18.9))
        self.assertEqual(c.payload["properties"]["reporter"], "YSE")
        self.assertEqual(len(c.payload["properties"]["photometry"]), 3)
        self.assertEqual(Transient.objects.count(), 0)
        # The scanning page lists it and saving goes through the Hermes provider (photometry from the payload).
        client = Client()
        client.force_login(self.user)
        page = client.get(reverse("candidate_list"), {"broker": "hermes"})
        self.assertContains(page, "2026brt")
        response = client.post(reverse("candidate_save", args=[c.pk]), {"status": "Watch"})
        self.assertEqual(response.status_code, 302)
        c.refresh_from_db()
        self.assertEqual(c.status, Candidate.SAVED)
        t = Transient.objects.get(name="2026brt")
        self.assertEqual(t.status.name, "Watch")
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=t).count(), 2)
        # A re-poll of the same object marks nothing twice.
        result2, _ = self.run_hermes(source, bright)
        self.assertEqual(result2["linked"], 1)
        self.assertEqual(Candidate.objects.count(), 1)

    def test_poll_failure_is_recorded_on_the_source(self):
        source = self.make_source()
        provider = feed_registry.provider_for(source)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"/messages/": FakeResponse({"detail": "nope"}, status_code=503)})):
            result = provider.run(source)
        self.assertIn("HTTP 503", result["error"])
        source.refresh_from_db()
        self.assertTrue(source.last_summary.startswith("failed: HTTP 503"))
        self.assertIn("HTTP 503", source.last_error)

    def test_dry_run_and_kind_filter(self):
        source = self.make_source(config={"kinds": ["classification"]})
        result, _ = self.run_hermes(source, hermes_message())
        self.assertEqual((result["fetched"], result["created"]), (0, 0))
        source2 = self.make_source(slug="dry")
        result2, _ = self.run_hermes(source2, hermes_message(), dry_run=True)
        self.assertEqual((result2["passed"], result2["created"]), (1, 0))
        self.assertEqual(Transient.objects.count(), 0)
        source2.refresh_from_db()
        self.assertIsNone(source2.last_polled)

    def test_consume_from_a_fake_hop_stream(self):
        source = self.make_source(topic="hermes.test")

        class FakeStream:
            opened = []

            def open(self, url, mode):
                FakeStream.opened.append((url, mode))
                stream = self

                class _Ctx:
                    def __enter__(self_inner):
                        return iter([type("Msg", (), {"content": hermes_message(topic="hermes.test", name="2026hop")})(),
                                     json.dumps(hermes_message(topic="hermes.test", name="2026hop2", uuid="dead", ra=20.0, dec=-5.0)),
                                     b"not json"])

                    def __exit__(self_inner, *exc):
                        return False
                stream.ctx = _Ctx()
                return stream.ctx

        provider = feed_registry.provider_for(source)
        messages = provider.consume(source, stream=FakeStream(), max_messages=10)
        self.assertEqual([m.object_id for m in messages], ["2026hop", "2026hop2"])
        self.assertEqual(FakeStream.opened, [("kafka://kafka.scimma.org/hermes.test", "r")])
        result = provider.run(source, messages=messages)
        self.assertEqual(result["created"], 2)
        with mock.patch.object(hermes_mod.HermesFeed, "can_consume", return_value=False):
            with self.assertRaises(feeds_base.FeedError):
                provider.consume(source)


class HermesPublishTests(FeedBase):
    def setUp(self):
        self.transient = create_transient_with_synthetic_data(self.admin, name="yse_pub_001", n_phot_points=3,
                                                              with_host=False, with_spectrum=False, with_log=False)
        self.service = SharingService.objects.create(
            name="YSE Hermes", slug="yse-hermes", kind=SharingService.KIND_HERMES, credential=self.cred,
            tns_group_name="YSE", default_coauthors="R. J. Foley (UCSC)", testing=False, hermes_topic="yse.transients",
            created_by=self.admin, modified_by=self.admin)

    def test_build_message_has_targets_and_photometry(self):
        message = hermes_mod.build_message(self.transient, "yse.transients", reporting_group="YSE", authors="A. B.")
        self.assertEqual(message["topic"], "yse.transients")
        target = message["data"]["targets"][0]
        self.assertEqual(target["name"], "yse_pub_001")
        self.assertEqual(target["discovery_info"]["reporting_group"], "YSE")
        self.assertEqual(len(message["data"]["photometry"]), 3)
        self.assertEqual(message["data"]["photometry"][0]["brightness_unit"], "AB mag")
        self.assertEqual(message["authors"], "A. B.")
        TransientClass.objects.get_or_create(name="SN Ia", defaults={"created_by": self.admin, "modified_by": self.admin})
        classified = hermes_mod.build_message(self.transient, "t", kind="classification", classification="SN Ia", redshift=0.02)
        self.assertEqual(classified["data"]["spectroscopy"][0]["classification"], "SN Ia")
        self.assertIn("classification (SN Ia)", classified["title"])

    def test_submission_is_published_through_the_queue(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_HERMES, self.admin, remarks="hello")
        with mock.patch.object(hermes_mod.requests, "post", return_value=FakeResponse({"uuid": "abc-123"})) as post:
            run_pass()
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_ACCEPTED)
        self.assertEqual(submission.external_id, "abc-123")
        sent = json.loads(post.call_args[1]["data"])
        self.assertEqual(sent["topic"], "yse.transients")
        self.assertEqual(sent["message_text"], "hello")
        self.assertEqual(post.call_args[1]["headers"]["Authorization"], "Token secret-token")
        self.assertEqual(submission.response["topic"], "yse.transients")

    def test_testing_service_publishes_to_hermes_test_and_missing_token_fails_clearly(self):
        self.service.testing = True
        self.service.save()
        submission = tns.create_submission(self.service, self.transient, tns.KIND_HERMES, self.admin)
        with mock.patch.object(hermes_mod.requests, "post", return_value=FakeResponse({"uuid": "t-1"})) as post:
            run_pass()
        self.assertEqual(json.loads(post.call_args[1]["data"])["topic"], "hermes.test")
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_ACCEPTED)
        self.service.credential = None
        self.service.save()
        failed = tns.create_submission(self.service, self.transient, tns.KIND_HERMES, self.admin)
        run_pass()
        failed.refresh_from_db()
        self.assertEqual(failed.status, SharingSubmission.STATUS_FAILED)
        self.assertIn("no Hermes token", failed.error)
        self.service.credential = self.cred
        self.service.hermes_topic = ""
        self.service.testing = False
        self.service.save()
        no_topic = tns.create_submission(self.service, self.transient, tns.KIND_HERMES, self.admin)
        run_pass()
        no_topic.refresh_from_db()
        self.assertIn("no hermes_topic", no_topic.error)

    def test_hermes_http_error_marks_failed(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_HERMES, self.admin)
        with mock.patch.object(hermes_mod.requests, "post", return_value=FakeResponse({"detail": "bad topic"}, status_code=403)):
            run_pass()
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_FAILED)
        self.assertIn("HTTP 403", submission.error)


class EinsteinProbeTests(FeedBase):
    def run_ep(self, source, notices):
        provider = feed_registry.provider_for(source)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"ep-mirror": notices})):
            return provider.run(source)

    def test_notice_becomes_candidate_and_second_notice_updates_it(self):
        source = self.make_source(kind="ep", credential=None, config={"url": "https://ep-mirror.example/notices.json"})
        result = self.run_ep(source, [ep_notice()])
        self.assertEqual((result["fetched"], result["candidates"]), (1, 1))
        c = Candidate.objects.get()
        self.assertEqual((c.broker, c.alert_id, c.status), ("ep", "EP01709131705", Candidate.NEW))
        self.assertAlmostEqual(c.payload["properties"]["error_radius_arcsec"], 180.0)
        self.assertEqual(c.classification, "X-ray transient (WXT)")
        self.assertEqual(c.n_alerts, 1)
        refined = ep_notice(ra=120.51, dec=-30.26, error_deg=0.02, trigger="2026-09-28T02:15:00Z", snr=11.0)
        result2 = self.run_ep(source, {"notices": [refined]})
        self.assertEqual(result2["candidates"], 1)
        self.assertEqual(Candidate.objects.count(), 1)
        c.refresh_from_db()
        self.assertAlmostEqual(c.ra, 120.51)
        self.assertAlmostEqual(c.payload["properties"]["error_radius_arcsec"], 72.0)
        self.assertEqual(c.n_alerts, 2)
        client = Client()
        client.force_login(self.user)
        self.assertContains(client.get(reverse("candidate_list"), {"broker": "ep"}), "EP01709131705")

    def test_cross_match_annotates_transients_in_the_error_circle(self):
        source = self.make_source(kind="ep", credential=None, config={"url": "https://ep-mirror.example/n.json", "comment": True})
        inside = create_minimal_transient(self.admin, name="2026ins", obs_group_name="YSE", ra=120.5 + 60.0 / 3600 / 0.866, dec=-30.25)
        outside = create_minimal_transient(self.admin, name="2026out", obs_group_name="YSE", ra=120.5, dec=-30.25 + 600.0 / 3600)
        self.run_ep(source, [ep_notice()])
        annotation = TransientAnnotation.objects.get(transient=inside, origin=ep_mod.ORIGIN)
        self.assertEqual(annotation.data["event"], "EP01709131705")
        self.assertLess(annotation.data["separation_arcsec"], 180)
        self.assertEqual(annotation.data["error_radius_arcsec"], 180.0)
        self.assertEqual(annotation.data["instrument"], "WXT")
        self.assertTrue(Log.objects.filter(transient=inside, comment__contains="Einstein Probe: EP01709131705").exists())
        self.assertFalse(TransientAnnotation.objects.filter(transient=outside).exists())
        self.assertTrue(annotation.values.filter(key="separation_arcsec").exists())

    def test_poll_without_url_fails_clearly_and_error_cap(self):
        source = self.make_source(kind="ep", credential=None)
        result = feed_registry.provider_for(source).run(source)
        self.assertIn("config.url", result["error"])
        capped = self.make_source(kind="ep", slug="ep-capped", credential=None,
                                  config={"url": "https://ep-mirror.example/n.json", "max_error_arcsec": 100})
        result2 = self.run_ep(capped, [ep_notice(), ep_notice(ident="02", error_deg=0.01)])
        self.assertEqual((result2["fetched"], result2["candidates"]), (1, 1))

    def test_consume_from_a_fake_gcn_consumer(self):
        source = self.make_source(kind="ep", credential=None)

        class Record:
            def __init__(self, value):
                self._value = value

            def error(self):
                return None

            def value(self):
                return self._value

        class FakeConsumer:
            _yse_fake = True
            batches = [[Record(json.dumps(ep_notice())), Record(b"garbage"), Record(json.dumps(ep_notice(ident="77")))], []]

            def consume(self, n, timeout=1):
                return self.batches.pop(0) if self.batches else []

        provider = feed_registry.provider_for(source)
        messages = provider.consume(source, consumer=FakeConsumer(), max_messages=10)
        self.assertEqual([m.object_id for m in messages], ["EP01709131705", "EP77"])
        result = provider.run(source, messages=messages)
        self.assertEqual(result["candidates"], 2)
        with mock.patch.object(ep_mod.EinsteinProbeFeed, "can_consume", return_value=False):
            with self.assertRaises(feeds_base.FeedError):
                provider.consume(source)


@override_settings(FEEDS_MPC_SCREEN_RADIUS_ARCSEC=5.0)
class ScoutTests(FeedBase):
    def test_scout_list_becomes_neo_candidates(self):
        source = self.make_source(kind="scout", credential=None, config={"min_neo_score": 10})
        provider = feed_registry.provider_for(source)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"scout.api": SCOUT_LIST})):
            result = provider.run(source)
        self.assertEqual((result["fetched"], result["candidates"]), (2, 2))
        c = Candidate.objects.get(alert_id="P22abcD")
        self.assertEqual((c.broker, c.rb, c.last_mag, c.last_band), ("scout", 1.0, 19.8, "V"))
        self.assertEqual(c.classification, "NEO candidate (score 100)")
        self.assertEqual(c.payload["properties"]["tag"], "NEO")
        self.assertEqual(c.payload["properties"]["unc_arcmin"], 2.5)
        self.assertTrue(c.url.endswith("P22abcD"))

    def test_scout_match_annotates_a_transient_at_that_position(self):
        source = self.make_source(kind="scout", credential=None,
                                  config={"neofixer": {"url": "https://neofixer.example/api/list"}})
        t = create_minimal_transient(self.admin, name="2026neo", obs_group_name="YSE", ra=157.5, dec=15.0)
        routes = {"scout.api": SCOUT_LIST, "neofixer.example": {"objects": [{"designation": "P22abcD", "rank": 3}]}}
        provider = feed_registry.provider_for(source)
        with mock.patch.object(feeds_base.requests, "get", fake_get(routes)):
            result = provider.run(source)
        self.assertEqual(result["linked"], 1)
        annotation = TransientAnnotation.objects.get(transient=t, origin=scout_mod.ORIGIN)
        self.assertEqual(annotation.data["possible_mpc"], "P22abcD")
        self.assertEqual(annotation.verdict, scout_mod.VERDICT_MOVING)
        self.assertTrue(annotation.has_badge)
        rank = TransientAnnotation.objects.get(transient=t, origin=scout_mod.NEOFIXER_ORIGIN)
        self.assertEqual(rank.data["rank"], 3)
        self.assertEqual(AlternateTransientNames.objects.get(name="P22abcD").transient, t)

    def test_screen_transient_writes_the_minor_planet_annotation_and_badge(self):
        t = create_minimal_transient(self.admin, name="2026mp", obs_group_name="YSE", ra=150.0, dec=20.0)
        t.disc_date = datetime.datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
        t.save()
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": SBIDENT_HIT})) as get:
            document = scout_mod.screen_transient(t, user=self.admin)
        params = get.calls[0]["params"]
        self.assertEqual(params["fov-ra-center"], "10-00-00.00")
        self.assertEqual(params["mpc-code"], "500")
        self.assertAlmostEqual(float(params["obs-time"]), 2461311.625, places=5)
        self.assertEqual(document["possible_mpc"], "12345 Example (2001 AB1)")
        self.assertEqual(document["verdict"], scout_mod.VERDICT_MOVING)
        self.assertAlmostEqual(document["separation_arcsec"], 3.14)
        annotation = TransientAnnotation.objects.get(transient=t, origin=scout_mod.ORIGIN)
        self.assertTrue(annotation.has_badge)
        client = Client()
        client.force_login(self.user)
        summary = client.get(reverse("transient_annotations_summary", args=[t.pk])).json()
        self.assertEqual(summary["badges"][0]["origin"], scout_mod.ORIGIN)
        self.assertEqual(summary["badges"][0]["verdict"], "moving object")
        fragment = client.get(reverse("transient_detail_annotations_fragment", args=[t.pk]))
        self.assertContains(fragment, "12345 Example (2001 AB1)")
        self.assertContains(fragment, "badge-info")

    def test_screen_clean_result_is_recorded_without_badge(self):
        t = create_minimal_transient(self.admin, name="2026cln", obs_group_name="YSE", ra=150.0, dec=20.0)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": SBIDENT_CLEAN})):
            document = scout_mod.screen_transient(t, user=self.admin)
        self.assertEqual(document["verdict"], "clean")
        self.assertIsNone(document["possible_mpc"])
        self.assertIn("nearest: (2020 XY99) at 68 arcsec", document["summary"])
        annotation = TransientAnnotation.objects.get(transient=t, origin=scout_mod.ORIGIN)
        self.assertFalse(annotation.has_badge)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": SBIDENT_EMPTY})):
            empty = scout_mod.screen_transient(t, user=self.admin)
        self.assertEqual(empty["n_bodies_in_field"], 0)

    def test_screen_job_sweep_skips_screened_and_handles_errors(self):
        t1 = create_minimal_transient(self.admin, name="2026sw1", obs_group_name="YSE", ra=150.0, dec=20.0)
        t2 = create_minimal_transient(self.admin, name="2026sw2", obs_group_name="YSE", ra=151.0, dec=21.0)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": SBIDENT_CLEAN})):
            scout_mod.screen_transient(t1, user=self.admin)
        job = enqueue_screen(created_by=self.admin)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": SBIDENT_HIT})):
            run_pass()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual((job.result["screened"], job.result["matches"]), (1, 1))
        self.assertEqual(job.result["names"], ["2026sw2=12345 Example (2001 AB1)"])
        self.assertTrue(TransientAnnotation.objects.filter(transient=t2, origin=scout_mod.ORIGIN).exists())
        t3 = create_minimal_transient(self.admin, name="2026sw3", obs_group_name="YSE", ra=152.0, dec=22.0)
        one = enqueue_screen(t3, created_by=self.admin)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": FakeResponse({"message": "down"}, status_code=500)})):
            run_pass()
        one.refresh_from_db()
        self.assertEqual(one.status, Job.QUEUED)  # retried with backoff
        self.assertEqual(one.attempts, 1)

    def test_screen_on_create_signal_is_off_by_default(self):
        create_minimal_transient(self.admin, name="2026sig", obs_group_name="YSE")
        self.assertFalse(Job.objects.filter(kind=SCREEN_KIND).exists())
        with override_settings(FEEDS_MPC_SCREEN_ON_CREATE=True):
            t = create_minimal_transient(self.admin, name="2026sig2", obs_group_name="YSE")
        job = Job.objects.get(kind=SCREEN_KIND)
        self.assertEqual((job.payload["transient_id"], job.transient_id), (t.pk, t.pk))

    def test_screen_view_and_management_command(self):
        t = create_minimal_transient(self.admin, name="2026cmd", obs_group_name="YSE", ra=150.0, dec=20.0)
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("feed_screen_transient", args=[t.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.filter(kind=SCREEN_KIND, transient=t).count(), 1)
        self.assertEqual(client.get(reverse("feed_screen_transient", args=[t.pk])).status_code, 405)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"sb_ident": SBIDENT_HIT})):
            out = _call("feeds", "--screen", "2026cmd")
        self.assertIn("12345 Example", out)


def _call(*args):
    import io

    buf = io.StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


class JobsCronAndPagesTests(FeedBase):
    def test_poll_job_runs_source_and_cron_enqueues_enabled_sources_once(self):
        source = self.make_source(kind="scout", credential=None)
        disabled = self.make_source(kind="ep", slug="ep-off", credential=None, enabled=False)
        self.assertEqual(FeedPoll().do(), "disabled")
        with override_settings(FEEDS_POLL_CRON_ENABLED=True):
            self.assertIn("scout-feed", FeedPoll().do())
            self.assertEqual(enqueue_polls(), [])  # already queued
        self.assertEqual(Job.objects.filter(kind=POLL_KIND).count(), 1)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"scout.api": SCOUT_LIST})):
            run_pass()
        job = Job.objects.get(kind=POLL_KIND)
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(job.result["candidates"], 2)
        source.refresh_from_db()
        self.assertIn("candidates 2", source.last_summary)
        skipped = enqueue_poll(disabled)
        run_pass()
        skipped.refresh_from_db()
        self.assertEqual(skipped.result, {"source": "ep-off", "skipped": "disabled"})
        self.assertEqual(MinorPlanetScreen().do(), "disabled")
        with override_settings(FEEDS_MPC_SCREEN_CRON_ENABLED=True):
            self.assertIn("queued job", MinorPlanetScreen().do())
            self.assertIn("already queued", MinorPlanetScreen().do())

    def test_poll_job_fails_for_missing_source_or_unusable_provider(self):
        job = enqueue_poll(FeedSource(pk=987654))
        run_pass()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.FAILED)
        self.assertIn("not found", job.error)
        source = self.make_source(kind="ep", credential=None)
        with self.assertRaises(feeds_base.FeedError):
            run_source(source)
        source.refresh_from_db()
        self.assertIn("config.url", source.last_error)
        failed = enqueue_poll(source)
        run_pass()
        failed.refresh_from_db()
        self.assertEqual(failed.status, Job.FAILED)
        self.assertIn("config.url", failed.error)

    def test_feeds_page_json_and_poll_button(self):
        source = self.make_source(kind="scout", credential=None)
        self.make_source(kind="ep", slug="ep-hidden", credential=None, enabled=False)
        client = Client()
        self.assertEqual(client.get(reverse("feed_sources")).status_code, 302)
        client.force_login(self.user)
        page = client.get(reverse("feed_sources"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "scout feed")
        self.assertNotContains(page, "ep-hidden")
        self.assertNotContains(page, "Poll now")
        data = client.get(reverse("feed_sources_json")).json()
        self.assertEqual([s["slug"] for s in data["sources"]], ["scout-feed"])
        self.assertEqual(client.post(reverse("feed_source_poll", args=[source.pk])).status_code, 403)
        client.force_login(self.admin)
        page = client.get(reverse("feed_sources"))
        self.assertContains(page, "ep-hidden")
        self.assertContains(page, "Poll now")
        response = client.post(reverse("feed_source_poll", args=[source.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Job.objects.filter(kind=POLL_KIND).count(), 1)
        client.post(reverse("feed_source_poll", args=[source.pk]))
        self.assertEqual(Job.objects.filter(kind=POLL_KIND).count(), 1)  # already queued
        page = client.get(reverse("feed_sources"))
        self.assertContains(page, "polling")
        self.assertContains(page, "feeds.poll")

    def test_api_lists_sources_and_staff_can_queue_a_poll(self):
        source = self.make_source(kind="scout", credential=None)
        self.make_source(kind="ep", slug="ep-hidden", credential=None, enabled=False)
        client = Client()
        client.force_login(self.user)
        listing = client.get("/api/feedsources/").json()
        rows = listing["results"] if isinstance(listing, dict) else listing
        self.assertEqual([r["slug"] for r in rows], ["scout-feed"])
        self.assertEqual(rows[0]["credential_name"], None)
        self.assertNotIn("secret", json.dumps(rows))
        self.assertEqual(client.post("/api/feedsources/%d/poll/" % source.pk).status_code, 403)
        client.force_login(self.admin)
        listing = client.get("/api/feedsources/", {"kind": "ep"}).json()
        rows = listing["results"] if isinstance(listing, dict) else listing
        self.assertEqual([r["slug"] for r in rows], ["ep-hidden"])
        response = client.post("/api/feedsources/%d/poll/" % source.pk)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Job.objects.get(kind=POLL_KIND).payload["source_id"], source.pk)
        self.assertEqual(client.post("/api/feedsources/%d/poll/" % source.pk).status_code, 409)

    def test_management_command_lists_and_polls(self):
        source = self.make_source(kind="scout", credential=None)
        out = _call("feeds")
        self.assertIn("scout-feed", out)
        self.assertIn("providers:", out)
        with mock.patch.object(feeds_base.requests, "get", fake_get({"scout.api": SCOUT_LIST})):
            out = _call("feeds", "--poll", "scout-feed", "--json")
        self.assertEqual(json.loads(out)["candidates"], 2)
        source.refresh_from_db()
        self.assertIn("candidates 2", source.last_summary)
        listing = json.loads(_call("feeds", "--json"))
        self.assertEqual(listing["sources"][0]["slug"], "scout-feed")

    def test_admin_pages_render(self):
        source = self.make_source(config={"since_hours": 6})
        client = Client()
        client.force_login(self.admin)
        self.assertEqual(client.get(reverse("admin:YSE_App_feedsource_changelist")).status_code, 200)
        self.assertEqual(client.get(reverse("admin:YSE_App_feedsource_change", args=[source.pk])).status_code, 200)
        self.assertEqual(client.get(reverse("admin:YSE_App_feedsource_add")).status_code, 200)

    def test_group_scoped_visibility_of_feed_candidates(self):
        """Feed candidates have no BrokerFilter, so every scanner sees them (like shared filters)."""
        group, _ = Group.objects.get_or_create(name="Other scanners")
        self.user.groups.add(group)
        alert = ep_mod.parse_notice(ep_notice()).to_alert(obs_group="Einstein Probe", instrument="Unknown")
        ingest.upsert_candidate(alert, [])
        client = Client()
        client.force_login(self.user)
        self.assertContains(client.get(reverse("candidate_list"), {"broker": "ep"}), "EP01709131705")
