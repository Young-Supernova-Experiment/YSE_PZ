"""Broker providers, filter evaluator, polling ingest, candidate save/reject, page and API (#272, #276)."""

from __future__ import annotations

import io
import json
import math
from unittest import mock

import pandas as pd
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App.brokers import alerce as alerce_mod
from YSE_App.brokers import antares as antares_mod
from YSE_App.brokers import fink as fink_mod
from YSE_App.brokers import ingest, registry
from YSE_App.brokers.base import (
    ALL_CAPABILITIES,
    CONE_SEARCH,
    CUTOUTS,
    FILTER_CRUD,
    GET_ALERT,
    PHOTOMETRY,
    QUERY_ALERTS,
    SAVE_AS_TRANSIENT,
    BrokerAlert,
    BrokerError,
    BrokerProvider,
    BrokerUnavailable,
    CapabilityNotSupported,
    make_point,
    points_to_upload_blocks,
)
from YSE_App.brokers.filters import CriteriaError, evaluate, validate_criteria
from YSE_App.brokers.jobs import INGEST_KIND
from YSE_App.data_ingest.Broker_Ingest import BrokerPoll, enqueue_polls
from YSE_App.jobs import run_pass
from YSE_App.models import (
    AlternateTransientNames,
    BrokerFilter,
    Candidate,
    Job,
    ObservationGroup,
    Transient,
    TransientPhotData,
)
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user, ensure_transient_statuses

NOW_MJD = 61300.0


def alert(**kw):
    base = dict(broker="fake", object_id="ZTF26aaaaaaa", alert_id="c1", ra=150.0, dec=40.0, mjd=NOW_MJD - 0.5,
                discovery_mjd=NOW_MJD - 3.0, mag=18.7, mag_err=0.05, band="r", rb=0.9, drb=0.95, ndet=4,
                is_positive=True, sgscore=0.1, distpsnr1=3.0, classification="SN candidate",
                class_probabilities={"SN Ia": 0.8}, url="https://example.org/ZTF26aaaaaaa",
                cutout_urls={"science": "https://example.org/sci.png"})
    base.update(kw)
    return BrokerAlert(**base)


class FakeProvider(BrokerProvider):
    """In-memory provider used by the ingest / view / API tests."""

    slug = "fake"
    name = "Fake broker"
    capabilities = (QUERY_ALERTS, GET_ALERT, CUTOUTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT)
    query_keys = ("classes", "days")
    alerts = []
    photometry = {}
    calls = []

    def query_alerts(self, query=None, *, since_mjd=None, limit=100):
        self.validate_query(query)
        FakeProvider.calls.append(("query", dict(query or {}), limit))
        return list(FakeProvider.alerts)[:limit]

    def cone_search(self, ra, dec, radius_arcsec, *, limit=50):
        return [a for a in FakeProvider.alerts if abs(a.ra - ra) < 0.01 and abs(a.dec - dec) < 0.01][:limit]

    def get_alert(self, object_id):
        return next((a for a in FakeProvider.alerts if a.object_id == object_id), None)

    def get_cutouts(self, object_id, alert_id=""):
        a = self.get_alert(object_id)
        return dict(a.cutout_urls) if a else {}

    def get_photometry(self, object_id):
        FakeProvider.calls.append(("photometry", object_id))
        if object_id in FakeProvider.photometry and FakeProvider.photometry[object_id] == "error":
            raise BrokerError("broker down")
        return list(FakeProvider.photometry.get(object_id, []))


registry.register(FakeProvider)


def fake_points(n=3, start=NOW_MJD - 3.0):
    pts = [make_point(start - 1.0, "r", None, None, limit=20.5, alert_id="lim")]
    for i in range(n):
        pts.append(make_point(start + i, "r" if i % 2 == 0 else "g", 19.0 - 0.1 * i, 0.05, alert_id="c%d" % i))
    return pts


def reset_fake(alerts=None, photometry=None):
    FakeProvider.alerts = list(alerts or [])
    FakeProvider.photometry = dict(photometry or {})
    FakeProvider.calls = []


class BaseAndRegistryTests(TestCase):
    def test_registered_and_capabilities(self):
        slugs = registry.registered_slugs()
        for s in ("antares", "fink", "alerce", "fake"):
            self.assertIn(s, slugs)
        fink = registry.get_provider("fink")
        self.assertEqual(fink.capability_set(), [c for c in ALL_CAPABILITIES if c != FILTER_CRUD])
        self.assertTrue(fink.has(CUTOUTS))
        self.assertFalse(fink.has(FILTER_CRUD))
        with self.assertRaises(CapabilityNotSupported):
            fink.list_filters()
        antares = registry.get_provider("antares")
        self.assertFalse(antares.has(CUTOUTS))  # ANTARES has no public cutout API: the UI hides the column
        self.assertIn("slug", antares.describe())

    def test_enabled_setting_narrows_registry(self):
        with override_settings(BROKERS_ENABLED=["fink"]):
            self.assertEqual(registry.enabled_slugs(), ["fink"])
            self.assertIsNone(registry.get_provider("alerce"))
            self.assertEqual([p.slug for p in registry.all_providers()], ["fink"])
        with override_settings(BROKERS_ENABLED="fink, alerce"):
            self.assertEqual(registry.enabled_slugs(), ["alerce", "fink"])

    def test_unavailable_provider_is_listed_but_not_usable(self):
        with mock.patch.object(antares_mod, "HAS_ANTARES", False):
            p = registry.get_provider("antares")
            self.assertFalse(p.available())
            self.assertIn("antares_client", p.unavailable_reason())
            self.assertNotIn("antares", [q.slug for q in registry.enabled_providers()])
            with self.assertRaises(BrokerUnavailable):
                registry.get_provider("antares", require_available=True)
            with self.assertRaises(BrokerUnavailable):
                p.cone_search(1.0, 2.0, 3.0)

    def test_credential_lookup_without_row_is_empty(self):
        self.assertEqual(registry.credential_for("fink"), {})

    def test_alert_roundtrip_and_filter_values(self):
        a = alert()
        d = a.to_dict()
        self.assertAlmostEqual(d["gal_b"], a.gal_b)
        b = BrokerAlert.from_dict(dict(d, unknown_key=1))
        self.assertEqual(b.object_id, a.object_id)
        self.assertEqual(b.class_probabilities, {"SN Ia": 0.8})
        v = a.filter_values(NOW_MJD)
        self.assertAlmostEqual(v["age_days"], 3.0)
        self.assertEqual(v["classification"], "SN candidate")

    def test_upload_blocks_group_by_instrument_and_skip_limits(self):
        pts = fake_points(2)
        pts[1]["instrument"] = "LSSTCam"
        pts[1]["obs_group"] = "LSST"
        blocks = points_to_upload_blocks(pts, "ZTF-Cam", "ZTF")
        self.assertEqual(set(blocks), {"ZTF-Cam", "LSSTCam"})
        self.assertEqual(sum(len(b["photdata"]) for b in blocks.values()), 2)  # the limit is dropped
        first = next(iter(blocks["LSSTCam"]["photdata"].values()))
        self.assertEqual(first["discovery_point"], 1)
        self.assertEqual(blocks["LSSTCam"]["obs_group"], "LSST")


# --- ANTARES ------------------------------------------------------------------------

class FakeLocus:
    def __init__(self, locus_id, ra, dec, rows, properties=None, tags=None):
        self.locus_id, self.ra, self.dec = locus_id, ra, dec
        self.lightcurve = pd.DataFrame(rows)
        self.properties = properties or {}
        self.tags = tags or []
        self.alerts = []


def ant_row(alert_id, survey, mjd, band, mag, magerr, maglim=None):
    return {"alert_id": alert_id, "ant_mjd": mjd, "ant_survey": survey, "ant_ra": 10.0, "ant_dec": 20.0,
            "ant_passband": band, "ant_mag": mag, "ant_magerr": magerr, "ant_maglim": maglim if maglim is not None else mag}


def ztf_locus():
    rows = [
        ant_row("ztf_upper_limit:0", 2, 61290.0, "R", math.nan, math.nan, 20.4),
        ant_row("ztf_candidate:1", 1, 61291.0, "g", 19.2, 0.1),
        ant_row("ztf_candidate:2", 1, 61292.0, "R", 18.9, 0.08),
        ant_row("lsst:3", 4, 61293.0, "i", 21.0, 0.05),  # an LSST row on the same locus
    ]
    return FakeLocus("ANT2026abc", 10.0, 20.0, rows, tags=["nuclear_transient"],
                     properties={"ztf_object_id": "ZTF26xyz", "num_mag_values": 2, "ztf_rb": 0.77,
                                 "newest_alert_id": "ztf_candidate:2", "survey": {"lsst": {"dia_object_id": ["1"]}}})


class AntaresProviderTests(SimpleTestCase):
    def test_locus_points_cover_ztf_and_lsst_rows(self):
        pts = antares_mod.locus_points(ztf_locus())
        self.assertEqual([p["instrument"] for p in pts], ["ZTF-Cam", "ZTF-Cam", "ZTF-Cam", "LSSTCam"])
        self.assertIsNone(pts[0]["mag"])
        self.assertAlmostEqual(pts[0]["limit"], 20.4)
        self.assertEqual(pts[2]["band"], "r")  # ANTARES 'R' -> YSE 'r'
        self.assertEqual(pts[3]["obs_group"], "LSST")

    def test_locus_to_alert(self):
        a = antares_mod.locus_to_alert(ztf_locus())
        self.assertEqual(a.broker, "antares")
        self.assertEqual(a.object_id, "ZTF26xyz")
        self.assertEqual(a.alert_id, "ztf_candidate:2")
        self.assertAlmostEqual(a.mag, 21.0)  # latest detection of any survey
        self.assertEqual(a.band, "i")
        self.assertAlmostEqual(a.discovery_mjd, 61291.0)
        self.assertEqual(a.ndet, 2)
        self.assertAlmostEqual(a.rb, 0.77)
        self.assertEqual(a.classification, "nuclear_transient")
        self.assertEqual(a.instrument, "ZTF-Cam")
        self.assertIn("ANT2026abc", a.url)
        json.dumps(a.to_dict())  # payload must be JSON-serialisable (NaN-free)

    def test_build_query(self):
        q = antares_mod.AntaresProvider.build_query({"tags": "yse_candidate_test, high_amplitude", "rb_min": 0.5,
                                                     "dec_min": -30}, since_mjd=61000.0)
        must = q["query"]["bool"]["must"]
        self.assertEqual(must[0], {"range": {"properties.newest_alert_observation_time": {"gte": 61000.0}}})
        self.assertEqual(must[1], {"terms": {"tags": ["yse_candidate_test", "high_amplitude"]}})
        self.assertEqual(must[2], {"range": {"properties.ztf_rb": {"gte": 0.5}}})
        self.assertEqual(must[3], {"range": {"dec": {"gte": -30.0}}})
        self.assertEqual(antares_mod.AntaresProvider.build_query({}), {"query": {"match_all": {}}})

    def test_query_and_cone_search_with_client_mocked(self):
        locus = ztf_locus()
        with mock.patch.object(antares_mod, "HAS_ANTARES", True), \
                mock.patch.object(antares_mod, "search", return_value=iter([locus, locus]), create=True), \
                mock.patch.object(antares_mod, "cone_search", return_value=iter([locus]), create=True):
            p = antares_mod.AntaresProvider()
            self.assertTrue(p.available())
            alerts = p.query_alerts({"days": 2}, limit=1)
            self.assertEqual(len(alerts), 1)
            near = p.cone_search(10.0, 20.0, 3.0)
            self.assertEqual(near[0].object_id, "ZTF26xyz")
        with mock.patch.object(antares_mod, "HAS_ANTARES", True), \
                mock.patch.object(antares_mod, "search", side_effect=RuntimeError("api down"), create=True):
            with self.assertRaises(BrokerError):
                antares_mod.AntaresProvider().query_alerts({})

    def test_unknown_query_key_rejected(self):
        with self.assertRaises(ValueError):
            antares_mod.AntaresProvider().validate_query({"bogus": 1})


# --- Fink -----------------------------------------------------------------------------

def fink_row(oid="ZTF26fink01", jd=2461300.3, fid=2, mag=18.4, candid="26001", cls="SN candidate", tag=None, **extra):
    row = {"i:objectId": oid, "i:jd": jd, "i:fid": fid, "i:magpsf": mag, "i:sigmapsf": 0.06, "i:ra": 150.1, "i:dec": 40.2,
           "i:candid": candid, "i:rb": 0.88, "i:drb": 0.99, "i:isdiffpos": "t", "i:sgscore1": 0.02, "i:distpsnr1": 1.5,
           "i:ndethist": 5, "i:jdstarthist": 2461297.1, "v:classification": cls, "d:snn_snia_vs_nonia": 0.91,
           "d:cdsxmatch": "Unknown", "b:cutoutScience_stampData": "binary"}
    if tag:
        row["d:tag"] = tag
    row.update(extra)
    return row


class FinkProviderTests(SimpleTestCase):
    def test_query_alerts_posts_per_class_and_dedupes(self):
        posted = []

        def fake_request(method, url, *, params=None, json=None, headers=None, timeout=None):
            posted.append((method, url, json))
            if url.endswith("/latests"):
                return [fink_row(candid="1", jd=2461300.1), fink_row(candid="2", jd=2461300.3), fink_row(oid="ZTF26fink02", candid="3")]
            return []

        with mock.patch.object(fink_mod, "request_json", side_effect=fake_request):
            alerts = fink_mod.FinkProvider().query_alerts({"classes": ["SN candidate", "Early SN Ia candidate"], "days": 2}, limit=10)
        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[0][0], "POST")
        self.assertTrue(posted[0][1].endswith("/api/v1/latests"))
        self.assertEqual(posted[0][2]["class"], "SN candidate")
        self.assertEqual(posted[0][2]["n"], 10)
        self.assertIn("startdate", posted[0][2])
        oids = sorted(a.object_id for a in alerts)
        self.assertEqual(oids, ["ZTF26fink01", "ZTF26fink02"])
        a = next(a for a in alerts if a.object_id == "ZTF26fink01")
        self.assertEqual(a.alert_id, "2")  # latest candid kept
        self.assertEqual(a.band, "r")
        self.assertAlmostEqual(a.mjd, 2461300.3 - 2400000.5)
        self.assertAlmostEqual(a.discovery_mjd, 2461297.1 - 2400000.5)
        self.assertTrue(a.is_positive)
        self.assertEqual(a.class_probabilities, {"SN Ia (SNN)": 0.91})
        self.assertNotIn("b:cutoutScience_stampData", a.properties)
        self.assertIn("kind=Science", a.cutout_urls["science"])
        self.assertEqual(a.url, "https://fink-portal.org/ZTF26fink01")

    def test_photometry_with_upper_limits_and_bad_quality(self):
        rows = [fink_row(jd=2461298.0, tag="upperlim", mag=None, **{"i:diffmaglim": 20.7}),
                fink_row(jd=2461299.0, tag="valid", mag=19.0, fid=1),
                fink_row(jd=2461300.0, tag="badquality", mag=18.5)]
        with mock.patch.object(fink_mod, "request_json", return_value=rows) as rq:
            pts = fink_mod.FinkProvider().get_photometry("ZTF26fink01")
        self.assertTrue(rq.call_args.kwargs["json"]["withupperlim"])
        self.assertEqual(len(pts), 3)
        self.assertIsNone(pts[0]["mag"])
        self.assertAlmostEqual(pts[0]["limit"], 20.7)
        self.assertEqual(pts[1]["band"], "g")
        self.assertTrue(pts[2]["bad"])
        self.assertAlmostEqual(pts[1]["flux"], 10 ** (-0.4 * (19.0 - 27.5)))

    def test_get_alert_and_cone_search(self):
        with mock.patch.object(fink_mod, "request_json", return_value=[fink_row(jd=2461299.0, candid="a"), fink_row(jd=2461300.0, candid="b")]):
            a = fink_mod.FinkProvider().get_alert("ZTF26fink01")
            near = fink_mod.FinkProvider().cone_search(150.1, 40.2, 5)
        self.assertEqual(a.alert_id, "b")
        self.assertEqual(a.ndet, 2)
        self.assertAlmostEqual(a.discovery_mjd, 2461299.0 - 2400000.5)
        self.assertEqual(len(near), 1)
        with mock.patch.object(fink_mod, "request_json", return_value=[]):
            self.assertIsNone(fink_mod.FinkProvider().get_alert("ZTF00nothing"))

    def test_http_error_becomes_broker_error(self):
        class Resp:
            status_code = 500
            text = "boom"

            def json(self):
                return {}

        with mock.patch("YSE_App.brokers.http.requests.request", return_value=Resp()):
            with self.assertRaises(BrokerError):
                fink_mod.FinkProvider().cone_search(1, 2, 3)


# --- ALeRCE ---------------------------------------------------------------------------

def alerce_object(oid="ZTF26alerce1", **extra):
    obj = {"oid": oid, "meanra": 150.3, "meandec": 40.4, "firstmjd": 61297.2, "lastmjd": 61300.1, "ndet": 6,
           "class": "SN", "classifier": "stamp_classifier", "probability": 0.72, "stellar": False}
    obj.update(extra)
    return obj


ALERCE_LC = {
    "detections": [
        {"mjd": 61297.2, "candid": "9001", "fid": 1, "magpsf": 19.5, "sigmapsf": 0.1, "rb": 0.8, "drb": 0.9, "isdiffpos": 1, "dubious": False},
        {"mjd": 61300.1, "candid": "9002", "fid": 2, "magpsf": 18.2, "sigmapsf": 0.05, "rb": 0.85, "drb": 0.97, "isdiffpos": 1, "dubious": True},
    ],
    "non_detections": [{"mjd": 61296.1, "fid": 2, "diffmaglim": 20.9}],
}
ALERCE_PROBS = [
    {"classifier_name": "stamp_classifier", "class_name": "SN", "probability": 0.72, "ranking": 1},
    {"classifier_name": "stamp_classifier", "class_name": "AGN", "probability": 0.2, "ranking": 2},
    {"classifier_name": "lc_classifier", "class_name": "SNIa", "probability": 0.55, "ranking": 1},
]


def alerce_fake_request(method, url, *, params=None, json=None, headers=None, timeout=None):
    alerce_fake_request.calls.append((method, url, params))
    if url.endswith("/objects"):
        return {"total": 1, "page": 1, "items": [alerce_object()]}
    if url.endswith("/lightcurve"):
        return ALERCE_LC
    if url.endswith("/probabilities"):
        return ALERCE_PROBS
    if url.endswith("/objects/ZTF26alerce1"):
        return alerce_object()
    return None


class AlerceProviderTests(SimpleTestCase):
    def setUp(self):
        alerce_fake_request.calls = []

    def test_query_alerts_params_and_enrichment(self):
        with mock.patch.object(alerce_mod, "request_json", side_effect=alerce_fake_request):
            alerts = alerce_mod.AlerceProvider().query_alerts({"classes": ["SN"], "probability_min": 0.5, "days": 3, "ndet_min": 2}, limit=20)
        method, url, params = alerce_fake_request.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/ztf/v1/objects"))
        self.assertEqual(params["class_name"], ["SN"])
        self.assertEqual(params["classifier"], "stamp_classifier")
        self.assertEqual(params["probability"], 0.5)
        self.assertEqual(params["ndet"], [2])
        self.assertEqual(len(params["lastmjd"]), 1)
        self.assertEqual(params["order_by"], "lastmjd")
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a.object_id, "ZTF26alerce1")
        self.assertEqual(a.alert_id, "9002")
        self.assertAlmostEqual(a.mag, 18.2)
        self.assertEqual(a.band, "r")
        self.assertAlmostEqual(a.rb, 0.85)
        self.assertEqual(a.ndet, 6)
        self.assertEqual(a.classification, "SN")
        self.assertEqual(a.class_probabilities, {"SN": 0.72, "AGN": 0.2, "SNIa": 0.55})
        self.assertIn("candid=9002", a.cutout_urls["science"])
        self.assertEqual(a.url, "https://alerce.online/object/ZTF26alerce1")

    def test_photometry_get_alert_and_missing(self):
        with mock.patch.object(alerce_mod, "request_json", side_effect=alerce_fake_request):
            p = alerce_mod.AlerceProvider()
            pts = p.get_photometry("ZTF26alerce1")
            a = p.get_alert("ZTF26alerce1")
            missing = p.get_alert("ZTF00none")
            near = p.cone_search(150.3, 40.4, 3)
        self.assertEqual([pt["mjd"] for pt in pts], [61296.1, 61297.2, 61300.1])
        self.assertIsNone(pts[0]["mag"])
        self.assertTrue(pts[2]["bad"])
        self.assertEqual(a.object_id, "ZTF26alerce1")
        self.assertIsNone(missing)
        self.assertEqual(alerce_fake_request.calls[-3][2]["radius"], 3.0)
        self.assertEqual(len(near), 1)


# --- filters ----------------------------------------------------------------------------

class FilterEvaluatorTests(SimpleTestCase):
    def test_validate(self):
        out = validate_criteria({"mag_max": 20, "rb_min": 0.6, "bands": "g, r", "classes": ["SN candidate"],
                                 "class_probabilities": {"SN Ia": 0.5}, "ndet_min": 2, "positive_only": True, "mag_min": None})
        self.assertEqual(out["bands"], ["g", "r"])
        self.assertEqual(out["mag_max"], 20.0)
        self.assertNotIn("mag_min", out)
        self.assertEqual(validate_criteria(None), {})
        for bad in ({"nope": 1}, {"mag_max": "20"}, {"ndet_min": 2.5}, {"positive_only": 1}, {"classes": [1]},
                    {"class_probabilities": {"SN": "x"}}, {"mag_min": 21, "mag_max": 19}, []):
            with self.assertRaises(CriteriaError, msg=repr(bad)):
                validate_criteria(bad)

    def test_evaluate_cuts(self):
        a = alert()
        self.assertEqual(evaluate({}, a, now_mjd=NOW_MJD), (True, []))
        ok, failed = evaluate({"mag_min": 14, "mag_max": 20, "rb_min": 0.8, "ndet_min": 2, "ndet_max": 10, "gal_lat_min": 10,
                               "dec_min": -30, "age_max_days": 5, "positive_only": True, "sgscore_max": 0.5,
                               "distpsnr1_min": 1.0, "classes": ["sn candidate"], "class_probabilities": {"SN Ia": 0.5},
                               "bands": ["r"]}, a, now_mjd=NOW_MJD)
        self.assertTrue(ok, failed)
        ok, failed = evaluate({"mag_max": 18.0, "rb_min": 0.95, "age_max_days": 1, "classes": ["AGN"],
                               "class_probabilities": {"SN Ia": 0.9}, "bands": ["g"], "exclude_classes": ["SN candidate"]},
                              a, now_mjd=NOW_MJD)
        self.assertFalse(ok)
        self.assertEqual(failed, ["mag_max", "bands", "rb_min", "age_max_days", "classes", "exclude_classes", "class_probabilities"])

    def test_missing_values_fail_their_cuts(self):
        a = alert(rb=None, drb=None, discovery_mjd=None, is_positive=None, class_probabilities={})
        ok, failed = evaluate({"rb_min": 0.5, "age_max_days": 3, "positive_only": True, "class_probabilities": {"SN Ia": 0.1}}, a)
        self.assertEqual(failed, ["rb_min", "age_max_days", "positive_only", "class_probabilities"])
        self.assertTrue(evaluate({"gal_lat_min": 20}, alert(ra=150.0, dec=40.0))[0])   # b ~ 56 deg
        self.assertFalse(evaluate({"gal_lat_min": 20}, alert(ra=266.4, dec=-29.0))[0])  # Galactic centre


# --- ingest, save, reject -----------------------------------------------------------------

class IngestTests(TestCase):
    def setUp(self):
        reset_fake([alert(), alert(object_id="ZTF26bbbbbbb", alert_id="c9", ra=151.0, dec=41.0, mag=20.4, rb=0.3)],
                   {"ZTF26aaaaaaa": fake_points(3), "ZTF26bbbbbbb": fake_points(2)})
        self.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(self.admin)
        self.group = Group.objects.create(name="yse-scanners")
        self.bf = BrokerFilter.objects.create(name="young SNe", broker="fake", group=self.group, created_by=self.admin,
                                              query={"classes": ["SN candidate"]}, criteria={"mag_max": 20.0, "rb_min": 0.5})

    def test_poll_registers_passing_alerts_only(self):
        result = ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        self.assertEqual((result["fetched"], result["passed"], result["new"], result["saved"]), (2, 1, 1, 0))
        c = Candidate.objects.get()
        self.assertEqual((c.broker, c.alert_id, c.status), ("fake", "ZTF26aaaaaaa", Candidate.NEW))
        self.assertEqual(c.last_mag, 18.7)
        self.assertEqual(c.last_band, "r")
        self.assertEqual(c.passed_filter_names, ["young SNe"])
        self.assertEqual(list(c.filters.all()), [self.bf])
        self.assertEqual(c.payload["class_probabilities"], {"SN Ia": 0.8})
        self.assertAlmostEqual(c.gal_b, alert().gal_b)
        self.assertEqual(FakeProvider.calls[0], ("query", {"classes": ["SN candidate"]}, 200))
        self.bf.refresh_from_db()
        self.assertIn("passed 1", self.bf.last_run_summary)
        self.assertIsNotNone(self.bf.last_run_at)

    def test_repoll_updates_without_reopening(self):
        ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        c = Candidate.objects.get()
        ingest.reject_candidate(c, self.admin, note="bogus")
        FakeProvider.alerts[0] = alert(mag=18.1, alert_id="c2")
        other = BrokerFilter.objects.create(name="everything", broker="fake", criteria={})
        ingest.poll_filter(other, FakeProvider(), now_mjd=NOW_MJD)
        c.refresh_from_db()
        self.assertEqual(c.status, Candidate.REJECTED)
        self.assertEqual(c.last_mag, 18.1)
        self.assertEqual(c.latest_alert_id, "c2")
        self.assertEqual(c.n_alerts, 2)
        self.assertEqual(c.passed_filter_names, ["everything", "young SNe"])
        self.assertEqual(Candidate.objects.count(), 2)  # the faint one passed the empty filter

    def test_dry_run_writes_nothing(self):
        result = ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD, dry_run=True)
        self.assertEqual(result["passed"], 1)
        self.assertEqual(Candidate.objects.count(), 0)
        self.bf.refresh_from_db()
        self.assertIsNone(self.bf.last_run_at)

    def test_save_creates_transient_through_add_transient_with_photometry(self):
        ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        c = Candidate.objects.get()
        t = ingest.save_candidate(c, self.admin, status="Watch")
        self.assertEqual(t.name, "ZTF26aaaaaaa")
        self.assertEqual(t.status.name, "Watch")
        self.assertEqual(t.obs_group.name, "ZTF")
        self.assertAlmostEqual(t.ra, 150.0)
        self.assertIsNotNone(t.disc_date)
        self.assertAlmostEqual(t.real_bogus_score, 0.9)
        self.assertEqual(t.created_by, self.admin)
        rows = TransientPhotData.objects.filter(photometry__transient=t)
        self.assertEqual(rows.count(), 3)  # detections only; the limit is not a row
        self.assertEqual(rows.filter(discovery_point=True).count(), 1)
        self.assertEqual({r.photometry.instrument.name for r in rows}, {"ZTF-Cam"})
        self.assertEqual({r.band.name for r in rows}, {"g", "r"})
        c.refresh_from_db()
        self.assertEqual((c.status, c.transient_id, c.status_changed_by), (Candidate.SAVED, t.pk, self.admin))
        self.assertIn(("photometry", "ZTF26aaaaaaa"), FakeProvider.calls)

    def test_save_links_existing_transient_by_position(self):
        existing = create_minimal_transient(self.admin, name="2026abc", obs_group_name="TNS", ra=150.0 + 0.5 / 3600, dec=40.0)
        ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        c = Candidate.objects.get()
        t = ingest.save_candidate(c, self.admin)
        self.assertEqual(t.pk, existing.pk)
        self.assertEqual(Transient.objects.count(), 1)
        alias = AlternateTransientNames.objects.get(name="ZTF26aaaaaaa")
        self.assertEqual((alias.transient_id, alias.obs_group.name), (existing.pk, "ZTF"))
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=existing).count(), 3)
        c.refresh_from_db()
        self.assertEqual(c.status, Candidate.SAVED)
        self.assertIn("linked to existing 2026abc", c.note)

    def test_save_survives_broker_photometry_error(self):
        FakeProvider.photometry["ZTF26aaaaaaa"] = "error"
        ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        t = ingest.save_candidate(Candidate.objects.get(), self.admin)
        self.assertEqual(t.name, "ZTF26aaaaaaa")
        self.assertEqual(TransientPhotData.objects.count(), 0)

    def test_save_without_photometry_or_with_unknown_status(self):
        ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        c = Candidate.objects.get()
        with self.assertRaises(ingest.IngestError):
            ingest.save_candidate(c, self.admin, status="NoSuchStatus")
        t = ingest.save_candidate(c, self.admin, import_photometry=False, obs_group="Fake-Survey")
        self.assertEqual(TransientPhotData.objects.count(), 0)
        self.assertEqual(t.obs_group.name, "Fake-Survey")
        self.assertTrue(ObservationGroup.objects.filter(name="Fake-Survey").exists())

    def test_auto_save_filter_promotes_at_poll_time(self):
        self.bf.auto_save = True
        self.bf.save_status = "New"
        self.bf.save()
        result = ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        self.assertEqual(result["saved"], 1)
        c = Candidate.objects.get()
        self.assertEqual(c.status, Candidate.SAVED)
        self.assertEqual(c.transient.name, "ZTF26aaaaaaa")
        self.assertEqual(c.transient.status.name, "New")
        self.assertIn("auto-saved", c.note)

    def test_run_ingest_via_job_queue(self):
        BrokerFilter.objects.create(name="off", broker="fake", enabled=False, criteria={})
        job = Job.objects.create(kind=INGEST_KIND, payload={"broker": "fake"})
        run_pass(worker_id="test")
        job.refresh_from_db()
        self.assertEqual(job.status, Job.DONE, job.error)
        self.assertEqual(job.result["passed"], 1)
        self.assertEqual([f["filter"] for f in job.result["filters"]], ["young SNe"])
        self.assertEqual(Candidate.objects.count(), 1)

    def test_run_ingest_isolates_a_failing_filter(self):
        bad = BrokerFilter.objects.create(name="bad query", broker="fake", query={"nope": 1}, criteria={})
        result = ingest.run_ingest("fake")
        self.assertEqual(result["passed"], 1)
        bad.refresh_from_db()
        self.assertIn("failed", bad.last_run_summary)
        self.assertTrue(any("error" in r for r in result["filters"]))
        with self.assertRaises(BrokerError):
            ingest.run_ingest("no-such-broker")

    def test_cron_disabled_by_default_and_enqueues_when_on(self):
        self.assertEqual(BrokerPoll().do(), "disabled")
        self.assertEqual(Job.objects.count(), 0)
        with override_settings(BROKER_INGEST_CRON_ENABLED=True):
            summary = BrokerPoll().do()
        self.assertIn("fake", summary)
        job = Job.objects.get(kind=INGEST_KIND)
        self.assertEqual(job.payload["broker"], "fake")
        self.assertEqual(enqueue_polls(), [])  # already queued: not duplicated
        self.assertIn(BrokerPoll.code, [c.code for c in (BrokerPoll,)])

    def test_model_clean_validates(self):
        from django.core.exceptions import ValidationError

        bf = BrokerFilter(name="x", broker="fake", criteria={"nope": 1})
        with self.assertRaises(ValidationError):
            bf.clean()
        bf = BrokerFilter(name="x", broker="unknown", criteria={})
        with self.assertRaises(ValidationError):
            bf.clean()
        bf = BrokerFilter(name="x", broker="fake", criteria={"mag_max": 20}, query={"days": 1})
        bf.clean()

    def test_commands(self):
        out = io.StringIO()
        call_command("brokers", stdout=out)
        self.assertIn("fake", out.getvalue())
        self.assertIn("Cone search", out.getvalue())
        out = io.StringIO()
        call_command("brokers", "--json", stdout=out)
        self.assertTrue(any(d["slug"] == "fink" for d in json.loads(out.getvalue())))
        out = io.StringIO()
        call_command("broker_poll", "--broker", "fake", "--dry-run", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["passed"], 1)
        self.assertEqual(Candidate.objects.count(), 0)
        out = io.StringIO()
        call_command("broker_poll", "--broker", "fake", "--enqueue", stdout=out)
        self.assertEqual(Job.objects.filter(kind=INGEST_KIND).count(), 1)


# --- page and API -------------------------------------------------------------------------

class CandidatePageTests(TestCase):
    def setUp(self):
        reset_fake([alert()], {"ZTF26aaaaaaa": fake_points(2)})
        self.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(self.admin)
        self.group = Group.objects.create(name="yse-scanners")
        self.member = create_test_user("scanner", is_staff=False)
        self.member.groups.add(self.group)
        self.outsider = create_test_user("outsider", is_staff=False)
        self.bf = BrokerFilter.objects.create(name="young SNe", broker="fake", group=self.group, criteria={})
        ingest.poll_filter(self.bf, FakeProvider(), now_mjd=NOW_MJD)
        self.candidate = Candidate.objects.get()
        self.client = Client()

    def test_login_required(self):
        r = self.client.get(reverse("candidate_list"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r["Location"])

    def test_page_lists_candidates_with_cutouts_and_actions(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse("candidate_list"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("ZTF26aaaaaaa", html)
        self.assertIn("https://example.org/sci.png", html)
        self.assertIn("young SNe", html)
        self.assertIn('data-status="new"', html)
        self.assertIn(reverse("candidate_save", args=[self.candidate.pk]), html)
        self.assertIn("Fake broker", html)
        self.assertIn("Cone search", html)  # capability badges
        r = self.client.get(reverse("candidate_list"), {"status": "saved"})
        self.assertNotIn("ZTF26aaaaaaa", r.content.decode())
        r = self.client.get(reverse("candidate_list"), {"mag_max": "18", "status": "all"})
        self.assertNotIn("ZTF26aaaaaaa", r.content.decode())
        r = self.client.get(reverse("candidate_list"), {"filter": str(self.bf.pk), "sort": "mag", "q": "ZTF26"})
        self.assertIn("ZTF26aaaaaaa", r.content.decode())

    def test_group_scoping(self):
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("candidate_list"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ZTF26aaaaaaa", r.content.decode())
        r = self.client.post(reverse("candidate_reject", args=[self.candidate.pk]))
        self.assertEqual(r.status_code, 400)
        self.client.force_login(self.admin)
        self.assertIn("ZTF26aaaaaaa", self.client.get(reverse("candidate_list")).content.decode())

    def test_save_and_reject_actions(self):
        self.client.force_login(self.member)
        r = self.client.post(reverse("candidate_save", args=[self.candidate.pk]), {"status": "New"})
        self.assertEqual(r.status_code, 302)
        t = Transient.objects.get(name="ZTF26aaaaaaa")
        self.assertEqual(t.created_by, self.member)
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=t).count(), 2)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, Candidate.SAVED)
        r = self.client.get(reverse("candidate_list"), {"status": "saved"})
        self.assertIn(reverse("transient_detail", args=[t.slug]), r.content.decode())
        r = self.client.post(reverse("candidate_reject", args=[self.candidate.pk]), {"note": "not for us"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "rejected")
        r = self.client.post(reverse("candidate_reopen", args=[self.candidate.pk]))
        self.assertEqual(r.status_code, 302)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, Candidate.NEW)

    def test_save_error_is_reported(self):
        self.client.force_login(self.member)
        r = self.client.post(reverse("candidate_save", args=[self.candidate.pk]), {"status": "Nope"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 400)
        self.assertIn("unknown transient status", r.json()["error"])
        r = self.client.post(reverse("candidate_save", args=[self.candidate.pk]), {"status": "Nope"}, follow=True)
        self.assertContains(r, "Could not save")

    def test_brokers_status_json_and_sidebar_link(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse("brokers_status_json"))
        self.assertEqual(r.status_code, 200)
        slugs = [b["slug"] for b in r.json()["brokers"]]
        self.assertIn("fink", slugs)
        self.assertTrue(any(c["key"] == "gal_lat_min" for c in r.json()["criteria"]))
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, reverse("candidate_list"))


class CandidateApiTests(TestCase):
    def setUp(self):
        reset_fake([alert()], {"ZTF26aaaaaaa": fake_points(2)})
        self.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(self.admin)
        self.group = Group.objects.create(name="yse-scanners")
        self.member = create_test_user("scanner", is_staff=False)
        self.member.groups.add(self.group)
        self.outsider = create_test_user("outsider", is_staff=False)
        self.client = Client()

    def test_brokers_endpoint(self):
        self.client.force_login(self.member)
        r = self.client.get("/api/brokers/")
        self.assertEqual(r.status_code, 200)
        fink = next(b for b in r.json()["brokers"] if b["slug"] == "fink")
        self.assertIn("cutouts", fink["capabilities"])
        self.assertEqual(self.client.get("/api/brokers/fink/").json()["name"], "Fink")
        self.assertEqual(self.client.get("/api/brokers/nope/").status_code, 404)
        r = self.client.get("/api/brokers/fake/cone_search/", {"ra": 150.0, "dec": 40.0, "radius": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"][0]["object_id"], "ZTF26aaaaaaa")
        self.assertEqual(self.client.get("/api/brokers/fake/cone_search/").status_code, 400)
        with mock.patch.object(antares_mod, "HAS_ANTARES", False):
            r = self.client.get("/api/brokers/antares/cone_search/", {"ra": 1, "dec": 2})
        self.assertEqual(r.status_code, 503)

    def test_filter_crud_is_group_scoped_and_validated(self):
        self.client.force_login(self.member)
        payload = {"name": "api filter", "broker": "fake", "group": self.group.pk, "criteria": {"mag_max": 19.5, "nope": 1}}
        r = self.client.post("/api/brokerfilters/", json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("nope", json.dumps(r.json()))
        payload["criteria"] = {"mag_max": 19.5}
        payload["query"] = {"bogus": 1}
        r = self.client.post("/api/brokerfilters/", json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        payload["query"] = {"classes": ["SN candidate"]}
        r = self.client.post("/api/brokerfilters/", json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        bf = BrokerFilter.objects.get(name="api filter")
        self.assertEqual((bf.created_by, bf.group), (self.member, self.group))
        # no group -> staff only
        r = self.client.post("/api/brokerfilters/", json.dumps({"name": "shared", "broker": "fake", "criteria": {}}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/brokerfilters/", json.dumps({"name": "x", "broker": "nope", "criteria": {}}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        # outsider sees nothing and cannot edit
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get("/api/brokerfilters/").json()["count"] if "count" in self.client.get("/api/brokerfilters/").json() else len(self.client.get("/api/brokerfilters/").json()), 0)
        self.assertEqual(self.client.patch("/api/brokerfilters/%d/" % bf.pk, json.dumps({"enabled": False}),
                                           content_type="application/json").status_code, 404)
        self.client.force_login(self.admin)
        r = self.client.patch("/api/brokerfilters/%d/" % bf.pk, json.dumps({"enabled": False}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(BrokerFilter.objects.get(pk=bf.pk).enabled)

    def test_candidates_read_and_actions(self):
        bf = BrokerFilter.objects.create(name="young SNe", broker="fake", group=self.group, criteria={})
        ingest.poll_filter(bf, FakeProvider(), now_mjd=NOW_MJD)
        c = Candidate.objects.get()
        self.client.force_login(self.outsider)
        data = self.client.get("/api/candidates/").json()
        self.assertEqual(data["count"] if isinstance(data, dict) else len(data), 0)
        self.client.force_login(self.member)
        data = self.client.get("/api/candidates/", {"broker": "fake", "status": "new"}).json()
        rows = data["results"] if isinstance(data, dict) else data
        self.assertEqual(rows[0]["alert_id"], "ZTF26aaaaaaa")
        self.assertEqual(rows[0]["filter_names"], ["young SNe"])
        self.assertIn("science", rows[0]["cutout_urls"])
        r = self.client.post("/api/candidates/%d/save/" % c.pk, json.dumps({"status": "Watch"}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "saved")
        self.assertEqual(r.json()["transient_name"], "ZTF26aaaaaaa")
        self.assertEqual(Transient.objects.get(name="ZTF26aaaaaaa").status.name, "Watch")
        r = self.client.post("/api/candidates/%d/reject/" % c.pk, json.dumps({"note": "dup"}), content_type="application/json")
        self.assertEqual(r.json()["status"], "rejected")
        self.assertEqual(self.client.post("/api/candidates/%d/reopen/" % c.pk).json()["status"], "new")
        # saving again links the transient that now exists under the broker name
        r = self.client.post("/api/candidates/%d/save/" % c.pk, json.dumps({"status": "New"}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("linked to existing", r.json()["note"])
        self.assertEqual(Transient.objects.count(), 1)
