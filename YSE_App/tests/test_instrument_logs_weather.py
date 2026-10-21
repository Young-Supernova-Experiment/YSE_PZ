"""Instrument logs (#310) and the weather / SkyCam widget (#311); umbrella #309. HTTP is mocked throughout."""

import datetime
import json
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from YSE_App.data_ingest.Instrument_Logs import InstrumentLogPull, WeatherRefresh
from YSE_App.facilities import FacilityAPI, FacilityError, get_facility
from YSE_App.facilities.generic import instrument_log_entries
from YSE_App.jobs import run_pass
from YSE_App.models import (
    Allocation,
    ClassicalNightType,
    ClassicalObservingDate,
    ClassicalResource,
    ExternalService,
    ExternalServiceRun,
    Instrument,
    InstrumentLog,
    Job,
    Telescope,
    ToOResource,
)
from YSE_App.services import instrument_logs as il
from YSE_App.services import weather as wx
from YSE_App.tests.fixtures_minimal import audit_fields, create_instrument_stack, create_minimal_transient, create_test_user

UTC = datetime.timezone.utc


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _stack(user, name="instlog"):
    _group, instrument, _band = create_instrument_stack(user, obs_group_name=name)
    return instrument


def _allocation(user, instrument, **params):
    now = timezone.now()
    return Allocation.objects.create(
        name="Alloc %s" % instrument.name, telescope=instrument.telescope, instrument=instrument, facility="generic",
        hours_allocated=10, start_date=now - datetime.timedelta(days=5), end_date=now + datetime.timedelta(days=60),
        default_request_params=params, **audit_fields(user),
    )


OPEN_METEO = {
    "latitude": 19.8, "longitude": -155.5, "elevation": 4200.0,
    "current_units": {"time": "iso8601", "temperature_2m": "°C", "wind_speed_10m": "km/h"},
    "current": {"time": "2026-09-29T20:00", "temperature_2m": 3.4, "relative_humidity_2m": 22,
                "wind_speed_10m": 36.0, "wind_direction_10m": 250, "cloud_cover": 5, "precipitation": 0.0,
                "weather_code": 1, "surface_pressure": 610.2},
}
OPENWEATHER = {
    "weather": [{"main": "Clear", "description": "clear sky"}],
    "main": {"temp": 276.55, "humidity": 22, "pressure": 610}, "wind": {"speed": 10.0, "deg": 250, "gust": 14.0},
    "clouds": {"all": 5}, "dt": 1790712000, "visibility": 10000,
}


# --- entries, fingerprint, add / list ------------------------------------------------

class EntryNormalisationTests(TestCase):
    def test_shapes_are_normalised(self):
        entries = il.normalize_entries({"logs": [
            {"timestamp": "2026-09-29T05:00:00Z", "message": "Dome opened", "level": "INFO"},
            {"time": 1790712000, "msg": "Seeing 0.6\""},
            {"date": "2026-09-29 06:10", "text": "Humidity 90%", "severity": "warning", "sensor": "hum-1"},
            "plain string entry",
            {"unrelated": 1},
            None, "",
        ]})
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[0], {"timestamp": "2026-09-29T05:00:00Z", "message": "Dome opened", "level": "info"})
        self.assertEqual(entries[1]["timestamp"], "2026-09-29T20:00:00Z")
        self.assertEqual(entries[1]["message"], 'Seeing 0.6"')
        self.assertEqual(entries[2]["level"], "warning")
        self.assertEqual(entries[2]["extra"], {"sensor": "hum-1"})
        self.assertEqual(entries[3]["message"], "plain string entry")
        self.assertEqual(entries[4]["message"], '{"unrelated": 1}')
        self.assertEqual(il.normalize_entries(None), [])
        self.assertEqual(il.normalize_entries("just text")[0]["message"], "just text")
        self.assertEqual(il.normalize_entries('[{"message": "from json"}]')[0]["message"], "from json")

    def test_mjd_timestamps(self):
        entries = il.normalize_entries([{"mjd": 61312.5, "message": "x"}])
        self.assertEqual(entries[0]["timestamp"], "2026-09-29T12:00:00Z")

    def test_fingerprint_is_stable_and_specific(self):
        start = datetime.datetime(2026, 9, 29, tzinfo=UTC)
        a = il.fingerprint(1, start, "m", [{"timestamp": "t", "message": "x"}])
        self.assertEqual(a, il.fingerprint(1, start, "m", [{"timestamp": "t", "message": "x", "level": "info"}]))
        self.assertNotEqual(a, il.fingerprint(2, start, "m", [{"timestamp": "t", "message": "x"}]))
        self.assertNotEqual(a, il.fingerprint(1, start, "other", [{"timestamp": "t", "message": "x"}]))

    def test_generic_adapter_entry_extraction(self):
        self.assertEqual(instrument_log_entries([{"message": "a"}, "b", 3]), [{"message": "a"}, {"message": "b"}])
        self.assertEqual(instrument_log_entries({"data": {"logs": [{"message": "a"}]}}), [{"message": "a"}])
        self.assertEqual(instrument_log_entries({"results": [{"msg": "a"}]}), [{"msg": "a"}])
        self.assertEqual(instrument_log_entries({"message": "single"}), [{"message": "single"}])
        self.assertEqual(instrument_log_entries({"nothing": 1}), [])
        with self.assertRaises(FacilityError):
            instrument_log_entries("not json at all")


class InstrumentLogServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("instlog_staff", is_staff=True)
        cls.user = create_test_user("instlog_user", is_staff=False)
        cls.instrument = _stack(cls.staff)

    def test_add_log_dedups_by_fingerprint_and_fills_span(self):
        entries = [{"timestamp": "2026-09-29T05:00:00Z", "message": "Dome opened"},
                   {"timestamp": "2026-09-29T11:30:00Z", "message": "Dome closed"}]
        row, created = il.add_log(self.instrument, self.staff, entries=entries, source=InstrumentLog.SOURCE_API,
                                  source_name="generic")
        self.assertTrue(created)
        self.assertEqual(row.start, datetime.datetime(2026, 9, 29, 5, tzinfo=UTC))
        self.assertEqual(row.end, datetime.datetime(2026, 9, 29, 11, 30, tzinfo=UTC))
        self.assertEqual(row.entry_count, 2)
        self.assertEqual(row.summary, "Dome opened (+1 more)")
        again, created2 = il.add_log(self.instrument, self.user, entries=entries, source=InstrumentLog.SOURCE_API)
        self.assertFalse(created2)
        self.assertEqual(again.pk, row.pk)
        self.assertEqual(InstrumentLog.objects.count(), 1)
        with self.assertRaises(ValueError):
            il.add_log(self.instrument, self.staff, start=timezone.now())

    def test_logs_between_uses_overlap(self):
        d = lambda day, hour=0: datetime.datetime(2026, 9, day, hour, tzinfo=UTC)  # noqa: E731
        il.add_log(self.instrument, self.staff, start=d(1), end=d(3), message="span 1-3")
        il.add_log(self.instrument, self.staff, start=d(10), message="point 10")
        il.add_log(self.instrument, self.staff, start=d(20), end=d(21), message="span 20-21")
        names = lambda qs: sorted(r.message for r in qs)  # noqa: E731
        self.assertEqual(names(il.logs_between(instrument=self.instrument, start=d(2), end=d(11))), ["point 10", "span 1-3"])
        self.assertEqual(names(il.logs_between(instrument=self.instrument, start=d(4), end=d(9))), [])
        self.assertEqual(names(il.logs_between(telescope=self.instrument.telescope, start=d(15))), ["span 20-21"])
        self.assertEqual(names(il.logs_between(instrument=self.instrument, end=d(5))), ["span 1-3"])
        self.assertEqual(len(il.logs_between(instrument=self.instrument)), 3)

    def test_can_add_logs(self):
        self.assertTrue(il.can_add_logs(self.staff))
        self.assertFalse(il.can_add_logs(self.user))
        self.user.user_permissions.add(Permission.objects.get(codename="add_instrumentlog"))
        user = type(self.user).objects.get(pk=self.user.pk)  # permission cache
        self.assertTrue(il.can_add_logs(user))


# --- facility pull --------------------------------------------------------------------

@override_settings(JOB_RUNNER_INLINE=False)
class InstrumentLogPullTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("instlog_pull_staff", is_staff=True)
        cls.instrument = _stack(cls.staff, name="pull")
        cls.allocation = _allocation(cls.staff, cls.instrument,
                                     instrument_log_url="https://facility.example/logs?instrument={instrument}&from={start}&to={end}")

    def _payload(self):
        return {"logs": [{"timestamp": "2026-09-29T05:00:00Z", "message": "Dome opened", "level": "info"},
                         {"timestamp": "2026-09-29T11:30:00Z", "message": "Dome closed"}]}

    def test_base_default_and_capability(self):
        self.assertTrue(get_facility("generic").can("instrument_log"))
        self.assertFalse(get_facility("lco").can("instrument_log"))
        with self.assertRaises(NotImplementedError):
            FacilityAPI().fetch_instrument_log(self.allocation, self.instrument, timezone.now(), timezone.now())
        self.assertEqual(il.pull_allocations(self.instrument), [self.allocation])
        self.assertEqual(il.instruments_with_log_source(), [self.instrument])

    def test_generic_adapter_fills_placeholders_and_sends_token(self):
        adapter = get_facility("generic")
        start, end = datetime.datetime(2026, 9, 29, tzinfo=UTC), datetime.datetime(2026, 9, 30, tzinfo=UTC)
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(200, self._payload())) as get:
            entries = adapter.fetch_instrument_log(self.allocation, self.instrument, start, end)
        self.assertEqual(len(entries), 2)
        url = get.call_args[0][0]
        self.assertIn("instrument=%s" % self.instrument.name, url)
        self.assertIn("from=2026-09-29T00:00:00Z", url)
        self.assertIsNone(get.call_args[1]["params"])
        # without placeholders the window goes into the query string
        self.allocation.default_request_params = {"instrument_log_url": "https://facility.example/logs"}
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(200, [])) as get:
            adapter.fetch_instrument_log(self.allocation, self.instrument, start, end)
        self.assertEqual(get.call_args[1]["params"]["start"], "2026-09-29T00:00:00Z")
        self.assertEqual(get.call_args[1]["params"]["instrument"], self.instrument.name)
        # errors become FacilityError
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(500, text="boom")):
            with self.assertRaises(FacilityError):
                adapter.fetch_instrument_log(self.allocation, self.instrument, start, end)
        self.allocation.default_request_params = {}
        with self.assertRaises(FacilityError):
            adapter.fetch_instrument_log(self.allocation, self.instrument, start, end)

    def test_pull_is_idempotent_and_recorded_as_run(self):
        start, end = datetime.datetime(2026, 9, 29, tzinfo=UTC), datetime.datetime(2026, 9, 30, tzinfo=UTC)
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(200, self._payload())):
            first = il.pull_instrument_logs(self.instrument, start, end, user=self.staff)
            second = il.pull_instrument_logs(self.instrument, start, end, user=self.staff)
        self.assertEqual((first["created"], first["duplicates"]), (1, 0))
        self.assertEqual((second["created"], second["duplicates"]), (0, 1))
        self.assertEqual(InstrumentLog.objects.filter(source=InstrumentLog.SOURCE_API).count(), 1)
        row = InstrumentLog.objects.get()
        self.assertEqual(row.source_name, "generic")
        self.assertEqual(row.start, datetime.datetime(2026, 9, 29, 5, tzinfo=UTC))
        runs = ExternalServiceRun.objects.filter(service__slug=self.allocation.service_slug())
        self.assertEqual(runs.count(), 2)
        self.assertTrue(all(r.status == ExternalServiceRun.STATUS_SUCCEEDED for r in runs))
        self.assertEqual(row.run.request_payload["purpose"], "instrument_log")
        self.assertEqual(row.run.target, self.instrument)

    def test_pull_failure_marks_run_failed(self):
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(503, text="down")):
            result = il.pull_instrument_logs(self.instrument, timezone.now() - datetime.timedelta(days=1), timezone.now())
        self.assertIn("503", result["error"])
        self.assertEqual(result["created"], 0)
        run = ExternalServiceRun.objects.get(uuid=result["run"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("503", run.error)

    def test_pull_without_facility_is_skipped(self):
        other = _stack(self.staff, name="pull-other")
        result = il.pull_instrument_logs(other, timezone.now() - datetime.timedelta(days=1), timezone.now())
        self.assertIn("skipped", result)
        self.assertEqual(ExternalServiceRun.objects.count(), 0)

    def test_job_and_cron(self):
        # cron off by default
        self.assertEqual(InstrumentLogPull().do(), "disabled")
        self.assertEqual(Job.objects.filter(kind=il.PULL_JOB_KIND).count(), 0)
        with override_settings(INSTRUMENT_LOG_PULL_CRON_ENABLED=True, INSTRUMENT_LOG_PULL_HOURS=6):
            self.assertIn("queued job", InstrumentLogPull().do())
            self.assertIn("already queued", InstrumentLogPull().do())
        job = Job.objects.get(kind=il.PULL_JOB_KIND)
        self.assertEqual(job.payload, {"hours": 6})
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(200, self._payload())):
            run_pass(worker_id="test")
        job.refresh_from_db()
        self.assertEqual(job.status, Job.DONE, job.error)
        self.assertEqual(job.result["instruments"], 1)
        self.assertEqual(job.result["created"], 1)
        self.assertEqual(InstrumentLog.objects.count(), 1)


# --- API ------------------------------------------------------------------------------

class InstrumentLogAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("instlog_api_staff", is_staff=True)
        cls.user = create_test_user("instlog_api_user", is_staff=False)
        cls.bot = create_test_user("facility_bot", is_staff=False)
        cls.bot.user_permissions.add(Permission.objects.get(codename="add_instrumentlog"))
        cls.token = Token.objects.create(user=cls.bot)
        cls.instrument = _stack(cls.staff, name="api")
        cls.other = _stack(cls.staff, name="api-other")
        d = lambda day: datetime.datetime(2026, 9, day, 12, tzinfo=UTC)  # noqa: E731
        il.add_log(cls.instrument, cls.staff, start=d(1), message="first")
        il.add_log(cls.instrument, cls.staff, start=d(10), end=d(11), message="second", source=InstrumentLog.SOURCE_API)
        il.add_log(cls.other, cls.staff, start=d(10), message="other instrument")

    def setUp(self):
        self.client = Client()

    def _list(self, **params):
        response = self.client.get("/api/instrumentlogs/", params)
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_list_and_filters(self):
        self.client.force_login(self.user)
        self.assertEqual(len(self._list()), 3)
        rows = self._list(instrument=self.instrument.pk)
        self.assertEqual([r["message"] for r in rows], ["second", "first"])
        self.assertEqual(rows[0]["instrument_name"], self.instrument.name)
        self.assertEqual(rows[0]["telescope_name"], self.instrument.telescope.name)
        self.assertEqual(rows[0]["entries"], [])
        self.assertEqual([r["message"] for r in self._list(instrument=self.instrument.pk, start_after="2026-09-05")], ["second"])
        self.assertEqual([r["message"] for r in self._list(instrument=self.instrument.pk, end_before="2026-09-05")], ["first"])
        self.assertEqual([r["message"] for r in self._list(source="api")], ["second"])
        self.assertEqual(len(self._list(telescope=self.other.telescope.pk)), 1)
        detail = self.client.get("/api/instrumentlogs/%d/" % InstrumentLog.objects.get(message="first").pk)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["source"], "manual")

    def test_anonymous_is_refused(self):
        self.assertIn(self.client.get("/api/instrumentlogs/").status_code, (401, 403))

    def _payload(self, message="Posted via API", **extra):
        data = {"instrument": "http://testserver/api/instruments/%d/" % self.instrument.pk,
                "start": "2026-09-29T04:00:00Z", "message": message,
                "log": {"logs": [{"timestamp": "2026-09-29T04:00:00Z", "message": message}]}}
        data.update(extra)
        return data

    def test_post_requires_staff_or_permission(self):
        self.client.force_login(self.user)
        response = self.client.post("/api/instrumentlogs/", data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.staff)
        response = self.client.post("/api/instrumentlogs/", data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["message"], "Posted via API")
        self.assertEqual(body["entries"][0]["message"], "Posted via API")
        self.assertTrue(body["fingerprint"])
        # the same block again is a 200 with the existing row (idempotent)
        response = self.client.post("/api/instrumentlogs/", data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], body["id"])
        # validation: nothing to log
        response = self.client.post("/api/instrumentlogs/", data=json.dumps(self._payload(message="", log={})),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_post_with_facility_api_token(self):
        response = Client().post("/api/instrumentlogs/", data=json.dumps(self._payload("From the facility bot", source="api")),
                                 content_type="application/json", HTTP_AUTHORIZATION="Token %s" % self.token.key)
        self.assertEqual(response.status_code, 201, response.content)
        row = InstrumentLog.objects.get(message="From the facility bot")
        self.assertEqual(row.created_by, self.bot)
        self.assertEqual(row.source, InstrumentLog.SOURCE_API)
        bad = Client().post("/api/instrumentlogs/", data=json.dumps(self._payload()), content_type="application/json",
                            HTTP_AUTHORIZATION="Token nope")
        self.assertIn(bad.status_code, (401, 403))


# --- pages ------------------------------------------------------------------------------

@override_settings(JOB_RUNNER_INLINE=True)
class InstrumentLogPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("instlog_page_staff", is_staff=True)
        cls.user = create_test_user("instlog_page_user", is_staff=False)
        cls.instrument = _stack(cls.staff, name="page")
        cls.allocation = _allocation(cls.staff, cls.instrument, instrument_log_url="https://facility.example/logs")
        now = timezone.now()
        il.add_log(cls.instrument, cls.staff, start=now - datetime.timedelta(days=2), message="Recent entry",
                   entries=[{"timestamp": (now - datetime.timedelta(days=2)).isoformat(), "message": "Recent entry", "level": "warning"}])
        il.add_log(cls.instrument, cls.staff, start=now - datetime.timedelta(days=40), message="Old entry")

    def setUp(self):
        self.client = Client()
        self.url = reverse("instrument_logs", kwargs={"instrument_id": self.instrument.pk})

    def test_page_default_window_and_date_picker(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)  # login required
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Recent entry", html)
        self.assertNotIn("Old entry", html)
        self.assertIn('id="instlog-start"', html)
        self.assertIn("yse-datepicker", html)
        self.assertNotIn('id="instlog-add-card"', html)  # not staff
        self.assertNotIn('id="instlog-pull-card"', html)
        self.assertIn('data-source="manual"', html)
        self.assertIn('data-level="warning"', html)

    def test_page_custom_range_and_staff_forms(self):
        self.client.force_login(self.staff)
        old = timezone.now() - datetime.timedelta(days=40)
        response = self.client.get(self.url, {"start": (old - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
                                             "end": (old + datetime.timedelta(days=1)).strftime("%Y-%m-%d")})
        html = response.content.decode()
        self.assertIn("Old entry", html)
        self.assertNotIn("Recent entry", html)
        self.assertIn('id="instlog-add-card"', html)
        self.assertIn('id="instlog-pull-card"', html)
        response = self.client.get(self.url, {"start": "garbage", "end": "2026-13-45", "source": "weird"})
        self.assertEqual(response.status_code, 200)

    def test_add_form(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("instrument_log_add", kwargs={"instrument_id": self.instrument.pk}),
                                    {"start": "2026-09-29T04:10", "message": "Dome closed"})
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.staff)
        response = self.client.post(reverse("instrument_log_add", kwargs={"instrument_id": self.instrument.pk}),
                                    {"start": "2026-09-29T04:10", "end": "2026-09-29T05:00", "message": "Dome closed",
                                     "level": "warning"})
        self.assertEqual(response.status_code, 302)
        row = InstrumentLog.objects.get(message="Dome closed")
        self.assertEqual(row.start, datetime.datetime(2026, 9, 29, 4, 10, tzinfo=UTC))
        self.assertEqual(row.end, datetime.datetime(2026, 9, 29, 5, 0, tzinfo=UTC))
        self.assertEqual(row.entries[0]["level"], "warning")
        self.assertEqual(row.source, InstrumentLog.SOURCE_MANUAL)
        # shows up on the page and in the API
        self.assertIn("Dome closed", self.client.get(self.url, {"start": "2026-09-28", "end": "2026-09-30"}).content.decode())
        api = self.client.get("/api/instrumentlogs/", {"instrument": self.instrument.pk, "start_after": "2026-09-29"})
        self.assertIn("Dome closed", [r["message"] for r in api.json()["results"]])
        # missing message
        response = self.client.post(reverse("instrument_log_add", kwargs={"instrument_id": self.instrument.pk}),
                                    {"start": "2026-09-29T04:10", "message": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InstrumentLog.objects.filter(message="").count(), 0)

    def test_pull_button_runs_inline(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("instrument_log_pull", kwargs={"instrument_id": self.instrument.pk})).status_code, 403)
        self.client.force_login(self.staff)
        payload = [{"timestamp": "2026-09-29T05:00:00Z", "message": "Pulled entry"}]
        with mock.patch("YSE_App.facilities.generic.requests.get", return_value=_FakeResponse(200, payload)):
            response = self.client.post(reverse("instrument_log_pull", kwargs={"instrument_id": self.instrument.pk}),
                                        {"start": "2026-09-29", "end": "2026-09-29"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(InstrumentLog.objects.filter(message="", source=InstrumentLog.SOURCE_API).exists())
        self.assertIn("Pulled entry", response.content.decode())

    def test_telescope_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("telescope_detail", kwargs={"telescope_id": self.instrument.telescope.pk}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(self.instrument.name, html)
        self.assertIn('id="telescope-weather-unconfigured"', html)
        self.assertIn("Recent entry", html)
        self.assertEqual(self.client.get(reverse("telescope_detail", kwargs={"telescope_id": 999999})).status_code, 404)

    def test_admin_pages(self):
        self.client.force_login(create_test_user("instlog_admin", is_staff=True, is_superuser=True))
        self.assertEqual(self.client.get("/admin/YSE_App/instrumentlog/").status_code, 200)
        self.assertEqual(self.client.get("/admin/YSE_App/instrumentlog/%d/change/" % InstrumentLog.objects.first().pk).status_code, 200)
        self.assertEqual(self.client.get("/admin/YSE_App/telescope/%d/change/" % self.instrument.telescope.pk).status_code, 200)


# --- weather ------------------------------------------------------------------------------

class WeatherNormalisationTests(TestCase):
    def test_open_meteo(self):
        w = wx.normalize_weather(OPEN_METEO)
        self.assertEqual(w["temperature_c"], 3.4)
        self.assertEqual(w["humidity_pct"], 22)
        self.assertEqual(w["wind_speed_ms"], 10.0)  # km/h converted
        self.assertEqual(w["wind_direction_deg"], 250)
        self.assertEqual(w["cloud_cover_pct"], 5)
        self.assertEqual(w["description"], "Mainly clear")
        self.assertEqual(w["observed_at"], "2026-09-29T20:00:00Z")
        self.assertEqual(w["pressure_hpa"], 610.2)

    def test_openweathermap(self):
        w = wx.normalize_weather(OPENWEATHER)
        self.assertAlmostEqual(w["temperature_c"], 3.4, places=1)
        self.assertEqual(w["wind_speed_ms"], 10.0)
        self.assertEqual(w["wind_gust_ms"], 14.0)
        self.assertEqual(w["description"], "Clear sky")
        self.assertEqual(w["observed_at"], "2026-09-29T20:00:00Z")
        self.assertEqual(w["visibility_m"], 10000)

    def test_flat_and_garbage(self):
        w = wx.normalize_weather({"temperature": "2.5", "humidity": 40, "conditions": "Fog", "time": "2026-09-29T20:00:00Z"})
        self.assertEqual((w["temperature_c"], w["humidity_pct"], w["description"]), (2.5, 40, "Fog"))
        self.assertEqual(wx.normalize_weather(["nope"])["temperature_c"], None)
        self.assertEqual(wx.normalize_weather(None)["description"], "")


@override_settings(WEATHER_CACHE_MINUTES=10, JOB_RUNNER_INLINE=False)
class WeatherServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("weather_staff", is_staff=True)
        cls.user = create_test_user("weather_user", is_staff=False)
        cls.instrument = _stack(cls.staff, name="weather")
        cls.telescope = cls.instrument.telescope
        cls.telescope.latitude, cls.telescope.longitude, cls.telescope.elevation = 19.82, -155.47, 4205.0
        cls.telescope.weather_url = "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m"
        cls.telescope.skycam_url = "https://skycam.example/latest.jpg"
        cls.telescope.weather_link = "https://weather.example/summit"
        cls.telescope.save()
        cls.bare = _stack(cls.staff, name="weather-bare").telescope

    def setUp(self):
        Telescope.objects.filter(pk=self.telescope.pk).update(weather={}, weather_fetched_at=None)
        self.telescope.refresh_from_db()

    def test_render_url_fills_coordinates(self):
        self.assertEqual(wx.render_url(self.telescope),
                         "https://api.open-meteo.com/v1/forecast?latitude=19.82&longitude=-155.47&current=temperature_2m")
        self.assertEqual(wx.render_url(self.bare), "")

    def test_fetch_caches_and_records_run(self):
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(200, OPEN_METEO)) as get:
            snap = wx.get_weather(self.telescope, user=self.user)
            self.assertEqual(get.call_count, 1)
            self.assertIn("latitude=19.82", get.call_args[0][0])
            again = wx.get_weather(self.telescope, user=self.user)  # fresh cache: no second call
            self.assertEqual(get.call_count, 1)
        self.assertEqual(snap["temperature_c"], 3.4)
        self.assertEqual(snap["provider"], "open-meteo")
        self.assertEqual(snap["source"], "api.open-meteo.com")
        self.assertEqual(again["temperature_c"], 3.4)
        self.telescope.refresh_from_db()
        self.assertTrue(wx.is_fresh(self.telescope))
        self.assertEqual(self.telescope.weather["description"], "Mainly clear")
        run = ExternalServiceRun.objects.get(service__slug=wx.SERVICE_SLUG)
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(run.target, self.telescope)
        self.assertEqual(run.created_by, self.user)
        self.assertEqual(ExternalService.objects.get(slug=wx.SERVICE_SLUG).kind, ExternalService.KIND_GENERIC)
        # stale cache is refetched; allow_fetch=False serves the cache regardless
        Telescope.objects.filter(pk=self.telescope.pk).update(weather_fetched_at=timezone.now() - datetime.timedelta(minutes=30))
        self.telescope.refresh_from_db()
        self.assertFalse(wx.is_fresh(self.telescope))
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(200, OPENWEATHER)) as get:
            self.assertEqual(wx.get_weather(self.telescope, allow_fetch=False)["provider"], "open-meteo")
            self.assertEqual(get.call_count, 0)
            self.assertEqual(wx.get_weather(self.telescope)["provider"], "openweathermap")
            self.assertEqual(get.call_count, 1)
        self.assertIsNone(wx.get_weather(self.bare))

    def test_failure_keeps_last_snapshot(self):
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(200, OPEN_METEO)):
            wx.fetch_weather(self.telescope)
        import requests as _requests

        with mock.patch("YSE_App.services.weather.requests.get", side_effect=_requests.ConnectionError("CONNECT 403")):
            snap = wx.fetch_weather(self.telescope)
        self.assertIn("CONNECT 403", snap["error"])
        self.assertEqual(snap["temperature_c"], 3.4)  # kept
        self.telescope.refresh_from_db()
        self.assertEqual(self.telescope.weather["temperature_c"], 3.4)
        self.assertIn("error", self.telescope.weather)
        statuses = sorted(ExternalServiceRun.objects.filter(service__slug=wx.SERVICE_SLUG).values_list("status", flat=True))
        self.assertEqual(statuses, [ExternalServiceRun.STATUS_FAILED, ExternalServiceRun.STATUS_SUCCEEDED])
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(500, text="oops")):
            self.assertIn("500", wx.fetch_weather(self.telescope)["error"])
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(200, None, text="<html>")):
            self.assertIn("ValueError", wx.fetch_weather(self.telescope)["error"])

    def test_refresh_job_and_cron(self):
        self.assertEqual(WeatherRefresh().do(), "disabled")
        self.assertEqual(Job.objects.filter(kind=wx.REFRESH_JOB_KIND).count(), 0)
        with override_settings(WEATHER_REFRESH_CRON_ENABLED=True):
            self.assertIn("queued job", WeatherRefresh().do())
            self.assertIn("already queued", WeatherRefresh().do())
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(200, OPEN_METEO)) as get:
            run_pass(worker_id="test")
        self.assertEqual(get.call_count, 1)  # only the configured telescope
        job = Job.objects.get(kind=wx.REFRESH_JOB_KIND)
        self.assertEqual(job.status, Job.DONE, job.error)
        self.assertEqual(job.result["telescopes"], 1)
        self.telescope.refresh_from_db()
        self.assertTrue(wx.is_fresh(self.telescope))

    def test_widget_context(self):
        ctx = wx.widget_context(self.telescope)
        self.assertTrue(ctx["has_weather"] and ctx["has_skycam"])
        self.assertIsNone(ctx["weather"])
        self.assertIn("skycam_refresh_seconds", ctx)
        bare = wx.widget_context(self.bare)
        self.assertFalse(bare["has_weather"] or bare["has_skycam"] or bare["weather_link"])


class WeatherWidgetPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("wxpage_staff", is_staff=True)
        cls.user = create_test_user("wxpage_user", is_staff=False)
        cls.instrument = _stack(cls.staff, name="wxpage")
        cls.telescope = cls.instrument.telescope
        cls.telescope.weather_url = "https://weather.example/current.json?lat={lat}&lon={lon}"
        cls.telescope.skycam_url = "https://skycam.example/latest.jpg"
        cls.telescope.save()
        audit = audit_fields(cls.staff)
        cls.obs_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        night_type, _ = ClassicalNightType.objects.get_or_create(name="Full", defaults=audit)
        cls.resource = ClassicalResource.objects.create(
            telescope=cls.telescope, begin_date_valid=cls.obs_date - datetime.timedelta(days=3),
            end_date_valid=cls.obs_date + datetime.timedelta(days=3), **audit)
        cls.night = ClassicalObservingDate.objects.create(resource=cls.resource, night_type=night_type, obs_date=cls.obs_date, **audit)
        cls.too = ToOResource.objects.create(
            telescope=cls.telescope, begin_date_valid=cls.obs_date - datetime.timedelta(days=3),
            end_date_valid=cls.obs_date + datetime.timedelta(days=3), **audit)
        il.add_log(cls.instrument, cls.staff, start=timezone.now() - datetime.timedelta(days=1), message="Mirror recoated")
        cls.transient = create_minimal_transient(cls.staff, name="2026wx", ra=10.0, dec=-5.0)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_telescope_page_shows_widget_without_fetching(self):
        with mock.patch("YSE_App.services.weather.requests.get") as get:
            response = self.client.get(reverse("telescope_detail", kwargs={"telescope_id": self.telescope.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_count, 0)  # page render never blocks on the network
        html = response.content.decode()
        self.assertIn('id="yse-weather-%d"' % self.telescope.pk, html)
        self.assertIn('data-status="pending"', html)
        self.assertIn("https://skycam.example/latest.jpg", html)
        self.assertIn("weather-widget.js", html)
        self.assertNotIn('id="telescope-weather-unconfigured"', html)

    def test_fragment_fetches_and_renders(self):
        url = reverse("telescope_weather_fragment", kwargs={"telescope_id": self.telescope.pk})
        with mock.patch("YSE_App.services.weather.requests.get", return_value=_FakeResponse(200, OPEN_METEO)) as get:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertIn("Mainly clear", html)
            self.assertIn('data-field="temperature_c"', html)
            self.assertIn("3.4", html)
            self.assertIn('data-status="fresh"', html)
            self.assertEqual(get.call_count, 1)
            # cached now
            self.client.get(url)
            self.assertEqual(get.call_count, 1)
            # staff may force; plain users may not
            self.client.get(url + "?refresh=1")
            self.assertEqual(get.call_count, 1)
            self.client.force_login(self.staff)
            self.client.get(url + "?refresh=1")
            self.assertEqual(get.call_count, 2)
            data = self.client.get(url + "?format=json").json()
        self.assertEqual(data["weather"]["temperature_c"], 3.4)
        self.assertEqual(data["skycam_url"], "https://skycam.example/latest.jpg")
        self.assertFalse(data["stale"])

    def test_fragment_error_is_graceful(self):
        import requests as _requests

        url = reverse("telescope_weather_fragment", kwargs={"telescope_id": self.telescope.pk})
        with mock.patch("YSE_App.services.weather.requests.get", side_effect=_requests.ConnectionError("CONNECT 403")):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Weather unavailable", html)
        self.assertIn('data-status="error"', html)
        self.assertIn("skycam.example", html)  # SkyCam still shown

    def test_observing_night_page_has_widget_and_logs_box(self):
        url = reverse("observing_night", kwargs={"telescope": self.telescope.name.replace(" ", "_"),
                                                  "obs_date": self.obs_date.strftime("%Y-%m-%d"), "pi_name": "None"})
        with mock.patch("YSE_App.services.weather.requests.get") as get:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_count, 0)
        html = response.content.decode()
        self.assertIn('id="yse-weather-%d"' % self.telescope.pk, html)
        self.assertIn('id="yse-instrument-logs-box"', html)
        self.assertIn("Mirror recoated", html)

    def test_resources_fragment_shows_compact_widget(self):
        url = reverse("transient_detail_resources_fragment", kwargs={"transient_id": self.transient.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("yse-resource-weather", html)
        self.assertIn("yse-weather-compact", html)
        self.assertEqual(html.count('id="yse-weather-%d"' % self.telescope.pk), 1)  # one widget per telescope

    def test_unconfigured_telescope_renders_nothing(self):
        bare = _stack(self.staff, name="wxpage-bare").telescope
        response = self.client.get(reverse("telescope_weather_fragment", kwargs={"telescope_id": bare.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("yse-weather-readings", response.content.decode())
        # API surfaces the new telescope fields
        self.client.force_login(self.staff)
        data = self.client.get("/api/telescopes/%d/" % self.telescope.pk).json()
        self.assertEqual(data["skycam_url"], "https://skycam.example/latest.jpg")
