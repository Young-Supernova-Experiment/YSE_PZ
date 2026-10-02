"""Notification queue (issue #266, part of #69): notify(), preferences, delivery jobs, views."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.jobs import run_pass
from YSE_App.models import Job, Notification, NotificationPreference
from YSE_App.services import notify as svc
from YSE_App.services.notify import notify
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


@override_settings(NOTIFICATION_EMAIL_ENABLED=True, EMAIL_BACKEND=LOCMEM,
                   NOTIFICATION_BASE_URL="https://ziggy.example/yse/", NOTIFICATION_EMAIL_SUBJECT_PREFIX="[YSE-PZ]")
class NotifyServiceTests(TestCase):
    def setUp(self):
        self.alice = create_test_user("notif_alice", email="alice@example.com")
        self.bob = create_test_user("notif_bob", email="bob@example.com", is_staff=False)

    def test_notify_writes_rows_and_queues_email_delivery(self):
        rows = notify([self.alice, self.bob], "SN 2026abc was classified", "/transient_detail/2026abc/",
                      "followup_status", subject="Classification")
        self.assertEqual(len(rows), 2)
        n = rows[0]
        self.assertEqual(n.recipient, self.alice)
        self.assertEqual(n.kind, "followup_status")
        self.assertIsNone(n.read_at)
        self.assertIn("in_app", n.delivered)
        self.assertEqual(Job.objects.filter(kind=svc.DELIVER_KIND, status=Job.QUEUED).count(), 2)
        job = Job.objects.get(payload__contains='"notification_id": %d' % n.pk)
        self.assertEqual(job.payload["channels"], ["email"])
        result = run_pass()
        self.assertEqual(result.done, 2)
        self.assertEqual(len(mail.outbox), 2)
        msg = [m for m in mail.outbox if m.to == ["alice@example.com"]][0]
        self.assertEqual(msg.subject, "[YSE-PZ] Classification")
        self.assertIn("SN 2026abc was classified", msg.body)
        self.assertIn("https://ziggy.example/yse/transient_detail/2026abc/", msg.body)
        n.refresh_from_db()
        self.assertIn("sent_at", n.delivered["email"])

    def test_notify_accepts_single_user_and_dedupes_and_skips_inactive(self):
        inactive = create_test_user("notif_inactive", is_active=False)
        rows = notify(self.alice, "solo")
        self.assertEqual(len(rows), 1)
        rows = notify([self.alice, self.alice, inactive, None], "dupes")
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            notify([self.alice], "")

    @override_settings(NOTIFICATION_EMAIL_ENABLED=False)
    def test_email_disabled_globally_means_no_delivery_job(self):
        rows = notify([self.alice], "in-app only")
        self.assertEqual(len(rows), 1)
        self.assertEqual(Job.objects.count(), 0)

    def test_preference_gating(self):
        NotificationPreference.objects.create(user=self.alice, in_app=True, email=False)
        NotificationPreference.objects.create(user=self.bob, in_app=False, email=True)
        carol = create_test_user("notif_carol", email="carol@example.com")
        NotificationPreference.objects.create(user=carol, in_app=False, email=False)
        dave = create_test_user("notif_dave", email="")  # email on by default but no address
        # kind "alert": email/Slack on by default for that group (the "system" group is in-app only)
        rows = notify([self.alice, self.bob, carol, dave], "gated", "", "alert")
        by_user = {r.recipient.username: r for r in rows}
        self.assertEqual(set(by_user), {"notif_alice", "notif_bob", "notif_dave"})
        self.assertIsNone(by_user["notif_alice"].read_at)
        self.assertIsNotNone(by_user["notif_bob"].read_at, "in_app off: row pre-marked read")
        self.assertEqual(Job.objects.count(), 1, "only bob gets an email job")
        self.assertEqual(svc.unread_count(self.bob), 0)
        self.assertEqual(svc.unread_count(self.alice), 1)
        self.assertEqual(svc.unread_count(dave), 1)

    def test_email_failure_is_recorded_and_retried(self):
        rows = notify([self.alice], "flaky", "/x/", "alert")
        n = rows[0]
        with mock.patch.object(svc, "send_mail", side_effect=RuntimeError("smtp down")):
            result = run_pass()
        self.assertEqual(result.retried, 1)
        job = Job.objects.get(kind=svc.DELIVER_KIND)
        self.assertEqual(job.status, Job.QUEUED)
        self.assertIn("smtp down", job.error)
        n.refresh_from_db()
        self.assertEqual(n.delivered["email"]["error"], "smtp down")
        self.assertEqual(n.delivered["email"]["attempts"], 1)
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        run_pass()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.DONE)
        n.refresh_from_db()
        self.assertIn("sent_at", n.delivered["email"])
        self.assertNotIn("error", n.delivered["email"])
        self.assertEqual(len(mail.outbox), 1)

    def test_slack_webhook_delivery(self):
        NotificationPreference.objects.create(user=self.alice, email=False,
                                              slack_webhook_url="https://hooks.slack.test/abc")
        rows = notify([self.alice], "ping", "/transient_detail/x/", "alert", subject="Hi")
        job = Job.objects.get(kind=svc.DELIVER_KIND)
        self.assertEqual(job.payload["channels"], ["slack"])
        fake = mock.Mock()
        fake.raise_for_status.return_value = None
        with mock.patch("requests.post", return_value=fake) as post:
            run_pass()
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://hooks.slack.test/abc")
        self.assertIn("*Hi*", kwargs["json"]["text"])
        self.assertIn("https://ziggy.example/yse/transient_detail/x/", kwargs["json"]["text"])
        rows[0].refresh_from_db()
        self.assertIn("sent_at", rows[0].delivered["slack"])
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(NOTIFICATION_SLACK_ENABLED=False)
    def test_slack_disabled_globally(self):
        NotificationPreference.objects.create(user=self.alice, email=False, slack_webhook_url="https://h/x")
        notify([self.alice], "no slack")
        self.assertEqual(Job.objects.count(), 0)

    def test_deliver_handler_with_missing_notification(self):
        self.assertIn("skipped", svc.deliver_notification({"notification_id": 999999, "channels": ["email"]}))

    def test_absolute_url_helpers(self):
        self.assertEqual(svc.absolute_url("/a/b/"), "https://ziggy.example/yse/a/b/")
        self.assertEqual(svc.absolute_url("https://x/y"), "https://x/y")
        self.assertEqual(svc.absolute_url(""), "")

    def test_transient_link_and_mark_all_read(self):
        t = create_minimal_transient(self.alice, name="notif-sn")
        rows = notify([self.alice], "with transient", transient=t)
        self.assertEqual(rows[0].transient, t)
        notify([self.alice], "second")
        self.assertEqual(svc.unread_count(self.alice), 2)
        self.assertEqual(svc.mark_all_read(self.alice), 2)
        self.assertEqual(svc.unread_count(self.alice), 0)


@override_settings(NOTIFICATION_EMAIL_ENABLED=False)
class NotificationViewTests(TestCase):
    def setUp(self):
        self.user = create_test_user("notif_view_user", is_staff=False)
        self.other = create_test_user("notif_view_other", is_staff=False)
        self.client = Client()
        self.client.force_login(self.user)
        self.n1 = notify([self.user], "first note", "/dashboard/", "system", subject="One")[0]
        self.n2 = notify([self.user], "second note", "", "comment_mention")[0]
        self.n2.mark_read()
        self.foreign = notify([self.other], "not yours")[0]

    def test_list_and_unread_filter(self):
        resp = self.client.get(reverse("notification_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "first note")
        self.assertContains(resp, "second note")
        self.assertNotContains(resp, "not yours")
        self.assertContains(resp, 'data-status="unread"', count=1)
        self.assertContains(resp, "1 unread")
        resp = self.client.get(reverse("notification_list") + "?unread=1")
        self.assertContains(resp, "first note")
        self.assertNotContains(resp, "second note")

    def test_unread_count_json(self):
        resp = self.client.get(reverse("notification_unread_count"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"unread": 1})

    def test_mark_read_follow_redirects_to_url(self):
        resp = self.client.post(reverse("notification_mark_read", args=[self.n1.pk]), {"follow": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/dashboard/")
        self.n1.refresh_from_db()
        self.assertIsNotNone(self.n1.read_at)

    def test_mark_read_plain_redirects_to_list_and_json_variant(self):
        resp = self.client.post(reverse("notification_mark_read", args=[self.n1.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("notification_list"))
        self.n1.read_at = None
        self.n1.save()
        resp = self.client.post(reverse("notification_mark_read", args=[self.n1.pk]) + "?format=json")
        self.assertEqual(resp.json()["unread"], 0)

    def test_mark_read_requires_post_and_ownership(self):
        self.assertEqual(self.client.get(reverse("notification_mark_read", args=[self.n1.pk])).status_code, 405)
        self.assertEqual(self.client.post(reverse("notification_mark_read", args=[self.foreign.pk])).status_code, 404)

    def test_mark_all_read(self):
        notify([self.user], "third")
        resp = self.client.post(reverse("notification_mark_all_read"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Marked 2 notifications as read.")
        self.assertEqual(svc.unread_count(self.user), 0)

    def test_preferences_form_round_trip(self):
        resp = self.client.get(reverse("notification_preferences"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "email delivery is not enabled on this server")
        resp = self.client.post(reverse("notification_preferences"),
                                {"in_app": "on", "slack_webhook_url": "https://hooks.slack.com/services/T/B/x"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Notification preferences saved.")
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertTrue(pref.in_app)
        self.assertFalse(pref.email)
        self.assertEqual(pref.slack_webhook_url, "https://hooks.slack.com/services/T/B/x")
        resp = self.client.post(reverse("notification_preferences"), {"slack_webhook_url": "not a url"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter a valid URL")

    def test_anonymous_redirects_to_login(self):
        anon = Client()
        for name in ("notification_list", "notification_unread_count", "notification_preferences"):
            resp = anon.get(reverse(name))
            self.assertEqual(resp.status_code, 302, name)
            self.assertIn("login", resp["Location"])

    def test_navbar_has_bell_and_menu_links(self):
        resp = self.client.get("/dashboard/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="yse-notif-link"')
        self.assertContains(resp, reverse("notification_unread_count"))
        self.assertContains(resp, reverse("notification_preferences"))
        self.assertNotContains(resp, reverse("jobs_status"), msg_prefix="non-staff must not see the jobs link")
        staff = create_test_user("notif_view_staff", is_staff=True)
        client = Client()
        client.force_login(staff)
        self.assertContains(client.get("/dashboard/"), reverse("jobs_status"))

    def test_admin_lists(self):
        admin_user = User.objects.create_user("notif_admin", "n@example.com", "pw", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(admin_user)
        self.assertEqual(client.get(reverse("admin:YSE_App_notification_changelist")).status_code, 200)
        self.assertEqual(client.get(reverse("admin:YSE_App_notificationpreference_changelist")).status_code, 200)
