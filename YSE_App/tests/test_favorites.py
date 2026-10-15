"""Favorite transients and activity notifications (#323), Slack DM channel and notifications API (#321),
paper-interest table column (#290); umbrella #320."""

from __future__ import annotations

import datetime
import json
from unittest import mock

from django.contrib.auth.models import Group
from django.core import mail
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.jobs import run_pass
from YSE_App.models import (
    ClassicalResource,
    FollowupStatus,
    Job,
    Notification,
    NotificationPreference,
    Transient,
    TransientFollowup,
    TransientInterest,
    TransientPhotData,
    UserFavoriteTransient,
)
from YSE_App.models.notification_models import kind_group
from YSE_App.services import favorites as svc
from YSE_App.services import notify as notify_svc
from YSE_App.services import photstat
from YSE_App.services.comments import create_transient_comment
from YSE_App.services.interests import register_interest
from YSE_App.table_utils import FavoriteTransientTable, SearchTransientTable, TransientTable
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)
from YSE_App.tests.js_syntax_utils import html_script_problems

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_ON = dict(NOTIFICATION_EMAIL_ENABLED=True, EMAIL_BACKEND=LOCMEM,
                NOTIFICATION_BASE_URL="https://ziggy.example/yse/", SLACK_ENABLED=False)
XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _member(username, *groups, staff=False):
    user = create_test_user(username, is_staff=staff, is_superuser=False, email="%s@example.com" % username)
    user.groups.set(list(groups))
    return user


class FavoriteBase(TestCase):
    def setUp(self):
        self.yse = Group.objects.create(name="YSE")
        self.admin = create_test_user("fav_admin", is_staff=True, is_superuser=True)
        self.alice = _member("alice", self.yse)
        self.bob = _member("bob", self.yse)
        self.transient = create_minimal_transient(self.admin, name="2026fav", obs_group_name="fav-group")
        attach_synthetic_photometry(self.admin, self.transient, n_points=3)  # public: everyone may see it
        self.other = create_minimal_transient(self.admin, name="2026oth", obs_group_name="fav-group")
        attach_synthetic_photometry(self.admin, self.other, n_points=2)

    def star(self, user, transient=None):
        return svc.add(user, transient or self.transient)[0]

    def activity(self, user):
        return list(Notification.objects.filter(recipient=user, kind=svc.KIND).order_by("id"))


class ServiceTests(FavoriteBase):
    def test_add_toggle_remove_and_state(self):
        self.assertEqual(svc.favorite_state(self.alice, self.transient.id), (False, 0))
        self.assertTrue(svc.toggle(self.alice, self.transient))
        self.assertTrue(svc.is_favorite(self.alice, self.transient.id))
        self.star(self.bob)
        self.assertEqual(svc.favorite_state(self.alice, self.transient.id), (True, 2))
        self.assertEqual(svc.favorite_ids(self.alice), [self.transient.id])
        row, created = svc.add(self.alice, self.transient)
        self.assertFalse(created)
        self.assertFalse(svc.toggle(self.alice, self.transient))
        self.assertFalse(svc.remove(self.alice, self.transient))
        self.assertEqual(svc.favorite_state(self.alice, self.transient.id), (False, 1))
        self.assertEqual(svc.favorite_counts([self.transient.id, self.other.id]), {self.transient.id: 1})

    def test_favorites_for_user_orders_newest_first(self):
        first = self.star(self.alice, self.other)
        UserFavoriteTransient.objects.filter(pk=first.pk).update(created=timezone.now() - datetime.timedelta(days=1))
        self.star(self.alice)
        names = [t.name for t in svc.favorites_for_user(self.alice)]
        self.assertEqual(names, ["2026fav", "2026oth"])
        self.assertTrue(all(hasattr(t, "favorited_at") for t in svc.favorites_for_user(self.alice)))

    def test_favoriters_skip_inactive_and_excluded(self):
        self.star(self.alice)
        self.star(self.bob)
        self.bob.is_active = False
        self.bob.save()
        self.assertEqual(svc.favoriters(self.transient.id), [self.alice])
        self.assertEqual(svc.favoriters(self.transient.id, exclude=[self.alice]), [])

    def test_kind_group(self):
        self.assertEqual(kind_group("favorite_activity"), "favorite")
        pref = NotificationPreference(user=self.alice)
        self.assertTrue(pref.allows("favorite_activity", "in_app"))
        self.assertTrue(pref.allows("favorite_activity", "email"))


class ActivityTests(FavoriteBase):
    def test_comment_by_another_user_yields_one_notification(self):
        """Acceptance (#323): star, then a comment by someone else -> one in-app row for the starrer."""
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="Looks like a young SN Ia.", user=self.bob)
        rows = self.activity(self.alice)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(row.read_at)
        self.assertEqual(row.transient_id, self.transient.id)
        self.assertIn('new comment: "Looks like a young SN Ia."', row.text)
        self.assertIn("(bob)", row.text)
        self.assertEqual(row.url, reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(len(row.payload["events"]), 1)
        self.assertEqual(self.activity(self.bob), [])  # the actor never hears about their own event

    def test_own_comment_is_not_an_event(self):
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="my own note", user=self.alice)
        self.assertEqual(self.activity(self.alice), [])

    def test_restricted_comment_text_only_for_its_audience(self):
        carol = _member("carol", Group.objects.create(name="UCSC"))
        self.star(self.alice)  # in YSE: may read the comment
        self.star(carol)       # not in YSE: hears of the comment, not its text
        create_transient_comment(transient=self.transient, comment="secret", user=self.bob, is_public=False,
                                 audience_groups=[self.yse])
        [row] = self.activity(self.alice)
        self.assertIn('new comment: "secret"', row.text)
        [row] = self.activity(carol)
        self.assertNotIn("secret", row.text)
        self.assertIn("restricted", row.text)

    def test_log_saved_directly_hides_text_unless_public(self):
        from YSE_App.models import Log

        self.star(self.alice)
        Log.objects.create(transient=self.transient, comment="script note", is_public=False, **audit_fields(self.bob))
        Log.objects.create(transient=self.transient, comment="public note", is_public=True, **audit_fields(self.bob))
        [row] = self.activity(self.alice)
        texts = [e["text"] for e in row.payload["events"]]
        self.assertEqual(texts, ["new comment (restricted to collaboration groups)", 'new comment: "public note"'])

    def test_events_within_the_window_share_one_row(self):
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="first", user=self.bob)
        create_transient_comment(transient=self.transient, comment="second", user=self.bob)
        [row] = self.activity(self.alice)
        self.assertEqual(len(row.payload["events"]), 2)
        self.assertTrue(row.text.startswith("2 updates on 2026fav:"), row.text)
        self.assertIn("- new comment: \"first\"", row.text)
        self.assertIn("- new comment: \"second\"", row.text)

    @override_settings(FAVORITE_ACTIVITY_BATCH_MINUTES=0)
    def test_window_zero_sends_every_event(self):
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="first", user=self.bob)
        create_transient_comment(transient=self.transient, comment="second", user=self.bob)
        self.assertEqual(len(self.activity(self.alice)), 2)

    def test_read_row_is_not_extended(self):
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="first", user=self.bob)
        [row] = self.activity(self.alice)
        row.mark_read()
        create_transient_comment(transient=self.transient, comment="second", user=self.bob)
        rows = self.activity(self.alice)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1].read_at)

    def test_old_row_is_not_extended(self):
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="first", user=self.bob)
        Notification.objects.filter(kind=svc.KIND).update(created=timezone.now() - datetime.timedelta(hours=2))
        create_transient_comment(transient=self.transient, comment="second", user=self.bob)
        self.assertEqual(len(self.activity(self.alice)), 2)

    @override_settings(**EMAIL_ON)
    def test_email_delivery_is_delayed_and_carries_every_event(self):
        self.star(self.alice)
        create_transient_comment(transient=self.transient, comment="first", user=self.bob)
        [job] = Job.objects.filter(kind=notify_svc.DELIVER_KIND)
        self.assertGreater(job.run_after, timezone.now() + datetime.timedelta(minutes=50))
        run_pass()
        self.assertEqual(len(mail.outbox), 0)  # not due yet
        create_transient_comment(transient=self.transient, comment="second", user=self.bob)
        self.assertEqual(Job.objects.filter(kind=notify_svc.DELIVER_KIND).count(), 1)
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now() - datetime.timedelta(seconds=1))
        run_pass()
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("2 updates on 2026fav", body)
        self.assertIn("second", body)
        self.assertIn("Favorite activity: 2026fav", mail.outbox[0].subject)

    def test_preference_group_off_means_no_row(self):
        self.star(self.alice)
        pref = NotificationPreference(user=self.alice)
        pref.set_group_channels("favorite", in_app=False, email=False, slack=False)
        pref.save()
        create_transient_comment(transient=self.transient, comment="quiet", user=self.bob)
        self.assertEqual(self.activity(self.alice), [])

    def test_status_and_redshift_changes_come_from_the_audit_log(self):
        self.star(self.alice)
        statuses = ensure_transient_statuses(self.admin)
        self.transient.status = statuses["Following"]
        self.transient.redshift = 0.031
        self.transient.save()
        [row] = self.activity(self.alice)
        self.assertIn("status changed New -> Following", row.text)
        self.assertIn("redshift set to 0.031", row.text)
        # an unrelated edit is silent
        self.transient.mw_ebv = 0.05
        self.transient.save()
        self.assertEqual(len(self.activity(self.alice)[0].payload["events"]), 1)

    def test_new_spectrum(self):
        self.star(self.alice)
        attach_synthetic_spectrum(self.bob, self.transient)
        [row] = self.activity(self.alice)
        self.assertIn("new spectrum from", row.text)
        self.assertIn("observed", row.text)

    def test_photometry_recompute_counts_new_points_and_merges(self):
        photstat.recompute(self.transient.id)  # the row exists from now on
        self.star(self.alice)
        photometry = self.transient.transientphotometry_set.first()
        band = photometry.transientphotdata_set.first().band
        for i in range(2):
            TransientPhotData.objects.create(photometry=photometry, band=band, mag=17.5 - 0.1 * i, mag_err=0.05,
                                             obs_date=timezone.now() + datetime.timedelta(minutes=i),
                                             **audit_fields(self.admin))
        photstat.recompute(self.transient.id)
        [row] = self.activity(self.alice)
        self.assertEqual(len(row.payload["events"]), 1)
        self.assertIn("2 new photometry points, latest 17.40", row.text)
        TransientPhotData.objects.create(photometry=photometry, band=band, mag=17.2, mag_err=0.05,
                                         obs_date=timezone.now() + datetime.timedelta(minutes=5),
                                         **audit_fields(self.admin))
        photstat.recompute(self.transient.id)
        [row] = self.activity(self.alice)
        self.assertEqual(len(row.payload["events"]), 1)  # merged into the photometry event
        self.assertEqual(row.payload["events"][0]["count"], 3)
        self.assertIn("3 new photometry points, latest 17.20", row.text)

    def test_followup_request_and_status_change(self):
        self.star(self.alice)
        _obs, instrument, _b = create_instrument_stack(self.bob, obs_group_name="fav-stack")
        now = timezone.now()
        resource = ClassicalResource.objects.create(
            telescope=instrument.telescope, begin_date_valid=now - datetime.timedelta(days=1),
            end_date_valid=now + datetime.timedelta(days=30), **audit_fields(self.bob))
        requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.bob))
        done, _ = FollowupStatus.objects.get_or_create(name="Successful", defaults=audit_fields(self.bob))
        followup = TransientFollowup.objects.create(
            transient=self.transient, status=requested, valid_start=now, valid_stop=now + datetime.timedelta(days=3),
            classical_resource=resource, **audit_fields(self.bob))
        [row] = self.activity(self.alice)
        self.assertIn("follow-up requested on %s (Requested)" % instrument.telescope.name, row.text)
        followup.comment = "no change of status"
        followup.save()
        self.assertEqual(len(self.activity(self.alice)[0].payload["events"]), 1)
        followup.status = done
        followup.save()
        [row] = self.activity(self.alice)
        self.assertEqual(len(row.payload["events"]), 2)
        self.assertIn("status Requested -> Successful", row.text)

    def test_hook_failure_never_breaks_the_save(self):
        self.star(self.alice)
        with mock.patch.object(svc, "record_activity", side_effect=RuntimeError("boom")):
            log = create_transient_comment(transient=self.transient, comment="still saved", user=self.bob)
        self.assertIsNotNone(log.pk)


class PageTests(FavoriteBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.alice)

    def test_toggle_view_json_and_redirect(self):
        url = reverse("favorite_toggle", kwargs={"transient_id": self.transient.id})
        response = self.client.post(url, **XHR)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"transient": self.transient.id, "favorite": True, "count": 1})
        response = self.client.post(url, {"state": "on"}, **XHR)
        self.assertTrue(response.json()["favorite"])
        response = self.client.post(url)
        self.assertRedirects(response, reverse("transient_detail", kwargs={"slug": self.transient.slug}),
                             fetch_redirect_response=False)
        self.assertFalse(svc.is_favorite(self.alice, self.transient.id))
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_toggle_requires_visibility(self):
        hidden = create_minimal_transient(self.admin, name="2026hid", obs_group_name="fav-group")
        private = Group.objects.create(name="private-club")
        attach_synthetic_photometry(self.admin, hidden, n_points=2).groups.add(private)
        response = self.client.post(reverse("favorite_toggle", kwargs={"transient_id": hidden.id}), **XHR)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserFavoriteTransient.objects.filter(transient=hidden).exists())

    def test_ids_json(self):
        self.star(self.alice)
        response = self.client.get(reverse("favorite_ids"))
        self.assertEqual(response.json(), {"ids": [self.transient.id]})

    def test_detail_header_star_state(self):
        url = reverse("transient_detail", kwargs={"slug": self.transient.slug})
        html = self.client.get(url).content.decode()
        self.assertIn('id="yse-fav-header"', html)
        self.assertIn('data-state="off"', html.split('id="yse-fav-header"')[1][:400])
        self.star(self.alice)
        self.star(self.bob)
        html = self.client.get(url).content.decode()
        header = html.split('id="yse-fav-header"')[1][:600]
        self.assertIn('data-state="on"', header)
        self.assertIn('fa-star"', header)
        self.assertIn(">2</span>", header)

    def test_base_menu_link_and_star_script(self):
        html = self.client.get(reverse("my_favorites")).content.decode()
        self.assertIn('id="yse-fav-menu-link"', html)
        self.assertIn('data-toggle-url="%s"' % reverse("favorite_toggle", kwargs={"transient_id": 0}), html)
        self.assertIn('data-ids-url="%s"' % reverse("favorite_ids"), html)
        self.assertIn("yse-fav-star", html)
        self.assertEqual(html_script_problems(html), [])

    def test_my_favorites_page(self):
        response = self.client.get(reverse("my_favorites"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("yse-favorites-empty", response.content.decode())
        self.star(self.alice)
        html = self.client.get(reverse("my_favorites")).content.decode()
        self.assertIn("2026fav", html)
        self.assertIn('id="favorite_transient_tbl"', html)
        self.assertIn('data-state="unknown"', html)  # painted by the ids fetch
        self.assertIn("Starred", html)
        self.assertNotIn("2026oth", html)

    def test_dashboard_box_and_section_fragment(self):
        html = self.client.get(reverse("personaldashboard")).content.decode()
        self.assertIn('id="yse-favorites-box"', html)
        self.assertIn('data-url="%s"' % reverse("my_favorites_section"), html)
        fragment = self.client.get(reverse("my_favorites_section")).content.decode()
        self.assertIn("yse-favorites-empty", fragment)
        self.star(self.alice)
        fragment = self.client.get(reverse("my_favorites_section")).content.decode()
        self.assertIn("2026fav", fragment)
        self.assertNotIn("<html", fragment)

    def test_search_filters(self):
        self.star(self.alice)
        factory = RequestFactory()

        def names(params, user):
            request = factory.get("/search/", params)
            request.user = user
            fs = TransientSearchFilterSet(request.GET, queryset=Transient.objects.all(), request=request)
            self.assertTrue(fs.is_valid(), fs.errors)
            return set(fs.qs.values_list("name", flat=True))

        self.assertEqual(names({"favorites": "true"}, self.alice), {"2026fav"})
        self.assertEqual(names({"favorites": "false"}, self.alice), {"2026oth"})
        self.assertEqual(names({"favorites": "true"}, self.bob), set())
        register_interest(self.other, self.bob, "A paper", comment=False, notify_others=False)
        self.assertEqual(names({"has_interest": "true"}, self.alice), {"2026oth"})
        self.assertEqual(names({"has_interest": "false"}, self.alice), {"2026fav"})
        html = self.client.get(reverse("search"), {"favorites": "true"}).content.decode()
        self.assertIn("My favorites only", html)
        table = html.split('id="search_transient_tbl"')[1].split("</table>")[0]
        self.assertIn("2026fav", table)
        self.assertNotIn("2026oth", table)
        self.assertIn("yse-fav-star", table)

    def test_table_columns(self):
        register_interest(self.transient, self.bob, "A paper", comment=False, notify_others=False)
        register_interest(self.transient, self.alice, "Another", comment=False, notify_others=False)
        table = TransientTable(Transient.objects.filter(pk__in=[self.transient.pk, self.other.pk]))
        names = [c.name for c in table.columns]
        self.assertEqual(names[-2:], ["interest_count", "favorite"])
        rows = {row.record.name: row for row in table.rows}
        self.assertEqual(rows["2026fav"].record.open_interest_count, 2)
        self.assertIn(rows["2026oth"].record.open_interest_count, (None, 0))
        self.assertIn('yse-interest-count', rows["2026fav"].get_cell("interest_count"))
        self.assertIn('data-state="unknown"', rows["2026fav"].get_cell("favorite"))
        search_names = [c.name for c in SearchTransientTable(Transient.objects.all()).columns]
        self.assertEqual(search_names[-2:], ["interest_count", "favorite"])
        self.star(self.alice)
        fav = FavoriteTransientTable(svc.favorites_for_user(self.alice))
        self.assertEqual([c.name for c in fav.columns][0], "favorite")
        self.assertIn("favorited_at", [c.name for c in fav.columns])

    def test_interest_count_ordering(self):
        register_interest(self.other, self.bob, "A paper", comment=False, notify_others=False)
        table = TransientTable(Transient.objects.filter(pk__in=[self.transient.pk, self.other.pk]), order_by="-interest_count")
        self.assertEqual([row.record.name for row in table.rows][0], "2026oth")


class SlackDmTests(TestCase):
    def setUp(self):
        self.user = create_test_user("dm_user", email="dm@example.com")

    @override_settings(SLACK_BOT_TOKEN="xoxb-test", NOTIFICATION_SLACK_ENABLED=True, NOTIFICATION_EMAIL_ENABLED=False)
    def test_channel_follows_matrix_and_delivers(self):
        pref = NotificationPreference.objects.create(user=self.user, slack_user_id="U123ABC")
        self.assertEqual(notify_svc.channels_for(self.user, pref, "comment_mention"), [notify_svc.SLACK_DM])
        pref.set_group_channels("comment_mention", slack=False)
        pref.save()
        self.assertEqual(notify_svc.channels_for(self.user, pref, "comment_mention"), [])
        [row] = notify_svc.notify([self.user], "hello", "/transient_detail/x/", "alert", subject="Alert")
        with mock.patch("YSE_App.integrations.slack.client.chat_post_message",
                        return_value={"ok": True, "ts": "1.2"}) as post:
            run_pass()
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "U123ABC")
        self.assertIn("hello", post.call_args[0][1])
        row.refresh_from_db()
        self.assertTrue(row.delivered[notify_svc.SLACK_DM]["sent_at"])

    @override_settings(SLACK_BOT_TOKEN="xoxb-test", NOTIFICATION_EMAIL_ENABLED=False)
    def test_failed_dm_is_recorded_and_retried(self):
        NotificationPreference.objects.create(user=self.user, slack_user_id="U123ABC")
        [row] = notify_svc.notify([self.user], "hello", "", "alert")
        with mock.patch("YSE_App.integrations.slack.client.chat_post_message",
                        return_value={"ok": False, "error": "channel_not_found"}):
            run_pass()
        row.refresh_from_db()
        self.assertIn("channel_not_found", row.delivered[notify_svc.SLACK_DM]["error"])
        job = Job.objects.get(kind=notify_svc.DELIVER_KIND)
        self.assertEqual(job.status, Job.QUEUED)

    def test_disabled_without_token(self):
        with override_settings(SLACK_BOT_TOKEN=""):
            pref = NotificationPreference(user=self.user, slack_user_id="U123ABC")
            self.assertNotIn(notify_svc.SLACK_DM, notify_svc.channels_for(self.user, pref, "alert"))

    @override_settings(SLACK_BOT_TOKEN="xoxb-test")
    def test_lookup_helper(self):
        with mock.patch("YSE_App.integrations.slack.client.users_lookup_by_email",
                        return_value={"ok": True, "user": {"id": "U777"}}):
            self.assertEqual(notify_svc.lookup_slack_user_id("dm@example.com"), "U777")
        with mock.patch("YSE_App.integrations.slack.client.users_lookup_by_email",
                        return_value={"ok": False, "error": "users_not_found"}):
            self.assertEqual(notify_svc.lookup_slack_user_id("dm@example.com"), "")
        with mock.patch("YSE_App.integrations.slack.client.users_lookup_by_email",
                        return_value={"ok": False, "error": "missing_scope"}):
            with self.assertRaises(ValueError):
                notify_svc.lookup_slack_user_id("dm@example.com")

    def test_client_form_call(self):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"ok": true, "user": {"id": "U1"}}'

        def fake_urlopen(req, timeout=15):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["ctype"] = req.get_header("Content-type")
            return _Resp()

        from YSE_App.integrations.slack import client

        with override_settings(SLACK_BOT_TOKEN="xoxb-test"), mock.patch.object(client.request, "urlopen", fake_urlopen):
            self.assertEqual(client.users_lookup_by_email("a@b.c")["user"]["id"], "U1")
        self.assertTrue(captured["url"].endswith("/users.lookupByEmail"))
        self.assertEqual(captured["data"], b"email=a%40b.c")
        self.assertEqual(captured["ctype"], "application/x-www-form-urlencoded")
        with override_settings(SLACK_BOT_TOKEN=""):
            self.assertEqual(client.users_lookup_by_email("a@b.c"), {"ok": False, "error": "missing_token"})

    @override_settings(SLACK_BOT_TOKEN="xoxb-test")
    def test_preferences_page_and_lookup_view(self):
        client = Client()
        client.force_login(self.user)
        html = client.get(reverse("notification_preferences")).content.decode()
        self.assertIn('name="slack_user_id"', html)
        self.assertIn('id="yse-slack-lookup-form"', html)
        self.assertIn("Look up from my email", html)
        with mock.patch("YSE_App.integrations.slack.client.users_lookup_by_email",
                        return_value={"ok": True, "user": {"id": "U555"}}):
            response = client.post(reverse("notification_slack_lookup"))
        self.assertRedirects(response, reverse("notification_preferences"), fetch_redirect_response=False)
        self.assertEqual(NotificationPreference.objects.get(user=self.user).slack_user_id, "U555")
        # saving the form keeps / validates the id
        response = client.post(reverse("notification_preferences"), {"in_app": "on", "slack_user_id": "not-an-id"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Slack member id starts with U", response.content.decode())
        response = client.post(reverse("notification_preferences"), {"in_app": "on", "email": "on", "slack_user_id": "W42AB"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NotificationPreference.objects.get(user=self.user).slack_user_id, "W42AB")

    def test_lookup_view_without_token(self):
        client = Client()
        client.force_login(self.user)
        with override_settings(SLACK_BOT_TOKEN=""):
            response = client.post(reverse("notification_slack_lookup"), follow=True)
        self.assertContains(response, "not enabled on this server")


class ApiTests(FavoriteBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.alice)

    def test_favorites_api(self):
        response = self.client.post("/api/favorites/", {"transient": self.transient.id})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["transient_name"], "2026fav")
        response = self.client.post("/api/favorites/", {"transient": self.transient.id})
        self.assertEqual(response.status_code, 201)  # idempotent
        self.assertEqual(UserFavoriteTransient.objects.count(), 1)
        listing = self.client.get("/api/favorites/").json()
        results = listing["results"] if isinstance(listing, dict) else listing
        self.assertEqual([r["transient"] for r in results], [self.transient.id])
        self.assertEqual(len(self.client.get("/api/favorites/?transient=2026oth").json().get("results", [])), 0)
        toggled = self.client.post("/api/favorites/toggle/", json.dumps({"transient": self.other.id}),
                                   content_type="application/json").json()
        self.assertEqual(toggled, {"transient": self.other.id, "favorite": True, "count": 1})
        self.assertEqual(self.client.post("/api/favorites/toggle/", {"transient": "nope"}).status_code, 400)
        fav_id = results[0]["id"]
        self.assertEqual(self.client.delete("/api/favorites/%d/" % fav_id).status_code, 204)
        self.assertFalse(svc.is_favorite(self.alice, self.transient.id))
        # someone else's row is invisible
        other_row = svc.add(self.bob, self.transient)[0]
        self.assertEqual(self.client.get("/api/favorites/%d/" % other_row.pk).status_code, 404)

    def test_notifications_api(self):
        notify_svc.notify([self.alice], "one", "/x/", "alert", subject="A")
        notify_svc.notify([self.alice], "two", "/y/", "system", subject="B", transient=self.transient)
        notify_svc.notify([self.bob], "theirs", "", "alert")
        listing = self.client.get("/api/notifications/").json()
        results = listing["results"] if isinstance(listing, dict) else listing
        self.assertEqual([r["text"] for r in results], ["two", "one"])
        self.assertEqual(results[0]["transient_name"], "2026fav")
        self.assertEqual(self.client.get("/api/notifications/unread_count/").json(), {"unread": 2})
        one = [r for r in results if r["text"] == "one"][0]
        read = self.client.post("/api/notifications/%d/read/" % one["id"]).json()
        self.assertTrue(read["is_read"])
        unread = self.client.get("/api/notifications/?unread=1").json()
        unread = unread["results"] if isinstance(unread, dict) else unread
        self.assertEqual([r["text"] for r in unread], ["two"])
        kinds = self.client.get("/api/notifications/?kind=alert").json()
        kinds = kinds["results"] if isinstance(kinds, dict) else kinds
        self.assertEqual([r["text"] for r in kinds], ["one"])
        self.assertEqual(self.client.post("/api/notifications/read_all/").json(), {"marked": 1, "unread": 0})
        theirs = Notification.objects.get(recipient=self.bob)
        self.assertEqual(self.client.get("/api/notifications/%d/" % theirs.pk).status_code, 404)
        self.assertEqual(self.client.post("/api/notifications/%d/read/" % theirs.pk).status_code, 404)
        self.assertIsNone(theirs.read_at)
        self.assertIn(Client().get("/api/notifications/").status_code, (401, 403))
