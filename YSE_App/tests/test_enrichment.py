"""Enrichment framework (#439): registry, run log, retry policy, sweep, command, ported services.

Fake clients and fake services only: no network in CI. Synthetic names and positions.
"""

from __future__ import annotations

import datetime
from io import StringIO
from unittest import mock

import pandas as pd
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from YSE_App.models import ExternalServiceRun, Host
from YSE_App.models.annotation_models import TransientAnnotation
from YSE_App.services import enrichment, prost_hosts
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

SERVICE_USERNAME = "enrich_cron"


class FakeRemote:
    """Stands in for a remote catalogue: per-name answers ('hit', 'miss', 'boom')."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def run_one(self, obj, ctx):
        self.calls.append(obj.pk)
        answer = self.answers.get(getattr(obj, "name", ""), "hit")
        if answer == "boom":
            return enrichment.Outcome(enrichment.ERROR, error="remote said no")
        if answer == "raise":
            raise RuntimeError("unexpected")
        if answer == "limit":
            raise enrichment.RateLimited("slow down", retry_after=5)
        if answer == "miss":
            return enrichment.Outcome(enrichment.NO_MATCH, "nothing there")
        return enrichment.Outcome(enrichment.UPDATED, "filled")


def _fake_service(remote, name="fake", target=enrichment.TARGET_TRANSIENT, **kw):
    def candidates(missing_only):
        from YSE_App.models import Transient

        if target == enrichment.TARGET_HOST:
            return Host.objects.filter(transient__isnull=False).distinct()
        return Transient.objects.filter(name__startswith="2026e")

    opts = dict(retry_hours=6.0, recheck_days=None, max_attempts=3)
    opts.update(kw)
    return enrichment.EnrichmentService(
        name=name, slug="fake-%s" % name, title="Fake %s" % name, description="test service",
        target=target, candidates=candidates, run_one=remote.run_one, **opts)


class RegistryTests(SimpleTestCase):
    def test_builtins_are_registered(self):
        for name in ("tns", "ned", "desi", "ztf-forced", "prost", "photoz-ml", "sdss-photoz", "ps1-cutouts",
                     "gaia-lc", "blast"):
            self.assertIn(name, enrichment.names())
        self.assertTrue(enrichment.get("tns").records_itself)
        self.assertFalse(enrichment.get("prost").records_itself)
        with self.assertRaises(enrichment.EnrichmentError):
            enrichment.get("ghost")

    def test_register_needs_exactly_one_runner(self):
        with self.assertRaises(ValueError):
            enrichment.register(enrichment.EnrichmentService(
                name="bad", slug="bad", title="", description="", target="transient", candidates=lambda m: None))

    @override_settings(BLAST_ENABLED=False)
    def test_blast_is_off_by_default(self):
        self.assertFalse(enrichment.get("blast").is_enabled())


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], ENRICH_SWEEP_SERVICES=[], PROST_HOST_MATCH_ENABLED=False)
class RunLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)
        cls.a = create_minimal_transient(cls.user, name="2026eaa", ra=10.0, dec=1.0)
        cls.b = create_minimal_transient(cls.user, name="2026ebb", ra=11.0, dec=1.0)
        cls.c = create_minimal_transient(cls.user, name="2026ecc", ra=12.0, dec=1.0)

    def setUp(self):
        self.remote = FakeRemote({"2026ebb": "boom", "2026ecc": "miss"})
        self.service = enrichment.register(_fake_service(self.remote))
        self.addCleanup(enrichment.unregister, "fake")

    def _runs(self, transient):
        return ExternalServiceRun.objects.filter(service__slug="fake-fake", transient=transient)

    def _age(self, transient, hours):
        self._runs(transient).update(finished_at=timezone.now() - datetime.timedelta(hours=hours))

    def test_dry_run_lists_and_calls_nothing(self):
        summary = enrichment.run_selection("fake", dry_run=True)
        self.assertEqual(summary.selected, 3)
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(ExternalServiceRun.objects.filter(service__slug="fake-fake").exists())

    def test_runs_are_recorded_and_not_repeated(self):
        summary = enrichment.run_selection("fake", dry_run=False)
        self.assertEqual(summary.counts, {"updated": 1, "error": 1, "no_match": 1})
        self.assertEqual(self._runs(self.a).get().status, ExternalServiceRun.STATUS_SUCCEEDED)
        failed = self._runs(self.b).get()
        self.assertEqual((failed.status, failed.error, failed.attempts), ("failed", "remote said no", 1))
        # resumable: nothing is due straight away (success never rechecked, failure waits 6 h)
        self.assertEqual(list(enrichment.select(self.service)), [])

    def test_failed_run_is_retried_by_the_next_sweep_then_given_up(self):
        enrichment.run_selection("fake", dry_run=False)
        for attempt in (2, 3):
            self._age(self.b, 7)
            with override_settings(ENRICH_SWEEP_SERVICES=["fake"]):
                out = enrichment.sweep(budget_seconds=60)
            self.assertEqual(out["fake"]["selected"], 1)
            run = self._runs(self.b).get()  # the same row: failures in a row
            self.assertEqual(run.attempts, attempt)
        self._age(self.b, 7)
        self.assertEqual(list(enrichment.select(self.service)), [])  # max_attempts reached
        self.assertEqual(list(enrichment.select(self.service, retry_failed=True, rerun=True)), [self.b])

    def test_no_change_checks_reuse_one_row_and_recheck_cadence(self):
        service = enrichment.register(_fake_service(self.remote, name="fake", recheck_days=1.0))
        enrichment.run(service, [self.c], dry_run=False)
        self._age(self.c, 30)
        self.assertIn(self.c, list(enrichment.select(service)))
        enrichment.run(service, [self.c], dry_run=False)
        run = self._runs(self.c).get()
        self.assertEqual(run.result["checks"], 2)

    def test_never_run_first_and_selection_options(self):
        enrichment.run(self.service, [self.a], dry_run=False)
        self.assertEqual(list(enrichment.select(self.service, rerun=True))[-1], self.a)
        self.assertEqual(list(enrichment.select(self.service, names=["2026ebb"])), [self.b])
        self.assertEqual(list(enrichment.select(self.service, never_run=True, rerun=True)), [self.c, self.b])

    def test_unexpected_exception_is_a_recorded_error_and_rate_limit_stops(self):
        self.remote.answers = {"2026eaa": "raise", "2026ebb": "limit"}
        with self.assertRaises(enrichment.RateLimited):
            enrichment.run(self.service, [self.a, self.b, self.c], dry_run=False)
        self.assertIn("RuntimeError", self._runs(self.a).get().error)
        self.assertFalse(self._runs(self.c).exists())  # stopped before it

    def test_host_target_records_on_the_host(self):
        host = Host.objects.create(ra=10.0, dec=1.0, name="synthetic-host", created_by=self.user,
                                   modified_by=self.user)
        self.a.host = host
        self.a.save()
        remote = FakeRemote()
        service = enrichment.register(_fake_service(remote, name="fakehost", target=enrichment.TARGET_HOST))
        self.addCleanup(enrichment.unregister, "fakehost")
        self.assertEqual(list(enrichment.select(service, names=["2026eaa"])), [host])
        enrichment.run(service, [host], dry_run=False)
        run = ExternalServiceRun.objects.get(service__slug="fake-fakehost")
        self.assertEqual((run.target_object_id, run.transient_id), (host.pk, self.a.pk))
        self.assertEqual(list(enrichment.select(service)), [])

    def test_command_dry_run_apply_and_list(self):
        out = StringIO()
        call_command("enrich", "--service", "fake", "--max", "2", stdout=out)
        self.assertIn("Dry run fake: 2 transient(s)", out.getvalue())
        self.assertEqual(self.remote.calls, [])
        out = StringIO()
        call_command("enrich", "--service", "fake", "--names", "2026eaa", "--apply", stdout=out)
        self.assertIn("updated 1", out.getvalue())
        out = StringIO()
        call_command("enrich", "--list", stdout=out)
        self.assertIn("prost", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("enrich", "--service", "ztf-forced", "--apply", stdout=StringIO())

    def test_command_queue(self):
        out = StringIO()
        with mock.patch.object(enrichment, "enqueue") as enqueue:
            call_command("enrich", "--service", "fake", "--apply", "--queue", "--batch-size", "2", stdout=out)
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(enqueue.call_args_list[0][0][0], enrichment.OBJECTS_KIND)

    def test_sweep_cron_is_off_by_default(self):
        from YSE_App.data_ingest.Enrichment import EnrichmentSweep

        with override_settings(ENRICH_SWEEP_CRON_ENABLED=False):
            self.assertEqual(EnrichmentSweep().do(), "disabled")


# --- wrapped services ---------------------------------------------------------------

@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME])
class WrappedServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)
        cls.t = create_minimal_transient(cls.user, name="2026ewr", ra=10.0, dec=1.0)

    def test_tns_wrapper_calls_resync_object_and_records_nothing_itself(self):
        from YSE_App.services import tns_resync

        result = tns_resync.ResyncResult(name="2026ewr", tns_name="2026ewr", action="unchanged", dry_run=False)
        with mock.patch.object(tns_resync, "resync_object", return_value=result) as resync:
            summary = enrichment.run(enrichment.get("tns"), [self.t], dry_run=False,
                                     ctx={"tns_client": object(), "tns_throttle": object()})
        self.assertEqual(summary.counts, {"unchanged": 1})
        self.assertFalse(resync.call_args.kwargs["import_missing"])
        self.assertFalse(ExternalServiceRun.objects.exists())  # tns_resync records its own runs

    def test_tns_selection_reads_the_tns_runs(self):
        from YSE_App.services import tns_resync

        service = enrichment.get("tns")
        self.assertIn(self.t, list(enrichment.select(service)))
        tns_resync._record(self.t, tns_resync.ResyncResult(name="2026ewr", dry_run=False), self.user)
        self.assertNotIn(self.t, list(enrichment.select(service)))

    @override_settings(REDSHIFT_CATALOG_SERVICES="ned,desi")
    def test_ned_wrapper_maps_check_results(self):
        from YSE_App.services import redshift_catalogs

        host = Host.objects.create(ra=10.0, dec=1.0, name="synthetic-host", created_by=self.user,
                                   modified_by=self.user)
        self.t.host = host
        self.t.save()
        check = redshift_catalogs.CheckResult(service="ned", host_id=host.pk, action="no_match", dry_run=False)
        with mock.patch.object(redshift_catalogs, "update_host", return_value=[check]) as update:
            summary = enrichment.run(enrichment.get("ned"), [host], dry_run=False, ctx={"clients": {}})
        self.assertEqual(summary.counts, {"no_match": 1})
        self.assertEqual(update.call_args.kwargs["services"], ["ned"])


# --- Prost ------------------------------------------------------------------------------

def _prost_table(name, *, spec=True):
    return pd.DataFrame({
        "IAUID": [name], "host_name": ["SYNTH GAL P"], "host_objID": [123], "host_ra": [10.0001],
        "host_dec": [1.0001], "host_redshift_mean": [0.042], "host_redshift_std": [0.001],
        "host_redshift_info": ["SPEC" if spec else "PHOT"],
    })


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], REDSHIFT_CATALOG_CRON_ENABLED=False)
class ProstTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)

    def setUp(self):
        self.t = create_minimal_transient(self.user, name="2026epr", ra=10.0, dec=1.0)

    def _match(self, table, **kw):
        fake = mock.Mock(return_value=table)
        outcome = prost_hosts.match_transient(self.t, user=self.user, associate_fn=fake, absmag_cls=mock.Mock(),
                                              scratch=self._scratch(), **kw)
        return outcome, fake

    def _scratch(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_links_host_with_prost_redshift_through_precedence(self):
        outcome, fake = self._match(_prost_table("2026epr"))
        self.assertEqual(outcome.action, enrichment.UPDATED)
        catalog = fake.call_args[0][0]
        self.assertEqual(list(catalog["IAUID"]), ["2026epr"])
        self.t.refresh_from_db()
        host = self.t.host
        self.assertEqual((host.name, host.redshift, host.redshift_source), ("SYNTH GAL P", 0.042, "Prost"))

    def test_existing_higher_ranked_redshift_is_kept(self):
        Host.objects.create(name="SYNTH GAL P", ra=10.0001, dec=1.0001, redshift=0.0400, redshift_source="NED",
                            created_by=self.user, modified_by=self.user)
        outcome, _ = self._match(_prost_table("2026epr"))
        self.t.refresh_from_db()
        self.assertEqual((self.t.host.redshift, self.t.host.redshift_source), (0.04, "NED"))
        self.assertIn("kept", outcome.summary)

    def test_photo_z_goes_to_photo_z_internal(self):
        self._match(_prost_table("2026epr", spec=False))
        self.t.refresh_from_db()
        self.assertIsNone(self.t.host.redshift)
        self.assertAlmostEqual(self.t.host.photo_z_internal, 0.042)

    def test_no_match_and_has_host(self):
        outcome, _ = self._match(_prost_table("2026other"))
        self.assertEqual(outcome.action, enrichment.NO_MATCH)
        self.t.host = Host.objects.create(name="h", ra=1, dec=1, created_by=self.user, modified_by=self.user)
        self.t.save()
        outcome, fake = self._match(_prost_table("2026epr"))
        self.assertEqual(outcome.action, enrichment.SKIPPED)
        fake.assert_not_called()

    def test_tns_cron_uses_the_same_function(self):
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        from YSE_App.data_ingest import TNS_uploads

        proc = TNS_uploads.processTNS()
        proc.ghost_path, proc.prost_path = self._scratch(), ""
        with mock.patch.object(TNS_uploads, "HAS_ASTRO_PROST", True), \
                mock.patch.object(TNS_uploads, "SnRateAbsmag", mock.Mock()), \
                mock.patch.object(TNS_uploads, "associate_sample", return_value=_prost_table("2026epr")):
            table = proc.getProstData(["2026epr"], [SkyCoord(10 * u.deg, 1 * u.deg)])
        hostdict, coords = proc.getProstHosts(None, None, table)
        self.assertEqual((hostdict["name"], hostdict["redshift_source"], coords), ("SYNTH GAL P", "Prost", ""))

    def test_gap_import_queues_prost_only_when_enabled(self):
        from YSE_App.services import tns_resync

        with override_settings(PROST_HOST_MATCH_ENABLED=False), \
                mock.patch.object(enrichment, "enqueue") as enqueue:
            tns_resync._queue_host_match(self.t.pk)
        enqueue.assert_not_called()
        with override_settings(PROST_HOST_MATCH_ENABLED=True), \
                mock.patch.object(enrichment, "enqueue") as enqueue:
            tns_resync._queue_host_match(self.t.pk)
        self.assertEqual(enqueue.call_args[0][:2], (enrichment.OBJECTS_KIND, {"service": "prost",
                                                                              "ids": [self.t.pk]}))

    def test_followup_host_step(self):
        from YSE_App.services import followup_enrich

        with override_settings(PROST_HOST_MATCH_ENABLED=False):
            self.assertIn("off", followup_enrich._host_step(self.t))
        with override_settings(PROST_HOST_MATCH_ENABLED=True), \
                mock.patch.object(prost_hosts, "associate", return_value=_prost_table("2026epr")):
            self.assertIn("linked", followup_enrich._host_step(self.t))
        self.t.refresh_from_db()
        self.assertIsNotNone(self.t.host_id)
        self.assertTrue(ExternalServiceRun.objects.filter(service__slug="prost-host", transient=self.t).exists())


# --- ported crons ------------------------------------------------------------------------

@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME])
class PortedCronTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)
        cls.t = create_minimal_transient(cls.user, name="2026ecr", ra=10.0, dec=1.0)
        cls.host = Host.objects.create(ra=10.0, dec=1.0, name="synthetic-host", created_by=cls.user,
                                       modified_by=cls.user)
        cls.t.host = cls.host
        cls.t.save()

    def test_photoz_ml_writes_internal_photoz_and_records(self):
        from YSE_App.data_ingest import Photo_Z

        with mock.patch.object(Photo_Z, "predict_hosts", return_value={self.host.pk: (0.12, 0.02)}):
            summary = enrichment.run(enrichment.get("photoz-ml"), list(Photo_Z.candidate_hosts()), dry_run=False)
        self.host.refresh_from_db()
        self.assertEqual((self.host.photo_z_internal, self.host.photo_z_err_internal), (0.12, 0.02))
        self.assertEqual(summary.counts, {"updated": 1})
        self.assertFalse(Photo_Z.candidate_hosts().exists())

    def test_sdss_photoz_and_unreturned_hosts_are_not_recorded(self):
        from YSE_App.data_ingest import SDSS_Photo_Z

        with mock.patch.object(SDSS_Photo_Z, "fetch_photoz", return_value={}):
            enrichment.run(enrichment.get("sdss-photoz"), [self.host], dry_run=False)
        self.assertFalse(ExternalServiceRun.objects.filter(service__slug="sdss-photoz").exists())
        with mock.patch.object(SDSS_Photo_Z, "fetch_photoz", return_value={self.host.pk: (0.2, 0.05)}):
            enrichment.run(enrichment.get("sdss-photoz"), [self.host], dry_run=False)
        self.host.refresh_from_db()
        self.assertEqual((self.host.photo_z, self.host.photo_z_source), (0.2, "SDSS"))
        self.assertIsNone(self.host.redshift)

    def test_batch_failure_records_nothing(self):
        from YSE_App.data_ingest import SDSS_Photo_Z

        with mock.patch.object(SDSS_Photo_Z, "fetch_photoz", side_effect=RuntimeError("SciServer down")):
            with self.assertRaises(enrichment.EnrichmentError):
                enrichment.run(enrichment.get("sdss-photoz"), [self.host], dry_run=False)
        self.assertFalse(ExternalServiceRun.objects.exists())

    @override_settings(PS1_CUTOUT_RETRY_DAYS=30.0, PS1_CUTOUT_MAX_ATTEMPTS=3)
    def test_ps1_cutout_failures_are_not_downloaded_again(self):
        from YSE_App.data_ingest import PS1_cutouts

        service = enrichment.get("ps1-cutouts")
        self.assertEqual(list(enrichment.select(service)), [self.t])
        fetch = mock.Mock(side_effect=PS1_cutouts.CutoutUnavailable("no PS1 image"))
        with mock.patch.object(PS1_cutouts, "fetch_cutout", fetch):
            enrichment.run(service, list(enrichment.select(service)), dry_run=False, ctx={"user": self.user})
            self.assertEqual(fetch.call_count, 1)
            # the cron's next runs select nothing until the retry is due
            self.assertEqual(list(enrichment.select(service)), [])
            ExternalServiceRun.objects.update(finished_at=timezone.now() - datetime.timedelta(days=31))
            self.assertEqual(list(enrichment.select(service)), [self.t])

    def test_ps1_host_with_cutouts_is_not_selected(self):
        from YSE_App.data_ingest import PS1_cutouts

        with mock.patch.object(PS1_cutouts, "hosts_with_cutouts",
                               return_value=Host.objects.filter(pk=self.host.pk).values("pk")):
            self.assertFalse(PS1_cutouts.candidate_transients(True).exists())
        self.assertTrue(PS1_cutouts.candidate_transients(False).exists())

    def test_gaia_candidates(self):
        from YSE_App.data_ingest import Gaia_LC
        from YSE_App.models import AlternateTransientNames

        self.assertFalse(Gaia_LC.candidate_transients().exists())
        AlternateTransientNames.objects.create(transient=self.t, name="Gaia26aaa", created_by=self.user,
                                               modified_by=self.user)
        self.assertEqual(list(Gaia_LC.candidate_transients()), [self.t])


# --- Blast -------------------------------------------------------------------------------

BLAST_TRANSIENT = {"count": 1, "next": None, "results": [{
    "name": "2026ebl", "ra_deg": 10.0, "dec_deg": 1.0,
    "host": {"name": "SYNTH BLAST HOST", "ra_deg": 10.0002, "dec_deg": 1.0001, "redshift": 0.051,
             "catalog_name": "SYNTHCAT", "object_id": "42"}}]}
BLAST_SED = {"count": 2, "next": None, "results": [
    {"aperture": {"type": "global"}, "log_mass_16": 9.8, "log_mass_50": 10.0, "log_mass_84": 10.2,
     "log_sfr_50": 0.1, "log_ssfr_50": -9.9},
    {"aperture": {"type": "local"}, "log_mass_50": 8.5, "log_sfr_50": None},
]}


class FakeBlastResponse:
    def __init__(self, doc, status=200):
        self.doc, self.status_code, self.headers = doc, status, {}

    def json(self):
        return self.doc


class FakeBlastSession:
    def __init__(self, transient=BLAST_TRANSIENT, sed=BLAST_SED, status=200):
        self.transient, self.sed, self.status = transient, sed, status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        doc = self.transient if "/transient/" in url else self.sed
        return FakeBlastResponse(doc, self.status)


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], BLAST_ENABLED=True, BLAST_API_TOKEN="test-token",
                   BLAST_REQUEST_INTERVAL_SECONDS=0)
class BlastTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)
        cls.t = create_minimal_transient(cls.user, name="2026ebl", ra=10.0, dec=1.0)

    def _run(self, session):
        from YSE_App.services import blast

        return enrichment.run(enrichment.get("blast"), [self.t], dry_run=False,
                              ctx={"client": blast.BlastClient(session=session)})

    def test_mocked_blast_response_fills_host_properties(self):
        session = FakeBlastSession()
        summary = self._run(session)
        self.assertEqual(summary.counts, {"updated": 1})
        ann = TransientAnnotation.objects.get(transient=self.t, origin="blast")
        self.assertEqual(ann.data["host_name"], "SYNTH BLAST HOST")
        self.assertEqual(ann.data["host_redshift"], 0.051)
        self.assertEqual((ann.data["global_log_mass"], ann.data["global_log_mass_84"]), (10.0, 10.2))
        self.assertEqual(ann.data["local_log_mass"], 8.5)
        self.assertNotIn("local_log_sfr", ann.data)
        self.assertEqual(ann.groups.count(), 0)  # no groups: every logged-in user, as other catalogue annotations
        url, params, headers = session.calls[0]
        self.assertTrue(url.endswith("/api/transient/"))
        self.assertEqual((params["name"], headers["Authorization"]), ("2026ebl", "Token test-token"))
        self.assertEqual(session.calls[1][1]["transient"], "2026ebl")
        self.assertEqual(self._run(FakeBlastSession()).counts, {"unchanged": 1})
        self.assertFalse(enrichment.get("blast").candidates(True).filter(pk=self.t.pk).exists())

    def test_not_in_blast_and_http_error(self):
        self.assertEqual(self._run(FakeBlastSession(transient={"results": []})).counts, {"no_match": 1})
        self.assertEqual(self._run(FakeBlastSession(status=500)).counts, {"error": 1})
        with self.assertRaises(enrichment.RateLimited):
            self._run(FakeBlastSession(status=429))

    def test_needs_token_and_flag(self):
        with override_settings(BLAST_API_TOKEN=""):
            with self.assertRaises(enrichment.EnrichmentError):
                self._run(FakeBlastSession())
        with override_settings(BLAST_ENABLED=False):
            with self.assertRaises(enrichment.EnrichmentError):
                self._run(FakeBlastSession())
