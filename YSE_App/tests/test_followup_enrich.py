"""Automatic TNS and ZTF refresh on a new classical-night follow-up (#114). Network and TNS are mocked."""

from __future__ import annotations

import datetime
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from YSE_App.jobs import registry
from YSE_App.models import ClassicalResource, FollowupStatus, Job, Log, ToOResource
from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.services import followup_enrich, tns_resync, ztf_forced
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.sharing import tns
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

RESYNC = "YSE_App.services.tns_resync.resync_object"
SUBMIT = "YSE_App.services.ztf_forced.submit_forced_phot"


class _Base(TestCase):
    def setUp(self):
        self.user = create_test_user("fe_user")
        self.transient = create_minimal_transient(self.user, name="2026fen")
        attach_synthetic_photometry(self.user, self.transient, n_points=3)
        _obs, instrument, _band = create_instrument_stack(self.user, obs_group_name="fe-stack")
        now = timezone.now()
        self.now, self.stop = now, now + datetime.timedelta(days=5)
        common = dict(telescope=instrument.telescope, begin_date_valid=now - datetime.timedelta(days=1),
                      end_date_valid=self.stop, **audit_fields(self.user))
        self.classical = ClassicalResource.objects.create(**common)
        self.too = ToOResource.objects.create(**common)
        self.requested = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.user))[0]

    def _request(self, **resource):
        return create_or_attach_request(self.user, self.transient, status=self.requested, valid_start=self.now,
                                        valid_stop=self.stop, **resource)

    def _jobs(self):
        return Job.objects.filter(kind=followup_enrich.KIND)

    def _tns_run(self, hours_ago, status=ExternalServiceRun.STATUS_SUCCEEDED):
        when = timezone.now() - datetime.timedelta(hours=hours_ago)
        return ExternalServiceRun.objects.create(
            service=tns_resync.tns_service(self.user), transient=self.transient, status=status,
            started_at=when, finished_at=when, created_by=self.user, modified_by=self.user)


class EnqueueTests(_Base):
    def test_handler_registered(self):
        self.assertIn("YSE_App.services.followup_enrich", registry.DEFAULT_HANDLER_MODULES)
        registry.autodiscover(force=True)
        self.assertIn(followup_enrich.KIND, registry.registered_kinds())

    def test_one_job_per_new_classical_followup_after_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            followup, _child, created = self._request(classical_resource=self.classical)
            self.assertTrue(created)
            self.assertEqual(self._jobs().count(), 0)  # nothing before the commit
        for callback in callbacks:
            callback()
        jobs = list(self._jobs())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].payload, {"followup_id": followup.pk, "transient_id": self.transient.pk})
        self.assertEqual(jobs[0].transient_id, self.transient.pk)

    def test_second_request_on_same_followup_does_not_enqueue_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._request(classical_resource=self.classical)
        other = create_test_user("fe_other")
        with self.captureOnCommitCallbacks(execute=True):
            _f, _c, created = create_or_attach_request(
                other, self.transient, status=self.requested, valid_start=self.now, valid_stop=self.stop,
                classical_resource=self.classical)
        self.assertFalse(created)
        self.assertEqual(self._jobs().count(), 1)

    def test_too_followup_is_unaffected(self):
        with self.captureOnCommitCallbacks(execute=True):
            _f, _c, created = self._request(too_resource=self.too)
        self.assertTrue(created)
        self.assertEqual(self._jobs().count(), 0)

    @override_settings(FOLLOWUP_AUTO_ENRICH=False)
    def test_setting_off_disables(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._request(classical_resource=self.classical)
        self.assertEqual(self._jobs().count(), 0)

    def test_on_by_default(self):
        from django.conf import settings

        self.assertTrue(settings.FOLLOWUP_AUTO_ENRICH)
        self.assertEqual(settings.FOLLOWUP_ENRICH_WINDOW_HOURS, 24)

    def test_enqueue_failure_never_breaks_the_save(self):
        with mock.patch.object(followup_enrich, "enqueue", side_effect=RuntimeError("queue down")):
            with self.captureOnCommitCallbacks(execute=True):
                _f, _c, created = self._request(classical_resource=self.classical)
        self.assertTrue(created)


class JobTests(_Base):
    def _followup(self):
        with self.captureOnCommitCallbacks(execute=False):
            followup, _c, _created = self._request(classical_resource=self.classical)
        return followup

    def _run(self, followup):
        return followup_enrich.enrich_followup(
            {"followup_id": followup.pk, "transient_id": self.transient.pk})

    def test_runs_tns_and_ztf(self):
        followup = self._followup()
        result = mock.Mock()
        result.summary.return_value = "2026fen: unchanged"
        with mock.patch(RESYNC, return_value=result) as resync, \
                mock.patch(SUBMIT, return_value=ztf_forced.Outcome("submitted", "success: x", via="legacy")) as submit:
            report = self._run(followup)
        resync.assert_called_once()
        self.assertTrue(resync.call_args.kwargs["dry_run"] is False)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[1], self.user)  # the requester's allocations apply
        self.assertEqual(report["tns"], "2026fen: unchanged")
        self.assertTrue(report["ztf"].startswith("submitted"))

    def test_skipped_within_24_hours(self):
        followup = self._followup()
        self._tns_run(hours_ago=3)
        ztf_forced.record_query(self.transient, self.user, via="legacy")
        with mock.patch(RESYNC) as resync, mock.patch(SUBMIT) as submit:
            report = self._run(followup)
        resync.assert_not_called()
        submit.assert_not_called()
        self.assertIn("skipped", report["tns"])
        self.assertIn("skipped", report["ztf"])

    def test_runs_again_after_the_window_and_after_failed_runs(self):
        followup = self._followup()
        self._tns_run(hours_ago=30)
        self._tns_run(hours_ago=1, status=ExternalServiceRun.STATUS_FAILED)  # a failed check does not count
        run = ztf_forced.record_query(self.transient, self.user, via="legacy")
        ExternalServiceRun.objects.filter(pk=run.pk).update(finished_at=timezone.now() - datetime.timedelta(hours=25))
        result = mock.Mock()
        result.summary.return_value = "ok"
        with mock.patch(RESYNC, return_value=result) as resync, \
                mock.patch(SUBMIT, return_value=ztf_forced.Outcome("submitted", "success", via="legacy")) as submit:
            self._run(followup)
        resync.assert_called_once()
        submit.assert_called_once()

    def test_ztf_freshness_counts_the_button_comment_and_facility_requests(self):
        self.assertIsNone(ztf_forced.last_queried(self.transient))
        Log.objects.create(transient=self.transient, comment="ZTF Forced Phot requested by x", **audit_fields(self.user))
        self.assertTrue(ztf_forced.queried_within(self.transient, 24))

    def test_off_setting_makes_a_queued_job_a_noop(self):
        followup = self._followup()
        with override_settings(FOLLOWUP_AUTO_ENRICH=False), mock.patch(RESYNC) as resync, mock.patch(SUBMIT) as submit:
            report = self._run(followup)
        resync.assert_not_called()
        submit.assert_not_called()
        self.assertIn("skipped", report)

    def test_tns_rate_limit_retries_with_its_delay(self):
        followup = self._followup()
        with mock.patch(RESYNC, side_effect=tns.TNSRateLimited("slow down", retry_after=120)), \
                mock.patch(SUBMIT, return_value=ztf_forced.Outcome("submitted", "success", via="legacy")):
            with self.assertRaises(followup_enrich.JobRetry) as ctx:
                self._run(followup)
        self.assertEqual(ctx.exception.delay, 120)

    def test_failed_ztf_submission_retries_but_refusal_does_not(self):
        followup = self._followup()
        result = mock.Mock()
        result.summary.return_value = "ok"
        with mock.patch(RESYNC, return_value=result), \
                mock.patch(SUBMIT, return_value=ztf_forced.Outcome("failed", "error: x", 502, via="legacy")):
            with self.assertRaises(followup_enrich.JobRetry):
                self._run(followup)
        with mock.patch(RESYNC, return_value=result), \
                mock.patch(SUBMIT, return_value=ztf_forced.Outcome("refused", "error: already open", via="allocation")):
            self.assertIn("refused", self._run(followup)["ztf"])

    def test_tns_not_configured_is_not_retried(self):
        followup = self._followup()
        with mock.patch(RESYNC, side_effect=tns_resync.ResyncError("no bot")), \
                mock.patch(SUBMIT, return_value=ztf_forced.Outcome("submitted", "success", via="legacy")):
            report = self._run(followup)
        self.assertIn("not run", report["tns"])


class ZtfSubmissionTests(_Base):
    def test_legacy_submission_is_recorded_as_a_ztf_forced_run(self):
        fake = mock.MagicMock()
        fake.return_value.run_ztf_fp.return_value = "ztf_fp_ci.log"
        with mock.patch("YSE_App.data_ingest.ZTF_Forced_Phot.ZTF_Forced_Phot", fake):
            outcome = ztf_forced.submit_forced_phot(self.transient, self.user)
        self.assertTrue(outcome.ok)
        runs = ExternalServiceRun.objects.filter(service__slug="ztf_forced", transient=self.transient)
        self.assertEqual(runs.count(), 1)
        self.assertTrue(ztf_forced.queried_within(self.transient, 24))
        # a failure records nothing
        other = create_minimal_transient(self.user, name="2026fen2")
        fake.return_value.run_ztf_fp.return_value = None
        with mock.patch("YSE_App.data_ingest.ZTF_Forced_Phot.ZTF_Forced_Phot", fake):
            outcome = ztf_forced.submit_forced_phot(other, self.user)
        self.assertEqual((outcome.kind, outcome.http_status), ("failed", 502))
        self.assertFalse(ExternalServiceRun.objects.filter(transient=other).exists())
