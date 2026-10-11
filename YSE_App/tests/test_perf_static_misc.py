"""Tests for the static/misc performance group (#481-#490): finder charts, prune commands, settings."""

import datetime
import io
import os
import struct
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import view_utils
from YSE_App.models import Transient
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_speed_regressions import _classical_night, _request_followup


def _png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    return struct.unpack(">II", head[16:24])


def _fake_finder_module():
    """Stand-in for util.mkFinderChart.finder(): no network, no image services."""
    finder = mock.MagicMock()
    finder.return_value.add_options.return_value.parse_args.return_value = (SimpleNamespace(), [])
    finder.return_value.mkChart.return_value = (
        mock.MagicMock(),
        [{"id": "star1", "ra": "03:20:00.0", "dec": "+01:00:00.0", "mag": 15.0, "ra_off": 1.5, "dec_off": -2.0}],
    )
    return finder


class FinderChartPathTests(SimpleTestCase):
    def test_paths_are_inside_the_configured_directory_and_sanitised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(FINDER_CHART_DIR=tmp):
                _d, txt, png = view_utils.finder_chart_paths("2026abc")
                self.assertEqual(os.path.dirname(os.path.dirname(png)), os.path.realpath(tmp))
                self.assertTrue(os.path.isdir(os.path.dirname(png)))
                self.assertTrue(txt.endswith("2026abc.offsetstars.txt"))
                _d, _t, evil_png = view_utils.finder_chart_paths("../../etc/passwd")
                self.assertEqual(os.path.dirname(os.path.dirname(evil_png)), os.path.realpath(tmp))
                self.assertNotIn("..", os.path.relpath(evil_png, tmp).split(os.sep))

    def test_static_relpath_matches_written_name(self):
        self.assertEqual(
            view_utils.finder_chart_static_relpath("2026abc"),
            "YSE_App/images/findercharts/2026abc/2026abc.finder.png",
        )


class DownloadTargetsAndFindersTests(TestCase):
    """#483: finderchart_noview() used to be called without its transient_id (TypeError)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("finder_download_user")
        cls.obs_date = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        cls.night = _classical_night(cls.user, tag="finder-night", obs_date=cls.obs_date)
        cls.transient = create_minimal_transient(cls.user, name="fnd0", ra=50.0, dec=1.0)
        attach_synthetic_photometry(cls.user, cls.transient, n_points=2)
        _request_followup(cls.user, cls.transient, resource=cls.night.resource,
                          classical_resource=cls.night.resource)

    def test_view_passes_the_transient_and_writes_a_small_png(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        client = Client()
        client.force_login(self.user)
        url = reverse("download_targets_and_finders", kwargs={
            "telescope": self.night.resource.telescope.name.replace(" ", "_"),
            "obs_date": self.obs_date.strftime("%Y-%m-%d"),
        })
        with tempfile.TemporaryDirectory() as tmp, override_settings(FINDER_CHART_DIR=tmp), \
                iers_offline(), \
                mock.patch("YSE_App.util.mkFinderChart.finder", _fake_finder_module()):
            response = client.get(url)
            png = os.path.join(os.path.realpath(tmp), "fnd0", "fnd0.finder.png")
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            self.assertIn("fnd0", body)
            self.assertTrue(os.path.isfile(png))
            # 6.4 x 4.8 in at FINDER_CHART_DPI (was 1000 dpi = 6400 x 4800 px)
            width, height = _png_size(png)
            self.assertEqual(view_utils.FINDER_CHART_DPI, 150)
            self.assertLess(width, 2000)  # not the old ~6400 px wide file
            self.assertGreater(width, 400)


class PruneCommandTests(TestCase):
    def test_prune_finder_charts_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(FINDER_CHART_DIR=tmp):
            old = os.path.join(tmp, "old1")
            new = os.path.join(tmp, "new1")
            os.makedirs(old)
            os.makedirs(new)
            for d in (old, new):
                with open(os.path.join(d, "x.finder.png"), "wb") as fh:
                    fh.write(b"0" * 10)
            ts = time.time() - 100 * 86400
            os.utime(os.path.join(old, "x.finder.png"), (ts, ts))

            out = io.StringIO()
            call_command("prune_finder_charts", "--older-than-days", "30", stdout=out)
            self.assertIn("dry run", out.getvalue())
            self.assertTrue(os.path.isdir(old))

            call_command("prune_finder_charts", "--older-than-days", "30", "--execute", stdout=io.StringIO())
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.isdir(new))

    def test_prune_commands_require_an_age(self):
        with self.assertRaises(CommandError):
            call_command("prune_finder_charts")
        with self.assertRaises(CommandError):
            call_command("prune_auditlog")
        with self.assertRaises(CommandError):
            call_command("prune_auditlog", "--older-than-days", "0")

    def test_prune_auditlog_dry_run_then_batched_delete(self):
        from auditlog.models import LogEntry

        ct = ContentType.objects.get_for_model(Transient)
        LogEntry.objects.all().delete()
        for i in range(5):
            LogEntry.objects.create(content_type=ct, object_pk=str(i), object_id=i,
                                    object_repr="t%d" % i, action=0, changes="{}")
        old = timezone.now() - datetime.timedelta(days=400)
        LogEntry.objects.filter(object_id__lt=3).update(timestamp=old)

        out = io.StringIO()
        call_command("prune_auditlog", "--older-than-days", "365", "--dry-run", stdout=out)
        self.assertIn("would delete 3", out.getvalue())
        self.assertEqual(LogEntry.objects.count(), 5)

        out = io.StringIO()
        call_command("prune_auditlog", "--older-than-days", "365", "--batch-size", "2",
                     "--model", "YSE_App.Transient", stdout=out)
        self.assertIn("deleted 3", out.getvalue())
        self.assertEqual(LogEntry.objects.count(), 2)


class SettingsTests(SimpleTestCase):
    def test_hashed_static_storage_is_lenient_and_off_by_default(self):
        from django.conf import settings
        from YSE_App.storage import LenientManifestStaticFilesStorage

        self.assertFalse(LenientManifestStaticFilesStorage.manifest_strict)
        if not os.environ.get("YSE_HASHED_STATIC"):
            self.assertNotIn("Manifest", settings.STATICFILES_STORAGE)

    def test_logging_configured_and_cached_db_only_with_redis(self):
        from django.conf import settings

        self.assertIn("console", settings.LOGGING["handlers"])
        if "redis" not in settings.CACHES["default"]["BACKEND"].lower():
            self.assertNotEqual(
                getattr(settings, "SESSION_ENGINE", ""), "django.contrib.sessions.backends.cached_db"
            )
