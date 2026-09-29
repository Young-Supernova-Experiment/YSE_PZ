"""Facility queue, adapters and accounting (#298: #300, #301, #302; #303: #304)."""

import datetime
import io
import json
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.data_ingest.Facility_Queue import FacilityPoll
from YSE_App.facilities import FacilityError, FacilityUnreachable, FacilityValidationError, get_facility, registered_slugs
from YSE_App.facilities import atlas as atlas_mod
from YSE_App.facilities import lt as lt_mod
from YSE_App.facilities import mmt as mmt_mod
from YSE_App.facilities import swift as swift_mod
from YSE_App.facilities import ztf as ztf_mod
from YSE_App.facilities.lco import REQUESTGROUPS
from YSE_App.jobs import run_pass
from YSE_App.models import (
    Allocation,
    EncryptedCredential,
    ExternalServiceRun,
    FacilityRequest,
    FollowupStatus,
    Instrument,
    Job,
    Notification,
    QueuedResource,
    Telescope,
    ToOResource,
    TransientFollowup,
    TransientPhotData,
)
from YSE_App.services import allocations as alloc_svc
from YSE_App.services import facility_requests as fr
from YSE_App.tests.fixtures_minimal import audit_fields, create_instrument_stack, create_minimal_transient, create_test_user

KEY = Fernet.generate_key().decode()


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.ok = status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _semester():
    now = timezone.now()
    return now - datetime.timedelta(days=10), now + datetime.timedelta(days=100)


def _credential(user, secret, name="key", service="generic"):
    cred = EncryptedCredential(name=name, service=service, kind=EncryptedCredential.KIND_FACILITY,
                               created_by=user, modified_by=user)
    cred.set_secret(secret)
    cred.save()
    return cred


def _allocation(user, telescope, *, name="Test allocation", facility="generic", credential=None, hours=10.0,
                params=None, groups=(), endpoint="", proposal="YSE2026B", instrument=None, **extra):
    start, end = _semester()
    allocation = Allocation.objects.create(
        name=name, telescope=telescope, instrument=instrument, facility=facility, credential=credential,
        hours_allocated=hours, start_date=start, end_date=end, proposal_id=proposal, endpoint_url=endpoint,
        default_request_params=params or {}, **audit_fields(user), **extra,
    )
    if groups:
        allocation.groups.set(groups)
    return allocation


def _rewind_jobs():
    Job.objects.filter(status=Job.QUEUED).update(run_after=timezone.now() - datetime.timedelta(seconds=1))


class _Base(TestCase):
    def setUp(self):
        self.staff = create_test_user("fq_staff", is_staff=True, is_superuser=True)
        self.user = create_test_user("fq_user", is_staff=False, is_superuser=False)
        self.group = Group.objects.create(name="fq-observers")
        self.user.groups.add(self.group)
        _obs_group, self.instrument, _band = create_instrument_stack(self.staff, obs_group_name="fq-group")
        self.telescope = self.instrument.telescope
        self.transient = create_minimal_transient(self.staff, name="2026fq", obs_group_name="fq-group", ra=35.2, dec=12.4)
        for name in ("Requested", "InProcess", "Successful", "Failed"):
            setattr(self, name.lower(), FollowupStatus.objects.get_or_create(name=name, defaults=audit_fields(self.staff))[0])

    def _request(self, allocation, params=None, user=None, **extra):
        facility = get_facility(allocation.facility)
        cleaned = facility.validate(params or {}, allocation)
        return FacilityRequest.objects.create(
            allocation=allocation, transient=self.transient, submitted_by=user or self.user, payload=cleaned,
            kind=FacilityRequest.KIND_PHOTOMETRY if facility.is_photometry else FacilityRequest.KIND_OBSERVATION,
            **audit_fields(user or self.user), **extra)


# --- registry ------------------------------------------------------------------------

class RegistryTests(TestCase):
    def test_all_adapters_registered_with_capabilities(self):
        slugs = registered_slugs()
        for slug in ("generic", "lco", "soar", "ztf", "atlas", "swift", "gemini", "mmt", "lt"):
            self.assertIn(slug, slugs)
        self.assertEqual(get_facility("ztf").kind, "photometry")
        self.assertEqual(get_facility("atlas").describe()["capabilities"], ["results", "status", "submit"])
        self.assertEqual(get_facility("mmt").describe()["capabilities"], ["delete", "submit", "update"])
        self.assertEqual(get_facility("lt").describe()["capabilities"], ["delete", "submit"])
        self.assertEqual(get_facility("swift").describe()["capabilities"], ["submit"])
        self.assertEqual(get_facility("gemini").describe()["capabilities"], ["submit"])
        self.assertEqual(get_facility("lco").describe()["capabilities"], ["delete", "status", "submit", "update"])
        self.assertEqual(get_facility("ztf").describe()["poll_interval_minutes"], 10)
        self.assertTrue(get_facility("generic").describe()["manual_status"])


# --- queue: retries and modifications --------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class QueueRetryTests(_Base):
    def _api_allocation(self, **kw):
        return _allocation(self.staff, self.telescope, name="api", endpoint="https://obs.example.org/req",
                           params={"exposure_time": 60}, groups=[self.group], **kw)

    def test_unreachable_facility_is_retried_then_submitted(self):
        import requests as _requests

        a = self._api_allocation()
        req = fr.submit_request(a, self.transient, self.user, {})
        responses = [_requests.ConnectionError("down"), _FakeResponse(201, {"id": 12})]
        with override_settings(FACILITY_SUBMIT_BACKOFF_SECONDS=1), mock.patch("YSE_App.facilities.generic.requests.post", side_effect=responses):
            run_pass(worker_id="test")
            req.refresh_from_db()
            self.assertEqual(req.state, FacilityRequest.STATE_QUEUED)
            self.assertEqual(req.attempts, 1)
            self.assertEqual(req.log[-1]["event"], "retry")
            job = Job.objects.get(kind="external_service.run")
            self.assertEqual(job.status, Job.QUEUED)
            self.assertEqual(job.attempts, 1)
            _rewind_jobs()
            run_pass(worker_id="test")
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_SUBMITTED)
        self.assertEqual(req.attempts, 2)
        self.assertEqual(req.external_id, "12")
        job.refresh_from_db()
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(req.run.status, ExternalServiceRun.STATUS_SUCCEEDED)

    def test_unreachable_facility_fails_after_max_attempts(self):
        import requests as _requests

        a = self._api_allocation()
        req = fr.submit_request(a, self.transient, self.user, {})
        with override_settings(FACILITY_SUBMIT_MAX_ATTEMPTS=2, FACILITY_SUBMIT_BACKOFF_SECONDS=1), \
                mock.patch("YSE_App.facilities.generic.requests.post", side_effect=_requests.ConnectionError("down")):
            run_pass(worker_id="test")
            _rewind_jobs()
            run_pass(worker_id="test")
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_FAILED)
        self.assertIn("unreachable after 2 attempts", req.state_detail)
        self.assertEqual(req.attempts, 2)
        self.assertEqual(Job.objects.get(kind="external_service.run").status, Job.DONE)
        self.assertEqual(req.run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertTrue(Notification.objects.filter(recipient=self.user, kind="followup_status").exists())

    def test_rejection_is_not_retried(self):
        a = self._api_allocation()
        req = fr.submit_request(a, self.transient, self.user, {})
        with mock.patch("YSE_App.facilities.generic.requests.post", return_value=_FakeResponse(400, text="bad")):
            run_pass(worker_id="test")
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_FAILED)
        self.assertEqual(req.attempts, 1)
        self.assertEqual(Job.objects.get(kind="external_service.run").status, Job.DONE)

    def test_update_request_generic_and_permissions(self):
        a = self._api_allocation()
        with override_settings(JOB_RUNNER_INLINE=True):
            with mock.patch("YSE_App.facilities.generic.requests.post", return_value=_FakeResponse(201, {"id": 5})):
                req = fr.submit_request(a, self.transient, self.user, {"exposure_time": 60})
            self.assertEqual(req.state, FacilityRequest.STATE_SUBMITTED)
            with mock.patch("YSE_App.facilities.generic.requests.post", return_value=_FakeResponse(200, {"ok": 1})) as post:
                fr.update_request(req, self.user, {"exposure_time": 90, "comment": "longer"})
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_SUBMITTED)
        self.assertEqual(req.payload["exposure_time"], 90.0)
        self.assertEqual(req.payload["comment"], "longer")
        self.assertEqual(req.external_id, "5")
        self.assertIn("update sent", req.state_detail)
        body = post.call_args[1]["json"]
        self.assertEqual(body["action"], "update")
        self.assertEqual(body["external_id"], "5")
        self.assertEqual([e["event"] for e in req.log], ["created", "queued", "submitted", "modified", "queued", "submitted"])
        self.assertEqual(req.run.request_payload["action"], "update")
        self.assertEqual(ExternalServiceRun.objects.filter(service=a.service).count(), 2)
        # a queued request cannot be modified; a final one neither
        queued = fr.submit_request(a, self.transient, self.user, {}, dispatch=False)
        with self.assertRaises(fr.FacilityRequestError):
            fr.update_request(queued, self.user, {"exposure_time": 10})
        fr.cancel_request(queued, self.user)
        with self.assertRaises(fr.FacilityRequestError):
            fr.update_request(queued, self.user, {"exposure_time": 10})
        with self.assertRaises(FacilityValidationError):
            fr.update_request(req, self.user, {"exposure_time": "lots"})

    def test_update_request_lco_replaces_request_group(self):
        cred = _credential(self.staff, {"api_token": "tok"}, service="lco")
        a = _allocation(self.staff, self.telescope, name="LCO", facility="lco", credential=cred, proposal="P1",
                        params={"exposure_time": 60}, groups=[self.group])
        with override_settings(JOB_RUNNER_INLINE=True):
            with mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(201, {"id": 70, "state": "PENDING"})):
                req = fr.submit_request(a, self.transient, self.user, {})
            self.assertEqual(req.state, FacilityRequest.STATE_ACCEPTED)
            self.assertTrue(Notification.objects.filter(recipient=self.user, kind="followup_status").exists())
            responses = [_FakeResponse(200, {"state": "CANCELED"}), _FakeResponse(201, {"id": 71, "state": "PENDING"})]
            with mock.patch("YSE_App.facilities.lco.requests.post", side_effect=responses) as post:
                fr.update_request(req, self.user, {"exposure_time": 120})
        req.refresh_from_db()
        self.assertEqual(req.external_id, "71")
        self.assertEqual(req.state, FacilityRequest.STATE_ACCEPTED)
        self.assertIn("replaced request group 70", req.state_detail)
        self.assertEqual(post.call_args_list[0][0][0], REQUESTGROUPS + "70/cancel/")
        self.assertEqual(post.call_args_list[1][1]["json"]["requests"][0]["configurations"][0]["instrument_configs"][0]["exposure_time"], 120.0)


# --- poll cron and poll spacing --------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=False)
class PollCronTests(_Base):
    def test_cron_is_off_by_default_and_queues_once(self):
        with override_settings(FACILITY_POLL_CRON_ENABLED=False):
            self.assertEqual(FacilityPoll().do(), "disabled")
        self.assertFalse(Job.objects.filter(kind=fr.POLL_JOB_KIND).exists())
        with override_settings(FACILITY_POLL_CRON_ENABLED=True):
            self.assertIn("queued job", FacilityPoll().do())
            self.assertIn("already queued", FacilityPoll().do())
        self.assertEqual(Job.objects.filter(kind=fr.POLL_JOB_KIND).count(), 1)
        self.assertEqual(FacilityPoll.code, "YSE_App.Facility_Queue.FacilityPoll")
        from django.conf import settings

        self.assertIn("YSE_App.data_ingest.Facility_Queue.FacilityPoll", settings.CRON_CLASSES)

    def test_poll_interval_spaces_polls(self):
        cred = _credential(self.staff, {"email": "me@example.org", "userpass": "pw"}, service="ztf")
        a = _allocation(self.staff, self.telescope, name="ZTF", facility="ztf", credential=cred, hours=0)
        req = self._request(a, {}, state=FacilityRequest.STATE_SUBMITTED, last_polled=timezone.now())
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, [])) as get:
            counts = fr.poll_open_requests()
            self.assertEqual(counts["skipped"], 1)
            self.assertFalse(get.called)
            counts = fr.poll_open_requests(force=True)
            self.assertEqual(counts["polled"], 1)
            self.assertTrue(get.called)
        req.last_polled = timezone.now() - datetime.timedelta(minutes=11)
        req.save()
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, [])):
            self.assertEqual(fr.poll_open_requests()["polled"], 1)


# --- ZTF forced photometry ---------------------------------------------------------------------

ZTF_LC = """# Requested input R.A. = 35.200000 degrees
# Requested input Dec. = 12.400000 degrees
# index, field, ccdid, qid, filter, pid, infobitssci, sciinpseeing, scibckgnd, scisigpix, zpmaginpsci, zpmaginpsciunc, zpmaginpscirms, clrcoeff, clrcoeffunc, ncalmatches, exptime, adpctdif1, adpctdif2, diffmaglim, zpdiff, programid, jd, rfid, forcediffimflux, forcediffimfluxunc, forcediffimsnr, forcediffimchisq, forcediffimfluxap, forcediffimfluxuncap, forcediffimsnrap, aperturecorr, dnearestrefsrc, nearestrefmag, nearestrefmagunc, nearestrefchi, nearestrefsharp, refjdstart, refjdend, procstatus
 0 600 1 1 ZTF_g 1 0 2.1 100.0 10.0 26.2 0.02 0.03 0.1 0.01 100 30.0 0.1 0.1 20.5 26.20 1 2460950.7500 1 1500.0 50.0 30.0 1.1 1490.0 55.0 27.0 1.0 5.0 18.0 0.1 1.0 0.1 2458000 2458100 0
 1 600 1 1 ZTF_r 1 0 2.1 100.0 10.0 26.3 0.02 0.03 0.1 0.01 100 30.0 0.1 0.1 20.5 26.30 1 2460950.7600 1 25.0 40.0 0.6 1.1 24.0 45.0 0.5 1.0 5.0 18.0 0.1 1.0 0.1 2458000 2458100 0
 2 600 1 1 ZTF_i 1 0 2.1 100.0 10.0 26.3 0.02 0.03 0.1 0.01 100 30.0 0.1 0.1 20.5 26.30 1 2460950.7700 1 null null null 1.1 null null null 1.0 5.0 18.0 0.1 1.0 0.1 2458000 2458100 64
"""


@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False)
class ZTFTests(_Base):
    def setUp(self):
        super().setUp()
        self.cred = _credential(self.staff, {"email": "me@example.org", "userpass": "pw"}, service="ztf")
        self.alloc = _allocation(self.staff, self.telescope, name="ZTF FP", facility="ztf", credential=self.cred, hours=0,
                                 groups=[self.group])
        self.ztf = get_facility("ztf")

    def test_parse_lightcurve_and_points(self):
        rows = ztf_mod.parse_lightcurve(ZTF_LC)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["filter"], "ZTF_g")
        points = ztf_mod.points_from_rows(rows)
        self.assertEqual(len(points), 2)  # procstatus 64 dropped
        g, r = points
        self.assertEqual(g["band"], "g")
        self.assertAlmostEqual(g["mag"], -2.5 * 3.1760913 + 26.2, places=4)
        self.assertAlmostEqual(g["flux"], 1500.0 * 10 ** (-0.4 * (26.2 - 27.5)), places=3)
        self.assertEqual(g["flux_zero_point"], 27.5)
        self.assertIsNone(r["mag"])  # SNR < 3
        self.assertTrue(r["forced"])
        self.assertEqual(g["obs_date"].year, 2025)

    def test_submit_poll_and_ingest(self):
        def fake_http(method, url, **kwargs):
            if url == ztf_mod.SUBMIT_URL:
                self.assertEqual(kwargs["auth"], (ztf_mod.HTTP_USER, ztf_mod.HTTP_PASSWORD))
                self.assertEqual(kwargs["params"]["email"], "me@example.org")
                self.assertAlmostEqual(kwargs["params"]["ra"], 35.2)
                return _FakeResponse(200, text="Request submitted successfully")
            raise AssertionError(url)

        with mock.patch("YSE_App.facilities.ztf.http_request", side_effect=fake_http):
            req = fr.submit_request(self.alloc, self.transient, self.user, {"days": 30})
        self.assertEqual(req.state, FacilityRequest.STATE_SUBMITTED)
        self.assertEqual(req.kind, FacilityRequest.KIND_PHOTOMETRY)
        self.assertIsNone(req.followup)  # photometry requests are not follow-ups
        self.assertAlmostEqual(req.payload["jdend"] - req.payload["jdstart"], 30, places=3)
        job = {"reqid": 8811, "ra": round(req.payload.get("ra", 35.2), 6), "dec": 12.4, "jdstart": req.payload["jdstart"],
               "jdend": req.payload["jdend"], "created": "2026-09-29 10:00", "started": "", "ended": "", "exitcode": "", "lightcurve": ""}
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, [dict(job, ra=35.2)])):
            fr.poll_request(req)
        self.assertEqual(req.state, FacilityRequest.STATE_ACCEPTED)
        self.assertEqual(req.external_id, "8811")
        done = dict(job, ra=35.2, started="x", ended="y", exitcode=0, lightcurve="/ztf/fp/lc_8811.txt")

        def fake_results(method, url, **kwargs):
            if url == ztf_mod.STATUS_URL:
                return _FakeResponse(200, [done])
            self.assertEqual(url, ztf_mod.BASE + "/ztf/fp/lc_8811.txt")
            return _FakeResponse(200, text=ZTF_LC)

        with mock.patch("YSE_App.facilities.ztf.http_request", side_effect=fake_results):
            fr.poll_request(req)  # complete -> results job runs inline
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_COMPLETE)
        self.assertIsNotNone(req.results_ingested_at, Job.objects.get(kind=fr.RESULTS_JOB_KIND).error)
        self.assertEqual(req.n_results, 2)
        points = TransientPhotData.objects.filter(photometry__transient=self.transient, photometry__instrument__name="ZTF-Cam")
        self.assertEqual(points.count(), 2)
        self.assertEqual(sorted(p.band.name for p in points), ["g", "r"])
        self.assertTrue(all(p.forced for p in points))
        self.assertEqual(points.get(band__name="g").photometry.obs_group.name, "ZTF")
        self.assertEqual(points.get(band__name="g").photometry.instrument.telescope.name, "P48")
        self.assertEqual(Job.objects.get(kind=fr.RESULTS_JOB_KIND).status, Job.DONE)
        self.assertEqual(req.log[-1]["event"], "results")
        # ingesting again is a no-op through the job and an error on the service
        self.assertEqual(fr.retrieve_results_job({"facility_request_id": req.pk})["skipped"], "already ingested")

    def test_status_matches_by_coordinates_and_errors(self):
        req = self._request(self.alloc, {"jdstart": 2460900.0, "jdend": 2460950.0}, state="submitted")
        jobs = [{"reqid": 1, "ra": 10.0, "dec": 10.0, "jdstart": 2460900.0, "jdend": 2460950.0},
                {"reqid": 2, "ra": 35.2, "dec": 12.4, "jdstart": 2460900.0, "jdend": 2460950.0, "exitcode": 3}]
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, jobs)):
            status = self.ztf.get_status(req)
        self.assertEqual(status.state, "failed")
        self.assertEqual(status.external_id, "2")
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, text="nope")):
            with self.assertRaises(FacilityError):
                self.ztf.get_status(req)
        self.alloc.credential = _credential(self.staff, {"email": "x"}, name="bad", service="ztf")
        with self.assertRaises(FacilityError):
            self.ztf.submit(req)
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, [])):
            with self.assertRaises(FacilityError):
                self.ztf.fetch_results(self._request(self.alloc, {}, state="complete"))

    def test_ztf_button_uses_allocation_and_request_state(self):
        self.user.set_password("pw")
        self.user.save()
        client = Client()
        client.login(username="fq_user", password="pw")
        with mock.patch("YSE_App.facilities.ztf.http_request", return_value=_FakeResponse(200, text="ok")):
            response = client.get(reverse("ztf_forced_phot", args=[self.transient.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("success", response.json()["msg"])
        self.assertEqual(FacilityRequest.objects.filter(allocation=self.alloc).count(), 1)
        response = client.get(reverse("ztf_forced_phot", args=[self.transient.slug]))
        self.assertIn("already submitted", response.json()["msg"])
        self.assertEqual(FacilityRequest.objects.filter(allocation=self.alloc).count(), 1)


# --- ATLAS forced photometry -----------------------------------------------------------------

ATLAS_RESULT = """###MJD          m      dm    uJy   duJy F err chi/N     RA        Dec        x        y     maj  min   phi  apfit mag5sig Sky   Obs
60950.25   17.500  0.050  3630.8  167.0 o  0  1.20  35.20000  12.40000  1000.0  1000.0 2.5 2.4 10.0 -0.3 19.5 21.0 01a60950o0123c
60950.30   -19.100  0.500    12.0   60.0 c  0  1.10  35.20000  12.40000  1000.0  1000.0 2.5 2.4 10.0 -0.3 19.8 21.0 01a60950c0124c
"""


@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False)
class ATLASTests(_Base):
    def setUp(self):
        super().setUp()
        self.cred = _credential(self.staff, {"api_token": "atlas-tok"}, service="atlas")
        self.alloc = _allocation(self.staff, self.telescope, name="ATLAS FP", facility="atlas", credential=self.cred, hours=0,
                                 groups=[self.group])
        self.atlas = get_facility("atlas")

    def test_parse_and_points(self):
        points = atlas_mod.points_from_rows(atlas_mod.parse_result(ATLAS_RESULT))
        self.assertEqual(len(points), 2)
        o, c = points
        self.assertEqual(o["band"], "o")
        self.assertEqual(o["mag"], 17.5)
        self.assertEqual(o["flux"], 3630.8)
        self.assertEqual(o["flux_zero_point"], 23.9)
        self.assertEqual(c["band"], "c")
        self.assertIsNone(c["mag"])
        self.assertEqual(o["obs_date"].date(), datetime.date(2025, 10, 2))

    def test_submit_status_results_and_queue_full(self):
        task = "https://fallingstar-data.com/forcedphot/queue/1234/"

        def fake_http(method, url, **kwargs):
            if method == "POST" and url == atlas_mod.QUEUE_URL:
                self.assertEqual(kwargs["headers"]["Authorization"], "Token atlas-tok")
                self.assertAlmostEqual(kwargs["data"]["ra"], 35.2)
                self.assertFalse(kwargs["data"]["send_email"])
                return _FakeResponse(201, {"url": task, "id": 1234, "finishtimestamp": None, "starttimestamp": None, "result_url": None})
            raise AssertionError((method, url))

        with mock.patch("YSE_App.facilities.atlas.http_request", side_effect=fake_http):
            req = fr.submit_request(self.alloc, self.transient, self.user, {"days": 100})
        self.assertEqual(req.state, FacilityRequest.STATE_ACCEPTED)
        self.assertEqual(req.external_id, task)
        self.assertTrue(Notification.objects.filter(recipient=self.user).exists())
        with mock.patch("YSE_App.facilities.atlas.http_request",
                        return_value=_FakeResponse(200, {"url": task, "starttimestamp": "2026-09-29T10:00Z", "finishtimestamp": None})):
            fr.poll_request(req)
        self.assertEqual(req.state, FacilityRequest.STATE_RUNNING)
        finished = {"url": task, "starttimestamp": "x", "finishtimestamp": "2026-09-29T10:05Z",
                    "result_url": "https://fallingstar-data.com/forcedphot/static/results/job1234.txt"}

        def fake_done(method, url, **kwargs):
            if url == task:
                return _FakeResponse(200, finished)
            self.assertEqual(url, finished["result_url"])
            return _FakeResponse(200, text=ATLAS_RESULT)

        with mock.patch("YSE_App.facilities.atlas.http_request", side_effect=fake_done):
            fr.poll_request(req)
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_COMPLETE)
        self.assertEqual(req.n_results, 2)
        bands = sorted(TransientPhotData.objects.filter(photometry__transient=self.transient).values_list("band__name", flat=True))
        self.assertEqual(bands, ["c", "o"])
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=self.transient).first().photometry.instrument.name, "ATLAS")
        # queue full -> retried by the runner (FacilityUnreachable)
        other = self._request(self.alloc, {})
        with mock.patch("YSE_App.facilities.atlas.http_request", return_value=_FakeResponse(429, text="Too many")):
            with self.assertRaises(FacilityUnreachable):
                self.atlas.submit(other)
        with mock.patch("YSE_App.facilities.atlas.http_request", return_value=_FakeResponse(200, {"finishtimestamp": "t", "result_url": None})):
            self.assertEqual(self.atlas.get_status(req).state, "failed")

    def test_username_password_token(self):
        cred = _credential(self.staff, {"username": "u", "password": "p"}, name="atlas up", service="atlas")
        self.alloc.credential = cred
        with mock.patch("YSE_App.facilities.atlas.http_request", return_value=_FakeResponse(200, {"token": "fresh"})) as post:
            self.assertEqual(self.atlas.auth_headers(self.alloc)["Authorization"], "Token fresh")
        self.assertEqual(post.call_args[0][1], atlas_mod.TOKEN_URL)
        self.alloc.credential = _credential(self.staff, {"nothing": 1}, name="atlas empty", service="atlas")
        with self.assertRaises(FacilityError):
            self.atlas.auth_headers(self.alloc)


# --- Swift, Gemini, MMT, LT, SOAR ------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY)
class OtherAdapterTests(_Base):
    def test_swift_jwt_and_submit(self):
        payload = {"api_name": "Swift_TOO", "api_version": "1.2", "api_data": {"username": "u", "ra": 35.2}}
        token = swift_mod.encode_jwt(payload, "s3cret")
        self.assertEqual(_decode_jwt(token, "s3cret"), payload)
        with self.assertRaises(ValueError):
            _decode_jwt(token, "wrong")
        try:  # PyJWT is not in the CI image; when present the tokens must be byte-identical
            import jwt as pyjwt
        except ImportError:
            pyjwt = None
        if pyjwt is not None:
            self.assertEqual(token, pyjwt.encode(payload, "s3cret", algorithm="HS256"))
        cred = _credential(self.staff, {"username": "yse", "shared_secret": "s3cret"}, service="swift")
        a = _allocation(self.staff, self.telescope, name="Swift", facility="swift", credential=cred, hours=0)
        swift = get_facility("swift")
        req = self._request(a, {"exposure": 1500, "urgency": 2})
        self.assertTrue(req.payload["debug"])
        doc = swift.build_payload(req)
        self.assertEqual(doc["api_data"]["source_name"], "2026fq")
        self.assertEqual(doc["api_data"]["urgency"], 2)
        self.assertEqual(doc["api_data"]["xrt_mode"], 7)
        self.assertNotIn("monitoring_freq", doc["api_data"])
        answer = {"api_name": "Swift_TOO", "api_data": {"status": {"status": "Accepted", "too_id": 0, "errors": [], "warnings": ["debug mode"]}}}
        with mock.patch("YSE_App.facilities.swift.http_request", return_value=_FakeResponse(200, answer)) as post:
            result = swift.submit(req)
        self.assertEqual(result.state, "complete")  # dry run
        self.assertIn("dry run accepted", result.detail)
        self.assertEqual(result.hours, 0.0)
        token = post.call_args[1]["data"]["jwt"]
        self.assertEqual(_decode_jwt(token, "s3cret")["api_data"]["debug"], True)
        live = _allocation(self.staff, self.telescope, name="Swift live", facility="swift", credential=cred, hours=0,
                           params={"debug": False})
        req2 = self._request(live, {"exposure": 2000})
        self.assertFalse(req2.payload["debug"])
        with mock.patch("YSE_App.facilities.swift.http_request",
                        return_value=_FakeResponse(200, {"api_data": {"status": {"status": "Accepted", "too_id": 19001}}})):
            result = swift.submit(req2)
        self.assertEqual(result.state, "accepted")
        self.assertEqual(result.external_id, "19001")
        self.assertIsNone(result.hours)
        with mock.patch("YSE_App.facilities.swift.http_request",
                        return_value=_FakeResponse(200, {"api_data": {"status": {"status": "Rejected", "errors": ["bad exposure"]}}})):
            with self.assertRaises(FacilityError) as ctx:
                swift.submit(req2)
        self.assertIn("bad exposure", str(ctx.exception))
        self.assertAlmostEqual(swift.estimate_hours({"exposure": 3600, "num_of_visits": 2}), 2.0)
        a.credential = None
        with self.assertRaises(FacilityError):
            swift.build_payload(self._request(a, {}))

    def test_gemini_submit(self):
        cred = _credential(self.staff, {"user_key": "k3y", "email": "pi@example.org"}, service="gemini")
        a = _allocation(self.staff, self.telescope, name="Gemini", facility="gemini", credential=cred, proposal="GN-2026B-Q-101",
                        params={"site": "north", "obsnum": 12})
        gemini = get_facility("gemini")
        with self.assertRaises(FacilityValidationError):
            gemini.validate({"site": "west"}, a)
        req = self._request(a, {"mag": 18.2, "band": "r", "exptime": 300})
        payload = gemini.build_payload(req)
        self.assertEqual(payload["prog"], "GN-2026B-Q-101")
        self.assertEqual(payload["password"], "k3y")
        self.assertEqual(payload["obsnum"], 12)
        self.assertEqual(payload["mags"], "18.20/r/AB")
        self.assertEqual(payload["exptime"], 300.0)
        self.assertEqual(payload["ready"], "true")
        with mock.patch("YSE_App.facilities.gemini.http_request", return_value=_FakeResponse(200, text="GN-2026B-Q-101-45\n")) as post:
            result = gemini.submit(req)
        self.assertEqual(result.state, "accepted")
        self.assertEqual(result.external_id, "GN-2026B-Q-101-45")
        self.assertEqual(post.call_args[0][1], "https://gnodb.gemini.edu:8443/too")
        self.assertFalse(post.call_args[1]["verify"])
        with mock.patch("YSE_App.facilities.gemini.http_request", return_value=_FakeResponse(401, text="bad key")):
            with self.assertRaises(FacilityError):
                gemini.submit(req)
        self.assertAlmostEqual(gemini.estimate_hours({"exptime": 1800}), 0.5)

    def test_mmt_submit_update_delete(self):
        self.assertEqual(mmt_mod.sexagesimal(35.2, 12.4), ("02:20:48.000", "+12:24:00.00"))
        self.assertEqual(mmt_mod.sexagesimal(0.0, -0.5)[1], "-00:30:00.00")
        cred = _credential(self.staff, {"api_token": "mmt-tok"}, service="mmt")
        a = _allocation(self.staff, self.telescope, name="MMT", facility="mmt", credential=cred, proposal="2026B-YSE")
        mmt = get_facility("mmt")
        req = self._request(a, {"exposure_time": 900, "exposure_count": 2, "magnitude": 19.0})
        payload = mmt.build_payload(req)
        self.assertEqual(payload["instrumentid"], 16)
        self.assertEqual(payload["grating"], "270")
        self.assertEqual(payload["numberexposures"], 2)
        self.assertEqual(payload["token"], "mmt-tok")
        self.assertAlmostEqual(req.hours_charged if req.hours_charged else mmt.estimate_hours(req.payload), 0.6, places=3)
        with mock.patch("YSE_App.facilities.mmt.http_request", return_value=_FakeResponse(201, {"id": 555})) as post:
            result = mmt.submit(req)
        self.assertEqual(result.external_id, "555")
        self.assertEqual(post.call_args[0][:2], ("POST", mmt_mod.BASE))
        req.set_state("accepted", external_id="555")
        with mock.patch("YSE_App.facilities.mmt.http_request", return_value=_FakeResponse(200, {"id": 555})) as put:
            result = mmt.update(req)
        self.assertEqual(put.call_args[0][:2], ("PUT", mmt_mod.BASE + "555/"))
        self.assertEqual(result.state, "accepted")
        with mock.patch("YSE_App.facilities.mmt.http_request", return_value=_FakeResponse(204)) as delete:
            result = mmt.delete(req)
        self.assertEqual(delete.call_args[0][:2], ("DELETE", mmt_mod.BASE + "555/"))
        self.assertEqual(result.state, "cancelled")
        mmirs = self._request(a, {"exposure_time": 60, "instrument": "mmirs", "observationtype": "imaging", "filter": "r"})
        self.assertEqual(mmirs.payload["filter"], "J")
        self.assertNotIn("grating", mmt.build_payload(mmirs))
        with self.assertRaises(FacilityValidationError):
            mmt.validate({"exposure_time": 60, "instrument": "hectospec"}, a)

    def test_lt_rtml_submit_and_abort(self):
        cred = _credential(self.staff, {"username": "ltuser", "password": "ltpass"}, service="lt")
        a = _allocation(self.staff, self.telescope, name="LT", facility="lt", credential=cred, proposal="PL26B01")
        lt = get_facility("lt")
        with self.assertRaises(FacilityValidationError) as ctx:
            lt.validate({"exposure_time": 60, "filters": "g,x", "start": "2026-10-02T00:00", "end": "2026-10-01T00:00"}, a)
        self.assertEqual(sorted(ctx.exception.errors), ["end", "filters"])
        req = self._request(a, {"exposure_time": 120, "filters": "g,r", "start": "2026-10-01T00:00", "end": "2026-10-03T00:00"})
        document = lt_mod.build_request_document(req, "ysepz-test")
        from xml.etree import ElementTree as ET

        root = ET.fromstring(document)
        ns = {"r": lt_mod.RTML_NS}
        self.assertEqual(root.attrib["mode"], "request")
        self.assertEqual(root.find("r:Project", ns).attrib["ProjectID"], "PL26B01")
        self.assertEqual(root.find("r:Project/r:Contact/r:Username", ns).text, "ltuser")
        schedules = root.findall("r:Schedule", ns)
        self.assertEqual(len(schedules), 2)
        self.assertEqual([s.find("r:Device/r:Setup/r:Filter", ns).attrib["type"] for s in schedules], ["g", "r"])
        self.assertEqual(schedules[0].find("r:Target", ns).attrib["name"], "2026fq")
        self.assertEqual(schedules[0].find("r:Target/r:Coordinates/r:RightAscension/r:Hours", ns).text, "2")
        self.assertEqual(schedules[0].find("r:Exposure", ns).attrib["count"], "1")
        self.assertAlmostEqual(lt.estimate_hours(req.payload), 2 * 150 / 3600.0, places=4)

        class FakeConn:
            sent = b""
            reply = b'<RTML xmlns="%s" mode="confirm" uid="ysepz-1-abc" version="3.1a"/>' % lt_mod.RTML_NS.encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def sendall(self, data):
                FakeConn.sent += data

            def shutdown(self, how):
                pass

            def recv(self, n):
                reply, FakeConn.reply = FakeConn.reply, b""
                return reply

        with mock.patch("YSE_App.facilities.lt.socket.create_connection", return_value=FakeConn()) as connect:
            result = lt.submit(req)
        self.assertEqual(result.state, "accepted")
        self.assertEqual(result.external_id, "ysepz-1-abc")
        self.assertEqual(connect.call_args[0][0], (lt_mod.HOST, lt_mod.PORT))
        self.assertIn(b'mode="request"', FakeConn.sent)
        req.set_state("accepted", external_id=result.external_id)
        FakeConn.reply = b'<RTML xmlns="%s" mode="confirm" uid="ysepz-1-abc"/>' % lt_mod.RTML_NS.encode()
        with mock.patch("YSE_App.facilities.lt.socket.create_connection", return_value=FakeConn()):
            self.assertEqual(lt.delete(req).state, "cancelled")
        FakeConn.reply = b'<RTML xmlns="%s" mode="reject" uid="x"><Error>bad project</Error></RTML>' % lt_mod.RTML_NS.encode()
        with mock.patch("YSE_App.facilities.lt.socket.create_connection", return_value=FakeConn()):
            with self.assertRaises(FacilityError) as ctx:
                lt.submit(req)
        self.assertIn("bad project", str(ctx.exception))
        with mock.patch("YSE_App.facilities.lt.socket.create_connection", side_effect=OSError("refused")):
            with self.assertRaises(FacilityUnreachable):
                lt.submit(req)
        sprat = self._request(a, {"exposure_time": 600, "instrument": "SPRAT", "start": "2026-10-01T00:00", "end": "2026-10-03T00:00"})
        root = ET.fromstring(lt_mod.build_request_document(sprat, "u"))
        self.assertEqual(root.find("r:Schedule/r:Device/r:Setup/r:Grating", ns).attrib["name"], "red")

    def test_soar_uses_lco_portal_with_soar_instruments(self):
        soar_tel = Telescope.objects.create(name="SOAR 4.1m", observatory=self.telescope.observatory, latitude=-30.2,
                                            longitude=-70.7, elevation=2738, **audit_fields(self.staff))
        cred = _credential(self.staff, {"api_token": "tok"}, service="soar")
        a = _allocation(self.staff, soar_tel, name="SOAR", facility="soar", credential=cred, proposal="SOAR2026B-1")
        soar = get_facility("soar")
        names = [f["name"] for f in soar.form_schema(a)]
        self.assertEqual(names[0], "strategy")
        self.assertIn("instrument", names)
        req = self._request(a, {"exposure_time": 1200, "start": "2026-10-01T00:00", "end": "2026-10-02T00:00"})
        self.assertEqual(req.payload["strategy"], "instrument")
        self.assertEqual(req.payload["instrument"], "SOAR_GHTS_REDCAM")
        payload = soar.build_payload(req)
        configs = payload["requests"][0]["configurations"]
        self.assertTrue(any(c["instrument_type"] == "SOAR_GHTS_REDCAM" for c in configs))
        self.assertEqual(payload["requests"][0]["location"]["telescope_class"], "4m0")
        self.assertEqual(payload["proposal"], "SOAR2026B-1")
        with mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(201, {"id": 31, "state": "PENDING"})) as post:
            result = soar.submit(req)
        self.assertEqual(result.external_id, "31")
        self.assertEqual(post.call_args[0][0], REQUESTGROUPS)
        imager = self._request(a, {"exposure_time": 60, "instrument": "SOAR_GHTS_REDCAM_IMAGER", "filters": "g-SDSS,z-SDSS",
                                   "start": "2026-10-01T00:00", "end": "2026-10-02T00:00"})
        configs = soar.build_payload(imager)["requests"][0]["configurations"]
        self.assertEqual([c["instrument_configs"][0]["optical_elements"]["filter"] for c in configs], ["g-SDSS", "z-SDSS"])


# --- #304: legacy resource accounting -----------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False)
class ResourceAccountingTests(_Base):
    def _too(self, **kw):
        start, end = _semester()
        defaults = dict(telescope=self.telescope, begin_date_valid=start, end_date_valid=end, awarded_too_hours=10,
                        used_too_hours=0, awarded_too_triggers=5, used_too_triggers=0)
        defaults.update(kw)
        return ToOResource.objects.create(**defaults, **audit_fields(self.staff))

    def _followup(self, status, **kw):
        start, end = _semester()
        return TransientFollowup.objects.create(transient=self.transient, status=status, valid_start=start, valid_stop=end,
                                                **kw, **audit_fields(self.user))

    def test_too_trigger_charged_once_and_refunded(self):
        too = self._too()
        f = self._followup(self.requested, too_resource=too, usage_hours=0.5)
        too.refresh_from_db()
        self.assertEqual(too.used_too_triggers, 0)
        f.status = self.successful
        f.save()
        f.save()  # idempotent on repeated saves
        too.refresh_from_db()
        self.assertEqual(too.used_too_triggers, 1.0)
        self.assertEqual(too.used_too_hours, 0.5)
        self.assertEqual(too.remaining_triggers, 4.0)
        self.assertEqual(too.remaining_hours, 9.5)
        f.refresh_from_db()
        self.assertIsNotNone(f.usage_charged_at)
        self.assertFalse(alloc_svc.record_followup_usage(f))
        f.status = self.failed
        f.save()
        too.refresh_from_db()
        self.assertEqual(too.used_too_triggers, 0.0)
        self.assertEqual(too.used_too_hours, 0.0)
        f.refresh_from_db()
        self.assertIsNone(f.usage_charged_at)
        # None fields are treated as zero
        bare = self._too(used_too_triggers=None, used_too_hours=None, awarded_too_hours=None)
        g = self._followup(self.successful, too_resource=bare)
        bare.refresh_from_db()
        self.assertEqual(bare.used_too_triggers, 1.0)
        self.assertIsNone(bare.remaining_hours)
        self.assertEqual(alloc_svc.resource_remaining(bare)["triggers"], None if bare.awarded_too_triggers is None else 4.0)

    def test_queued_hours_from_facility_requests(self):
        start, end = _semester()
        queued = QueuedResource.objects.create(telescope=self.telescope, begin_date_valid=start, end_date_valid=end,
                                               awarded_hours=20, used_hours=None, **audit_fields(self.staff))
        a = _allocation(self.staff, self.telescope, name="Q", groups=[self.group],
                        params={"notification_type": "email", "recipients": "q@example.org"})
        f = self._followup(self.requested, queued_resource=queued)
        FacilityRequest.objects.create(allocation=a, transient=self.transient, followup=f, submitted_by=self.user,
                                       hours_charged=1.25, **audit_fields(self.user))
        self.assertEqual(alloc_svc.followup_usage_hours(f), 1.25)
        f.status = self.successful
        f.save()
        queued.refresh_from_db()
        self.assertEqual(queued.used_hours, 1.25)
        self.assertEqual(queued.remaining_hours, 18.75)
        f.refresh_from_db()
        self.assertEqual(f.usage_hours, 1.25)
        # a follow-up without a resource is ignored by the signal
        plain = self._followup(self.successful)
        self.assertIsNone(plain.usage_charged_at)

    def test_backfill_command(self):
        too = self._too()
        with mock.patch("YSE_App.services.allocations.sync_followup_usage"):  # historical rows: signal did nothing
            f = self._followup(self.successful, too_resource=too)
        f.refresh_from_db()
        self.assertIsNone(f.usage_charged_at)
        out = io.StringIO()
        call_command("backfill_resource_usage", stdout=out)
        self.assertIn("1 successful follow-up(s) not charged", out.getvalue())
        self.assertIn("preview only", out.getvalue())
        too.refresh_from_db()
        self.assertEqual(too.used_too_triggers, 0)
        out = io.StringIO()
        call_command("backfill_resource_usage", "--apply", "--hours", "0.75", stdout=out)
        self.assertIn("charged 1 follow-up(s)", out.getvalue())
        too.refresh_from_db()
        self.assertEqual(too.used_too_triggers, 1.0)
        self.assertEqual(too.used_too_hours, 0.75)
        out = io.StringIO()
        call_command("backfill_resource_usage", "--apply", stdout=out)
        self.assertIn("charged 0", out.getvalue())

    def test_resource_binding_and_api_fields(self):
        cred = _credential(self.staff, {"api_token": "tok"}, service="lco")
        a = _allocation(self.staff, self.telescope, name="Bound", facility="lco", credential=cred, params={"exposure_time": 300})
        too = self._too(allocation=a, used_too_triggers=2)
        self.assertEqual(too.facility_api, "lco")
        self.assertTrue(too.has_credential)
        self.assertEqual(too.default_request_params, {"exposure_time": 300})
        self.assertEqual(list(a.tooresource_resources.all()), [too])
        self.staff.set_password("pw")
        self.staff.save()
        client = Client()
        client.login(username="fq_staff", password="pw")
        response = client.get("/api/tooresources/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        row = [r for r in response.json()["results"] if r["allocation"]][0]
        self.assertEqual(row["facility_api"], "lco")
        self.assertTrue(row["has_credential"])
        self.assertEqual(row["default_request_params"], {"exposure_time": 300})
        self.assertEqual(row["remaining_triggers"], 3.0)
        self.assertEqual(row["remaining_hours"], 10.0)
        self.assertTrue(row["allocation"].endswith("/api/allocations/%d/" % a.pk))
        self.assertNotIn("tok", json.dumps(row))
        response = client.get("/api/queuedresources/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        response = client.get("/api/classicalresources/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        # PUT through the serializer keeps triggers and hours apart (copy-paste bug fixed)
        response = client.patch("/api/tooresources/%d/" % too.pk, json.dumps({"awarded_too_triggers": 7, "used_too_hours": 1.5}),
                                content_type="application/json", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        too.refresh_from_db()
        self.assertEqual(too.awarded_too_triggers, 7)
        self.assertEqual(too.used_too_hours, 1.5)
        self.assertEqual(too.awarded_too_hours, 10)


# --- pages, API and the legacy spectrum form -----------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PagesTests(_Base):
    def setUp(self):
        super().setUp()
        for u in (self.staff, self.user):
            u.set_password("pw")
            u.save()
        self.client = Client()
        self.client.login(username="fq_staff", password="pw")
        self.member = Client()
        self.member.login(username="fq_user", password="pw")

    def test_facilities_api(self):
        response = self.client.get("/api/facilities/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        slugs = {row["slug"]: row for row in response.json()}
        self.assertIn("ztf", slugs)
        self.assertEqual(slugs["ztf"]["kind"], "photometry")
        self.assertEqual([f["name"] for f in slugs["ztf"]["fields"]], ["days", "jdstart", "jdend"])
        response = self.client.get("/api/facilities/lt/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["credential_keys"], ["username", "password"])
        self.assertEqual(self.client.get("/api/facilities/nope/", HTTP_ACCEPT="application/json").status_code, 404)
        self.assertIn(Client().get("/api/facilities/", HTTP_ACCEPT="application/json").status_code, (401, 403))

    def test_requests_list_page_filters_and_visibility(self):
        a = _allocation(self.staff, self.telescope, name="Email", groups=[self.group],
                        params={"notification_type": "email", "recipients": "q@example.org"})
        closed = _allocation(self.staff, self.telescope, name="Closed", groups=[Group.objects.create(name="closed")],
                             params={"notification_type": "email", "recipients": "q@example.org"})
        mine = fr.submit_request(a, self.transient, self.user, {"exposure_time": 60})
        other = fr.submit_request(closed, self.transient, self.staff, {"exposure_time": 60})
        fr.mark_request(other, "complete", self.staff)
        self.assertEqual(Client().get(reverse("facility_requests")).status_code, 302)
        response = self.client.get(reverse("facility_requests") + "?state=all")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-request-id="%d"' % mine.pk)
        self.assertContains(response, 'data-request-id="%d"' % other.pk)
        self.assertContains(response, "1 open, 1 finished")
        response = self.client.get(reverse("facility_requests"))  # open only
        self.assertContains(response, 'data-request-id="%d"' % mine.pk)
        self.assertNotContains(response, 'data-request-id="%d"' % other.pk)
        response = self.client.get(reverse("facility_requests") + "?state=complete&facility=generic&allocation=%d&transient=2026" % closed.pk)
        self.assertContains(response, 'data-request-id="%d"' % other.pk)
        self.assertNotContains(response, 'data-request-id="%d"' % mine.pk)
        response = self.member.get(reverse("facility_requests") + "?state=all")
        self.assertContains(response, 'data-request-id="%d"' % mine.pk)
        self.assertNotContains(response, 'data-request-id="%d"' % other.pk)
        self.assertContains(response, reverse("facility_requests"))  # nav link
        response = self.member.get(reverse("facility_requests") + "?state=all&mine=1")
        self.assertContains(response, 'data-request-id="%d"' % mine.pk)

    def test_panel_modify_log_and_followup_badges(self):
        cred = _credential(self.staff, {"api_token": "mmt"}, service="mmt")
        a = _allocation(self.staff, self.telescope, name="MMT", facility="mmt", credential=cred, proposal="P", groups=[self.group],
                        params={"exposure_time": 600})
        with mock.patch("YSE_App.facilities.mmt.http_request", return_value=_FakeResponse(201, {"id": 9})):
            response = self.member.post(reverse("transient_facility_submit", args=[self.transient.pk]),
                                        data=json.dumps({"allocation": a.pk, "parameters": {"exposure_time": 600}}),
                                        content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        req_id = response.json()["request"]["id"]
        response = self.member.get(reverse("transient_facility_requests_fragment", args=[self.transient.pk]))
        self.assertContains(response, 'data-fr-modify="%d"' % req_id)
        self.assertContains(response, 'data-fr-log="%d"' % req_id)
        self.assertNotContains(response, 'data-fr-action="poll"')  # MMT has no status endpoint
        self.assertContains(response, 'data-fr-action="complete"')
        with mock.patch("YSE_App.facilities.mmt.http_request", return_value=_FakeResponse(200, {"id": 9})) as put:
            response = self.member.post(reverse("facility_request_action", args=[req_id]),
                                        data=json.dumps({"action": "update", "parameters": {"exposure_time": 900}}),
                                        content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(put.call_args[0][0], "PUT")
        self.assertEqual(response.json()["request"]["state"], "accepted")
        req = FacilityRequest.objects.get(pk=req_id)
        self.assertEqual(req.payload["exposure_time"], 900.0)
        response = self.member.post(reverse("facility_request_action", args=[req_id]),
                                    data=json.dumps({"action": "update", "parameters": {"exposure_time": "x"}}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("exposure_time", response.json()["errors"])
        response = self.member.get(reverse("facility_request_log", args=[req_id]))
        self.assertEqual(response.status_code, 200)
        events = [e["event"] for e in response.json()["request"]["log"]]
        self.assertIn("modified", events)
        self.assertEqual(events[0], "created")
        stranger = Client()
        stranger.login(username="fq_user", password="pw")
        other = create_test_user("fq_other", is_staff=False, is_superuser=False)
        other.set_password("pw")
        other.save()
        stranger = Client()
        stranger.login(username="fq_other", password="pw")
        self.assertEqual(stranger.get(reverse("facility_request_log", args=[req_id])).status_code, 403)
        # follow-up boxes show the facility badge
        response = self.member.get(reverse("transient_detail_followup_boxes_fragment", args=[self.transient.pk])) \
            if _has_url("transient_detail_followup_boxes_fragment") else None
        if response is not None:
            self.assertContains(response, "facility-followup-badge")
            self.assertContains(response, "mmt: Accepted")

    def test_automated_spectrum_form_uses_lco_allocation(self):
        faulkes = Telescope.objects.create(name="Faulkes Telescope North", observatory=self.telescope.observatory, latitude=20.7,
                                           longitude=-156.3, elevation=3055, **audit_fields(self.staff))
        floyds = Instrument.objects.create(name="FLOYDS-N", telescope=faulkes, **audit_fields(self.staff))
        start, end = _semester()
        too = ToOResource.objects.create(telescope=faulkes, begin_date_valid=start, end_date_valid=end, awarded_too_triggers=3,
                                         used_too_triggers=0, **audit_fields(self.staff))
        lcogt_group, _ = Group.objects.get_or_create(name="LCOGT")
        self.user.groups.add(lcogt_group)
        valid_start = timezone.now() + datetime.timedelta(days=1)
        data = {"transient": self.transient.pk, "instrument": floyds.pk, "exp_time": 1500,
                "spectrum_valid_start": valid_start.strftime("%Y-%m-%d %H:%M:%S"),
                "spectrum_valid_stop": (valid_start + datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")}
        # no allocation: the legacy lcogt path
        with mock.patch("YSE_App.form_views.lcogt.main") as legacy:
            response = self.member.post(reverse("automated_spectrum_request"), data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["errorflag"], 0, response.content)
        self.assertTrue(legacy.called)
        self.assertEqual(FacilityRequest.objects.count(), 0)
        # bound allocation: through the adapter, request linked to the follow-up
        cred = _credential(self.staff, {"api_token": "tok"}, service="lco")
        a = _allocation(self.staff, faulkes, name="FLOYDS", facility="lco", credential=cred, proposal="LCO2026B-9", groups=[self.group])
        too.allocation = a
        too.save()
        with mock.patch("YSE_App.form_views.lcogt.main") as legacy, \
                mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(201, {"id": 4242, "state": "PENDING"})) as post:
            response = self.member.post(reverse("automated_spectrum_request"), data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["data"]
        self.assertEqual(body["errorflag"], 0, body)
        self.assertEqual(body["state"], "accepted")
        self.assertFalse(legacy.called)
        req = FacilityRequest.objects.get(pk=body["facility_request_id"])
        self.assertEqual(req.external_id, "4242")
        self.assertEqual(req.payload["strategy"], "spectroscopy")
        self.assertEqual(req.payload["exposure_time"], 1500.0)
        self.assertIsNotNone(req.followup)
        self.assertEqual(req.followup.too_resource, too)
        configs = post.call_args[1]["json"]["requests"][0]["configurations"]
        self.assertIn("2M0-FLOYDS-SCICAM", [c["instrument_type"] for c in configs])
        # a failed facility validation is reported through the form's error channel
        a.proposal_id = ""
        a.save()
        response = self.member.post(reverse("automated_spectrum_request"), data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json()["data"]["errorflag"], 1)
        self.assertIn("facility request refused", response.json()["data"]["errors"])


def _decode_jwt(token, secret):
    """Verify an HS256 JWT by hand and return its payload (ValueError on a bad signature)."""
    import base64
    import hashlib
    import hmac

    header, body, signature = token.split(".")
    expected = hmac.new(secret.encode(), ("%s.%s" % (header, body)).encode(), hashlib.sha256).digest()
    if base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)) != expected:
        raise ValueError("bad signature")
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _has_url(name):
    from django.urls import NoReverseMatch

    try:
        reverse(name, args=[1])
    except NoReverseMatch:
        return False
    return True
