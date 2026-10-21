"""Background job queue (issue #263): enqueue, claim, retry, failure, cron pass, admin, status page."""

from __future__ import annotations

import datetime
import io

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.data_ingest.Job_Queue import RunQueuedJobs
from YSE_App.jobs import (
    JobFailed,
    JobRetry,
    claim_job,
    enqueue,
    execute,
    job,
    queue_counts,
    reap_stale,
    registered_kinds,
    run_pass,
    unregister,
)
from YSE_App.jobs.runner import backoff_seconds
from YSE_App.models import Job
from YSE_App.tests.fixtures_minimal import create_test_user

CALLS = []


@job("test.echo")
def _echo(payload, job=None):
    CALLS.append(("echo", payload, job.pk if job is not None else None))
    return {"echo": payload}


@job("test.boom", max_attempts=2, backoff_seconds=30)
def _boom(payload):
    CALLS.append(("boom", payload))
    raise RuntimeError("boom %s" % payload.get("n", ""))


@job("test.retry_later")
def _retry_later(payload):
    raise JobRetry("not yet", delay=120)


@job("test.give_up")
def _give_up(payload):
    raise JobFailed("permanent")


@job("test.no_job_kwarg")
def _plain(payload):
    return payload.get("x", 0) * 2


class JobQueueTests(TestCase):
    def setUp(self):
        CALLS.clear()

    def test_enqueue_creates_queued_row_with_handler_defaults(self):
        j = enqueue("test.boom", {"n": 1})
        self.assertEqual(j.status, Job.QUEUED)
        self.assertEqual(j.max_attempts, 2)  # from the @job decorator
        self.assertEqual(j.attempts, 0)
        self.assertEqual(j.payload, {"n": 1})
        self.assertLessEqual(j.run_after, timezone.now())
        j2 = enqueue("test.echo", None)
        self.assertEqual(j2.max_attempts, 3)  # JOB_RUNNER_MAX_ATTEMPTS default
        self.assertEqual(j2.payload, {})

    def test_enqueue_delay_and_created_by(self):
        user = create_test_user("jq_user")
        j = enqueue("test.echo", {}, delay=600, created_by=user)
        self.assertGreater(j.run_after, timezone.now() + datetime.timedelta(seconds=500))
        self.assertEqual(j.created_by, user)
        self.assertIsNone(claim_job(), "a future job must not be claimed")

    def test_claim_is_exclusive(self):
        j = enqueue("test.echo", {})
        first = claim_job(worker_id="w1")
        self.assertEqual(first.pk, j.pk)
        self.assertEqual(first.status, Job.RUNNING)
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.locked_by, "w1")
        self.assertIsNotNone(first.locked_at)
        self.assertIsNone(claim_job(worker_id="w2"))
        self.assertIsNone(claim_job(j.pk, worker_id="w2"))

    def test_claim_filters_by_kind_and_orders_by_run_after(self):
        later = enqueue("test.echo", {"k": "later"}, run_after=timezone.now() - datetime.timedelta(seconds=1))
        earlier = enqueue("test.echo", {"k": "earlier"}, run_after=timezone.now() - datetime.timedelta(seconds=60))
        other = enqueue("test.no_job_kwarg", {"x": 2})
        self.assertEqual(claim_job(kinds=["test.no_job_kwarg"]).pk, other.pk)
        self.assertEqual(claim_job().pk, earlier.pk)
        self.assertEqual(claim_job().pk, later.pk)

    def test_run_pass_executes_and_stores_result(self):
        j = enqueue("test.echo", {"a": 1})
        p = enqueue("test.no_job_kwarg", {"x": 21})
        result = run_pass(worker_id="t")
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.done, 2)
        j.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual(j.status, Job.DONE)
        self.assertEqual(j.result, {"echo": {"a": 1}})
        self.assertEqual(p.result, 42)
        self.assertEqual(j.locked_by, "")
        self.assertIsNotNone(j.finished_at)
        self.assertEqual(CALLS[0], ("echo", {"a": 1}, j.pk))
        self.assertIn("2 done", result.summary())

    def test_failure_retries_with_backoff_then_fails(self):
        j = enqueue("test.boom", {"n": 7})
        before = timezone.now()
        result = run_pass()
        self.assertEqual(result.retried, 1)
        j.refresh_from_db()
        self.assertEqual(j.status, Job.QUEUED)
        self.assertEqual(j.attempts, 1)
        self.assertIn("RuntimeError: boom 7", j.error)
        # handler backoff_seconds=30 -> first retry 30 s later
        self.assertGreaterEqual(j.run_after, before + datetime.timedelta(seconds=29))
        self.assertLess(j.run_after, before + datetime.timedelta(seconds=90))
        self.assertIsNone(claim_job(), "a job waiting for its backoff is not due")
        Job.objects.filter(pk=j.pk).update(run_after=timezone.now() - datetime.timedelta(seconds=1))
        result = run_pass()
        self.assertEqual(result.failed, 1)
        j.refresh_from_db()
        self.assertEqual(j.status, Job.FAILED)
        self.assertEqual(j.attempts, 2)
        self.assertIn("Traceback", j.error)
        self.assertEqual(len(CALLS), 2)

    def test_backoff_doubles_and_caps(self):
        with override_settings(JOB_RUNNER_BACKOFF_SECONDS=60, JOB_RUNNER_BACKOFF_MAX_SECONDS=1000):
            self.assertEqual(backoff_seconds(1), 60)
            self.assertEqual(backoff_seconds(2), 120)
            self.assertEqual(backoff_seconds(3), 240)
            self.assertEqual(backoff_seconds(10), 1000)

    def test_job_retry_exception_sets_delay(self):
        j = enqueue("test.retry_later", {})
        before = timezone.now()
        run_pass()
        j.refresh_from_db()
        self.assertEqual(j.status, Job.QUEUED)
        self.assertGreaterEqual(j.run_after, before + datetime.timedelta(seconds=119))
        self.assertIn("not yet", j.error)

    def test_job_failed_exception_does_not_retry(self):
        j = enqueue("test.give_up", {}, max_attempts=5)
        run_pass()
        j.refresh_from_db()
        self.assertEqual(j.status, Job.FAILED)
        self.assertEqual(j.attempts, 1)
        self.assertIn("permanent", j.error)

    def test_unregistered_kind_fails_without_retry(self):
        j = enqueue("test.nobody_home", {}, max_attempts=3)
        run_pass()
        j.refresh_from_db()
        self.assertEqual(j.status, Job.FAILED)
        self.assertIn("no handler registered", j.error)

    def test_reap_stale_requeues_or_fails(self):
        j = enqueue("test.echo", {})
        claim_job(worker_id="dead")
        Job.objects.filter(pk=j.pk).update(locked_at=timezone.now() - datetime.timedelta(hours=3))
        self.assertEqual(reap_stale(stale_minutes=60), 1)
        j.refresh_from_db()
        self.assertEqual(j.status, Job.QUEUED)
        self.assertIn("requeued", j.error)
        # no attempts left -> failed
        k = enqueue("test.echo", {}, max_attempts=1)
        self.assertEqual(claim_job(k.pk, worker_id="dead").pk, k.pk)
        Job.objects.filter(pk=k.pk).update(locked_at=timezone.now() - datetime.timedelta(hours=3))
        reap_stale(stale_minutes=60)
        k.refresh_from_db()
        self.assertEqual(k.status, Job.FAILED)
        # a fresh running job is left alone
        m = enqueue("test.echo", {})
        self.assertEqual(claim_job(m.pk, worker_id="alive").pk, m.pk)
        self.assertEqual(reap_stale(stale_minutes=60), 0)
        m.refresh_from_db()
        self.assertEqual(m.status, Job.RUNNING)

    def test_run_pass_limit_and_stop(self):
        for i in range(3):
            enqueue("test.echo", {"i": i})
        result = run_pass(limit=2)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(Job.objects.filter(status=Job.QUEUED).count(), 1)
        result = run_pass(stop=lambda: True)
        self.assertEqual(len(result.jobs), 0)

    @override_settings(JOB_RUNNER_INLINE=True)
    def test_inline_mode_runs_in_enqueue(self):
        j = enqueue("test.echo", {"inline": True})
        self.assertEqual(j.status, Job.DONE)
        self.assertEqual(j.result, {"echo": {"inline": True}})
        self.assertEqual(len(CALLS), 1)

    def test_execute_directly_requires_claim_only(self):
        j = enqueue("test.echo", {"direct": 1})
        claimed = claim_job(j.pk)
        execute(claimed)
        j.refresh_from_db()
        self.assertEqual(j.status, Job.DONE)

    def test_model_helpers_requeue_and_cancel(self):
        j = enqueue("test.give_up", {})
        run_pass()
        j.refresh_from_db()
        self.assertTrue(j.can_retry)
        j.requeue()
        self.assertEqual((j.status, j.attempts, j.error), (Job.QUEUED, 0, ""))
        self.assertTrue(j.cancel())
        self.assertEqual(j.status, Job.CANCELLED)
        self.assertFalse(j.cancel(), "cancel is a no-op unless queued")
        self.assertIsNone(claim_job())

    def test_queue_counts_and_registry(self):
        enqueue("test.echo", {})
        enqueue("test.echo", {}, delay=3600)
        counts = queue_counts()
        self.assertEqual(counts["queued"], 2)
        self.assertEqual(counts["due"], 1)
        self.assertIn("test.echo", registered_kinds())
        self.assertIn("notifications.deliver", registered_kinds())

    def test_services_job_queue_alias(self):
        from YSE_App.services import job_queue

        self.assertIs(job_queue.enqueue, enqueue)
        self.assertIs(job_queue.job, job)
        self.assertIs(job_queue.JobRetry, JobRetry)

    def test_unregister(self):
        @job("test.temporary")
        def _tmp(payload):
            return 1

        self.assertIn("test.temporary", registered_kinds())
        unregister("test.temporary")
        self.assertNotIn("test.temporary", registered_kinds())


class RunJobsCommandTests(TestCase):
    def setUp(self):
        CALLS.clear()

    def test_one_pass(self):
        enqueue("test.echo", {"cmd": 1})
        enqueue("test.give_up", {})
        out = io.StringIO()
        call_command("run_jobs", stdout=out)
        text = out.getvalue()
        self.assertIn("ran 2 job(s): 1 done, 1 failed", text)
        self.assertIn("test.give_up -> failed: ", text)
        self.assertEqual(Job.objects.filter(status=Job.DONE).count(), 1)

    def test_status_flag(self):
        enqueue("test.echo", {})
        out = io.StringIO()
        call_command("run_jobs", "--status", stdout=out)
        self.assertIn('"queued": 1', out.getvalue())
        self.assertIn("notifications.deliver", out.getvalue())
        self.assertEqual(Job.objects.filter(status=Job.QUEUED).count(), 1, "--status runs nothing")

    def test_kind_filter_and_limit(self):
        enqueue("test.echo", {})
        enqueue("test.no_job_kwarg", {"x": 1})
        out = io.StringIO()
        call_command("run_jobs", "--kind", "test.no_job_kwarg", "--limit", "5", stdout=out)
        self.assertEqual(Job.objects.get(kind="test.echo").status, Job.QUEUED)
        self.assertEqual(Job.objects.get(kind="test.no_job_kwarg").status, Job.DONE)

    def test_loop_with_max_passes(self):
        enqueue("test.echo", {"loop": 1})
        out = io.StringIO()
        call_command("run_jobs", "--loop", "--sleep", "0", "--max-passes", "2", stdout=out)
        self.assertIn("loop stopped after 2 pass(es)", out.getvalue())
        self.assertEqual(Job.objects.get().status, Job.DONE)


class JobQueueCronTests(TestCase):
    def test_cron_runs_one_pass(self):
        j = enqueue("test.echo", {"cron": 1})
        self.assertEqual(RunQueuedJobs.code, "YSE_App.Job_Queue.RunQueuedJobs")
        summary = RunQueuedJobs().do()
        self.assertIn("1 done", summary)
        self.assertIn("queue now: 0 queued", summary)
        j.refresh_from_db()
        self.assertEqual(j.status, Job.DONE)
        self.assertEqual(j.locked_by, "")

    @override_settings(JOB_RUNNER_CRON_ENABLED=False)
    def test_cron_disabled(self):
        j = enqueue("test.echo", {})
        self.assertEqual(RunQueuedJobs().do(), "disabled")
        j.refresh_from_db()
        self.assertEqual(j.status, Job.QUEUED)

    def test_cron_is_registered(self):
        from django.conf import settings

        self.assertIn("YSE_App.data_ingest.Job_Queue.RunQueuedJobs", settings.CRON_CLASSES)


class JobAdminAndStatusTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("jq_admin", "a@example.com", "pw", is_staff=True, is_superuser=True)
        self.plain = create_test_user("jq_plain", is_staff=False)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_admin_changelist_and_actions(self):
        failed = enqueue("test.give_up", {})
        run_pass()
        queued = enqueue("test.echo", {})
        url = reverse("admin:YSE_App_job_changelist")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "test.give_up")
        resp = self.client.post(url, {"action": "retry_jobs", "_selected_action": [failed.pk, queued.pk]}, follow=True)
        self.assertEqual(resp.status_code, 200)
        failed.refresh_from_db()
        self.assertEqual(failed.status, Job.QUEUED)
        self.assertEqual(failed.attempts, 0)
        resp = self.client.post(url, {"action": "cancel_jobs", "_selected_action": [queued.pk]}, follow=True)
        queued.refresh_from_db()
        self.assertEqual(queued.status, Job.CANCELLED)
        resp = self.client.get(reverse("admin:YSE_App_job_change", args=[failed.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_status_page_and_json_for_staff(self):
        j = enqueue("test.echo", {})
        run_pass()
        resp = self.client.get(reverse("jobs_status"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "test.echo")
        self.assertContains(resp, 'data-status="done"')
        resp = self.client.get(reverse("jobs_status_json"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["counts"]["done"], 1)
        self.assertEqual(data["recent"][0]["id"], j.pk)
        self.assertIn("notifications.deliver", data["registered_kinds"])

    def test_status_page_not_for_non_staff(self):
        client = Client()
        client.force_login(self.plain)
        self.assertNotEqual(client.get(reverse("jobs_status")).status_code, 200)
        self.assertNotEqual(client.get(reverse("jobs_status_json")).status_code, 200)
        self.assertNotEqual(Client().get(reverse("jobs_status")).status_code, 200)
