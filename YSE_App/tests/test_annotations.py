"""Structured annotations (#316: #317 model / API / detail panel / legacy shim, #318 catalogue
checks with rerun, #319 search filters and column).

Pins:

* ``upsert`` semantics (replace, merge, audit fields kept, unique per transient and origin),
  the indexed value rows and their coercion, group visibility, the legacy shim;
* the three checks against recorded TAP answers (Gaia stellar / no match / insignificant,
  AllWISE AGN-like and the CatWISE fallback, Milliquas match), failures landing on the run
  (HTTP error, transport error, VOTable error), the job-queue path end to end, rerun in place;
* the Annotations tab fragment, the summary JSON, the Check / Rerun / delete actions and their
  permissions, the DRF endpoint and its permissions;
* the search filters and the optional column on the page and the API (both vendors run this module).
"""

from __future__ import annotations

import json
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.annotation_services import CHECKS, base, gaia, quasar, wise
from YSE_App.filters.transient_search import TransientSearchFilterSet, parse_annotation_column
from YSE_App.jobs import run_pass
from YSE_App.models import (
    AntaresClassification,
    ExternalService,
    ExternalServiceRun,
    Job,
    Transient,
    TransientAnnotation,
    TransientAnnotationValue,
)
from YSE_App.models.annotation_models import coerce_value
from YSE_App.services import annotations as svc
from YSE_App.services import external_services as runs
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)


def tap_json(columns, rows):
    return {"metadata": [{"name": c} for c in columns], "data": rows}


GAIA_COLUMNS = ["source_id", "ra", "dec", "parallax", "parallax_error", "parallax_over_error", "pmra", "pmra_error",
                "pmdec", "pmdec_error", "phot_g_mean_mag", "bp_rp", "ruwe"]
GAIA_STAR = tap_json(GAIA_COLUMNS, [[4295806720, 10.0001, 20.0001, 5.2, 0.1, 52.0, 12.0, 0.2, -3.0, 0.2, 15.1, 0.9, 1.01]])
GAIA_FAINT = tap_json(GAIA_COLUMNS, [[4295806721, 10.0002, 20.0, 0.2, 0.5, 0.4, 0.5, 0.6, 0.1, 0.6, 20.3, None, 1.2]])
GAIA_EMPTY = tap_json(GAIA_COLUMNS, [])
WISE_COLUMNS = ["AllWISE", "RAJ2000", "DEJ2000", "W1mag", "e_W1mag", "W2mag", "e_W2mag", "W3mag", "e_W3mag", "W4mag",
                "e_W4mag", "ccf", "ex"]
WISE_AGN = tap_json(WISE_COLUMNS, [["J004000.02+200000.3", 10.0001, 20.0001, 14.5, 0.03, 13.4, 0.03, 10.9, 0.1, 8.2, 0.3, "0000", 0]])
WISE_STAR = tap_json(WISE_COLUMNS, [["J004000.02+200000.3", 10.0, 20.0, 14.5, 0.03, 14.4, 0.03, 14.1, 0.2, None, None, "0000", 0]])
WISE_EMPTY = tap_json(WISE_COLUMNS, [])
CATWISE_COLUMNS = ["Name", "RA_ICRS", "DE_ICRS", "W1mproPM", "e_W1mproPM", "W2mproPM", "e_W2mproPM"]
CATWISE_MATCH = tap_json(CATWISE_COLUMNS, [["J004000.00+200000.0", 10.0, 20.0, 16.1, 0.05, 15.2, 0.06]])
MQ_COLUMNS = ["RAJ2000", "DEJ2000", "Name", "Type", "Rmag", "Bmag", "Comment", "z", "Qpct", "Xname", "Rname"]
MQ_MATCH = tap_json(MQ_COLUMNS, [[10.0001, 20.0, "SDSS J004000.02+200000.0", "Q", 18.9, 19.3, "", 1.234, 100, "", "NVSS J004000+200000"]])
MQ_EMPTY = tap_json(MQ_COLUMNS, [])
VOTABLE_ERROR = ('<?xml version="1.0"?><VOTABLE><RESOURCE type="results"><INFO name="QUERY_STATUS" value="ERROR">'
                 'Cannot parse query: no such table</INFO></RESOURCE></VOTABLE>')


def tap_responses(*payloads):
    """A ``requests.post`` replacement answering the payloads in order (the last one repeats)."""
    calls = []

    def _post(url, data=None, timeout=None, headers=None):
        calls.append({"url": url, "query": (data or {}).get("QUERY"), "timeout": timeout})
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)

    _post.calls = calls
    return _post


@override_settings(JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False, ANNOTATION_AUTORUN_SERVICES="")
class AnnotationBase(TestCase):
    def setUp(self):
        self.staff = create_test_user("ann_staff", is_superuser=True)
        self.user = create_test_user("ann_user", is_staff=False)
        self.other = create_test_user("ann_other", is_staff=False)
        self.group, _ = Group.objects.get_or_create(name="Annotators")
        self.user.groups.add(self.group)
        self.transient = create_minimal_transient(self.staff, name="2026ann", obs_group_name="ann-group", ra=10.0, dec=20.0)
        self.services = {s.slug: s for s in svc.ensure_builtin_services(self.staff)}
        self.client = Client()

    def _run_queue(self):
        return run_pass(worker_id="test")


# --- model and service --------------------------------------------------------------------

class UpsertTests(AnnotationBase):
    def test_upsert_creates_replaces_and_keeps_audit(self):
        a, created = svc.upsert(self.transient, "antares", {"score": 0.91, "label": "SN"}, user=self.staff)
        self.assertTrue(created)
        self.assertEqual(a.created_by, self.staff)
        self.assertEqual(sorted(a.values.values_list("key", flat=True)), ["label", "score"])
        first_modified = a.modified_date
        b, created = svc.upsert(self.transient, "antares", {"score": 0.5}, user=self.user)
        self.assertFalse(created)
        self.assertEqual(b.pk, a.pk)
        self.assertEqual(b.data, {"score": 0.5})
        self.assertEqual(b.created_by, self.staff)
        self.assertEqual(b.modified_by, self.user)
        self.assertGreaterEqual(b.modified_date, first_modified)
        self.assertEqual(list(b.values.values_list("key", flat=True)), ["score"])
        self.assertEqual(TransientAnnotation.objects.filter(transient=self.transient, origin="antares").count(), 1)

    def test_merge_updates_keys(self):
        svc.upsert(self.transient, "antares", {"score": 0.9, "label": "SN"}, user=self.staff)
        a, _ = svc.upsert(self.transient, "antares", {"score": 0.1, "extra": True}, user=self.staff, merge=True)
        self.assertEqual(a.data, {"score": 0.1, "label": "SN", "extra": True})
        row = a.values.get(key="extra")
        self.assertEqual((row.value_text, row.value_num), ("true", 1.0))

    def test_unique_per_transient_and_origin(self):
        svc.upsert(self.transient, "x", {"a": 1}, user=self.staff)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TransientAnnotation.objects.create(transient=self.transient, origin="x", data={}, **audit_fields(self.staff))

    def test_validation(self):
        with self.assertRaises(svc.AnnotationError):
            svc.upsert(self.transient, "", {"a": 1}, user=self.staff)
        with self.assertRaises(svc.AnnotationError):
            svc.upsert(self.transient, "legacy", {"a": 1}, user=self.staff)
        with self.assertRaises(svc.AnnotationError):
            svc.upsert(self.transient, "ok", ["not", "a", "dict"], user=self.staff)
        with self.assertRaises(svc.AnnotationError):
            svc.upsert(self.transient, "ok", {"": 1}, user=self.staff)

    def test_value_rows_and_coercion(self):
        a, _ = svc.upsert(self.transient, "mixed", {
            "num": 3.25, "int": 7, "text": "hello", "numeric_text": "1e3", "flag": False, "nothing": None,
            "nested": {"a": [1, 2]}, "blank": "   ",
        }, user=self.staff)
        rows = {r.key: (r.value_text, r.value_num) for r in a.values.all()}
        self.assertEqual(rows["num"], ("3.25", 3.25))
        self.assertEqual(rows["int"], ("7", 7.0))
        self.assertEqual(rows["text"], ("hello", None))
        self.assertEqual(rows["numeric_text"], ("1e3", 1000.0))
        self.assertEqual(rows["flag"], ("false", 0.0))
        self.assertEqual(rows["nested"], ('{"a": [1, 2]}', None))
        self.assertNotIn("nothing", rows)
        self.assertNotIn("blank", rows)
        self.assertEqual(coerce_value(float("nan")), ("nan", None))
        self.assertEqual(coerce_value("x" * 300)[0], "x" * 255)

    def test_items_put_verdict_and_summary_first(self):
        a, _ = svc.upsert(self.transient, "gaia_dr3", {"parallax": 5.0, "verdict": "stellar", "summary": "s", "bp_rp": 1.0},
                          user=self.staff)
        self.assertEqual([k for k, _ in a.items], ["verdict", "summary", "bp_rp", "parallax"])
        self.assertEqual(a.verdict, "stellar")
        self.assertTrue(a.has_badge)
        self.assertIn('"parallax": 5.0', a.data_json)

    def test_cascade_delete_with_transient(self):
        svc.upsert(self.transient, "x", {"a": 1}, user=self.staff)
        t = create_minimal_transient(self.staff, name="2026del", obs_group_name="ann-group")
        svc.upsert(t, "x", {"a": 1}, user=self.staff)
        t.delete()
        self.assertEqual(TransientAnnotation.objects.count(), 1)
        self.assertEqual(TransientAnnotationValue.objects.count(), 1)


class VisibilityTests(AnnotationBase):
    def setUp(self):
        super().setUp()
        svc.upsert(self.transient, "public", {"a": 1}, user=self.staff)
        svc.upsert(self.transient, "private", {"b": 2}, user=self.staff, groups=[self.group])

    def test_groups_restrict(self):
        self.assertEqual([a.origin for a in svc.visible_annotations(self.transient, self.staff)], ["private", "public"])
        self.assertEqual([a.origin for a in svc.visible_annotations(self.transient, self.user)], ["private", "public"])
        self.assertEqual([a.origin for a in svc.visible_annotations(self.transient, self.other)], ["public"])
        self.assertEqual(svc.visible_annotations(self.transient, None), [])
        private = TransientAnnotation.objects.get(origin="private")
        self.assertTrue(private.visible_to(self.user))
        self.assertFalse(private.visible_to(self.other))

    def test_bulk_lookup(self):
        t2 = create_minimal_transient(self.staff, name="2026ann2", obs_group_name="ann-group")
        svc.upsert(t2, "public", {"a": 2}, user=self.staff)
        with self.assertNumQueries(2):  # annotations (group check inline) + groups prefetch
            out = svc.annotations_for_transients([self.transient, t2], self.other)
        self.assertEqual([a.origin for a in out[self.transient.pk]], ["public"])
        self.assertEqual([a.origin for a in out[t2.pk]], ["public"])

    def test_write_permissions(self):
        self.assertTrue(svc.can_write_origin(self.staff, "anything"))
        self.assertTrue(svc.can_write_origin(self.user, "user:ann_user"))
        self.assertFalse(svc.can_write_origin(self.user, "user:ann_other"))
        self.assertFalse(svc.can_write_origin(self.user, "gaia_dr3"))
        self.assertEqual(svc.user_origin(self.user), "user:ann_user")

    def test_badges(self):
        svc.upsert(self.transient, "gaia_dr3", {"verdict": "stellar", "summary": "star"}, user=self.staff)
        svc.upsert(self.transient, "wise", {"verdict": "clean"}, user=self.staff)
        badges = svc.badges_for(svc.visible_annotations(self.transient, self.staff))
        self.assertEqual(badges, [{"origin": "gaia_dr3", "verdict": "stellar", "summary": "star"}])


class LegacyShimTests(AnnotationBase):
    def test_legacy_annotation(self):
        self.assertIsNone(svc.legacy_annotation(self.transient))
        cls, _ = AntaresClassification.objects.get_or_create(name="SN-like", defaults=audit_fields(self.staff))
        self.transient.point_source_probability = 0.97
        self.transient.real_bogus_score = 0.8
        self.transient.has_hst = True
        self.transient.antares_classification = cls
        self.transient.save()
        legacy = svc.legacy_annotation(self.transient)
        self.assertEqual(legacy["origin"], "legacy")
        self.assertTrue(legacy["read_only"])
        self.assertEqual(legacy["data"], {"point_source_probability": 0.97, "real_bogus_score": 0.8, "has_hst": True,
                                          "antares_classification": "SN-like"})


# --- checks and runners -------------------------------------------------------------------

class CheckTests(AnnotationBase):
    def test_gaia_stellar(self):
        post = tap_responses(GAIA_STAR)
        with mock.patch.object(base.requests, "post", post):
            data, verdict, summary = gaia.check(10.0, 20.0, 3.0)
        self.assertEqual(verdict, "stellar")
        self.assertEqual(data["n_matches"], 1)
        self.assertEqual(data["parallax"], 5.2)
        self.assertEqual(data["parallax_over_error"], 52.0)
        self.assertAlmostEqual(data["pm"], 12.369, places=3)
        self.assertEqual(data["distance_pc"], 192.3)
        self.assertEqual(data["source_id"], "4295806720")
        self.assertLess(data["separation_arcsec"], 1.0)
        self.assertIn("parallax 5.20", summary)
        self.assertEqual(post.calls[0]["url"], base.GAIA_TAP_URL_DEFAULT)
        self.assertIn("gaiadr3.gaia_source", post.calls[0]["query"])
        self.assertIn("CIRCLE('ICRS', 10.0000000, 20.0000000, 0.00083333)", post.calls[0]["query"])

    def test_gaia_clean_and_unknown(self):
        with mock.patch.object(base.requests, "post", tap_responses(GAIA_EMPTY)):
            data, verdict, summary = gaia.check(10.0, 20.0, 3.0)
        self.assertEqual((verdict, data["n_matches"]), ("clean", 0))
        self.assertIn("No Gaia DR3 source", summary)
        with mock.patch.object(base.requests, "post", tap_responses(GAIA_FAINT)):
            data, verdict, summary = gaia.check(10.0, 20.0, 3.0)
        self.assertEqual(verdict, "unknown")
        self.assertIsNone(data["bp_rp"])
        self.assertNotIn("distance_pc", data)
        self.assertIn("without significant parallax", summary)

    def test_wise_agn_like_and_clean(self):
        with mock.patch.object(base.requests, "post", tap_responses(WISE_AGN)):
            data, verdict, summary = wise.check(10.0, 20.0, 3.0)
        self.assertEqual(verdict, "AGN-like")
        self.assertEqual(data["catalog"], "AllWISE")
        self.assertAlmostEqual(data["w1_w2"], 1.1)
        self.assertAlmostEqual(data["w2_w3"], 2.5)
        self.assertEqual(data["ccf"], "0000")
        self.assertIn("W1-W2 = 1.10", summary)
        with mock.patch.object(base.requests, "post", tap_responses(WISE_STAR)):
            data, verdict, summary = wise.check(10.0, 20.0, 3.0)
        self.assertEqual(verdict, "clean")
        self.assertAlmostEqual(data["w1_w2"], 0.1)
        self.assertIsNone(data["w4"])

    def test_wise_catwise_fallback(self):
        post = tap_responses(WISE_EMPTY, CATWISE_MATCH)
        with mock.patch.object(base.requests, "post", post):
            data, verdict, _ = wise.check(10.0, 20.0, 3.0)
        self.assertEqual(len(post.calls), 2)
        self.assertIn("II/328/allwise", post.calls[0]["query"])
        self.assertIn("II/365/catwise", post.calls[1]["query"])
        self.assertEqual(data["catalog"], "CatWISE2020")
        self.assertEqual(verdict, "AGN-like")
        self.assertAlmostEqual(data["w1_w2"], 0.9)
        self.assertNotIn("w3", data)
        with mock.patch.object(base.requests, "post", tap_responses(WISE_EMPTY, tap_json(CATWISE_COLUMNS, []))):
            data, verdict, summary = wise.check(10.0, 20.0, 3.0)
        self.assertEqual((verdict, data["n_matches"]), ("clean", 0))
        self.assertIn("No AllWISE or CatWISE2020", summary)

    def test_quasar_match_and_clean(self):
        with mock.patch.object(base.requests, "post", tap_responses(MQ_MATCH)) as post:
            data, verdict, summary = quasar.check(10.0, 20.0, 3.0)
        self.assertEqual(verdict, "AGN-like")
        self.assertEqual(data["type"], "Q")
        self.assertEqual(data["type_label"], "quasar")
        self.assertEqual(data["redshift"], 1.234)
        self.assertEqual(data["qpct"], 100)
        self.assertEqual(data["radio_name"], "NVSS J004000+200000")
        self.assertIsNone(data["comment"])
        self.assertIn("z = 1.234", summary)
        self.assertIn("VII/294/catalog", post.calls[0]["query"])
        with mock.patch.object(base.requests, "post", tap_responses(MQ_EMPTY)):
            data, verdict, _ = quasar.check(10.0, 20.0, 3.0)
        self.assertEqual(verdict, "clean")

    def test_tap_errors(self):
        with mock.patch.object(base.requests, "post", tap_responses(FakeResponse(VOTABLE_ERROR, 400))):
            with self.assertRaises(base.AnnotationCheckError) as ctx:
                gaia.check(10.0, 20.0, 3.0)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("Cannot parse query", str(ctx.exception))
        with mock.patch.object(base.requests, "post", tap_responses(FakeResponse(VOTABLE_ERROR, 200))):
            with self.assertRaises(base.AnnotationCheckError) as ctx:
                gaia.check(10.0, 20.0, 3.0)
        self.assertIn("did not answer with JSON", str(ctx.exception))
        with mock.patch.object(base.requests, "post", tap_responses(requests.ConnectionError("proxy refused"))):
            with self.assertRaises(base.AnnotationCheckError) as ctx:
                gaia.check(10.0, 20.0, 3.0)
        self.assertIn("proxy refused", str(ctx.exception))
        with mock.patch.object(base.requests, "post", tap_responses({"nope": 1})):
            with self.assertRaises(base.AnnotationCheckError):
                gaia.check(10.0, 20.0, 3.0)

    @override_settings(GAIA_TAP_URL="https://tap.example.org/sync", ANNOTATION_HTTP_TIMEOUT_SECONDS=7)
    def test_settings_override_endpoint_and_timeout(self):
        post = tap_responses(GAIA_EMPTY)
        with mock.patch.object(base.requests, "post", post):
            gaia.check(1.0, 2.0, 3.0)
        self.assertEqual(post.calls[0]["url"], "https://tap.example.org/sync")
        self.assertEqual(post.calls[0]["timeout"], 7.0)


class RunnerTests(AnnotationBase):
    def _start(self, slug, user=None):
        return svc.start_annotation_run(self.services[slug], self.transient, user or self.staff)

    def test_builtin_services_registered(self):
        self.assertEqual(set(self.services), {"gaia_dr3", "wise", "quasar"})
        for service in self.services.values():
            self.assertEqual(service.kind, ExternalService.KIND_ANNOTATION)
            self.assertIsNotNone(runs.get_runner(service))
        self.assertEqual(set(CHECKS), {"gaia_dr3", "wise", "quasar"})
        # idempotent
        again = svc.ensure_builtin_services(self.staff)
        self.assertEqual({s.pk for s in again}, {s.pk for s in self.services.values()})

    def test_queue_path_writes_annotation_and_completes_run(self):
        run = self._start("gaia_dr3")
        self.assertEqual(run.status, ExternalServiceRun.STATUS_PENDING)
        self.assertEqual(run.request_payload, {"annotation": True, "ra": 10.0, "dec": 20.0, "radius_arcsec": 3.0})
        self.assertEqual(Job.objects.filter(kind=runs.JOB_KIND, status=Job.QUEUED).count(), 1)
        with mock.patch.object(base.requests, "post", tap_responses(GAIA_STAR)):
            result = self._run_queue()
        self.assertEqual(result.done, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(run.result["verdict"], "stellar")
        annotation = TransientAnnotation.objects.get(transient=self.transient, origin="gaia_dr3")
        self.assertEqual(annotation.run, run)
        self.assertEqual(annotation.service, self.services["gaia_dr3"])
        self.assertEqual(annotation.created_by, self.staff)
        self.assertEqual(annotation.data["verdict"], "stellar")
        self.assertEqual(annotation.data["radius_arcsec"], 3.0)
        self.assertEqual(annotation.values.get(key="parallax_over_error").value_num, 52.0)
        job = Job.objects.get(kind=runs.JOB_KIND)
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(job.result["verdict"], "stellar")

    def test_all_three_inline_then_rerun_overwrites_in_place(self):
        with override_settings(JOB_RUNNER_INLINE=True):
            with mock.patch.object(base.requests, "post", tap_responses(GAIA_STAR)):
                self._start("gaia_dr3")
            with mock.patch.object(base.requests, "post", tap_responses(WISE_AGN)):
                self._start("wise")
            with mock.patch.object(base.requests, "post", tap_responses(MQ_MATCH)):
                self._start("quasar")
        rows = {a.origin: a for a in TransientAnnotation.objects.filter(transient=self.transient)}
        self.assertEqual(set(rows), {"gaia_dr3", "wise", "quasar"})
        self.assertEqual({o: a.verdict for o, a in rows.items()}, {"gaia_dr3": "stellar", "wise": "AGN-like", "quasar": "AGN-like"})
        self.assertEqual(ExternalServiceRun.objects.filter(status=ExternalServiceRun.STATUS_SUCCEEDED).count(), 3)
        before = rows["gaia_dr3"]
        with override_settings(JOB_RUNNER_INLINE=True):
            with mock.patch.object(base.requests, "post", tap_responses(GAIA_EMPTY)):
                rerun = self._start("gaia_dr3", user=self.user)
        after = TransientAnnotation.objects.get(pk=before.pk)
        self.assertEqual(TransientAnnotation.objects.filter(transient=self.transient, origin="gaia_dr3").count(), 1)
        self.assertEqual(after.verdict, "clean")
        self.assertEqual(after.run_id, rerun.pk)
        self.assertEqual(after.created_by, self.staff)
        self.assertEqual(after.modified_by, self.user)
        self.assertGreaterEqual(after.modified_date, before.modified_date)
        self.assertFalse(after.values.filter(key="parallax").exists())

    def test_failures_land_on_the_run(self):
        run = self._start("wise")
        with mock.patch.object(base.requests, "post", tap_responses(FakeResponse(VOTABLE_ERROR, 500))):
            self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("HTTP 500", run.error)
        self.assertFalse(TransientAnnotation.objects.filter(origin="wise").exists())
        run2 = self._start("quasar")
        with mock.patch.object(base.requests, "post", tap_responses(requests.Timeout("read timed out"))):
            self._run_queue()
        run2.refresh_from_db()
        self.assertEqual(run2.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("read timed out", run2.error)
        # an unexpected exception inside a check is reported, not raised into the queue
        run3 = self._start("gaia_dr3")
        with mock.patch.dict(CHECKS, {"gaia_dr3": mock.Mock(side_effect=ZeroDivisionError("boom"))}):
            result = self._run_queue()
        run3.refresh_from_db()
        self.assertEqual(run3.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("ZeroDivisionError: boom", run3.error)
        self.assertEqual(result.failed, 0)

    def test_unknown_annotation_slug_fails_cleanly(self):
        service = ExternalService.objects.create(name="Other", slug="other_cat", kind=ExternalService.KIND_ANNOTATION,
                                                 **audit_fields(self.staff))
        run = svc.start_annotation_run(service, self.transient, self.staff)
        self._run_queue()
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("no annotation check is registered", run.error)

    def test_register_check_extends(self):
        from YSE_App.annotation_services import register_check

        service = ExternalService.objects.create(name="Custom", slug="custom_cat", kind=ExternalService.KIND_ANNOTATION,
                                                 **audit_fields(self.staff))
        register_check("custom_cat", lambda ra, dec, r: ({"hit": 1}, "clean", "ok"))
        try:
            run = svc.start_annotation_run(service, self.transient, self.staff)
            self._run_queue()
        finally:
            CHECKS.pop("custom_cat", None)
            runs.unregister_runner("custom_cat")
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(TransientAnnotation.objects.get(origin="custom_cat").data["hit"], 1)

    def test_non_annotation_service_rejected(self):
        service = ExternalService.objects.create(name="Fit", slug="fit", kind=ExternalService.KIND_ANALYSIS,
                                                 **audit_fields(self.staff))
        with self.assertRaises(svc.AnnotationError):
            svc.start_annotation_run(service, self.transient, self.staff)

    def test_stale_runs_fail(self):
        run = self._start("gaia_dr3")
        self.assertEqual(svc.fail_stale_runs(), 0)
        ExternalServiceRun.objects.filter(pk=run.pk).update(created_date=run.created_date - __import__("datetime").timedelta(hours=3))
        self.assertEqual(svc.fail_stale_runs(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)

    def test_autorun_setting(self):
        t = create_minimal_transient(self.staff, name="2026auto0", obs_group_name="ann-group")
        self.assertFalse(ExternalServiceRun.objects.filter(transient=t).exists())
        with override_settings(ANNOTATION_AUTORUN_SERVICES="gaia_dr3, quasar"):
            t2 = create_minimal_transient(self.staff, name="2026auto1", obs_group_name="ann-group")
        slugs = set(ExternalServiceRun.objects.filter(transient=t2).values_list("service__slug", flat=True))
        self.assertEqual(slugs, {"gaia_dr3", "quasar"})

    def test_management_command(self):
        ExternalService.objects.filter(slug__in=svc.BUILTIN_SLUGS).delete()
        out = StringIO()
        call_command("register_annotation_services", "--disabled", stdout=out)
        self.assertEqual(ExternalService.objects.filter(kind=ExternalService.KIND_ANNOTATION, enabled=False).count(), 3)
        self.assertIn("gaia_dr3", out.getvalue())
        call_command("register_annotation_services", "--user", "ann_staff", stdout=StringIO())
        self.assertEqual(ExternalService.objects.filter(kind=ExternalService.KIND_ANNOTATION).count(), 3)


# --- detail page -------------------------------------------------------------------------------

class DetailPanelTests(AnnotationBase):
    def setUp(self):
        super().setUp()
        svc.upsert(self.transient, "gaia_dr3", {"verdict": "stellar", "summary": "Gaia star.", "parallax": 5.2},
                   user=self.staff, service=self.services["gaia_dr3"])
        svc.upsert(self.transient, "user:ann_user", {"note": "looks nuclear"}, user=self.user)
        svc.upsert(self.transient, "private", {"secret": 1}, user=self.staff, groups=[self.group])
        self.transient.point_source_probability = 0.97
        self.transient.save(update_fields=["point_source_probability"])
        self.fragment_url = reverse("transient_detail_annotations_fragment", args=[self.transient.pk])
        self.summary_url = reverse("transient_annotations_summary", args=[self.transient.pk])
        self.run_url = reverse("transient_annotation_run", args=[self.transient.pk])
        self.delete_url = reverse("transient_annotation_delete", args=[self.transient.pk])

    def test_detail_page_has_tab_and_loader(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="annotations_tab_header"', html)
        self.assertIn('id="annotations_container"', html)
        self.assertIn('id="annotation_badges"', html)
        self.assertIn(self.fragment_url, html)
        self.assertIn(self.summary_url, html)

    def test_fragment_for_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.fragment_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Gaia DR3 check", html)
        self.assertIn('data-origin="gaia_dr3"', html)
        self.assertIn('data-verdict="stellar"', html)
        self.assertIn("Gaia star.", html)
        self.assertIn('data-origin="user:ann_user"', html)
        self.assertIn('data-origin="private"', html)
        self.assertIn('data-origin="legacy"', html)
        self.assertIn("point_source_probability", html)
        self.assertIn('data-annotation-action="run" data-service="gaia_dr3"', html)
        self.assertIn('data-annotation-action="delete" data-origin="private"', html)
        self.assertIn('data-service="wise"', html)
        self.assertIn(" Check</button>", html)
        self.assertIn('data-active="0"', html)

    def test_fragment_for_users(self):
        self.client.force_login(self.user)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn('data-origin="private"', html)  # in the group
        self.assertNotIn('data-annotation-action="run"', html)  # not staff, no group on the services
        self.assertIn('data-annotation-action="delete" data-origin="user:ann_user"', html)
        self.assertNotIn('data-annotation-action="delete" data-origin="gaia_dr3"', html)
        self.assertIn("Staff (or a group named on a service)", html)
        self.client.force_login(self.other)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertNotIn('data-origin="private"', html)
        self.assertNotIn('data-annotation-action="delete"', html)
        # a service that names the user's group offers its button
        self.services["wise"].groups.add(self.group)
        self.client.force_login(self.user)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn('data-annotation-action="run" data-service="wise"', html)
        self.assertNotIn('data-annotation-action="run" data-service="quasar"', html)

    def test_fragment_query_budget(self):
        self.client.force_login(self.staff)
        with self.assertNumQueries(9):
            self.client.get(self.fragment_url)

    def test_empty_fragment(self):
        t = create_minimal_transient(self.staff, name="2026none", obs_group_name="ann-group")
        self.client.force_login(self.staff)
        html = self.client.get(reverse("transient_detail_annotations_fragment", args=[t.pk])).content.decode()
        self.assertIn('id="annotations_empty"', html)
        ExternalService.objects.filter(kind=ExternalService.KIND_ANNOTATION).delete()
        html = self.client.get(reverse("transient_detail_annotations_fragment", args=[t.pk])).content.decode()
        self.assertIn("register_annotation_services", html)

    def test_summary_json(self):
        self.client.force_login(self.other)
        json_ = self.client.get(self.summary_url).json()
        self.assertEqual(json_["count"], 3)  # gaia, user note, legacy; not the private one
        self.assertEqual(json_["badges"], [{"origin": "gaia_dr3", "verdict": "stellar", "summary": "Gaia star."}])
        self.assertFalse(json_["active"])
        self.assertEqual(self.client.get(reverse("transient_annotations_summary", args=[999999])).status_code, 404)

    def test_run_action_permissions_and_conflict(self):
        self.client.force_login(self.user)
        response = self.client.post(self.run_url, data=json.dumps({"service": "gaia_dr3"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.staff)
        response = self.client.post(self.run_url, data=json.dumps({"service": "nope"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(self.run_url, {"service": "gaia_dr3"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["run"]["status"], "pending")
        run = ExternalServiceRun.objects.get(uuid=body["run"]["uuid"])
        self.assertEqual(run.created_by, self.staff)
        self.assertEqual(run.transient, self.transient)
        response = self.client.post(self.run_url, {"service": "gaia_dr3"})
        self.assertEqual(response.status_code, 409)
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn('data-active="1"', html)
        self.assertTrue(self.client.get(self.summary_url).json()["active"])
        self.assertEqual(self.client.get(self.run_url).status_code, 405)
        # a group member of the service may run it
        self.services["wise"].groups.add(self.group)
        self.client.force_login(self.user)
        response = self.client.post(self.run_url, {"service": "wise"})
        self.assertEqual(response.status_code, 200)
        # disabled service -> 400 for everyone
        self.services["quasar"].enabled = False
        self.services["quasar"].save()
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.run_url, {"service": "quasar"}).status_code, 403)

    def test_rerun_from_button_updates_row(self):
        self.client.force_login(self.staff)
        with override_settings(JOB_RUNNER_INLINE=True), mock.patch.object(base.requests, "post", tap_responses(GAIA_EMPTY)):
            response = self.client.post(self.run_url, {"service": "gaia_dr3"})
        self.assertEqual(response.json()["run"]["status"], "succeeded")
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn('data-verdict="clean"', html)
        self.assertNotIn('data-verdict="stellar"', html)
        self.assertIn(" Rerun</button>", html)
        self.assertEqual(self.client.get(self.summary_url).json()["badges"], [])

    def test_failed_run_shows_error(self):
        self.client.force_login(self.staff)
        with override_settings(JOB_RUNNER_INLINE=True), mock.patch.object(
                base.requests, "post", tap_responses(requests.ConnectionError("CONNECT 403"))):
            self.client.post(self.run_url, {"service": "wise"})
        html = self.client.get(self.fragment_url).content.decode()
        self.assertIn("CONNECT 403", html)
        self.assertIn('data-status="failed"', html)

    def test_delete_action(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.delete_url, {"origin": "user:ann_user"}).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.delete_url, {"origin": "gaia_dr3"}).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url, {"origin": "user:ann_user"}).status_code, 200)
        self.assertFalse(TransientAnnotation.objects.filter(origin="user:ann_user").exists())
        self.assertEqual(self.client.post(self.delete_url, {"origin": "user:ann_user"}).status_code, 404)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.delete_url, {"origin": "private"}).status_code, 200)

    def test_login_required(self):
        for url in (self.fragment_url, self.summary_url):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(self.run_url, {"service": "wise"}).status_code, 302)


# --- API -------------------------------------------------------------------------------------

class ApiTests(AnnotationBase):
    def setUp(self):
        super().setUp()
        attach_synthetic_photometry(self.staff, self.transient)  # public photometry: users may see it
        svc.upsert(self.transient, "gaia_dr3", {"verdict": "stellar", "parallax": 5.2}, user=self.staff,
                   service=self.services["gaia_dr3"])
        svc.upsert(self.transient, "private", {"secret": 1}, user=self.staff, groups=[self.group])
        self.transient.mw_ebv = 0.03
        self.transient.save(update_fields=["mw_ebv"])
        self.list_url = reverse("transientannotation-list")

    def _results(self, response):
        data = response.json()
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def test_list_and_legacy(self):
        self.client.force_login(self.staff)
        rows = self._results(self.client.get(self.list_url, {"transient": self.transient.pk}))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3", "private", "legacy"])
        self.assertEqual(rows[0]["data"]["parallax"], 5.2)
        self.assertEqual(rows[0]["verdict"], "stellar")
        self.assertEqual(rows[0]["service"], "gaia_dr3")
        self.assertEqual(rows[0]["transient_name"], "2026ann")
        self.assertEqual(rows[1]["groups"], ["Annotators"])
        self.assertEqual(rows[2], {**rows[2], "origin": "legacy", "read_only": True, "data": {"mw_ebv": 0.03}})
        rows = self._results(self.client.get(self.list_url, {"transient": "2026ann", "legacy": "0"}))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3", "private"])
        rows = self._results(self.client.get(self.list_url, {"transient": self.transient.pk, "origin": "gaia_dr3"}))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3"])
        rows = self._results(self.client.get(self.list_url, {"key": "parallax"}))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3"])
        rows = self._results(self.client.get(self.list_url, {"verdict": "stellar"}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._results(self.client.get(self.list_url, {"verdict": "clean"})), [])
        # no transient filter -> no legacy entry
        rows = self._results(self.client.get(self.list_url))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3", "private"])

    def test_list_visibility(self):
        self.client.force_login(self.other)
        rows = self._results(self.client.get(self.list_url, {"transient": self.transient.pk}))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3", "legacy"])
        self.client.force_login(self.user)
        rows = self._results(self.client.get(self.list_url, {"transient": self.transient.pk}))
        self.assertEqual([r["origin"] for r in rows], ["gaia_dr3", "private", "legacy"])
        self.client.logout()
        self.assertIn(self.client.get(self.list_url).status_code, (401, 403))

    def test_create_upsert_permissions(self):
        self.client.force_login(self.user)
        response = self.client.post(self.list_url, data=json.dumps({"transient": "2026ann", "data": {"note": "hi"}}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["origin"], "user:ann_user")
        self.assertEqual(response.json()["created_by"], "ann_user")
        response = self.client.post(self.list_url, data=json.dumps({"transient": "2026ann", "origin": "user:ann_user", "data": {"note": "again"}}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TransientAnnotation.objects.get(origin="user:ann_user").data, {"note": "again"})
        response = self.client.post(self.list_url, data=json.dumps({"transient": "2026ann", "origin": "antares", "data": {"score": 1}}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)
        response = self.client.post(self.list_url, data=json.dumps({"transient": "nope", "data": {}}), content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.client.force_login(self.staff)
        response = self.client.post(self.list_url, data=json.dumps(
            {"transient": self.transient.pk, "origin": "antares", "data": {"score": 0.9}, "groups": ["Annotators"]}),
            content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["groups"], ["Annotators"])
        response = self.client.post(self.list_url, data=json.dumps({"transient": self.transient.pk, "origin": "antares", "data": {"x": 1}, "groups": ["Nope"]}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(self.list_url, data=json.dumps({"transient": self.transient.pk, "origin": "legacy", "data": {"x": 1}}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_update_and_delete(self):
        mine, _ = svc.upsert(self.transient, "user:ann_user", {"note": "a", "n": 1}, user=self.user)
        url = reverse("transientannotation-detail", args=[mine.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.patch(url, data=json.dumps({"data": {"n": 2}}), content_type="application/json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.client.force_login(self.user)
        response = self.client.patch(url, data=json.dumps({"data": {"n": 2}}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"note": "a", "n": 2})  # PATCH merges
        response = self.client.put(url, data=json.dumps({"data": {"n": 3}}), content_type="application/json")
        self.assertEqual(response.json()["data"], {"n": 3})  # PUT replaces
        response = self.client.put(url, data=json.dumps({"origin": "other", "data": {}}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(TransientAnnotation.objects.filter(pk=mine.pk).exists())
        gaia_row = TransientAnnotation.objects.get(origin="gaia_dr3")
        self.assertEqual(self.client.delete(reverse("transientannotation-detail", args=[gaia_row.pk])).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.delete(reverse("transientannotation-detail", args=[gaia_row.pk])).status_code, 204)


# --- search filters and column (#319) ------------------------------------------------------------

class FilterTests(AnnotationBase):
    def setUp(self):
        super().setUp()
        self.star = self.transient
        self.agn = create_minimal_transient(self.staff, name="2026agn", obs_group_name="ann-group", ra=11.0, dec=21.0)
        self.plain = create_minimal_transient(self.staff, name="2026sn", obs_group_name="ann-group", ra=12.0, dec=22.0)
        svc.upsert(self.star, "gaia_dr3", {"verdict": "stellar", "parallax_over_error": 52.0, "pm": 12.4}, user=self.staff)
        svc.upsert(self.agn, "gaia_dr3", {"verdict": "unknown", "parallax_over_error": 0.4}, user=self.staff)
        svc.upsert(self.agn, "wise", {"verdict": "AGN-like", "w1_w2": 1.1}, user=self.staff)
        svc.upsert(self.plain, "wise", {"verdict": "clean", "w1_w2": 0.1}, user=self.staff)
        svc.upsert(self.plain, "user:ann_user", {"parallax_over_error": 99.0}, user=self.user)
        self.plain.point_source_probability = 0.2
        self.plain.save(update_fields=["point_source_probability"])
        self.star.point_source_probability = 0.95
        self.star.save(update_fields=["point_source_probability"])
        self.base_qs = Transient.objects.filter(pk__in=[self.star.pk, self.agn.pk, self.plain.pk])

    def _names(self, **params):
        fs = TransientSearchFilterSet(params, queryset=self.base_qs)
        self.assertTrue(fs.form.is_valid(), fs.form.errors)
        return set(fs.qs.values_list("name", flat=True))

    def test_origin_and_key_filters(self):
        self.assertEqual(self._names(annotation_origin="gaia_dr3"), {"2026ann", "2026agn"})
        self.assertEqual(self._names(annotation_origin="wise"), {"2026agn", "2026sn"})
        self.assertEqual(self._names(annotation_origin="nope"), set())
        self.assertEqual(self._names(annotation_key="parallax_over_error"), {"2026ann", "2026agn", "2026sn"})
        self.assertEqual(self._names(annotation_origin="gaia_dr3", annotation_key="parallax_over_error"), {"2026ann", "2026agn"})

    def test_value_filters(self):
        # the acceptance case of #319: gaia_dr3.parallax_over_error > 3 returns the stellar fixture only
        self.assertEqual(self._names(annotation_origin="gaia_dr3", annotation_key="parallax_over_error", annotation_value_min=3), {"2026ann"})
        self.assertEqual(self._names(annotation_key="parallax_over_error", annotation_value_min=3), {"2026ann", "2026sn"})
        self.assertEqual(self._names(annotation_key="parallax_over_error", annotation_value_max=1), {"2026agn"})
        self.assertEqual(self._names(annotation_key="parallax_over_error", annotation_value_min=0, annotation_value_max=60), {"2026ann", "2026agn"})
        self.assertEqual(self._names(annotation_key="verdict", annotation_value_eq="AGN-like"), {"2026agn"})
        self.assertEqual(self._names(annotation_origin="wise", annotation_key="verdict", annotation_value_eq="clean"), {"2026sn"})
        self.assertEqual(self._names(annotation_key="w1_w2", annotation_value_eq="1.1"), {"2026agn"})
        self.assertEqual(self._names(annotation_key="pm", annotation_value_eq="12.4"), {"2026ann"})

    def test_legacy_column_filters(self):
        self.assertEqual(self._names(annotation_origin="legacy", annotation_key="point_source_probability"), {"2026ann", "2026sn"})
        self.assertEqual(self._names(annotation_origin="legacy", annotation_key="point_source_probability", annotation_value_min=0.5), {"2026ann"})
        self.assertEqual(self._names(annotation_origin="legacy", annotation_key="point_source_probability", annotation_value_eq="0.2"), {"2026sn"})
        self.assertEqual(self._names(annotation_origin="legacy", annotation_key="no_such_column"), set())
        self.assertEqual(self._names(annotation_origin="legacy"), {"2026ann", "2026agn", "2026sn"})

    def test_value_without_key_is_a_form_error(self):
        fs = TransientSearchFilterSet({"annotation_value_min": "3"}, queryset=self.base_qs)
        self.assertFalse(fs.form.is_valid())
        self.assertIn("annotation key", str(fs.form.errors))
        fs = TransientSearchFilterSet({"annotation_column": "gaia_dr3."}, queryset=self.base_qs)
        self.assertFalse(fs.form.is_valid())
        self.assertEqual(parse_annotation_column("gaia_dr3.parallax"), ("gaia_dr3", "parallax"))
        self.assertEqual(parse_annotation_column("parallax"), (None, "parallax"))
        self.assertIsNone(parse_annotation_column(""))
        self.assertIsNone(parse_annotation_column("a."))

    def test_annotation_column_and_ordering(self):
        fs = TransientSearchFilterSet({"annotation_column": "gaia_dr3.parallax_over_error", "ordering": "-annotation_value"},
                                      queryset=self.base_qs)
        self.assertTrue(fs.form.is_valid(), fs.form.errors)
        self.assertEqual(fs.annotation_column_spec, ("gaia_dr3", "parallax_over_error"))
        self.assertEqual(fs.annotation_column_label, "gaia_dr3.parallax_over_error")
        rows = list(fs.qs.values_list("name", "annotation_value", "annotation_value_num"))
        self.assertEqual(rows[0], ("2026ann", "52.0", 52.0))
        self.assertEqual(rows[1], ("2026agn", "0.4", 0.4))
        self.assertEqual(rows[2][0], "2026sn")
        self.assertIsNone(rows[2][1])
        # ascending: nulls sort first on both vendors, then 0.4, 52
        fs = TransientSearchFilterSet({"annotation_column": "gaia_dr3.parallax_over_error", "ordering": "annotation_value"},
                                      queryset=self.base_qs)
        self.assertEqual([r[0] for r in fs.qs.values_list("name", "annotation_value")][-2:], ["2026agn", "2026ann"])
        # key only -> first origin alphabetically that has the key
        fs = TransientSearchFilterSet({"annotation_column": "parallax_over_error"}, queryset=self.base_qs)
        values = dict(fs.qs.values_list("name", "annotation_value"))
        self.assertEqual(values["2026sn"], "99.0")
        # the filtered origin.key doubles as the column when none is named
        fs = TransientSearchFilterSet({"annotation_origin": "wise", "annotation_key": "w1_w2"}, queryset=self.base_qs)
        self.assertEqual(fs.annotation_column_spec, ("wise", "w1_w2"))
        self.assertEqual(dict(fs.qs.values_list("name", "annotation_value")), {"2026agn": "1.1", "2026sn": "0.1"})
        # legacy column
        fs = TransientSearchFilterSet({"annotation_column": "legacy.point_source_probability", "ordering": "-annotation_value"},
                                      queryset=self.base_qs)
        self.assertEqual([r[0] for r in fs.qs.values_list("name", "annotation_value_num")][:2], ["2026ann", "2026sn"])
        # ordering by the column without one is dropped, not an error
        fs = TransientSearchFilterSet({"ordering": "annotation_value"}, queryset=self.base_qs)
        self.assertTrue(fs.form.is_valid())
        self.assertEqual(fs.qs.count(), 3)

    def test_search_page_and_api(self):
        self.client.force_login(self.staff)
        response = self.client.get("/search/", {"annotation_origin": "gaia_dr3", "annotation_key": "parallax_over_error",
                                                "annotation_value_min": "3"})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("2026ann", html)
        self.assertNotIn("2026agn", html)
        self.assertIn("gaia_dr3.parallax_over_error", html)  # the column header
        self.assertIn("52.0", html)
        self.assertIn('id="search-group-annotations"', html)
        response = self.client.get("/search/", {"name_contains": "2026"})
        self.assertNotIn(">Annotation<", response.content.decode())
        response = self.client.get("/search/", {"annotation_value_min": "3"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Give an annotation key", response.content.decode())
        response = self.client.get(reverse("transient-list"), {"annotation_key": "verdict", "annotation_value_eq": "AGN-like"})
        self.assertEqual(response.status_code, 200)
        names = [r["name"] for r in response.json()["results"]]
        self.assertEqual(names, ["2026agn"])

    def test_filters_are_one_query_per_page(self):
        fs = TransientSearchFilterSet({"annotation_origin": "gaia_dr3", "annotation_key": "parallax_over_error",
                                       "annotation_value_min": "3", "annotation_column": "wise.w1_w2"}, queryset=self.base_qs)
        with self.assertNumQueries(1):
            list(fs.qs)
