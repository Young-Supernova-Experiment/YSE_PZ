"""Notification center (#320: #321 preferences matrix, #322 mentions; senders on notify(); retention)."""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.common import alert
from YSE_App.data_ingest.Job_Queue import PruneNotifications
from YSE_App.jobs import enqueue, run_pass
from YSE_App.models import (
    ClassicalResource,
    FollowupStatus,
    Instrument,
    Job,
    Log,
    Notification,
    NotificationPreference,
    PrincipalInvestigator,
    Profile,
    ToOResource,
    TransientFollowup,
    UserTelescopeToFollow,
)
from YSE_App.models.notification_models import KIND_GROUPS, kind_group
from YSE_App.services import notifications as mentions
from YSE_App.services import notify as svc
from YSE_App.services.comments import create_transient_comment
from YSE_App.services.followup_notices import notify_followup_created
from YSE_App.services.notify import notify
from YSE_App.templatetags.comment_extras import format_comment_text
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.js_syntax_utils import inline_script_bodies, strip_js_literals

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_ON = dict(NOTIFICATION_EMAIL_ENABLED=True, EMAIL_BACKEND=LOCMEM, NOTIFICATION_BASE_URL="https://ziggy.example/yse/",
                NOTIFICATION_EMAIL_SUBJECT_PREFIX="[YSE-PZ]", SLACK_ENABLED=False)


def _profile(user):
    profile, _ = Profile.objects.get_or_create(user=user, defaults={
        "phone_country_code": "1", "phone_area": "831", "phone_first_three": "555", "phone_last_four": "0100",
        "phone_provider_str": "txt.example.com", **audit_fields(user)})
    return profile


def _follow(user, telescope):
    return UserTelescopeToFollow.objects.create(profile=_profile(user), telescope=telescope, **audit_fields(user))


def _resource(user, telescope, *, model=ClassicalResource, days=30, **extra):
    now = timezone.now()
    return model.objects.create(telescope=telescope, begin_date_valid=now - datetime.timedelta(days=1),
                                end_date_valid=now + datetime.timedelta(days=days), **extra, **audit_fields(user))


class KindPreferenceTests(TestCase):
    def setUp(self):
        self.user = create_test_user("kp_user", email="kp@example.com")

    def test_defaults_and_group_mapping(self):
        pref = NotificationPreference.for_user(self.user)
        self.assertEqual(kind_group("comment_mention"), "comment_mention")
        self.assertEqual(kind_group("followup_status"), "followup")
        self.assertEqual(kind_group("upload_error"), "alert")
        self.assertEqual(kind_group("job_result"), "system")
        self.assertEqual(kind_group("something_new"), "system")
        self.assertTrue(pref.allows("comment_mention", "email"))
        self.assertTrue(pref.allows("system", "in_app"))
        self.assertFalse(pref.allows("system", "email"), "system chatter is in-app only by default")
        self.assertFalse(pref.allows("system", "nope"))
        matrix = pref.matrix()
        self.assertEqual(set(matrix), {g for g, *_ in KIND_GROUPS})

    def test_set_group_channels_round_trip(self):
        pref = NotificationPreference.objects.create(user=self.user)
        pref.set_group_channels("comment_mention", email=False)
        pref.save()
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(pref.allows("comment_mention", "email"))
        self.assertTrue(pref.allows("comment_mention", "in_app"), "untouched channels keep their default")
        self.assertEqual(pref.kinds["comment_mention"]["email"], False)

    @override_settings(**EMAIL_ON)
    def test_notify_honours_kind_matrix(self):
        pref = NotificationPreference.objects.create(user=self.user)
        pref.set_group_channels("comment_mention", email=False)
        pref.set_group_channels("system", in_app=False)
        pref.save()
        # mention: in-app row, no email job (acceptance criterion of #321)
        rows = notify([self.user], "hi @kp_user", "/x/", "comment_mention")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].read_at)
        self.assertEqual(Job.objects.filter(kind=svc.DELIVER_KIND).count(), 0)
        # follow-up: defaults -> in-app + email
        notify([self.user], "followup", "/y/", "followup_request")
        self.assertEqual(Job.objects.filter(kind=svc.DELIVER_KIND).count(), 1)
        # system: in-app off and email off by default -> no row at all
        self.assertEqual(notify([self.user], "system noise", "", "system"), [])
        # system with email on -> row pre-marked read, email job
        pref.set_group_channels("system", email=True)
        pref.save()
        rows = notify([self.user], "system mail", "", "system")
        self.assertIsNotNone(rows[0].read_at)
        self.assertEqual(Job.objects.filter(kind=svc.DELIVER_KIND).count(), 2)

    @override_settings(**EMAIL_ON)
    def test_html_payload_sends_multipart_email(self):
        rows = notify([self.user], "plain text", "/t/", "alert", subject="Alert", html="<h1>Hello</h1><p>world</p>")
        self.assertEqual(rows[0].html, "<h1>Hello</h1><p>world</p>")
        run_pass()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn("plain text", msg.body)
        self.assertEqual(msg.alternatives[0][1], "text/html")
        self.assertIn("<h1>Hello</h1>", msg.alternatives[0][0])
        self.assertEqual(svc.html_to_text("<p>a</p><br>b<br />\n\n\nc"), "a\n\nb\n\nc")

    def test_exclude_skips_actor(self):
        other = create_test_user("kp_other")
        rows = notify([self.user, other], "x", exclude=[self.user])
        self.assertEqual([r.recipient for r in rows], [other])
        rows = notify([self.user, other], "y", exclude=[other.pk])
        self.assertEqual([r.recipient for r in rows], [self.user])


@override_settings(**EMAIL_ON)
class CommentMentionTests(TestCase):
    def setUp(self):
        self.author = create_test_user("cm_author", email="author@example.com")
        self.alice = create_test_user("cm_alice", email="alice@example.com", is_staff=False)
        self.bob = create_test_user("cm_bob", email="bob@example.com", is_staff=False)
        self.transient = create_minimal_transient(self.author, name="2026cmt")
        _obs, self.instrument, _band = create_instrument_stack(self.author, obs_group_name="cm-stack")
        self.instrument.name = "Binospec"
        self.instrument.save()
        self.telescope = self.instrument.telescope

    def test_parse_mentions(self):
        m = mentions.parse_mentions("hey @cm_alice and @Channel, see #Binospec + #bino-spec and issue #12 x@y.z a&#39;")
        self.assertEqual(m.usernames, ["cm_alice"])
        self.assertTrue(m.channel)
        self.assertEqual(m.instrument_tokens, ["Binospec"], "#12 is not an instrument token; #bino-spec dedupes")
        self.assertFalse(mentions.parse_mentions("nothing here"))

    def test_user_mention_creates_notification_and_email(self):
        log = create_transient_comment(transient=self.transient, comment="@cm_alice please check @cm_author",
                                       user=self.author, is_public=True)
        rows = Notification.objects.filter(kind="comment_mention")
        self.assertEqual([r.recipient for r in rows], [self.alice], "author excluded, bob not mentioned")
        n = rows[0]
        self.assertEqual(n.transient, self.transient)
        self.assertEqual(n.url, "/transient_detail/%s/" % self.transient.slug)
        self.assertEqual(n.payload["log_id"], log.pk)
        self.assertIn("Comment added!", n.html)
        run_pass()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["alice@example.com"])
        self.assertEqual(msg.subject, "[YSE-PZ] new comment added to event 2026cmt")
        html = msg.alternatives[0][0]
        self.assertIn("https://ziggy.example/yse/transient_detail/%s/" % self.transient.slug, html)
        self.assertIn("cm_author says:", html)
        self.assertIn("@cm_alice please check", html)

    def test_mention_email_off_keeps_in_app_row(self):
        pref = NotificationPreference.objects.create(user=self.alice)
        pref.set_group_channels("comment_mention", email=False)
        pref.save()
        create_transient_comment(transient=self.transient, comment="@cm_alice ping", user=self.author, is_public=True)
        self.assertEqual(svc.unread_count(self.alice), 1)
        self.assertEqual(Job.objects.count(), 0)

    def test_channel_mention_respects_visibility(self):
        group = Group.objects.create(name="cm-private")
        self.alice.groups.add(group)
        log = Log.objects.create(transient=self.transient, comment="@channel private", is_public=False,
                                 created_by=self.author, modified_by=self.author)
        log.groups.set([group])
        rows = mentions.notify_comment_mentions(log)
        recipients = {r.recipient.username for r in rows}
        self.assertIn("cm_alice", recipients)
        self.assertNotIn("cm_bob", recipients)
        self.assertNotIn("cm_author", recipients)
        # legacy helper keeps its contract (author included, sorted unique emails)
        self.assertEqual(mentions.collect_mention_emails(log.comment, log=log), ["alice@example.com", "author@example.com"])

    def test_instrument_mention_targets_resource_holders_and_followers(self):
        holders = Group.objects.create(name="MMT-holders")
        self.alice.groups.add(holders)
        pi_user = create_test_user("cm_pi", email="PI@example.com", is_staff=False)
        pi = PrincipalInvestigator.objects.create(name="PI", email="pi@example.com", **audit_fields(self.author))
        resource = _resource(self.author, self.telescope, principal_investigator=pi)
        resource.groups.add(holders)
        follower = create_test_user("cm_follower", email="f@example.com", is_staff=False)
        _follow(follower, self.telescope)
        expired_user = create_test_user("cm_expired", is_staff=False)
        expired = Group.objects.create(name="expired-holders")
        expired_user.groups.add(expired)
        _resource(self.author, self.telescope, model=ToOResource, days=-5).groups.add(expired)
        other_tel_user = create_test_user("cm_othertel", is_staff=False)
        _obs, other_inst, _b = create_instrument_stack(self.author, obs_group_name="cm-other")
        _follow(other_tel_user, other_inst.telescope)

        users, instruments = mentions.collect_mention_users("please observe with #binospec tonight")
        self.assertEqual(instruments, [self.instrument])
        self.assertEqual({u.username for u in users}, {"cm_alice", "cm_pi", "cm_follower"})

        log = create_transient_comment(transient=self.transient, comment="#Binospec can we get a spectrum?",
                                       user=self.author, is_public=True)
        rows = Notification.objects.filter(kind="comment_mention")
        self.assertEqual({r.recipient.username for r in rows}, {"cm_alice", "cm_pi", "cm_follower"})
        self.assertEqual(rows[0].payload["mentioned_instruments"], ["Binospec"])
        self.assertEqual(rows[0].payload["log_id"], log.pk)

    def test_unknown_instrument_or_user_is_silent(self):
        create_transient_comment(transient=self.transient, comment="#NoSuchInstrument @nobody_here",
                                 user=self.author, is_public=True)
        self.assertEqual(Notification.objects.count(), 0)

    def test_format_comment_text_highlights_both(self):
        html = format_comment_text("@cm_alice see #Binospec <b>x</b> a@b.c")
        self.assertIn('<span class="yse-mention">@cm_alice</span>', html)
        self.assertIn('<span class="yse-mention yse-mention-instrument">#Binospec</span>', html)
        self.assertIn("&lt;b&gt;", html)
        self.assertNotIn('<span class="yse-mention">@b', html)

    def test_mention_suggest_endpoint(self):
        client = Client()
        client.force_login(self.author)
        resp = client.get(reverse("mention_suggest") + "?q=@cm_a")
        self.assertEqual(resp.status_code, 200)
        values = [r["value"] for r in resp.json()["results"]]
        self.assertIn("@cm_alice", values)
        self.assertIn("@cm_author", values)
        self.assertNotIn("@cm_bob", values)
        self.assertFalse(any(v.startswith("#") for v in values))
        resp = client.get(reverse("mention_suggest") + "?q=%23bino")
        results = resp.json()["results"]
        self.assertEqual([r["value"] for r in results], ["#Binospec"])
        self.assertEqual(results[0]["type"], "instrument")
        self.assertIn(self.telescope.name, results[0]["label"])
        resp = client.get(reverse("mention_suggest") + "?q=ch")
        self.assertIn("@channel", [r["value"] for r in resp.json()["results"]])
        self.assertEqual(Client().get(reverse("mention_suggest")).status_code, 302)

    def test_comment_box_has_autocomplete_script(self):
        client = Client()
        client.force_login(self.author)
        resp = client.get(reverse("transient_detail", args=[self.transient.slug]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('data-mention-url="%s"' % reverse("mention_suggest"), html)
        self.assertIn('id="yse-mention-menu"', html)
        self.assertIn("#instrument to notify", html)
        bodies = [b for b in inline_script_bodies(html) if "yseMentionsInit" in b]
        self.assertEqual(len(bodies), 1)
        stripped = strip_js_literals(bodies[0])
        for open_ch, close_ch in (("(", ")"), ("[", "]"), ("{", "}")):
            self.assertEqual(stripped.count(open_ch), stripped.count(close_ch), open_ch)


@override_settings(**EMAIL_ON)
class FollowupNoticeTests(TestCase):
    def setUp(self):
        self.requester = create_test_user("fn_requester", email="req@example.com")
        self.follower = create_test_user("fn_follower", email="fol@example.com", is_staff=False)
        self.bystander = create_test_user("fn_bystander", is_staff=False)
        self.transient = create_minimal_transient(self.requester, name="2026fnt")
        _obs, instrument, _b = create_instrument_stack(self.requester, obs_group_name="fn-stack")
        self.telescope = instrument.telescope
        self.status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.requester))
        _follow(self.follower, self.telescope)
        _follow(self.requester, self.telescope)  # requester follows too: must not notify themself
        self.resource = _resource(self.requester, self.telescope)

    def _followup(self, **kwargs):
        now = timezone.now()
        fields = dict(transient=self.transient, status=self.status, valid_start=now,
                      valid_stop=now + datetime.timedelta(days=3), classical_resource=self.resource,
                      **audit_fields(self.requester))
        fields.update(kwargs)
        return TransientFollowup.objects.create(**fields)

    def test_post_save_notifies_telescope_followers(self):
        followup = self._followup()
        rows = Notification.objects.filter(kind="followup_request")
        self.assertEqual([r.recipient for r in rows], [self.follower])
        n = rows[0]
        self.assertEqual(n.subject, "New %s request for 2026fnt" % self.telescope.name)
        self.assertEqual(n.payload["followup_id"], followup.pk)
        self.assertIn("Followup requested for", n.html)
        run_pass()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["fol@example.com"])
        self.assertIn("https://ziggy.example/yse/transient_detail/%s/" % self.transient.slug, mail.outbox[0].alternatives[0][0])
        # an update is not a new request
        followup.priority = 2.0
        followup.save()
        self.assertEqual(Notification.objects.filter(kind="followup_request").count(), 1)

    def test_too_resource_followups_notify_too(self):
        too = _resource(self.requester, self.telescope, model=ToOResource)
        self._followup(classical_resource=None, too_resource=too)
        self.assertEqual(Notification.objects.filter(kind="followup_request", recipient=self.follower).count(), 1)

    def test_followup_group_preference_off(self):
        pref = NotificationPreference.objects.create(user=self.follower)
        pref.set_group_channels("followup", in_app=False, email=False, slack=False)
        pref.save()
        self._followup()
        self.assertEqual(Notification.objects.count(), 0)

    def test_notification_failure_does_not_break_save(self):
        with mock.patch("YSE_App.services.followup_notices.notify_followup_created", side_effect=RuntimeError("x")):
            followup = self._followup()
        self.assertIsNotNone(followup.pk)

    def test_direct_helper_with_explicit_recipients(self):
        followup = self._followup()
        Notification.objects.all().delete()
        rows = notify_followup_created(followup, recipients=[self.bystander, self.requester])
        self.assertEqual([r.recipient for r in rows], [self.bystander])

    def test_legacy_send_following_notice(self):
        rows = alert.SendFollowingNotice(1, "2026fnt", self.telescope, _profile(self.follower))
        self.assertEqual(rows[0].kind, "followup_request")
        self.assertIn("2026fnt", rows[0].subject)
        with self.assertRaises(RuntimeError):
            alert.SendFollowingNotice(1, "2026fnt", self.telescope, _profile(self.bystander.__class__.objects.create_user("fn_noemail", email="")))


@override_settings(**EMAIL_ON, LOCAL_UTC_OFFSET=-7)
class AlertSenderTests(TestCase):
    def setUp(self):
        self.a = create_test_user("al_a", email="a@example.com")
        self.b = create_test_user("al_b", email="b@example.com", is_staff=False)
        User.objects.create_user("admin", "admin@example.com", "pw")

    def test_send_transient_alert_uses_notify_and_sms(self):
        _profile(self.a)
        with mock.patch.object(alert, "sendsms") as sms:
            rows = alert.SendTransientAlert(1, "2026alert", 10.0, 20.0)
        self.assertEqual({r.recipient.username for r in rows}, {"al_a", "al_b"}, "admin excluded")
        self.assertEqual(rows[0].kind, "alert")
        self.assertIn("New K2 Transient!", rows[0].html)
        self.assertIn("Detail: https://ziggy.example/yse/transient_detail/2026alert/", rows[0].text)
        run_pass()
        self.assertEqual(len(mail.outbox), 2)
        if sms.called:
            self.assertEqual(sms.call_args[0][1], "8315550100@txt.example.com")

    def test_alert_group_preference_off(self):
        pref = NotificationPreference.objects.create(user=self.b)
        pref.set_group_channels("alert", in_app=False, email=False, slack=False)
        pref.save()
        with mock.patch.object(alert, "sendsms"):
            rows = alert.SendTransientAlert(1, "2026alert", 10.0, 20.0)
        self.assertEqual([r.recipient for r in rows], [self.a])

    def test_sendemail_and_sendsms_use_django_backend(self):
        self.assertTrue(alert.sendemail("from@example.com", "to@example.com", "Subj", "<p>Hi <b>there</b></p>",
                                        "login", "pw", "smtp:587"))
        self.assertTrue(alert.send_email_simple("to2@example.com", "S2", "<p>x</p>"))
        self.assertTrue(alert.sendsms(None, "5551234@txt.example.com", "S", "plain"))
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(mail.outbox[0].body, "Hi there")
        self.assertEqual(mail.outbox[0].alternatives[0][0], "<p>Hi <b>there</b></p>")
        self.assertEqual(mail.outbox[0].from_email, "from@example.com")
        self.assertEqual(mail.outbox[2].body, "plain")
        self.assertFalse(hasattr(alert, "smtplib"))
        with mock.patch("YSE_App.common.alert.EmailMultiAlternatives.send", side_effect=RuntimeError("down")):
            self.assertFalse(alert.sendemail(None, "x@example.com", "S", "<p>fail</p>"))

    def test_upload_failure_helper(self):
        from YSE_App.data_utils import _notify_upload_failure

        rows = _notify_upload_failure(self.a, "Transient Upload Failure", "Alert : YSE_PZ Failed to upload transient X")
        self.assertEqual(rows[0].kind, "upload_error")
        self.assertEqual(rows[0].subject, "Transient Upload Failure")
        self.assertEqual(_notify_upload_failure(None, "s", "m"), [])
        with mock.patch("YSE_App.services.notify.notify", side_effect=RuntimeError("db")):
            self.assertEqual(_notify_upload_failure(self.a, "s", "m"), [])


class RetentionTests(TestCase):
    def setUp(self):
        self.user = create_test_user("rt_user")
        now = timezone.now()
        self.old_read = notify([self.user], "old read")[0]
        self.old_read.mark_read()
        Notification.objects.filter(pk=self.old_read.pk).update(created=now - datetime.timedelta(days=100))
        self.old_unread = notify([self.user], "old unread")[0]
        Notification.objects.filter(pk=self.old_unread.pk).update(created=now - datetime.timedelta(days=400))
        self.recent_read = notify([self.user], "recent read")[0]
        self.recent_read.mark_read()
        self.fresh = notify([self.user], "fresh")[0]
        self.old_job = enqueue("test.echo_rt", {})
        Job.objects.filter(pk=self.old_job.pk).update(status=Job.DONE, finished_at=now - datetime.timedelta(days=40))
        self.recent_job = enqueue("test.echo_rt", {})
        Job.objects.filter(pk=self.recent_job.pk).update(status=Job.FAILED, finished_at=now - datetime.timedelta(days=2))
        self.queued_job = enqueue("test.echo_rt", {})
        Job.objects.filter(pk=self.queued_job.pk).update(run_after=now - datetime.timedelta(days=100))

    def test_prune_defaults(self):
        result = svc.prune(dry_run=True)
        self.assertEqual(result, {"notifications_read": 1, "notifications_unread": 1, "jobs": 1})
        self.assertEqual(Notification.objects.count(), 4)
        result = svc.prune()
        self.assertEqual(result, {"notifications_read": 1, "notifications_unread": 1, "jobs": 1})
        self.assertEqual(set(Notification.objects.values_list("text", flat=True)), {"recent read", "fresh"})
        self.assertEqual(set(Job.objects.values_list("pk", flat=True)), {self.recent_job.pk, self.queued_job.pk})

    @override_settings(NOTIFICATION_RETENTION_DAYS=0, NOTIFICATION_UNREAD_RETENTION_DAYS=0, JOB_RETENTION_DAYS=1)
    def test_zero_disables_and_settings_apply(self):
        result = svc.prune()
        self.assertEqual(result, {"notifications_read": 0, "notifications_unread": 0, "jobs": 2})
        self.assertEqual(Notification.objects.count(), 4)
        self.assertEqual(Job.objects.filter(pk=self.queued_job.pk).count(), 1, "queued jobs never pruned")

    def test_prune_job_kind_and_cron_and_command(self):
        job = enqueue(svc.PRUNE_KIND, {"job_days": 1})
        result = run_pass(kinds=[svc.PRUNE_KIND])
        self.assertEqual(result.done, 1)
        job.refresh_from_db()
        self.assertEqual(job.result["jobs"], 2)
        self.assertEqual(job.result["notifications_read"], 1)
        Notification.objects.filter(pk=self.recent_read.pk).update(created=timezone.now() - datetime.timedelta(days=100))
        self.assertIn("pruned 1 read", PruneNotifications().do())
        with override_settings(NOTIFICATION_PRUNE_CRON_ENABLED=False):
            self.assertEqual(PruneNotifications().do(), "disabled")
        self.assertIn("YSE_App.data_ingest.Job_Queue.PruneNotifications", __import__("django.conf").conf.settings.CRON_CLASSES)
        import io

        out = io.StringIO()
        call_command("prune_notifications", "--dry-run", "--unread-days", "1", stdout=out)
        self.assertIn("would delete 0 read notifications, 0 unread notifications", out.getvalue())


@override_settings(NOTIFICATION_EMAIL_ENABLED=True, EMAIL_BACKEND=LOCMEM)
class PreferencePageTests(TestCase):
    def setUp(self):
        self.user = create_test_user("pp_user", is_staff=False, email="pp@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_matrix_renders_with_defaults(self):
        resp = self.client.get(reverse("notification_preferences"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "yse-pref-matrix")
        self.assertContains(resp, "Comment mentions")
        self.assertContains(resp, "Follow-up requests and status")
        self.assertContains(resp, 'name="kind_system_email"')
        html = resp.content.decode()
        self.assertRegex(html, r'name="kind_comment_mention_email"[^>]*checked')
        self.assertNotRegex(html, r'name="kind_system_email"[^>]*checked')

    def test_matrix_saves(self):
        data = {"in_app": "on", "email": "on", "slack_webhook_url": ""}
        for group, *_ in KIND_GROUPS:
            for channel in ("in_app", "email", "slack"):
                data["kind_%s_%s" % (group, channel)] = "on"
        del data["kind_comment_mention_email"]
        del data["kind_followup_in_app"]
        resp = self.client.post(reverse("notification_preferences"), data, follow=True)
        self.assertContains(resp, "Notification preferences saved.")
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(pref.allows("comment_mention", "email"))
        self.assertFalse(pref.allows("followup_request", "in_app"))
        self.assertTrue(pref.allows("system", "email"))
        self.assertTrue(pref.email)
        rows = notify([self.user], "mention", "", "comment_mention")
        self.assertEqual(len(rows), 1)
        self.assertEqual(Job.objects.count(), 0)

    def test_list_kind_filter_and_labels(self):
        notify([self.user], "a mention", "", "comment_mention")
        notify([self.user], "a followup", "", "followup_status")
        resp = self.client.get(reverse("notification_list") + "?kind=comment_mention")
        self.assertContains(resp, "a mention")
        self.assertNotContains(resp, "a followup")
        self.assertContains(resp, "Comment mention")
        self.assertContains(resp, "yse-notif-kind-filter")
        resp = self.client.get(reverse("notification_list") + "?kind=followup_status&unread=1")
        self.assertContains(resp, "a followup")
        self.assertContains(resp, "Follow-up status")
        self.assertNotContains(resp, "a mention")
        self.assertContains(resp, 'name="next" value="%s?unread=1&amp;kind=followup_status"' % reverse("notification_list"))

    def test_personal_dashboard_links_preferences(self):
        _obs, instrument, _b = create_instrument_stack(self.user, obs_group_name="pp-stack")
        _follow(self.user, instrument.telescope)
        resp = self.client.get(reverse("personaldashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "yse-notif-prefs-link")
