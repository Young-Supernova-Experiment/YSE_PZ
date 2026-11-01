"""https follow-up (#441): URL helpers and ``manage.py https_urls``."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from YSE_App.common.url_utils import https_url, is_https_capable_host, public_base_url, url_host
from YSE_App.models import ExternalService, Observatory, Telescope


class HttpsUrlHelperTests(SimpleTestCase):
    def test_own_host_and_known_service_upgraded(self):
        self.assertEqual(https_url("http://ziggy.ucolick.org/yse/x/?a=1#f"), "https://ziggy.ucolick.org/yse/x/?a=1#f")
        self.assertEqual(https_url("http://sub.ziggy.ucolick.org/a"), "https://sub.ziggy.ucolick.org/a")
        self.assertEqual(https_url("HTTP://ps1images.stsci.edu/cgi-bin/x"), "https://ps1images.stsci.edu/cgi-bin/x")
        self.assertEqual(https_url("http://www.wis-tns.org/object/2024a"), "https://www.wis-tns.org/object/2024a")

    def test_placeholders_survive(self):
        url = "http://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        self.assertEqual(https_url(url), "https" + url[4:])

    def test_unknown_and_http_only_hosts_untouched(self):
        for url in (
            "http://example.com/a",
            "http://skyvision.caltech.edu/ztf/msip/nightly_summary",
            "http://schedule.ztf.uw.edu/x.json",
            "http://datastore.ipp.ifa.hawaii.edu/pstampresults/",
            "http://notziggy.ucolick.org.evil.example/a",
            "http://evilziggy.ucolick.org/a",
        ):
            self.assertEqual(https_url(url), url)

    def test_ports_non_http_and_junk_untouched(self):
        for value in ("http://ziggy.ucolick.org:8000/a", "https://ziggy.ucolick.org/a", "ftp://ziggy.ucolick.org/a",
                      "", "ziggy.ucolick.org", "http://[bad", None):
            self.assertEqual(https_url(value), value)

    def test_host_helpers(self):
        self.assertEqual(url_host("http://Ziggy.UCOLICK.org/a"), "ziggy.ucolick.org")
        self.assertIsNone(url_host("mailto:a@b.c"))
        self.assertTrue(is_https_capable_host("ziggy.ucolick.org"))
        self.assertFalse(is_https_capable_host("skyvision.caltech.edu"))
        self.assertFalse(is_https_capable_host(None))

    @override_settings(YSE_PUBLIC_BASE_URL="https://example.test/yse", DEBUG=False)
    def test_public_base_url_setting_wins(self):
        self.assertEqual(public_base_url(), "https://example.test/yse/")

    @override_settings(YSE_PUBLIC_BASE_URL="", DEBUG=False)
    def test_public_base_url_production_default_is_https(self):
        self.assertTrue(public_base_url().startswith("https://"))

    @override_settings(YSE_PUBLIC_BASE_URL="", DEBUG=True)
    def test_public_base_url_debug_default_is_local(self):
        self.assertTrue(public_base_url().startswith("http://127.0.0.1"))


class HttpsUrlsCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("https_cmd_user", password="x")
        audit = {"created_by": cls.user, "modified_by": cls.user}
        obs = Observatory.objects.create(name="HttpsObs", utc_offset=0, tz_name="UTC", **audit)
        cls.tel = Telescope.objects.create(
            observatory=obs, name="HttpsTel", latitude=0.0, longitude=0.0, elevation=0.0,
            weather_url="http://api.open-meteo.com/v1/forecast?latitude={lat}",
            weather_link="http://example.com/weather",
            skycam_url="https://already.example/cam.jpg",
            **audit)
        cls.svc = ExternalService.objects.create(
            name="Svc", slug="https-svc", base_url="http://ziggy.ucolick.org/yse/hook/", **audit)
        cls.svc_port = ExternalService.objects.create(
            name="SvcPort", slug="https-svc-port", base_url="http://ziggy.ucolick.org:8000/hook/", **audit)

    def run_cmd(self, *args):
        out = StringIO()
        call_command("https_urls", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_is_default_and_changes_nothing(self):
        out = self.run_cmd()
        self.assertIn("dry run: 2 URLs would be rewritten, 2 left on http", out)
        self.assertIn("Telescope.weather_url: 1 rewritable", out)
        self.assertIn("api.open-meteo.com", out)
        self.assertIn("Re-run with --apply", out)
        self.tel.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertTrue(self.tel.weather_url.startswith("http://"))
        self.assertTrue(self.svc.base_url.startswith("http://"))
        self.assertEqual(self.run_cmd("--dry-run"), out)

    def test_apply_rewrites_only_known_hosts_without_ports(self):
        out = self.run_cmd("--apply")
        self.assertIn("applied: 2 URLs rewritten, 2 left on http", out)
        self.tel.refresh_from_db()
        self.svc.refresh_from_db()
        self.svc_port.refresh_from_db()
        self.assertEqual(self.tel.weather_url, "https://api.open-meteo.com/v1/forecast?latitude={lat}")
        self.assertEqual(self.tel.weather_link, "http://example.com/weather")
        self.assertEqual(self.tel.skycam_url, "https://already.example/cam.jpg")
        self.assertEqual(self.svc.base_url, "https://ziggy.ucolick.org/yse/hook/")
        self.assertEqual(self.svc_port.base_url, "http://ziggy.ucolick.org:8000/hook/")

    def test_apply_is_idempotent_and_keeps_modified_date(self):
        before = ExternalService.objects.get(pk=self.svc.pk).modified_date
        self.run_cmd("--apply")
        after = ExternalService.objects.get(pk=self.svc.pk).modified_date
        self.assertEqual(before, after)
        self.assertIn("applied: 0 URLs rewritten", self.run_cmd("--apply"))

    def test_apply_and_dry_run_together_refused(self):
        with self.assertRaises(CommandError):
            self.run_cmd("--apply", "--dry-run")
