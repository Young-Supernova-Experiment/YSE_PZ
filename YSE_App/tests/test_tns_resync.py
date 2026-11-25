"""TNS re-sync and backfill (#438), with a fake TNS client (no network)."""

from __future__ import annotations

import copy
import datetime
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.common.tns_photometry_map import TNS_OBS_GROUP_NAME
from YSE_App.jobs import JobRetry
from YSE_App.models import (
    ExternalServiceRun,
    ObservationGroup,
    Transient,
    TransientClass,
    TransientPhotData,
    TransientSpectrum,
)
from YSE_App.models.job_models import Job
from YSE_App.services import tns_resync
from YSE_App.sharing import tns
from YSE_App.tests.fixtures_minimal import (
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)

SERVICE_USERNAME = "tns_resync_cron"
SPEC_URL = "https://tns.invalid/spectra/2026rsa_spec.txt"
SPEC_TEXT = "# a TNS ascii spectrum\nEXPTIME = 300\n4000.0 1.0e-16\n4005.0 1.1e-16\n4010.0 nan\n4015.0 1.2e-16\n"


def _point(date, mag, *, inst="ZTF-Cam", filt="r", rid=None):
    p = {"instrument": {"name": inst}, "filters": {"name": filt}, "flux_unit": {"name": "ABMag"},
         "flux": mag, "fluxerr": 0.1, "obsdate": date, "observer": "Robot"}
    if rid:
        p["report_id"] = rid
    return p


def _object(name, *, cls="SN Ia", z=0.031, points=(), spectra=(), ra=150.0, dec=2.0):
    return {
        "objname": name, "name_prefix": "SN", "radeg": ra, "decdeg": dec,
        "discoverydate": "2026-05-30 10:00:00", "redshift": z, "object_type": {"name": cls},
        "reporting_group": {"group_name": "ZTF"}, "internal_names": "",
        "photometry": list(points), "spectra": list(spectra),
    }


class FakeTNS:
    """Stands in for ``tns.TNSClient``: objects by name, file texts by URL, a public-since list."""

    def __init__(self, objects, files=None, public=()):
        self.objects = objects
        self.files = files or {}
        self.public = list(public)
        self.calls = []

    def get_object(self, name, photometry=False, spectra=False):
        self.calls.append(("get", name))
        return copy.deepcopy(self.objects.get(name, {}))

    def search_query(self, query):
        self.calls.append(("search", query.get("page")))
        page, size = int(query.get("page") or 0), int(query["num_page"])
        return [{"objname": n} for n in self.public[page * size:(page + 1) * size]]

    def get_file_text(self, url):
        self.calls.append(("file", url))
        return self.files[url]


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], SHARING_TNS_REQUEST_INTERVAL_SECONDS=0,
                   UPLOAD_GROUP_CHECKS_ENFORCED=True)
class TnsResyncTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cron = create_test_user(SERVICE_USERNAME, is_staff=True)
        ensure_transient_statuses(cls.cron)
        Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        for name in ("Unknown", "ZTF"):
            ObservationGroup.objects.get_or_create(name=name, defaults={"created_by": cls.cron,
                                                                         "modified_by": cls.cron})
        cls.spec_group, cls.spec_inst, _band = create_instrument_stack(cls.cron, obs_group_name="tns-spec-group")
        for name in ("SN Ia", "SN II"):
            TransientClass.objects.get_or_create(name=name, defaults={"created_by": cls.cron,
                                                                      "modified_by": cls.cron})
        cls.transient = create_minimal_transient(cls.cron, name="2026rsa", obs_group_name="tns-resync-survey")

    def _spectrum(self, date="2026-06-03 05:00:00"):
        return {"obsdate": date, "instrument": {"name": self.spec_inst.name},
                "source_group_name": self.spec_group.name, "asciifile": SPEC_URL, "report_id": 9}

    def _first_reply(self):
        return _object("2026rsa", points=[_point("2026-06-01 12:00:00", 18.5, rid=1)])

    def _second_reply(self):
        reply = self._first_reply()
        reply["photometry"].append(_point("2026-06-04 12:00:00", 18.1, rid=2))
        reply["spectra"].append(self._spectrum())
        return reply

    def _counts(self):
        return (TransientPhotData.objects.filter(photometry__transient=self.transient).count(),
                TransientSpectrum.objects.filter(transient=self.transient).count())


class ResyncObjectTests(TnsResyncTestBase):
    def test_adds_classification_spectrum_and_photometry_and_is_idempotent(self):
        first = FakeTNS({"2026rsa": self._first_reply()})
        result = tns_resync.resync_object(self.transient, dry_run=False, client=first)
        self.assertEqual(result.action, "updated")
        self.assertEqual(result.new_photometry, 1)
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.TNS_spec_class, "SN Ia")
        self.assertEqual(self.transient.best_spec_class.name, "SN Ia")
        self.assertAlmostEqual(self.transient.redshift, 0.031)
        self.assertEqual(self.transient.redshift_source, "TNS")
        self.assertEqual(result.classification["tns_class"], "SN Ia")

        # TNS later gets a new point and a classification spectrum
        later = FakeTNS({"2026rsa": self._second_reply()}, files={SPEC_URL: SPEC_TEXT})
        result = tns_resync.resync_object(self.transient, dry_run=False, client=later)
        self.assertEqual((result.action, result.new_photometry, result.new_spectra), ("updated", 1, 1))
        self.assertEqual(self._counts(), (2, 1))

        points = TransientPhotData.objects.filter(photometry__transient=self.transient)
        self.assertEqual({p.photometry.obs_group.name for p in points}, {TNS_OBS_GROUP_NAME})
        for point in points:
            self.assertEqual(set(point.photometry.groups.values_list("name", flat=True)),
                             {PUBLIC_COLLABORATION_GROUP_NAME})
            self.assertEqual(point.photometry.reference[:16], "TNS SN 2026rsa (")
        spectrum = TransientSpectrum.objects.get(transient=self.transient)
        self.assertEqual(set(spectrum.groups.values_list("name", flat=True)), {PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertEqual(spectrum.reference, "TNS SN 2026rsa (report 9)")
        # stored as arrays (#492); the NaN pixel is dropped
        from YSE_App.common import spectrum_arrays
        self.assertEqual(spectrum_arrays.pixel_counts([spectrum.id]), {spectrum.id: 3})

        # a second run with the same TNS reply changes nothing and downloads nothing
        again = FakeTNS({"2026rsa": self._second_reply()}, files={})
        result = tns_resync.resync_object(self.transient, dry_run=False, client=again)
        self.assertEqual(result.action, "unchanged")
        self.assertFalse(result.changed)
        self.assertEqual(self._counts(), (2, 1))
        self.assertNotIn("file", [c[0] for c in again.calls])

        runs = ExternalServiceRun.objects.filter(service__slug="tns", transient=self.transient)
        self.assertEqual(runs.count(), 3)
        tns_resync.resync_object(self.transient, dry_run=False, client=again)
        self.assertEqual(runs.count(), 3)  # a no-change re-sync updates the previous no-change run
        latest = runs.order_by("-finished_at").first()
        self.assertEqual(latest.status, ExternalServiceRun.STATUS_SUCCEEDED)
        self.assertEqual(latest.result["checks"], 2)

    def test_dry_run_writes_nothing(self):
        client = FakeTNS({"2026rsa": self._second_reply()}, files={})
        result = tns_resync.resync_object("2026rsa", dry_run=True, client=client)
        self.assertEqual((result.action, result.new_photometry, result.new_spectra), ("updated", 2, 1))
        self.assertIn("would update", result.summary())
        self.transient.refresh_from_db()
        self.assertIsNone(self.transient.TNS_spec_class)
        self.assertEqual(self._counts(), (0, 0))
        self.assertFalse(ExternalServiceRun.objects.exists())
        self.assertNotIn("file", [c[0] for c in client.calls])

    def test_our_classification_and_redshift_are_kept(self):
        ours = TransientClass.objects.get(name="SN II")
        Transient.objects.filter(pk=self.transient.pk).update(best_spec_class=ours, redshift=0.05)
        result = tns_resync.resync_object(self.transient, dry_run=False, client=FakeTNS({"2026rsa": self._first_reply()}))
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.TNS_spec_class, "SN Ia")
        self.assertEqual(self.transient.best_spec_class.name, "SN II")
        self.assertEqual(self.transient.redshift, 0.05)
        self.assertTrue(result.classification["differs_from_ours"])
        self.assertFalse(result.classification["best_class_set"])
        self.assertNotIn("redshift", result.fields)

    def test_tns_class_replaces_an_earlier_tns_class(self):
        old = TransientClass.objects.get(name="SN II")
        Transient.objects.filter(pk=self.transient.pk).update(best_spec_class=old, TNS_spec_class="SN II")
        tns_resync.resync_object(self.transient, dry_run=False, client=FakeTNS({"2026rsa": self._first_reply()}))
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.best_spec_class.name, "SN Ia")

    def test_unknown_object_is_recorded_as_not_found(self):
        result = tns_resync.resync_object(self.transient, dry_run=False, client=FakeTNS({}))
        self.assertEqual(result.action, "not_found")
        run = ExternalServiceRun.objects.get(service__slug="tns", transient=self.transient)
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)

    def test_rate_limit_propagates_and_records_nothing(self):
        client = mock.Mock()
        client.get_object.side_effect = tns.TNSRateLimited("slow down", retry_after=30)
        with self.assertRaises(tns.TNSRateLimited):
            tns_resync.resync_object(self.transient, dry_run=False, client=client)
        self.assertFalse(ExternalServiceRun.objects.exists())

    @override_settings(API_SERVICE_USERNAMES=["tns_resync_nobody"])
    def test_upload_checks_apply(self):
        create_test_user("tns_resync_nobody", is_staff=False)
        with mock.patch("YSE_App.sharing.tns.system_user", return_value=None):
            with self.assertRaises(tns_resync.ResyncError):
                tns_resync.resync_object(self.transient, dry_run=False, client=FakeTNS({}))


@mock.patch("YSE_App.data_ingest.TNS_uploads.get_ps_score", return_value=None)
class GapImportTests(TnsResyncTestBase):
    def test_imports_an_object_reported_in_the_gap(self, _ps):
        gap = _object("2026gap", cls="SN II", ra=210.0, dec=-10.0,
                      points=[_point("2026-06-02 06:00:00", 19.2), _point("2026-06-03 06:00:00", 19.0)])
        client = FakeTNS({"2026gap": gap}, public=["2026rsa", "2026gap"])
        since = timezone.now() - datetime.timedelta(days=10)

        dry = tns_resync.import_missing(since, dry_run=True, client=client)
        self.assertEqual([(r.name, r.action, r.new_photometry) for r in dry], [("2026gap", "imported", 2)])
        self.assertFalse(Transient.objects.filter(name="2026gap").exists())

        results = tns_resync.import_missing(since, dry_run=False, client=client)
        self.assertEqual([r.action for r in results], ["imported"])
        imported = Transient.objects.get(name="2026gap")
        self.assertEqual(imported.TNS_spec_class, "SN II")
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=imported).count(), 2)
        self.assertTrue(ExternalServiceRun.objects.filter(service__slug="tns", transient=imported).exists())
        self.assertNotIn(("get", "2026rsa"), client.calls)  # objects we have are not fetched

        self.assertEqual(tns_resync.import_missing(since, dry_run=False, client=client), [])

    def test_search_is_paged(self, _ps):
        names = ["2026%s" % "".join(chr(97 + (i // 26 ** k) % 26) for k in (2, 1, 0)) for i in range(120)]
        client = FakeTNS({}, public=names)
        found = tns_resync.tns_names_since(client, timezone.now(), throttle=tns_resync.Throttle(0))
        self.assertEqual(found, names)
        self.assertEqual([c for c in client.calls if c[0] == "search"], [("search", "0"), ("search", "1"),
                                                                         ("search", "2")])


class SelectionTests(TnsResyncTestBase):
    def _run(self, transient, days_ago):
        service = tns_resync.tns_service(self.cron)
        run = ExternalServiceRun.objects.create(service=service, transient=transient, status="succeeded",
                                                created_by=self.cron, modified_by=self.cron)
        ExternalServiceRun.objects.filter(pk=run.pk).update(
            finished_at=timezone.now() - datetime.timedelta(days=days_ago))

    def test_due_transients(self):
        never = create_minimal_transient(self.cron, name="2026rsb", status_name="Ignore", ra=11.0)
        recent = create_minimal_transient(self.cron, name="2026rsc", status_name="Following", ra=12.0)
        stale = create_minimal_transient(self.cron, name="2026rsd", status_name="Ignore", ra=13.0)
        create_minimal_transient(self.cron, name="ZTF26internal", status_name="New", ra=14.0)
        self._run(self.transient, 5)   # New: active, due after 2 days
        self._run(recent, 0.5)          # active, not due
        self._run(stale, 40)            # other status, due after 30 days
        due = tns_resync.due_transients(limit=10, active_days=2, other_days=30)
        self.assertEqual([t.name for t in due], ["2026rsa", "2026rsb", "2026rsd"])

    def test_select_skips_recently_synced(self):
        self._run(self.transient, 1)
        self.assertEqual(list(tns_resync.select_transients(names=["2026rsa"], skip_synced_days=7)), [])
        self.assertEqual(len(tns_resync.select_transients(names=["2026rsa"], skip_synced_days=0)), 1)


class CommandTests(TnsResyncTestBase):
    def _call(self, *args):
        out = StringIO()
        call_command("tns_resync", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_then_apply_then_resume(self):
        client = FakeTNS({"2026rsa": self._second_reply()}, files={SPEC_URL: SPEC_TEXT})
        with mock.patch.object(tns_resync, "default_client", return_value=client):
            out = self._call("--names", "2026rsa", "--sleep", "0")
            self.assertIn("would update", out)
            self.assertEqual(self._counts(), (0, 0))
            out = self._call("--names", "2026rsa", "--sleep", "0", "--apply")
            self.assertIn("2026rsa: updated", out)
            self.assertEqual(self._counts(), (2, 1))
            out = self._call("--names", "2026rsa", "--sleep", "0", "--apply")
            self.assertIn("Re-sync: 0 transients", out)  # resumes: re-synced within 7 days

    def test_named_object_we_lack(self):
        with mock.patch.object(tns_resync, "default_client", return_value=FakeTNS({})):
            out = self._call("--names", "2026zzz", "--sleep", "0")
        self.assertIn("2026zzz: not in YSE-PZ", out)


class CronAndJobTests(TnsResyncTestBase):
    def test_cron_is_registered_and_off_by_default(self):
        from YSE_App.data_ingest.TNS_Resync import TNSResync

        self.assertIn("YSE_App.data_ingest.TNS_Resync.TNSResync", settings.CRON_CLASSES)
        self.assertNotIn("YSE_App.data_ingest.TNS_uploads.TNS_recent_realtime", settings.CRON_CLASSES)
        with override_settings(TNS_RESYNC_CRON_ENABLED=False):
            self.assertEqual(TNSResync().do(), "disabled")
        with override_settings(TNS_RESYNC_CRON_ENABLED=True, JOB_RUNNER_INLINE=False):
            self.assertTrue(TNSResync().do().startswith("queued job"))
            self.assertIn("already queued", TNSResync().do())
        self.assertEqual(Job.objects.filter(kind=tns_resync.SWEEP_KIND).count(), 1)

    def test_removed_realtime_cron_is_a_noop(self):
        from YSE_App.data_ingest.TNS_uploads import TNS_recent_realtime

        self.assertEqual(TNS_recent_realtime().do(), "removed")

    def test_sweep_job_retries_on_rate_limit(self):
        with mock.patch.object(tns_resync, "sweep", side_effect=tns.TNSRateLimited("slow", retry_after=42)):
            with self.assertRaises(JobRetry) as ctx:
                tns_resync.resync_sweep({})
        self.assertEqual(ctx.exception.delay, 42)

    def test_sweep_imports_gap_and_resyncs_due(self):
        client = FakeTNS({"2026rsa": self._first_reply()}, public=["2026rsa"])
        summary = tns_resync.sweep(limit=5, gap_days=3, client=client, throttle=tns_resync.Throttle(0))
        self.assertEqual((summary["checked"], summary["updated"], summary["imported"]), (1, 1, 0))
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.TNS_spec_class, "SN Ia")
        summary = tns_resync.sweep(limit=5, gap_days=0, client=client, throttle=tns_resync.Throttle(0))
        self.assertEqual(summary["checked"], 0)  # synced just now: not due


class ParseTests(TestCase):
    def test_parse_spectrum_text(self):
        data = tns_resync.parse_spectrum_text(SPEC_TEXT)
        self.assertEqual([d["wavelength"] for d in data.values()], [4000.0, 4005.0, 4015.0])

    def test_classification_difference_helper(self):
        rec = tns_resync.record_classification_difference
        self.assertIsNone(rec(None, tns_class="", previous_tns_class="", our_class="", best_class_set=False))
        self.assertIsNone(rec(None, tns_class="SN Ia", previous_tns_class="SN Ia", our_class="SN II",
                              best_class_set=False))
        entry = rec(None, tns_class="SN Ia", previous_tns_class="SN", our_class="SN II", best_class_set=False)
        self.assertEqual((entry["source"], entry["differs_from_ours"]), ("TNS", True))
