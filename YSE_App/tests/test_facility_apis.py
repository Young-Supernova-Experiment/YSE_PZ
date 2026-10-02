"""Allocations (#303: #304, #305) and facility APIs (#298: #299, #300, #301 LCO, #302 GENERIC)."""

import datetime
import json
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import Group
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.facilities import (
    FacilityAPI,
    FacilityError,
    FacilityValidationError,
    Field,
    SubmitResult,
    facility_choices,
    get_facility,
    register,
    registered_slugs,
    unregister,
)
from YSE_App.facilities.generic import render_template
from YSE_App.facilities.lco import LCO_STATES, REQUESTGROUPS, TOKEN_AUTH, LCOFacility
from YSE_App.jobs import run_pass
from YSE_App.models import (
    Allocation,
    EncryptedCredential,
    ExternalService,
    ExternalServiceRun,
    FacilityRequest,
    FollowupStatus,
    Job,
    Notification,
    Observatory,
    Telescope,
    TransientFollowup,
)
from YSE_App.services import allocations as alloc_svc
from YSE_App.services import external_services as runs
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


def _now():
    return timezone.now()


def _semester():
    now = _now()
    return now - datetime.timedelta(days=10), now + datetime.timedelta(days=100)


def _credential(user, secret, name="LCO key", service="lco"):
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


class _Base(TestCase):
    def setUp(self):
        self.staff = create_test_user("alloc_staff", is_staff=True, is_superuser=True)
        self.user = create_test_user("alloc_user", is_staff=False, is_superuser=False)
        self.group = Group.objects.create(name="alloc-observers")
        self.user.groups.add(self.group)
        self.other = create_test_user("alloc_other", is_staff=False, is_superuser=False)
        _obs_group, self.instrument, _band = create_instrument_stack(self.staff, obs_group_name="alloc-group")
        self.telescope = self.instrument.telescope
        self.transient = create_minimal_transient(self.staff, name="2026fac", obs_group_name="alloc-group",
                                                  ra=35.2, dec=12.4)
        self.requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.staff))
        self.successful, _ = FollowupStatus.objects.get_or_create(name="Successful", defaults=audit_fields(self.staff))


# --- models ---------------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY)
class AllocationModelTests(_Base):
    def test_hours_and_percent(self):
        a = _allocation(self.staff, self.telescope, hours=10)
        self.assertEqual(a.hours_remaining, 10.0)
        alloc_svc.record_usage(a, 2.5)
        self.assertEqual(a.hours_used, 2.5)
        self.assertEqual(a.hours_remaining, 7.5)
        self.assertEqual(a.percent_used, 25)
        a.hours_allocated = 0
        self.assertEqual(a.percent_used, 0)
        self.assertIn(self.telescope.name, str(a))

    def test_usable_by(self):
        a = _allocation(self.staff, self.telescope, groups=[self.group])
        self.assertTrue(a.usable_by(self.staff))
        self.assertTrue(a.usable_by(self.user))
        self.assertFalse(a.usable_by(self.other))
        self.assertFalse(a.usable_by(None))
        everyone = _allocation(self.staff, self.telescope, name="open")
        self.assertTrue(everyone.usable_by(self.other))
        everyone.is_active = False
        self.assertFalse(everyone.usable_by(self.other))
        expired = _allocation(self.staff, self.telescope, name="old")
        expired.end_date = _now() - datetime.timedelta(days=1)
        self.assertFalse(expired.usable_by(self.staff))
        self.assertFalse(expired.is_current())

    def test_has_credential_and_secret(self):
        a = _allocation(self.staff, self.telescope)
        self.assertFalse(a.has_credential)
        self.assertEqual(a.secret(), {})
        a.credential = _credential(self.staff, {"api_token": "tok"})
        self.assertTrue(a.has_credential)
        self.assertEqual(a.secret(), {"api_token": "tok"})

    def test_ensure_service_creates_and_syncs(self):
        cred = _credential(self.staff, {"api_token": "tok"})
        a = _allocation(self.staff, self.telescope, credential=cred, endpoint="https://obs.example.org/api",
                        params={"exposure_time": 120})
        service = alloc_svc.ensure_service(a, self.staff)
        self.assertEqual(service.slug, "allocation-%d" % a.pk)
        self.assertEqual(service.kind, ExternalService.KIND_FACILITY)
        self.assertEqual(service.credential, cred)
        self.assertEqual(service.base_url, "https://obs.example.org/api")
        self.assertEqual(service.default_params, {"exposure_time": 120})
        self.assertTrue(service.enabled)
        a.refresh_from_db()
        self.assertEqual(a.service, service)
        a.is_active = False
        a.endpoint_url = ""
        a.save()
        again = alloc_svc.ensure_service(a, self.staff)
        self.assertEqual(again.pk, service.pk)
        self.assertFalse(again.enabled)
        self.assertEqual(again.base_url, "")
        self.assertEqual(ExternalService.objects.filter(slug=service.slug).count(), 1)

    def test_allocations_for_user(self):
        _allocation(self.staff, self.telescope, name="grp", groups=[self.group])
        _allocation(self.staff, self.telescope, name="open")
        _allocation(self.staff, self.telescope, name="manual", facility="")
        self.assertEqual(sorted(a.name for a in fr.allocations_for_user(self.user)), ["grp", "open"])
        self.assertEqual(sorted(a.name for a in fr.allocations_for_user(self.other)), ["open"])
        self.assertEqual(fr.allocations_for_user(self.staff).count(), 2)
        self.assertEqual(fr.allocations_for_user(self.staff, facility_only=False).count(), 3)

    def test_facility_request_state_and_log(self):
        a = _allocation(self.staff, self.telescope)
        req = FacilityRequest.objects.create(allocation=a, transient=self.transient, submitted_by=self.staff,
                                             payload={"x": 1}, **audit_fields(self.staff))
        self.assertEqual(req.state, FacilityRequest.STATE_DRAFT)
        req.set_state("submitted", "sent", external_id="abc", external_url="https://x/abc")
        req.refresh_from_db()
        self.assertEqual(req.state, "submitted")
        self.assertIsNotNone(req.submitted_at)
        self.assertEqual(req.external_id, "abc")
        self.assertEqual([e["event"] for e in req.log], ["submitted"])
        self.assertTrue(req.is_open)
        with self.assertRaises(ValueError):
            req.set_state("bogus")

    def test_charge_and_refund_are_idempotent(self):
        a = _allocation(self.staff, self.telescope, hours=5)
        req = FacilityRequest.objects.create(allocation=a, transient=self.transient, submitted_by=self.staff,
                                             hours_charged=1.5, **audit_fields(self.staff))
        self.assertTrue(alloc_svc.charge_request(req))
        self.assertFalse(alloc_svc.charge_request(req))
        a.refresh_from_db()
        self.assertAlmostEqual(a.hours_used, 1.5)
        self.assertTrue(alloc_svc.refund_request(req))
        self.assertFalse(alloc_svc.refund_request(req))
        a.refresh_from_db()
        self.assertAlmostEqual(a.hours_used, 0.0)


# --- registry and fields ------------------------------------------------------------

class RegistryTests(TestCase):
    def test_builtin_adapters_registered(self):
        self.assertIn("generic", registered_slugs())
        self.assertIn("lco", registered_slugs())
        self.assertIsNone(get_facility("nope"))
        self.assertIsNone(get_facility(""))
        choices = dict(facility_choices())
        self.assertIn("", choices)
        self.assertEqual(choices["lco"], "Las Cumbres Observatory")
        self.assertEqual(get_facility("lco").describe()["capabilities"], ["delete", "status", "submit", "update"])

    def test_register_and_unregister_custom(self):
        @register
        class Dummy(FacilityAPI):
            slug = "dummy-test"
            name = "Dummy"

        try:
            self.assertIsInstance(get_facility("dummy-test"), Dummy)
        finally:
            unregister("dummy-test")
        self.assertIsNone(get_facility("dummy-test"))
        with self.assertRaises(ValueError):
            register(type("NoSlug", (FacilityAPI,), {}))

    def test_field_cleaning(self):
        self.assertEqual(Field("n", "number", minimum=1).clean("2.5"), 2.5)
        self.assertEqual(Field("n", "integer").clean("3"), 3)
        for bad, kw in (("x", {}), ("0.5", {"minimum": 1}), ("2.5", {})):
            with self.assertRaises(ValueError):
                Field("n", "integer", **kw).clean(bad)
        self.assertTrue(Field("b", "boolean").clean("yes"))
        self.assertFalse(Field("b", "boolean").clean("0"))
        self.assertEqual(Field("c", "choice", choices=[("a", "A")]).clean("a"), "a")
        with self.assertRaises(ValueError):
            Field("c", "choice", choices=[("a", "A")]).clean("z")
        self.assertEqual(Field("d", "datetime").clean("2026-10-01 12:30"), "2026-10-01T12:30:00")
        self.assertEqual(Field("d", "datetime").clean(datetime.datetime(2026, 10, 1, 12, 30, 5)), "2026-10-01T12:30:05")
        with self.assertRaises(ValueError):
            Field("d", "datetime").clean("tomorrow")
        self.assertEqual(Field("l", "list").clean("g, r ,i"), ["g", "r", "i"])
        self.assertEqual(Field("l", "list", default=[]).clean(""), [])
        with self.assertRaises(ValueError):
            Field("r", "text", required=True).clean("")
        self.assertEqual(Field("r", "text", required=True, default="x").clean(""), "x")
        with self.assertRaises(ValueError):
            Field("t", "wat")

    def test_validate_merges_defaults_and_collects_errors(self):
        class Two(FacilityAPI):
            slug = "two"

            def fields(self, allocation=None):
                return [Field("a", "number", required=True), Field("b", "integer", default=1)]

        api = Two()
        with self.assertRaises(FacilityValidationError) as ctx:
            api.validate({"b": "x"})
        self.assertEqual(sorted(ctx.exception.errors), ["a", "b"])
        alloc = mock.Mock(default_request_params={"a": 2, "extra": "kept"})
        self.assertEqual(api.validate({}, alloc), {"a": 2.0, "b": 1, "extra": "kept"})
        self.assertEqual(api.estimate_hours({"exposure_time": 1800, "exposure_count": 2}), 1.0)
        with self.assertRaises(NotImplementedError):
            api.submit(None)
        with self.assertRaises(FacilityError):
            api.get_status(None)


# --- GENERIC ------------------------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class GenericFacilityTests(_Base):
    def _request(self, allocation, params=None):
        facility = get_facility("generic")
        cleaned = facility.validate(params or {}, allocation)
        return FacilityRequest.objects.create(allocation=allocation, transient=self.transient, submitted_by=self.user,
                                              payload=cleaned, **audit_fields(self.user))

    def test_schema_takes_allocation_defaults(self):
        a = _allocation(self.staff, self.telescope, params={"exposure_time": 900, "recipients": "a@x.org"})
        schema = {f["name"]: f for f in get_facility("generic").form_schema(a)}
        self.assertEqual(schema["exposure_time"]["default"], 900)
        self.assertEqual(schema["exposure_count"]["default"], 1)

    def test_mode_detection_and_validation(self):
        facility = get_facility("generic")
        api_alloc = _allocation(self.staff, self.telescope, endpoint="https://obs.example.org/req")
        self.assertEqual(facility.mode(api_alloc), "api")
        email_alloc = _allocation(self.staff, self.telescope, name="mail")
        self.assertEqual(facility.mode(email_alloc), "email")
        with self.assertRaises(FacilityValidationError) as ctx:
            facility.validate({}, email_alloc)
        self.assertIn("recipients", ctx.exception.errors)
        bad = _allocation(self.staff, self.telescope, name="bad", params={"notification_type": "carrier-pigeon"})
        with self.assertRaises(FacilityValidationError):
            facility.validate({}, bad)

    def test_api_mode_posts_json_with_token(self):
        cred = _credential(self.staff, {"api_token": "s3cret"}, service="generic")
        a = _allocation(self.staff, self.telescope, endpoint="https://obs.example.org/req", credential=cred,
                        params={"exposure_time": 120})
        req = self._request(a, {"filters": "g,r", "priority": 2})
        with mock.patch("YSE_App.facilities.generic.requests.post",
                        return_value=_FakeResponse(201, {"id": 4711, "status": "ok"})) as post:
            result = get_facility("generic").submit(req)
        self.assertEqual(result.state, "submitted")
        self.assertEqual(result.external_id, "4711")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://obs.example.org/req")
        self.assertEqual(kwargs["headers"]["Authorization"], "token s3cret")
        body = kwargs["json"]
        self.assertEqual(body["transient"]["name"], "2026fac")
        self.assertEqual(body["parameters"]["filters"], ["g", "r"])
        self.assertEqual(body["parameters"]["exposure_time"], 120.0)
        self.assertEqual(body["requester"], "alloc_user")
        self.assertEqual(body["request_id"], req.pk)
        cred.refresh_from_db()
        self.assertIsNotNone(cred.last_used_at)

    def test_api_mode_errors(self):
        a = _allocation(self.staff, self.telescope, endpoint="https://obs.example.org/req")
        req = self._request(a)
        with mock.patch("YSE_App.facilities.generic.requests.post", return_value=_FakeResponse(500, text="boom")):
            with self.assertRaises(FacilityError) as ctx:
                get_facility("generic").submit(req)
        self.assertIn("500", str(ctx.exception))
        import requests as _requests

        with mock.patch("YSE_App.facilities.generic.requests.post", side_effect=_requests.ConnectionError("down")):
            with self.assertRaises(FacilityError):
                get_facility("generic").submit(req)
        a.endpoint_url = ""
        a.default_request_params = {"notification_type": "api"}
        a.save()
        with self.assertRaises(FacilityError):
            get_facility("generic").submit(self._request(a))

    def test_payload_template(self):
        a = _allocation(self.staff, self.telescope, endpoint="https://obs.example.org/req", params={
            "payload_template": {"target": "{transient_name}", "coords": ["{ra}", "{dec}"],
                                 "exp": "{param_exposure_time}", "who": "{requester}", "keep": "{unknown}", "n": 3},
        })
        req = self._request(a, {"exposure_time": 60})
        payload = get_facility("generic").build_payload(req)
        self.assertEqual(payload["target"], "2026fac")
        self.assertEqual(payload["coords"], ["35.2", "12.4"])
        self.assertEqual(payload["exp"], "60.0")
        self.assertEqual(payload["who"], "alloc_user")
        self.assertEqual(payload["keep"], "{unknown}")
        self.assertEqual(payload["n"], 3)
        self.assertEqual(render_template("{a}-{b}", {"a": 1}), "1-{b}")

    def test_email_mode_sends_mail(self):
        a = _allocation(self.staff, self.telescope, name="Keck email", params={
            "notification_type": "email", "recipients": "obs1@example.org, obs2@example.org"})
        req = self._request(a, {"exposure_time": 600, "comment": "please hurry"})
        result = get_facility("generic").submit(req)
        self.assertEqual(result.state, "submitted")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(sorted(message.to), ["obs1@example.org", "obs2@example.org"])
        self.assertIn("2026fac", message.subject)
        self.assertIn(self.telescope.name, message.body)
        self.assertIn("please hurry", message.body)
        self.assertIn("obs1@example.org", result.detail)

    def test_slack_mode_posts_webhook(self):
        cred = _credential(self.staff, {"slack_webhook_url": "https://hooks.slack.com/services/T/B/x"}, service="generic")
        a = _allocation(self.staff, self.telescope, name="slack", credential=cred, params={"notification_type": "slack"})
        req = self._request(a, {"exposure_time": 300})
        with mock.patch("YSE_App.facilities.generic.requests.post", return_value=_FakeResponse(200, text="ok")) as post:
            result = get_facility("generic").submit(req)
        self.assertEqual(result.state, "submitted")
        self.assertEqual(post.call_args[0][0], "https://hooks.slack.com/services/T/B/x")
        self.assertIn("2026fac", post.call_args[1]["json"]["text"])
        a.credential = None
        with self.assertRaises(FacilityError):
            get_facility("generic").submit(self._request(a, {"exposure_time": 300}))


# --- LCO ------------------------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY)
class LCOFacilityTests(_Base):
    def setUp(self):
        super().setUp()
        observatory = self.telescope.observatory
        self.faulkes = Telescope.objects.create(name="Faulkes Telescope North", observatory=observatory, latitude=20.7,
                                                longitude=-156.3, elevation=3055, **audit_fields(self.staff))
        self.cred = _credential(self.staff, {"api_token": "lco-token"})
        self.lco = get_facility("lco")

    def _request(self, allocation, params):
        cleaned = self.lco.validate(params, allocation)
        return FacilityRequest.objects.create(allocation=allocation, transient=self.transient, submitted_by=self.user,
                                              payload=cleaned, hours_charged=self.lco.estimate_hours(cleaned, allocation),
                                              **audit_fields(self.user))

    def test_validation(self):
        a = _allocation(self.staff, self.telescope, facility="lco", credential=self.cred, proposal="")
        with self.assertRaises(FacilityValidationError) as ctx:
            self.lco.validate({"exposure_time": 100, "start": "2026-10-01T00:00", "end": "2026-10-02T00:00"}, a)
        self.assertIn("proposal", ctx.exception.errors)
        a.proposal_id = "LCO2026B-001"
        with self.assertRaises(FacilityValidationError) as ctx:
            self.lco.validate({"exposure_time": 100, "start": "2026-10-02T00:00", "end": "2026-10-01T00:00",
                               "strategy": "spectroscopy"}, a)
        self.assertEqual(sorted(ctx.exception.errors), ["end", "strategy"])
        cleaned = self.lco.validate({"exposure_time": "100", "start": "2026-10-01T00:00", "end": "2026-10-02T00:00"}, a)
        self.assertEqual(cleaned["proposal"], "LCO2026B-001")
        self.assertEqual(cleaned["filters"], ["up", "gp", "rp", "ip"])
        self.assertEqual(cleaned["strategy"], "default")
        self.assertAlmostEqual(self.lco.estimate_hours(cleaned, a), 4 * 190 / 3600.0, places=4)

    def test_build_imaging_payload_with_lcogt(self):
        a = _allocation(self.staff, self.telescope, facility="lco", credential=self.cred, proposal="LCO2026B-001",
                        params={"max_airmass": 1.8})
        req = self._request(a, {"exposure_time": 120, "filters": "gp,rp", "start": "2026-10-01T00:00",
                                "end": "2026-10-03T00:00", "ipp_value": 1.05, "observation_type": "RAPID_RESPONSE"})
        payload = self.lco.build_payload(req)
        self.assertEqual(payload["name"], "2026fac")
        self.assertEqual(payload["proposal"], "LCO2026B-001")
        self.assertEqual(payload["observation_type"], "RAPID_RESPONSE")
        self.assertEqual(payload["ipp_value"], 1.05)
        request_block = payload["requests"][0]
        self.assertEqual(request_block["location"], {"telescope_class": "1m0"})
        self.assertEqual(request_block["windows"], [{"start": "2026-10-01 00:00:00", "end": "2026-10-03 00:00:00"}])
        configs = request_block["configurations"]
        self.assertEqual([c["instrument_configs"][0]["optical_elements"]["filter"] for c in configs], ["gp", "rp"])
        self.assertEqual(configs[0]["constraints"], {"max_airmass": 1.8, "min_lunar_distance": 15.0})
        self.assertEqual(configs[0]["target"]["ra"], 35.2)
        self.assertEqual(configs[0]["instrument_configs"][0]["exposure_time"], 120.0)
        self.assertEqual(configs[0]["instrument_type"], "1M0-SCICAM-SINISTRO")

    def test_build_spectroscopy_payload(self):
        a = _allocation(self.staff, self.faulkes, facility="lco", credential=self.cred, proposal="LCO2026B-002")
        req = self._request(a, {"exposure_time": 1800, "strategy": "spectroscopy", "start": "2026-10-01T00:00",
                                "end": "2026-10-02T00:00"})
        payload = self.lco.build_payload(req)
        configs = payload["requests"][0]["configurations"]
        self.assertEqual([c["type"] for c in configs], ["LAMP_FLAT", "ARC", "SPECTRUM", "ARC", "LAMP_FLAT"])
        self.assertEqual(payload["requests"][0]["location"], {"telescope_class": "2m0"})
        self.assertEqual(configs[2]["instrument_type"], "2M0-FLOYDS-SCICAM")
        self.assertEqual(configs[2]["instrument_configs"][0]["exposure_time"], 1800.0)
        self.assertAlmostEqual(req.hours_charged, 2200 / 3600.0, places=4)

    def test_submit_status_and_cancel_with_token(self):
        a = _allocation(self.staff, self.telescope, facility="lco", credential=self.cred, proposal="LCO2026B-001")
        req = self._request(a, {"exposure_time": 60, "start": "2026-10-01T00:00", "end": "2026-10-02T00:00"})
        with mock.patch("YSE_App.facilities.lco.requests.post",
                        return_value=_FakeResponse(201, {"id": 9001, "state": "PENDING"})) as post:
            result = self.lco.submit(req)
        self.assertEqual(result.state, "accepted")
        self.assertEqual(result.external_id, "9001")
        self.assertTrue(result.external_url.endswith("/9001"))
        self.assertEqual(post.call_args[0][0], REQUESTGROUPS)
        self.assertEqual(post.call_args[1]["headers"], {"Authorization": "Token lco-token"})
        self.assertEqual(post.call_args[1]["json"]["proposal"], "LCO2026B-001")
        req.set_state(result.state, external_id=result.external_id)
        with mock.patch("YSE_App.facilities.lco.requests.get",
                        return_value=_FakeResponse(200, {"id": 9001, "state": "COMPLETED"})) as get:
            status = self.lco.get_status(req)
        self.assertEqual(status.state, "complete")
        self.assertEqual(get.call_args[0][0], REQUESTGROUPS + "9001/")
        with mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(200, {"state": "CANCELED"})) as post:
            cancelled = self.lco.delete(req)
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(post.call_args[0][0], REQUESTGROUPS + "9001/cancel/")
        self.assertEqual(LCO_STATES["WINDOW_EXPIRED"], "failed")

    def test_username_password_fetches_token_and_errors(self):
        cred = _credential(self.staff, {"username": "u", "password": "p"}, name="LCO userpass")
        a = _allocation(self.staff, self.telescope, facility="lco", credential=cred, proposal="LCO2026B-001")
        req = self._request(a, {"exposure_time": 60, "start": "2026-10-01T00:00", "end": "2026-10-02T00:00"})
        responses = [_FakeResponse(200, {"token": "fresh"}), _FakeResponse(400, {"requests": ["bad window"]})]
        with mock.patch("YSE_App.facilities.lco.requests.post", side_effect=responses) as post:
            with self.assertRaises(FacilityError) as ctx:
                self.lco.submit(req)
        self.assertEqual(post.call_args_list[0][0][0], TOKEN_AUTH)
        self.assertEqual(post.call_args_list[1][1]["headers"], {"Authorization": "Token fresh"})
        self.assertIn("400", str(ctx.exception))
        empty = _credential(self.staff, {"note": "nothing useful"}, name="LCO empty")
        a.credential = empty
        with self.assertRaises(FacilityError):
            self.lco.auth_headers(a)
        req.external_id = ""
        with self.assertRaises(FacilityError):
            self.lco.get_status(req)
        self.assertIsInstance(LCOFacility(), FacilityAPI)


# --- submit flow end to end -----------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   JOB_RUNNER_INLINE=False, NOTIFICATION_EMAIL_ENABLED=False)
class SubmitFlowTests(_Base):
    def _email_allocation(self, **kw):
        params = {"notification_type": "email", "recipients": "queue@example.org", "exposure_time": 300}
        params.update(kw.pop("params", {}))
        return _allocation(self.staff, self.telescope, name="Email queue", params=params, groups=[self.group], **kw)

    def test_submit_queues_run_and_job_then_runner_sends(self):
        a = self._email_allocation()
        req = fr.submit_request(a, self.transient, self.user, {"exposure_time": 600, "comment": "urgent"})
        self.assertEqual(req.state, FacilityRequest.STATE_QUEUED)
        self.assertEqual(req.payload["exposure_time"], 600.0)
        self.assertEqual(req.payload["recipients"], "queue@example.org")
        self.assertIsNotNone(req.run)
        self.assertEqual(req.run.status, ExternalServiceRun.STATUS_PENDING)
        self.assertEqual(req.run.service, a.service)
        self.assertEqual(req.run.transient, self.transient)
        self.assertEqual(req.run.target, req)
        self.assertEqual(Job.objects.filter(kind=runs.JOB_KIND, status=Job.QUEUED).count(), 1)
        # a TransientFollowup with status Requested was attached
        self.assertIsNotNone(req.followup)
        self.assertEqual(req.followup.status, self.requested)
        self.assertEqual(req.followup.transient, self.transient)
        self.assertEqual(req.followup.requests.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

        run_pass(worker_id="test")
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_SUBMITTED)
        self.assertIsNotNone(req.submitted_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2026fac", mail.outbox[0].subject)
        run = ExternalServiceRun.objects.get(pk=req.run_id)
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(run.result["state"], "submitted")
        job = Job.objects.get(kind=runs.JOB_KIND)
        self.assertEqual(job.status, Job.DONE)
        self.assertTrue(job.result["handled"])
        self.assertEqual(job.result["state"], "submitted")
        self.assertEqual([e["event"] for e in req.log], ["created", "queued", "submitted"])
        # a second pass does nothing more
        self.assertEqual(fr.run_facility_submission(run)["skipped"], "submitted")

        # manual completion (GENERIC has no status API) charges the allocation once and flips the follow-up
        fr.mark_request(req, FacilityRequest.STATE_COMPLETE, self.user, "observed 2026-10-02")
        a.refresh_from_db()
        self.assertAlmostEqual(a.hours_used, 600 / 3600.0, places=3)
        self.assertIsNotNone(req.charged_at)
        req.followup.refresh_from_db()
        self.assertEqual(req.followup.status, self.successful)
        self.assertTrue(Notification.objects.filter(recipient=self.user, kind="followup_status").exists())
        with self.assertRaises(fr.FacilityRequestError):
            fr.mark_request(req, FacilityRequest.STATE_COMPLETE, self.user)
        with self.assertRaises(fr.FacilityRequestError):
            fr.cancel_request(req, self.user)

    def test_permissions_and_budget(self):
        a = self._email_allocation(hours=0.1)
        with self.assertRaises(fr.FacilityRequestError):
            fr.submit_request(a, self.transient, self.other, {})
        with self.assertRaises(FacilityValidationError) as ctx:
            fr.submit_request(a, self.transient, self.user, {"exposure_time": 3600})
        self.assertIn("exposure_time", ctx.exception.errors)
        manual = _allocation(self.staff, self.telescope, name="manual", facility="")
        with self.assertRaises(fr.FacilityRequestError):
            fr.submit_request(manual, self.transient, self.staff, {})
        self.assertEqual(FacilityRequest.objects.count(), 0)

    def test_adapter_failure_marks_request_and_run_failed(self):
        a = _allocation(self.staff, self.telescope, name="api", endpoint="https://obs.example.org/req",
                        params={"exposure_time": 60})
        req = fr.submit_request(a, self.transient, self.staff, {}, attach_followup=False)
        self.assertIsNone(req.followup)
        with mock.patch("YSE_App.facilities.generic.requests.post", return_value=_FakeResponse(503, text="maintenance")):
            run_pass(worker_id="test")
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_FAILED)
        self.assertIn("503", req.state_detail)
        self.assertEqual(req.run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertEqual(Job.objects.get(kind=runs.JOB_KIND).status, Job.DONE)  # not retried: no double submission
        self.assertTrue(Notification.objects.filter(recipient=self.staff).exists())

    def test_inline_runner_submits_immediately(self):
        a = self._email_allocation()
        with override_settings(JOB_RUNNER_INLINE=True):
            req = fr.submit_request(a, self.transient, self.user, {})
        self.assertEqual(req.state, FacilityRequest.STATE_SUBMITTED)
        self.assertEqual(len(mail.outbox), 1)

    def test_lco_poll_and_cancel_flow(self):
        cred = _credential(self.staff, {"api_token": "tok"})
        a = _allocation(self.staff, self.telescope, name="LCO", facility="lco", credential=cred, proposal="P1",
                        params={"exposure_time": 60})
        req = fr.submit_request(a, self.transient, self.staff, {})
        with mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(201, {"id": 77, "state": "PENDING"})):
            run_pass(worker_id="test")
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_ACCEPTED)
        self.assertEqual(req.external_id, "77")
        self.assertEqual(req.followup.status, self.requested)
        with mock.patch("YSE_App.facilities.lco.requests.get", return_value=_FakeResponse(200, {"state": "PENDING"})):
            counts = fr.poll_open_requests()
        self.assertEqual(counts, {"polled": 1, "changed": 0, "errors": 0, "skipped": 0})
        req.refresh_from_db()
        self.assertIsNotNone(req.last_polled)
        with mock.patch("YSE_App.facilities.lco.requests.get", return_value=_FakeResponse(200, {"state": "COMPLETED"})):
            counts = fr.poll_open_requests()
        self.assertEqual(counts["changed"], 1)
        req.refresh_from_db()
        self.assertEqual(req.state, FacilityRequest.STATE_COMPLETE)
        self.assertIsNotNone(req.charged_at)
        a.refresh_from_db()
        self.assertGreater(a.hours_used, 0)
        req.followup.refresh_from_db()
        self.assertEqual(req.followup.status, self.successful)

        second = fr.submit_request(a, self.transient, self.staff, {})
        with mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(201, {"id": 78, "state": "PENDING"})):
            run_pass(worker_id="test")
        second.refresh_from_db()
        with mock.patch("YSE_App.facilities.lco.requests.post", return_value=_FakeResponse(200, {"state": "CANCELED"})) as post:
            fr.cancel_request(second, self.staff)
        self.assertEqual(post.call_args[0][0], REQUESTGROUPS + "78/cancel/")
        self.assertEqual(second.state, FacilityRequest.STATE_CANCELLED)
        self.assertIsNone(second.charged_at)
        from django.core.management import call_command
        import io

        out = io.StringIO()
        with mock.patch("YSE_App.facilities.lco.requests.get", return_value=_FakeResponse(200, {"state": "PENDING"})):
            call_command("poll_facility_requests", stdout=out)
        self.assertIn("polled=0", out.getvalue())  # nothing open any more
        call_command("poll_facility_requests", "--enqueue", stdout=out)
        self.assertTrue(Job.objects.filter(kind=fr.POLL_JOB_KIND).exists())

    def test_runner_registry_hook(self):
        service = ExternalService.objects.create(name="Other", slug="other-thing", kind="analysis",
                                                 **audit_fields(self.staff))
        run, _tok = runs.start_run(service, self.staff, {}, dispatch=False)
        self.assertFalse(runs.execute_run({"run_id": run.pk})["handled"])
        seen = []
        runs.register_runner("other-thing", lambda r: seen.append(r) or {"ok": True})
        try:
            out = runs.execute_run({"run_id": run.pk})
        finally:
            runs.unregister_runner("other-thing")
        self.assertTrue(out["handled"])
        self.assertTrue(out["ok"])
        self.assertEqual(seen, [run])
        self.assertIsNotNone(runs.get_runner(ExternalService(slug="x", kind=ExternalService.KIND_FACILITY)))
        with self.assertRaises(ValueError):
            runs.register_runner("", None)
        # a facility run without a request fails cleanly
        out = fr.run_facility_submission(run)
        self.assertFalse(out["ok"])
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)


# --- pages and API ------------------------------------------------------------------------

@override_settings(CREDENTIALS_KEY=KEY, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False)
class AllocationPagesTests(_Base):
    def setUp(self):
        super().setUp()
        self.staff.set_password("pw")
        self.staff.save()
        self.user.set_password("pw")
        self.user.save()
        self.other.set_password("pw")
        self.other.save()
        self.client = Client()
        self.client.login(username="alloc_staff", password="pw")

    def _form_data(self, **overrides):
        start, end = _semester()
        data = {
            "name": "Keck queue", "telescope": self.telescope.pk, "instrument": self.instrument.pk,
            "principal_investigator": "", "groups": [self.group.pk], "proposal_id": "K2026B",
            "hours_allocated": "12", "hours_used": "0", "start_date": start.strftime("%Y-%m-%dT%H:%M"),
            "end_date": end.strftime("%Y-%m-%dT%H:%M"), "facility": "generic", "credential": "",
            "endpoint_url": "https://queue.example.org/submit",
            "default_request_params_text": '{"exposure_time": 900}', "new_secret": "", "new_secret_name": "",
            "is_active": "on", "notes": "",
        }
        data.update(overrides)
        return data

    def test_staff_only(self):
        anon = Client()
        self.assertEqual(anon.get(reverse("allocations")).status_code, 302)
        anon.login(username="alloc_user", password="pw")
        self.assertEqual(anon.get(reverse("allocations")).status_code, 302)
        self.assertEqual(anon.get(reverse("allocation_create")).status_code, 302)

    def test_list_create_edit(self):
        response = self.client.get(reverse("allocations"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No allocations")
        self.assertContains(response, "New allocation")
        response = self.client.post(reverse("allocation_create"), self._form_data(
            new_secret='{"api_token": "very-secret-token"}', new_secret_name="Keck token"))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        allocation = Allocation.objects.get(name="Keck queue")
        self.assertEqual(allocation.facility, "generic")
        self.assertEqual(allocation.default_request_params, {"exposure_time": 900})
        self.assertEqual(list(allocation.groups.all()), [self.group])
        self.assertEqual(allocation.created_by, self.staff)
        self.assertIsNotNone(allocation.credential)
        self.assertEqual(allocation.credential.name, "Keck token")
        self.assertEqual(allocation.credential.service, "generic")
        self.assertEqual(allocation.credential.get_secret(), {"api_token": "very-secret-token"})
        self.assertIsNotNone(allocation.service)
        self.assertEqual(allocation.service.credential, allocation.credential)
        response = self.client.get(reverse("allocations"))
        self.assertContains(response, "Keck queue")
        self.assertContains(response, 'data-status="current"')
        self.assertNotContains(response, "very-secret-token")
        response = self.client.get(reverse("allocation_edit", args=[allocation.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Keck token")
        self.assertNotContains(response, "very-secret-token")
        self.assertContains(response, '&quot;exposure_time&quot;: 900')
        # edit keeps the credential when new_secret is blank and re-syncs the service
        response = self.client.post(reverse("allocation_edit", args=[allocation.pk]), self._form_data(
            name="Keck queue 2", credential=str(allocation.credential.pk), endpoint_url="https://queue.example.org/v2"))
        self.assertEqual(response.status_code, 302)
        allocation.refresh_from_db()
        self.assertEqual(allocation.name, "Keck queue 2")
        self.assertEqual(allocation.credential.get_secret(), {"api_token": "very-secret-token"})
        self.assertEqual(allocation.service.base_url, "https://queue.example.org/v2")
        self.assertEqual(EncryptedCredential.objects.filter(service="generic").count(), 1)

    def test_form_validation(self):
        start, end = _semester()
        response = self.client.post(reverse("allocation_create"), self._form_data(
            start_date=end.strftime("%Y-%m-%dT%H:%M"), end_date=start.strftime("%Y-%m-%dT%H:%M"),
            default_request_params_text="[1, 2]", new_secret="not json", facility="unknown-slug"))
        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors
        self.assertIn("end_date", errors)
        self.assertIn("default_request_params_text", errors)
        self.assertIn("new_secret", errors)
        self.assertIn("facility", errors)
        self.assertEqual(Allocation.objects.count(), 0)

    def test_nav_link_staff_only(self):
        self.assertContains(self.client.get(reverse("allocations")), reverse("allocations"))
        other = Client()
        other.login(username="alloc_user", password="pw")
        response = other.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<p>Allocations</p>')

    def test_followup_tab_panel_and_submit_action(self):
        a = _allocation(self.staff, self.telescope, name="Email queue", groups=[self.group],
                        params={"notification_type": "email", "recipients": "queue@example.org"})
        client = Client()
        client.login(username="alloc_user", password="pw")
        response = client.get(reverse("transient_detail_followup_rest_fragment", args=[self.transient.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Submit to facility")
        self.assertContains(response, 'value="%d"' % a.pk)
        self.assertContains(response, "No facility requests for this transient")
        self.assertContains(response, "exposure_time")
        # the non-deferred full page renders the same panel
        with mock.patch.dict("os.environ", {"YSE_TRANSIENT_DETAIL_DEFER": "0"}):
            page = client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Submit to facility")

        response = client.post(reverse("transient_facility_submit", args=[self.transient.pk]),
                               data=json.dumps({"allocation": a.pk, "parameters": {"exposure_time": 120, "priority": 1}}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["request"]["state"], "submitted")  # JOB_RUNNER_INLINE
        self.assertEqual(body["request"]["allocation"], "Email queue")
        req = FacilityRequest.objects.get(pk=body["request"]["id"])
        self.assertEqual(req.submitted_by, self.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(TransientFollowup.objects.filter(transient=self.transient, status=self.requested).exists())

        response = client.get(reverse("transient_facility_requests_fragment", args=[self.transient.pk]))
        self.assertContains(response, 'data-status="submitted"')
        self.assertContains(response, 'data-fr-action="complete"')
        self.assertContains(response, 'data-fr-action="cancel"')

        # validation errors come back as 400 with per-field messages
        response = client.post(reverse("transient_facility_submit", args=[self.transient.pk]),
                               data=json.dumps({"allocation": a.pk, "parameters": {"exposure_time": "lots"}}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("exposure_time", response.json()["errors"])
        response = client.post(reverse("transient_facility_submit", args=[self.transient.pk]),
                               data=json.dumps({"allocation": 999999}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

        # someone else may not touch the request; the requester can mark it complete
        stranger = Client()
        stranger.login(username="alloc_other", password="pw")
        response = stranger.post(reverse("facility_request_action", args=[req.pk]),
                                 data=json.dumps({"action": "complete"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        response = stranger.post(reverse("transient_facility_submit", args=[self.transient.pk]),
                                 data=json.dumps({"allocation": a.pk, "parameters": {}}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        response = client.post(reverse("facility_request_action", args=[req.pk]),
                               data=json.dumps({"action": "complete", "detail": "done"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request"]["state"], "complete")
        response = client.post(reverse("facility_request_action", args=[req.pk]),
                               data=json.dumps({"action": "cancel"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = client.post(reverse("facility_request_action", args=[req.pk]), {"action": "wat"})
        self.assertEqual(response.status_code, 400)
        a.refresh_from_db()
        self.assertGreater(a.hours_used, 0)

    def test_rest_api(self):
        cred = _credential(self.staff, {"api_token": "tok"})
        a = _allocation(self.staff, self.telescope, name="API alloc", facility="lco", credential=cred, groups=[self.group])
        _allocation(self.staff, self.telescope, name="Closed", groups=[Group.objects.create(name="closed-grp")])
        response = self.client.get("/api/allocations/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        names = {row["name"]: row for row in response.json()["results"]}
        self.assertEqual(set(names), {"API alloc", "Closed"})
        row = names["API alloc"]
        self.assertTrue(row["has_credential"])
        self.assertNotIn("credential", row)
        self.assertNotIn("tok", json.dumps(row))
        self.assertEqual(row["hours_remaining"], 10.0)
        self.assertEqual(row["facility_name"], "Las Cumbres Observatory")
        member = Client()
        member.login(username="alloc_user", password="pw")
        response = member.get("/api/allocations/", HTTP_ACCEPT="application/json")
        self.assertEqual([r["name"] for r in response.json()["results"]], ["API alloc"])
        start, end = _semester()
        response = member.post("/api/allocations/", {
            "name": "Nope", "telescope": "http://testserver/api/telescopes/%d/" % self.telescope.pk,
            "start_date": start.isoformat(), "end_date": end.isoformat()}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/allocations/", {
            "name": "Via API", "telescope": "http://testserver/api/telescopes/%d/" % self.telescope.pk,
            "start_date": start.isoformat(), "end_date": end.isoformat(), "hours_allocated": 3,
            "facility": "generic"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Allocation.objects.get(name="Via API").created_by, self.staff)

        req = FacilityRequest.objects.create(allocation=a, transient=self.transient, submitted_by=self.user,
                                             state="accepted", external_id="77", **audit_fields(self.user))
        response = self.client.get("/api/facilityrequests/?transient=%d" % self.transient.pk, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "77")
        self.assertEqual(rows[0]["transient_name"], "2026fac")
        self.assertEqual(rows[0]["facility"], "lco")
        response = self.client.get("/api/facilityrequests/?state=complete", HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["count"], 0)
        response = member.get("/api/facilityrequests/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["count"], 1)
        response = self.client.put("/api/facilityrequests/%d/" % req.pk, {}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 405)

    def test_admin_pages_render(self):
        a = _allocation(self.staff, self.telescope, name="Admin alloc")
        FacilityRequest.objects.create(allocation=a, transient=self.transient, submitted_by=self.staff,
                                       **audit_fields(self.staff))
        for url in (reverse("admin:YSE_App_allocation_changelist"), reverse("admin:YSE_App_allocation_change", args=[a.pk]),
                    reverse("admin:YSE_App_facilityrequest_changelist")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
