"""Sharing services: TNS reports, submission queue, retrieval, auto-publishers (#324-#327).

TNS is never contacted: ``requests.post`` is mocked with replies in the shape
of the TNS bulk-report API (``bulk-report`` -> ``report_id``,
``bulk-report-reply`` -> feedback with ``objname``, ``get/search`` -> objects).
"""

from __future__ import annotations

import datetime
import json
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.jobs import JobFailed, JobRetry, run_pass
from YSE_App.models import (
    AlternateTransientNames,
    AutoPublisher,
    DataQuality,
    EncryptedCredential,
    Job,
    Log,
    Notification,
    SharingService,
    SharingSubmission,
    Transient,
    TransientClass,
    TransientPhotData,
    TransientSpecData,
)
from YSE_App.sharing import autopublish, retrieval, tns
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_spectrum,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
    create_transient_with_synthetic_data,
    ensure_transient_statuses,
)

KEY = Fernet.generate_key().decode()
SECRET = {"tns_bot_id": "1234", "tns_bot_name": "YSE_Bot", "tns_api_key": "very-secret-key"}


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


def accepted_reply(objname="2026abc", prefix="AT", kind="at_report", code="100"):
    return {"id_code": 200, "id_message": "OK", "data": {
        "received_data": {}, "feedback": {kind: [{code: {"objname": objname, "prefix": prefix, "objid": 42,
                                                          "message": "Object created."}}]}}}


def rejected_reply(message="Report already exists for these coordinates"):
    return {"id_code": 200, "id_message": "OK", "data": {
        "received_data": {}, "feedback": {"at_report": [{"ra": {"120": {"message": message}}}]}}}


class TNSRouter:
    """Mock for ``requests.post``: routes by URL suffix and records calls."""

    def __init__(self, *, reply=None, report_id=777, search_hits=None, not_ready=0):
        self.calls = []
        self.reply = reply if reply is not None else accepted_reply()
        self.report_id = report_id
        self.search_hits = search_hits if search_hits is not None else []
        self.not_ready = not_ready

    def __call__(self, url, headers=None, data=None, files=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data, "files": files, "timeout": timeout})
        if url.endswith("/bulk-report"):
            return FakeResponse({"id_code": 200, "id_message": "OK", "data": {"report_id": self.report_id}})
        if url.endswith("/bulk-report-reply"):
            if self.not_ready > 0:
                self.not_ready -= 1
                return FakeResponse({"id_code": 404, "id_message": "Unknown report"}, status=404)
            return FakeResponse(self.reply)
        if url.endswith("/set/file-upload"):
            return FakeResponse({"id_code": 200, "id_message": "OK", "data": ["tns_upload_00042.ascii"]})
        if url.endswith("/get/search"):
            return FakeResponse({"id_code": 200, "id_message": "OK", "data": list(self.search_hits)})
        if url.endswith("/get/object"):
            return FakeResponse({"id_code": 200, "id_message": "OK", "data": {"objname": "2026abc"}})
        return FakeResponse({"id_code": 404, "id_message": "unknown endpoint"}, status=404)

    def by_suffix(self, suffix):
        return [c for c in self.calls if c["url"].endswith(suffix)]


class FakeJob:
    def __init__(self, attempts=1, max_attempts=3, pk=None):
        self.attempts = attempts
        self.max_attempts = max_attempts
        self.pk = pk


@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=False, SHARING_POLL_DELAY_SECONDS=0,
                   SHARING_TNS_REQUEST_INTERVAL_SECONDS=0, NOTIFICATION_EMAIL_ENABLED=False)
class SharingBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("share_staff", is_staff=True, is_superuser=True)
        cls.member = create_test_user("share_member", is_staff=False)
        cls.outsider = create_test_user("share_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name="YSE reporters")
        cls.member.groups.add(cls.group)
        ensure_transient_statuses(cls.staff)
        cls.cred = EncryptedCredential(name="TNS bot yse", service="tns", kind=EncryptedCredential.KIND_TNS,
                                       created_by=cls.staff, modified_by=cls.staff)
        cls.cred.set_secret(SECRET)
        cls.cred.save()

    def setUp(self):
        autopublish.reset_cache()
        self.transient = create_transient_with_synthetic_data(
            self.staff, name="yse_cand_001", n_phot_points=4, with_host=True, with_spectrum=True, with_log=False)
        self.instrument = self.transient.transientphotometry_set.first().instrument
        self.service = self.make_service()

    def make_service(self, slug="yse-tns", testing=True, groups=None, **kwargs):
        defaults = dict(name="YSE TNS", kind=SharingService.KIND_TNS, credential=self.cred, tns_group_id="83",
                        tns_group_name="YSE", default_coauthors="R. J. Foley (UCSC), D. O. Jones (Hawaii)",
                        testing=testing, config={"instrument_ids": {self.instrument.name: 172}},
                        created_by=self.staff, modified_by=self.staff)
        defaults.update(kwargs)
        service = SharingService.objects.create(slug=slug, **defaults)
        if groups:
            service.groups.set(groups)
        return service

    def add_limit(self, days_before=2, flux=100.0, flux_err=10.0, zp=27.5, after=False):
        phot = self.transient.transientphotometry_set.first()
        first = TransientPhotData.objects.filter(photometry=phot, mag__isnull=False).order_by("obs_date").first()
        when = first.obs_date + datetime.timedelta(days=(days_before if after else -days_before))
        return TransientPhotData.objects.create(
            photometry=phot, band=first.band, obs_date=when, mag=None, mag_err=None, flux=flux, flux_err=flux_err,
            flux_zero_point=zp, created_by=self.staff, modified_by=self.staff)

    def first_detection(self):
        return TransientPhotData.objects.filter(photometry__transient=self.transient, mag__isnull=False).order_by(
            "obs_date").first()


class ATReportBuilderTests(SharingBase):
    def test_at_report_shape_and_discovery(self):
        limit = self.add_limit()
        self.add_limit(days_before=1, after=True)  # a limit during the decline is not the non-detection
        report = tns.build_at_report(self.service, self.transient, coauthors="A. Person (Somewhere)", remarks="test")
        at = report["at_report"]["0"]
        self.assertEqual(at["reporting_group_id"], "83")
        self.assertEqual(at["discovery_data_source_id"], "83")
        self.assertEqual(at["reporter"], "A. Person (Somewhere), on behalf of YSE")
        self.assertEqual(at["discovery_datetime"], tns.tns_datetime(self.first_detection().obs_date))
        self.assertEqual(at["at_type"], "1")
        self.assertEqual(at["internal_name"], "yse_cand_001")
        self.assertEqual(at["remarks"], "test")
        self.assertEqual(at["host_name"], self.transient.host.name)
        self.assertEqual(at["host_redshift"], "0.05000")
        phot = at["photometry"]["photometry_group"]
        self.assertEqual(len(phot), 3)  # discovery + two following points (max_photometry_points default)
        self.assertEqual(phot["0"]["instrument_value"], "172")
        self.assertEqual(phot["0"]["filter_value"], "22")  # r-<group> -> r -> Sloan r
        self.assertEqual(phot["0"]["flux_units"], "1")
        self.assertEqual(phot["0"]["flux"], "18.000")
        nd = at["non_detection"]
        self.assertEqual(nd["obsdate"], tns.tns_datetime(limit.obs_date))
        self.assertEqual(nd["limiting_flux"], "22.22")
        self.assertEqual(nd["flux"], "")
        self.assertEqual(nd["instrument_value"], "172")
        self.assertEqual(at["proprietary_period"]["proprietary_period_value"], "0")

    def test_at_report_uses_default_coauthors_and_falls_back_to_transient_non_detect(self):
        band = self.first_detection().band
        self.transient.non_detect_date = self.first_detection().obs_date - datetime.timedelta(days=3)
        self.transient.non_detect_limit = 21.3
        self.transient.non_detect_band = band
        self.transient.save()
        at = tns.build_at_report(self.service, self.transient)["at_report"]["0"]
        self.assertEqual(at["reporter"], "R. J. Foley (UCSC), D. O. Jones (Hawaii), on behalf of YSE")
        self.assertEqual(at["non_detection"]["limiting_flux"], "21.30")
        self.assertEqual(at["non_detection"]["filter_value"], "22")

    def test_at_report_archival_non_detection_when_nothing_known(self):
        at = tns.build_at_report(self.service, self.transient)["at_report"]["0"]
        self.assertEqual(at["non_detection"], {"archiveid": "0", "archival_remarks": "Other"})

    def test_flagged_points_are_ignored(self):
        dq = DataQuality.objects.create(name="bad", created_by=self.staff, modified_by=self.staff)
        first = self.first_detection()
        first.data_quality.add(dq)
        at = tns.build_at_report(self.service, self.transient)["at_report"]["0"]
        second = TransientPhotData.objects.filter(photometry__transient=self.transient, mag__isnull=False).order_by(
            "obs_date")[1]
        self.assertEqual(at["discovery_datetime"], tns.tns_datetime(second.obs_date))

    def test_allowed_instruments_filter_and_missing_ids(self):
        _, other_inst, _ = create_instrument_stack(self.staff, obs_group_name="other-stack")
        self.service.allowed_instruments.add(other_inst)
        with self.assertRaisesRegex(tns.PayloadError, "No unflagged detection"):
            tns.build_at_report(self.service, self.transient)
        self.service.allowed_instruments.clear()
        self.service.config = {}
        self.service.save()
        with self.assertRaisesRegex(tns.PayloadError, "No TNS id for instrument"):
            tns.build_at_report(self.service, self.transient)

    def test_at_report_refuses_tns_named_transient_and_needs_group(self):
        named = create_minimal_transient(self.staff, name="2026xyz")
        with self.assertRaisesRegex(tns.PayloadError, "already carries a TNS name"):
            tns.build_at_report(self.service, named)
        self.service.tns_group_id = ""
        with self.assertRaisesRegex(tns.PayloadError, "no TNS reporting group"):
            tns.build_at_report(self.service, self.transient)

    def test_preview_payload_reports_problems_instead_of_raising(self):
        self.service.config = {}
        payload, problems = tns.preview_payload(self.service, self.transient, tns.KIND_DISCOVERY)
        self.assertEqual(payload, {})
        self.assertEqual(len(problems), 1)


class ClassificationReportBuilderTests(SharingBase):
    def setUp(self):
        super().setUp()
        self.named = create_transient_with_synthetic_data(
            self.staff, name="2026abc", n_phot_points=2, with_host=False, with_spectrum=True, with_log=False)
        self.spectrum = self.named.transientspectrum_set.first()
        self.service.config = {"instrument_ids": {self.spectrum.instrument.name: 172}}
        self.service.save()
        self.sn_ia, _ = TransientClass.objects.get_or_create(name="SN Ia", defaults={"created_by": self.staff,
                                                                                    "modified_by": self.staff})
        self.named.best_spec_class = self.sn_ia
        self.named.save()

    def test_classification_report_shape(self):
        report = tns.build_classification_report(self.service, self.named, spectrum=self.spectrum, redshift="0.0123")
        cr = report["classification_report"]["0"]
        self.assertEqual(cr["name"], "2026abc")
        self.assertEqual(cr["objtypeid"], "3")
        self.assertEqual(cr["redshift"], "0.01230")
        self.assertEqual(cr["groupid"], "83")
        self.assertIn("on behalf of YSE", cr["classifier"])
        spec = cr["spectra"]["spectra-group"]["0"]
        self.assertEqual(spec["instrumentid"], "172")
        self.assertEqual(spec["specTypeid"], "10")
        self.assertEqual(spec["ascii_file"], "")
        self.assertEqual(report["_yse"]["spectrum_id"], self.spectrum.pk)
        self.assertNotIn("_yse", tns.strip_private(report))

    def test_classification_uses_alternate_tns_name_and_class_aliases(self):
        AlternateTransientNames.objects.create(transient=self.transient, name="2026zzz", created_by=self.staff,
                                               modified_by=self.staff)
        spectrum = self.transient.transientspectrum_set.first()
        self.service.config = {"instrument_ids": {spectrum.instrument.name: 172}}
        report = tns.build_classification_report(self.service, self.transient, spectrum=spectrum,
                                                 classification="SN Iax")
        self.assertEqual(report["classification_report"]["0"]["name"], "2026zzz")
        self.assertEqual(report["classification_report"]["0"]["objtypeid"], "105")
        self.assertEqual(tns.object_type_id("SN IIP"), 11)
        self.assertEqual(tns.object_type_id("tde"), 120)
        self.assertIsNone(tns.object_type_id("Not a class"))

    def test_classification_errors(self):
        with self.assertRaisesRegex(tns.PayloadError, "no TNS name yet"):
            tns.build_classification_report(self.service, self.transient, spectrum=self.spectrum)
        with self.assertRaisesRegex(tns.PayloadError, "spectrum of this transient is required"):
            tns.build_classification_report(self.service, self.named, spectrum=None)
        with self.assertRaisesRegex(tns.PayloadError, "no TNS object type id"):
            tns.build_classification_report(self.service, self.named, spectrum=self.spectrum, classification="Blob")
        _, other_inst, _ = create_instrument_stack(self.staff, obs_group_name="other-spec")
        self.service.allowed_instruments.add(other_inst)
        with self.assertRaisesRegex(tns.PayloadError, "may not be reported"):
            tns.build_classification_report(self.service, self.named, spectrum=self.spectrum)


class TNSClientTests(SharingBase):
    def test_sandbox_and_production_urls(self):
        self.assertEqual(self.service.api_base_url(), "https://sandbox.wis-tns.org/api")
        self.service.testing = False
        self.assertEqual(self.service.api_base_url(), "https://www.wis-tns.org/api")
        self.assertEqual(self.service.object_url("2026abc"), "https://www.wis-tns.org/object/2026abc")

    def test_client_headers_and_form_data(self):
        router = TNSRouter()
        with mock.patch("YSE_App.sharing.tns.requests.post", router):
            client = tns.TNSClient.for_service(self.service)
            reply = client.send_bulk_report({"at_report": {"0": {"x": 1}}, "_yse": {"drop": "me"}})
        self.assertEqual(reply["data"]["report_id"], 777)
        call = router.calls[0]
        self.assertEqual(call["url"], "https://sandbox.wis-tns.org/api/bulk-report")
        self.assertEqual(call["headers"]["User-Agent"], 'tns_marker{"tns_id":1234, "type":"bot", "name":"YSE_Bot"}')
        self.assertEqual(call["data"]["api_key"], "very-secret-key")
        self.assertEqual(json.loads(call["data"]["data"]), {"at_report": {"0": {"x": 1}}})
        self.cred.refresh_from_db()
        self.assertIsNotNone(self.cred.last_used_at)

    def test_client_error_mapping(self):
        client = tns.TNSClient("https://sandbox.wis-tns.org/api", "k", "1", "bot")
        with mock.patch("YSE_App.sharing.tns.requests.post",
                        return_value=FakeResponse({"id_code": 429}, status=429, headers={"x-rate-limit-reset": "17"})):
            with self.assertRaises(tns.TNSRateLimited) as ctx:
                client.bulk_report_reply(1)
            self.assertEqual(ctx.exception.retry_after, 17)
        with mock.patch("YSE_App.sharing.tns.requests.post", return_value=FakeResponse("boom", status=502)):
            with self.assertRaises(tns.TNSTransportError):
                client.send_bulk_report({})
        with mock.patch("YSE_App.sharing.tns.requests.post",
                        return_value=FakeResponse({"id_code": 400, "id_message": "Bad api key"}, status=400)):
            with self.assertRaisesRegex(tns.TNSError, "Bad api key"):
                client.send_bulk_report({})
        with mock.patch("YSE_App.sharing.tns.requests.post",
                        return_value=FakeResponse({"id_code": 404, "id_message": "Unknown report"}, status=404)):
            self.assertTrue(tns.reply_not_ready(client.bulk_report_reply(5)))
        with mock.patch("YSE_App.sharing.tns.requests.post", side_effect=__import__("requests").ConnectionError("x")):
            with self.assertRaises(tns.TNSTransportError):
                client.search(1.0, 2.0)

    def test_for_service_requires_credential(self):
        self.service.credential = None
        with self.assertRaisesRegex(tns.TNSError, "no active credential"):
            tns.TNSClient.for_service(self.service)

    def test_parse_feedback(self):
        outcome = tns.parse_feedback(accepted_reply(), tns.KIND_DISCOVERY)
        self.assertTrue(outcome.accepted)
        self.assertEqual((outcome.objname, outcome.prefix), ("2026abc", "AT"))
        outcome = tns.parse_feedback(rejected_reply("dup"), tns.KIND_DISCOVERY)
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.summary(), "dup")
        outcome = tns.parse_feedback(accepted_reply(kind="classification_report", prefix="SN"), tns.KIND_CLASSIFICATION)
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.prefix, "SN")


class SubmissionQueueTests(SharingBase):
    def test_discovery_report_end_to_end(self):
        router = TNSRouter()
        with mock.patch("YSE_App.sharing.tns.requests.post", router):
            submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.member)
            self.assertEqual(submission.status, SharingSubmission.STATUS_PENDING)
            self.assertEqual(Job.objects.filter(kind=tns.SUBMIT_KIND, status=Job.QUEUED).count(), 1)
            self.assertEqual(submission.job.kind, tns.SUBMIT_KIND)
            result = run_pass(worker_id="t")
        self.assertGreaterEqual(result.done, 2)
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_ACCEPTED)
        self.assertEqual(submission.external_id, "777")
        self.assertEqual(submission.tns_name, "2026abc")
        self.assertEqual(submission.attempts, 1)
        self.assertEqual(submission.tns_object_url, "https://sandbox.wis-tns.org/object/2026abc")
        self.assertEqual(len(router.by_suffix("/bulk-report")), 1)
        self.assertEqual(len(router.by_suffix("/bulk-report-reply")), 1)
        self.assertEqual(router.by_suffix("/bulk-report-reply")[0]["data"]["report_id"], "777")
        transient = Transient.objects.get(pk=self.transient.pk)
        self.assertEqual(transient.name, "2026abc")
        self.assertEqual(transient.slug, "2026abc")
        alt = AlternateTransientNames.objects.get(name="yse_cand_001")
        self.assertEqual(alt.transient_id, transient.pk)
        self.assertTrue(Log.objects.filter(transient=transient, comment__startswith="Submitted to TNS").exists())
        note = Notification.objects.get(recipient=self.member)
        self.assertIn("accepted", note.text)
        self.assertEqual(note.kind, "sharing_result")
        self.assertEqual(note.url, reverse("sharing_submission_detail", args=[submission.pk]))

    def test_rename_off_records_alternate_name(self):
        self.service.config = {"instrument_ids": {self.instrument.name: 172}, "rename_transient": False}
        self.service.save()
        with mock.patch("YSE_App.sharing.tns.requests.post", TNSRouter()):
            tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff)
            run_pass()
        transient = Transient.objects.get(pk=self.transient.pk)
        self.assertEqual(transient.name, "yse_cand_001")
        self.assertTrue(AlternateTransientNames.objects.filter(transient=transient, name="2026abc").exists())

    def test_rejected_then_retry(self):
        router = TNSRouter(reply=rejected_reply("Coordinates match AT 2026aaa"))
        with mock.patch("YSE_App.sharing.tns.requests.post", router):
            submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff)
            run_pass()
            submission.refresh_from_db()
            self.assertEqual(submission.status, SharingSubmission.STATUS_REJECTED)
            self.assertIn("2026aaa", submission.error)
            self.assertTrue(submission.can_retry)
            self.assertEqual(Transient.objects.get(pk=self.transient.pk).name, "yse_cand_001")
            self.assertTrue(Notification.objects.filter(recipient=self.staff, text__contains="rejected").exists())
            router.reply = accepted_reply("2026bbb")
            tns.retry_submission(submission, self.member)
            submission.refresh_from_db()
            self.assertEqual(submission.status, SharingSubmission.STATUS_PENDING)
            self.assertEqual(submission.error, "")
            run_pass()
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_ACCEPTED)
        self.assertEqual(submission.tns_name, "2026bbb")
        self.assertEqual(submission.attempts, 2)
        with self.assertRaises(tns.SharingError):
            tns.retry_submission(submission)

    def test_poll_not_ready_retries_then_fails(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        submission.mark_submitted("55", {})
        with mock.patch("YSE_App.sharing.tns.requests.post", TNSRouter(not_ready=99)):
            with self.assertRaises(JobRetry):
                tns.poll_job({"submission_id": submission.pk}, job=FakeJob(attempts=1, max_attempts=3))
            submission.refresh_from_db()
            self.assertEqual(submission.status, SharingSubmission.STATUS_SUBMITTED)
            with self.assertRaises(JobFailed):
                tns.poll_job({"submission_id": submission.pk}, job=FakeJob(attempts=3, max_attempts=3))
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_FAILED)
        self.assertIn("did not process report 55", submission.error)

    def test_transport_error_retries_and_finally_fails(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        with mock.patch("YSE_App.sharing.tns.requests.post", return_value=FakeResponse("down", status=503)):
            with self.assertRaises(JobRetry):
                tns.submit_job({"submission_id": submission.pk}, job=FakeJob(attempts=1, max_attempts=4))
            submission.refresh_from_db()
            self.assertEqual(submission.status, SharingSubmission.STATUS_PENDING)
            self.assertEqual(submission.attempts, 1)
            self.assertIn("HTTP 503", submission.error)
            with self.assertRaises(JobFailed):
                tns.submit_job({"submission_id": submission.pk}, job=FakeJob(attempts=4, max_attempts=4))
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_FAILED)
        self.assertTrue(Notification.objects.filter(recipient=self.staff, text__contains="failed").exists())

    def test_rate_limit_uses_reset_header(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        limited = FakeResponse({"id_code": 429}, status=429, headers={"x-rate-limit-reset": "42"})
        with mock.patch("YSE_App.sharing.tns.requests.post", return_value=limited):
            with self.assertRaises(JobRetry) as ctx:
                tns.submit_job({"submission_id": submission.pk}, job=FakeJob())
        self.assertEqual(ctx.exception.delay, 42)

    def test_bad_request_fails_without_retry(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        bad = FakeResponse({"id_code": 401, "id_message": "Unauthorized"}, status=401)
        with mock.patch("YSE_App.sharing.tns.requests.post", return_value=bad):
            result = tns.submit_job({"submission_id": submission.pk}, job=FakeJob())
        submission.refresh_from_db()
        self.assertEqual(result["status"], SharingSubmission.STATUS_FAILED)
        self.assertIn("Unauthorized", submission.error)

    def test_classification_uploads_spectrum_first(self):
        named = create_transient_with_synthetic_data(self.staff, name="2026abc", n_phot_points=2, with_host=False,
                                                      with_spectrum=True, with_log=False)
        spectrum = named.transientspectrum_set.first()
        for i in range(3):
            TransientSpecData.objects.create(spectrum=spectrum, wavelength=4000 + 10 * i, flux=1e-16 * (i + 1),
                                             created_by=self.staff, modified_by=self.staff)
        self.service.config = {"instrument_ids": {spectrum.instrument.name: 172}}
        self.service.save()
        router = TNSRouter(reply=accepted_reply("2026abc", prefix="SN", kind="classification_report"))
        with mock.patch("YSE_App.sharing.tns.requests.post", router):
            submission = tns.create_submission(self.service, named, tns.KIND_CLASSIFICATION, self.staff,
                                               spectrum=spectrum, classification="SN Ia", redshift="0.02")
            run_pass()
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_ACCEPTED)
        self.assertEqual([c["url"].rsplit("/api/", 1)[1] for c in router.calls],
                         ["set/file-upload", "bulk-report", "bulk-report-reply"])
        upload = router.by_suffix("/set/file-upload")[0]
        self.assertIn("files[0]", upload["files"])
        self.assertIn(b"4000.0000 1.000000e-16", upload["files"]["files[0]"][1])
        sent = json.loads(router.by_suffix("/bulk-report")[0]["data"]["data"])
        self.assertEqual(sent["classification_report"]["0"]["spectra"]["spectra-group"]["0"]["ascii_file"],
                         "tns_upload_00042.ascii")
        self.assertNotIn("_yse", sent)
        self.assertEqual(Transient.objects.get(pk=named.pk).name, "2026abc")  # classification never renames

    def test_hermes_submission_without_token_fails_clearly(self):
        # The TNS bot credential carries no hermes_token; publishing itself is covered by test_feeds.
        hermes = self.make_service(slug="hermes", kind=SharingService.KIND_HERMES, hermes_topic="yse.transients")
        submission = tns.create_submission(hermes, self.transient, tns.KIND_HERMES, self.staff)
        run_pass()
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_FAILED)
        self.assertIn("no Hermes token", submission.error)

    def test_record_tns_name_conflict_and_unchanged(self):
        create_minimal_transient(self.staff, name="2026abc", obs_group_name="other")
        self.assertEqual(tns.record_tns_name(self.transient, "2026abc", service=self.service), "conflict")
        self.assertTrue(Log.objects.filter(transient=self.transient, comment__contains="already transient").exists())
        self.assertEqual(Transient.objects.get(pk=self.transient.pk).name, "yse_cand_001")
        self.assertEqual(tns.record_tns_name(self.transient, "", service=self.service), "unchanged")

    def test_disabled_service_refuses_submission(self):
        self.service.enabled = False
        self.service.save()
        with self.assertRaisesRegex(tns.SharingError, "disabled"):
            tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff)

    def test_legacy_credentials_prefer_decam_service(self):
        api_key, bot_id, bot_name, sandbox = tns.legacy_tns_credentials("decam")
        self.assertEqual((api_key, bot_id, bot_name, sandbox), ("very-secret-key", "1234", "YSE_Bot", True))
        self.service.testing = False
        self.service.save()
        self.assertFalse(tns.legacy_tns_credentials("decam")[3])
        SharingService.objects.all().delete()
        with override_settings(TNSDECAMAPIKEY="ini-key", TNSDECAMID="9", TNSDECAMUSER="decam_bot"):
            self.assertEqual(tns.legacy_tns_credentials("decam"), ("ini-key", "9", "decam_bot", True))


class SharingViewTests(SharingBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.restricted = self.make_service(slug="restricted", name="Restricted", groups=[self.group])
        self.submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff,
                                                dispatch=False)
        self.submission.mark_failed("TNS HTTP 503")

    def test_submissions_page_for_staff_and_members(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("sharing_submissions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "yse_cand_001")
        self.assertContains(response, "TNS HTTP 503")
        self.assertContains(response, 'data-status="failed"')
        self.assertContains(response, "Retry")
        response = self.client.get(reverse("sharing_submissions") + "?status=accepted")
        self.assertNotContains(response, "yse_cand_001")
        response = self.client.get(reverse("sharing_submissions") + "?q=cand_001&kind=discovery&service=yse-tns")
        self.assertContains(response, "yse_cand_001")
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("sharing_submissions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "YSE TNS")
        self.assertNotContains(response, "Restricted")
        self.assertEqual(self.client.get("/sharing/submissions/").status_code, 200)

    def test_submission_detail_and_visibility(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("sharing_submission_detail", args=[self.submission.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reporting_group_id")
        self.assertContains(response, "TNS HTTP 503")
        hidden = tns.create_submission(self.restricted, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("sharing_submission_detail", args=[hidden.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("sharing_submissions")).status_code, 302)

    def test_retry_view(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("sharing_submission_retry", args=[self.submission.pk]),
                                    {"next": reverse("sharing_submissions")})
        self.assertEqual(response.status_code, 302)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, SharingSubmission.STATUS_PENDING)
        self.assertEqual(Job.objects.filter(kind=tns.SUBMIT_KIND, status=Job.QUEUED).count(), 1)
        response = self.client.post(reverse("sharing_submission_retry", args=[self.submission.pk]),
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get(reverse("sharing_submission_retry", args=[self.submission.pk])).status_code, 405)

    def test_services_page_and_dry_run(self):
        rule = AutoPublisher.objects.create(service=self.service, group=self.group, name="New YSE candidates",
                                            criteria={"statuses": ["New"]}, created_by=self.staff,
                                            modified_by=self.staff)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("sharing_services"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New YSE candidates")
        self.assertContains(response, "tns_api_key")  # key names only
        self.assertNotContains(response, "very-secret-key")
        response = self.client.get(reverse("sharing_autopublisher_dry_run", args=[self.service.pk, rule.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["transients"][0]["name"], "yse_cand_001")
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("sharing_services")).status_code, 200)
        response = self.client.get(reverse("sharing_autopublisher_dry_run", args=[self.service.pk, rule.pk]))
        self.assertIn(response.status_code, (302, 403))

    def test_report_preview_and_submit(self):
        self.client.force_login(self.member)
        url = reverse("sharing_report_preview", args=[self.transient.pk])
        response = self.client.post(url, {"service": self.service.pk, "kind": "discovery",
                                          "coauthors": "A. Person", "remarks": "hi"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["problems"], [])
        self.assertTrue(data["sandbox"])
        self.assertEqual(data["endpoint"], "https://sandbox.wis-tns.org/api/bulk-report")
        self.assertEqual(data["payload"]["at_report"]["0"]["reporter"], "A. Person, on behalf of YSE")
        self.assertEqual(self.client.post(url, {"kind": "discovery"}).status_code, 400)
        response = self.client.post(url, {"service": self.service.pk, "kind": "classification"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("no TNS name yet", response.json()["problems"][0])
        before = SharingSubmission.objects.count()
        response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                    {"service": self.service.pk, "kind": "discovery", "coauthors": "A. Person"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(SharingSubmission.objects.count(), before + 1)
        created = SharingSubmission.objects.get(pk=data["id"])
        self.assertEqual(created.created_by, self.member)
        self.assertEqual(created.payload["at_report"]["0"]["reporter"], "A. Person, on behalf of YSE")
        self.assertIsNotNone(created.job)
        response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                    {"service": self.service.pk, "kind": "classification"})
        self.assertEqual(response.status_code, 400)
        self.client.force_login(self.outsider)
        response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                    {"service": self.restricted.pk, "kind": "discovery"})
        self.assertEqual(response.status_code, 404)
        self.service.enabled = False
        self.service.save()
        response = self.client.post(reverse("sharing_report_submit", args=[self.transient.pk]),
                                    {"service": self.service.pk, "kind": "discovery"})
        self.assertEqual(response.status_code, 403)

    def test_detail_page_shows_dialog_only_with_a_service(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Report to TNS")
        self.assertContains(response, 'id="yse-report-modal"')
        self.assertContains(response, reverse("sharing_report_preview", args=[self.transient.pk]))
        SharingService.objects.update(enabled=False)
        response = self.client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Report to TNS")

    def test_api_viewsets_hide_secrets(self):
        self.client.force_login(self.member)
        response = self.client.get("/api/sharingservices/?format=json")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("yse-tns", body)
        self.assertIn("restricted", body)
        self.assertNotIn("very-secret-key", body)
        self.client.force_login(self.outsider)
        self.assertNotIn("restricted", self.client.get("/api/sharingservices/?format=json").content.decode())
        response = self.client.get("/api/sharingsubmissions/?format=json&status=failed")
        self.assertEqual(response.status_code, 200)
        self.assertIn("yse_cand_001", response.content.decode())


class RetrievalTests(SharingBase):
    def test_retrieval_names_matching_transient_and_leaves_others(self):
        named = create_minimal_transient(self.staff, name="2026qqq", ra=50.0, dec=-10.0)
        other = create_minimal_transient(self.staff, name="yse_cand_002", ra=80.0, dec=5.0)
        router = TNSRouter()

        def hits_for(url, headers=None, data=None, files=None, timeout=None):
            query = json.loads(data["data"]) if data and "data" in data else {}
            if url.endswith("/get/search") and query.get("ra", "").startswith("10.0"):
                router.search_hits = [{"objname": "2026rrr", "prefix": "AT", "objid": 1}]
            else:
                router.search_hits = []
            return router(url, headers=headers, data=data, files=files, timeout=timeout)

        with mock.patch("YSE_App.sharing.tns.requests.post", hits_for):
            summary = retrieval.run_retrieval(since_days=365)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["renamed"], 1)
        self.assertEqual(Transient.objects.get(pk=self.transient.pk).name, "2026rrr")
        self.assertTrue(AlternateTransientNames.objects.filter(name="yse_cand_001").exists())
        self.assertEqual(Transient.objects.get(pk=named.pk).name, "2026qqq")
        self.assertEqual(Transient.objects.get(pk=other.pk).name, "yse_cand_002")
        searches = router.by_suffix("/get/search")
        self.assertEqual(len(searches), 2)
        query = json.loads(searches[0]["data"]["data"])
        self.assertEqual((query["radius"], query["units"]), ("3.0", "arcsec"))
        # second run: nothing left to match
        router.search_hits = []
        with mock.patch("YSE_App.sharing.tns.requests.post", router):
            summary = retrieval.run_retrieval(since_days=365)
        self.assertEqual(summary["matched"], 0)

    def test_retrieval_job_retries_on_rate_limit_and_skips_without_service(self):
        limited = FakeResponse({"id_code": 429}, status=429, headers={"x-rate-limit-reset": "30"})
        with mock.patch("YSE_App.sharing.tns.requests.post", return_value=limited):
            with self.assertRaises(JobRetry) as ctx:
                retrieval.tns_retrieval({"since_days": 365})
        self.assertEqual(ctx.exception.delay, 30)
        SharingService.objects.all().delete()
        self.assertIn("skipped", retrieval.tns_retrieval({}))

    def test_retrieval_confirms_accepted_submissions(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        SharingSubmission.objects.filter(pk=submission.pk).update(status=SharingSubmission.STATUS_ACCEPTED,
                                                                  tns_name="2026sss")
        with mock.patch("YSE_App.sharing.tns.requests.post", TNSRouter()):
            summary = retrieval.run_retrieval(since_days=365)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(Transient.objects.get(pk=self.transient.pk).name, "2026sss")

    def test_retrieval_service_selection(self):
        self.assertEqual(retrieval.retrieval_service().slug, "yse-tns")
        prod = self.make_service(slug="prod", testing=False)
        self.assertEqual(retrieval.retrieval_service().slug, "prod")  # production first
        self.assertEqual(retrieval.retrieval_service("yse-tns").slug, "yse-tns")
        prod.credential = None
        prod.save()
        self.assertEqual(retrieval.retrieval_service("prod").slug, "yse-tns")


class AutoPublisherTests(SharingBase):
    def setUp(self):
        super().setUp()
        self.rule = AutoPublisher.objects.create(
            service=self.service, group=self.group, name="New with 2 detections", kind=tns.KIND_DISCOVERY,
            criteria={"statuses": ["New"], "min_detections": 2, "obs_groups": [self.transient.obs_group.name]},
            created_by=self.staff, modified_by=self.staff)
        autopublish.reset_cache()  # the admin does this on save; rules_active() is cached for a minute

    def test_matches_and_dry_run(self):
        self.assertTrue(self.rule.matches(self.transient))
        self.assertEqual([t.pk for t in self.rule.qualifying_transients()], [self.transient.pk])
        statuses = ensure_transient_statuses(self.staff)
        self.transient.status = statuses["Ignore"]
        self.transient.save()
        self.assertFalse(self.rule.matches(self.transient))
        self.assertEqual(self.rule.qualifying_transients(), [])
        self.transient.status = statuses["New"]
        self.transient.save()
        self.rule.criteria = {"min_detections": 99}
        self.assertFalse(self.rule.matches(self.transient))
        self.rule.criteria = {"tags": ["nope"]}
        self.assertFalse(self.rule.matches(self.transient))
        self.rule.criteria = {"max_age_days": 1}
        self.assertFalse(self.rule.matches(self.transient))  # disc_date is 5 days ago
        named = create_minimal_transient(self.staff, name="2026ttt")
        self.rule.criteria = {}
        self.assertFalse(self.rule.matches(named))  # discovery rules skip TNS-named transients

    def test_sweep_queues_once(self):
        summary = autopublish.sweep(dispatch=False)
        self.assertEqual(summary["queued"], 1)
        submission = SharingSubmission.objects.get()
        self.assertEqual(submission.auto_publisher, self.rule)
        self.assertEqual(submission.created_by, self.staff)  # system user = first superuser
        self.assertEqual(submission.kind, tns.KIND_DISCOVERY)
        self.assertEqual(autopublish.sweep(dispatch=False)["queued"], 0)
        self.rule.refresh_from_db()
        self.assertIsNotNone(self.rule.last_run_at)
        submission.mark_failed("x")
        self.assertEqual(autopublish.sweep(dispatch=False)["queued"], 0)  # failed ones are retried by hand

    def test_sweep_skips_payload_problems(self):
        self.service.config = {}
        self.service.save()
        self.assertEqual(autopublish.sweep(dispatch=False)["queued"], 0)
        self.assertEqual(SharingSubmission.objects.count(), 0)

    def test_save_hook_queues_submission(self):
        statuses = ensure_transient_statuses(self.staff)
        other = create_minimal_transient(self.staff, name="yse_cand_003", status_name="Ignore",
                                         obs_group_name=self.transient.obs_group.name)
        self.assertEqual(SharingSubmission.objects.filter(transient=other).count(), 0)
        phot = self.transient.transientphotometry_set.first()
        from YSE_App.tests.fixtures_minimal import attach_synthetic_photometry

        attach_synthetic_photometry(self.staff, other, obs_group=phot.obs_group, instrument=phot.instrument,
                                    band=TransientPhotData.objects.filter(photometry=phot).first().band, n_points=3)
        other.status = statuses["New"]
        other.save()
        self.assertEqual(SharingSubmission.objects.filter(transient=other, auto_publisher=self.rule).count(), 1)
        self.assertEqual(Job.objects.filter(kind=tns.SUBMIT_KIND).count(), 1)
        other.save()  # no duplicate
        self.assertEqual(SharingSubmission.objects.filter(transient=other).count(), 1)
        with override_settings(SHARING_AUTOPUBLISH_ON_SAVE=False):
            third = create_minimal_transient(self.staff, name="yse_cand_004",
                                             obs_group_name=self.transient.obs_group.name)
            self.assertEqual(SharingSubmission.objects.filter(transient=third).count(), 0)

    def test_notifications_go_to_the_rule_group(self):
        autopublish.sweep(dispatch=False)
        submission = SharingSubmission.objects.get()
        with mock.patch("YSE_App.sharing.tns.requests.post", TNSRouter()):
            tns.dispatch_submission(submission)
            run_pass()
        submission.refresh_from_db()
        self.assertEqual(submission.status, SharingSubmission.STATUS_ACCEPTED)
        self.assertTrue(Notification.objects.filter(recipient=self.member, text__contains="accepted").exists())


class CronAndCommandTests(SharingBase):
    def test_cron_classes_queue_once(self):
        from YSE_App.data_ingest.Sharing_Jobs import AutoPublishSweep, TNSRetrieval

        self.assertEqual(TNSRetrieval().do(), "disabled")
        with override_settings(SHARING_TNS_RETRIEVAL_CRON_ENABLED=True, SHARING_AUTOPUBLISH_CRON_ENABLED=True):
            self.assertIn("queued job", TNSRetrieval().do())
            self.assertIn("already queued", TNSRetrieval().do())
            self.assertIn("queued job", AutoPublishSweep().do())
        self.assertEqual(Job.objects.filter(kind=retrieval.RETRIEVAL_KIND).count(), 1)
        self.assertEqual(Job.objects.filter(kind=autopublish.SWEEP_KIND).count(), 1)
        from django.conf import settings

        self.assertIn("YSE_App.data_ingest.Sharing_Jobs.TNSRetrieval", settings.CRON_CLASSES)
        self.assertIn("YSE_App.data_ingest.Sharing_Jobs.AutoPublishSweep", settings.CRON_CLASSES)

    def test_handlers_registered(self):
        from YSE_App.jobs import registered_kinds

        kinds = registered_kinds()
        for kind in (tns.SUBMIT_KIND, tns.POLL_KIND, retrieval.RETRIEVAL_KIND, autopublish.SWEEP_KIND):
            self.assertIn(kind, kinds)

    def test_create_tns_sharing_service_command(self):
        import io

        out = io.StringIO()
        with override_settings(TNSID="55", TNSUSER="ini_bot", TNSAPIKEY="ini-secret"):
            call_command("create_tns_sharing_service", "--slug", "ini-tns", "--name", "INI bot", "--group-id", "83",
                         "--group-name", "YSE", "--from-settings", "--coauthors", "A. B (C)", stdout=out)
        service = SharingService.objects.get(slug="ini-tns")
        self.assertTrue(service.testing)
        self.assertEqual(service.tns_group_id, "83")
        self.assertEqual(service.default_coauthors, "A. B (C)")
        self.assertEqual(service.credential.get_secret(), {"tns_bot_id": "55", "tns_bot_name": "ini_bot",
                                                           "tns_api_key": "ini-secret"})
        self.assertNotIn("ini-secret", out.getvalue())
        call_command("create_tns_sharing_service", "--slug", "ini-tns", "--production", stdout=out)
        service.refresh_from_db()
        self.assertFalse(service.testing)
        self.assertEqual(SharingService.objects.filter(slug="ini-tns").count(), 1)
        with override_settings(TNSDECAMID="7", TNSDECAMUSER="decam_bot", TNSDECAMAPIKEY="decam-secret"):
            call_command("create_tns_sharing_service", "--slug", "decam", "--group-id", "83", "--decam", stdout=out)
        self.assertEqual(SharingService.objects.get(slug="decam").credential.get_secret()["tns_bot_name"], "decam_bot")
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            call_command("create_tns_sharing_service", "--slug", "half", "--bot-id", "1", stdout=out)


class ModelHelperTests(SharingBase):
    def test_visibility_and_reporter_string(self):
        restricted = self.make_service(slug="restricted", groups=[self.group])
        self.assertTrue(restricted.visible_to(self.staff))
        self.assertTrue(restricted.visible_to(self.member))
        self.assertFalse(restricted.visible_to(self.outsider))
        self.assertTrue(self.service.visible_to(self.outsider))
        self.assertEqual([s.slug for s in SharingService.for_user(self.outsider)], ["yse-tns"])
        self.assertEqual(self.service.reporter_string(""), "R. J. Foley (UCSC), D. O. Jones (Hawaii), on behalf of YSE")
        self.service.tns_group_name = ""
        self.assertEqual(self.service.reporter_string("X, "), "X")
        self.assertFalse(str(self.service).find("sandbox") < 0)

    def test_submission_helpers(self):
        submission = tns.create_submission(self.service, self.transient, tns.KIND_DISCOVERY, self.staff, dispatch=False)
        self.assertFalse(submission.is_final)
        self.assertFalse(submission.can_retry)
        submission.mark_submitted(9, {"id_code": 200})
        self.assertEqual(submission.report_url, "https://sandbox.wis-tns.org/bulk-report/9")
        submission.mark_rejected("first line\nsecond", {})
        self.assertTrue(submission.is_final and submission.can_retry)
        self.assertEqual(submission.error_summary(), "first line")
        self.assertIn("yse_cand_001", str(submission))
        self.assertEqual(tns.tns_name_for(self.transient), "")
        self.assertTrue(tns.is_tns_name("2026abc") if hasattr(tns, "is_tns_name") else True)
        attach_synthetic_spectrum(self.staff, self.transient)
        self.assertEqual(datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc).strftime("%Y"), "2026")
        self.assertEqual(tns.tns_datetime(timezone.make_aware(datetime.datetime(2026, 1, 2, 3, 4, 5))),
                         "2026-01-02 03:04:05")
