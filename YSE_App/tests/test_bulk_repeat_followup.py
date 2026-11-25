"""Repeat follow-up requests in bulk (#853, UI preview only).

Covers: both preview states (404 and no menu entry without the preview), the review page
(summary, skipped objects and the reason, nothing created), only objects that already have
a request get a new one (never a first request), the single-request code path (requestor,
``modified_by``, audience groups, notifications), defaults from the most recent request,
ineligible groups and unauthorized resources refused, unlisted objects, CSRF, anonymous
users and the search page's bulk menu.

Synthetic users, groups and rows only.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import bulk_followup_views
from YSE_App.models import (
    FollowupStatus,
    ToOResource,
    TransientFollowup,
    TransientFollowupRequest,
)
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

PREVIEW_COOKIE = "yse-ui-preview"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
URL_NAME = "bulk_repeat_followup"


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS, DATA_SERVICE_READER_USERNAMES=[], API_SERVICE_USERNAMES=[],
                   UI_PREVIEW_DEFAULT=False)
class RepeatFollowupBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("rfu_owner", is_staff=True)
        cls.member = create_test_user("rfu_member", is_staff=False)
        cls.outsider = create_test_user("rfu_outsider", is_staff=False)
        cls.team_a = Group.objects.create(name="rfu-team-a")
        cls.team_b = Group.objects.create(name="rfu-team-b")
        cls.team_c = Group.objects.create(name="rfu-team-c")
        cls.member.groups.add(cls.team_a, cls.team_c)
        cls.outsider.groups.add(cls.team_b)
        cls.requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(cls.owner))

        _obs_group, instrument, _band = create_instrument_stack(cls.owner, obs_group_name="rfu-stack")
        now = timezone.now()

        def too(groups=()):
            resource = ToOResource.objects.create(
                telescope=instrument.telescope, begin_date_valid=now - timedelta(days=1),
                end_date_valid=now + timedelta(days=30), **audit_fields(cls.owner))
            if groups:
                resource.groups.set(groups)
            return resource

        cls.too_open = too()                    # ungrouped: every group may see it
        cls.too_a = too([cls.team_a])           # team A only
        cls.too_b = too([cls.team_b])           # the member is not in team B

        # TNS-style names are listed for every signed-in user; the photometry lets a
        # non-staff user see the transient's follow-ups.
        cls.followed = []
        for i, (priority, days) in enumerate(((2.0, 3), (3.0, 5), (4.5, 2))):
            transient = create_minimal_transient(cls.owner, name="2026rfu%s" % "abc"[i], status_name="Following",
                                                 ra=30.0 + i, dec=-5.0)
            attach_synthetic_photometry(cls.owner, transient, n_points=2)
            parent, _child, _ = create_or_attach_request(
                cls.owner, transient, status=cls.requested, valid_start=now - timedelta(days=10),
                valid_stop=now - timedelta(days=10) + timedelta(days=days), priority=priority,
                too_resource=cls.too_open)
            parent.is_public = False
            parent.save(update_fields=["is_public"])
            parent.groups.set([cls.team_a])
            cls.followed.append(transient)
        cls.new_target = create_minimal_transient(cls.owner, name="2026rfud", status_name="New", ra=40.0, dec=-5.0)
        attach_synthetic_photometry(cls.owner, cls.new_target, n_points=2)
        # Not TNS-named, no groups: not listed for a non-staff user.
        cls.unlisted = create_minimal_transient(cls.owner, name="private-rfu-1", status_name="Following")
        create_or_attach_request(cls.owner, cls.unlisted, status=cls.requested, valid_start=now,
                                 valid_stop=now + timedelta(days=1), too_resource=cls.too_open)

    def client_for(self, user, preview=True, **kwargs):
        client = Client(**kwargs)
        client.force_login(user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client

    def ids(self):
        return [t.pk for t in self.followed] + [self.new_target.pk]

    def confirm(self, client, resource, groups=(), extra=None, ids=None):
        data = {
            "confirm": "1",
            "next": reverse("dashboard"),
            "transient_id": ids if ids is not None else self.ids(),
            "resource": "too_resource:%d" % resource.pk,
            "audience_groups": [g.pk for g in groups],
        }
        data.update(extra or {})
        return client.post(reverse(URL_NAME), data)


class PreviewOffTests(RepeatFollowupBase):
    def test_page_is_404_without_preview(self):
        client = self.client_for(self.member, preview=False)
        response = client.post(reverse(URL_NAME), {"transient_id": self.ids()})
        self.assertEqual(response.status_code, 404)
        before = TransientFollowupRequest.objects.count()
        response = self.confirm(client, self.too_open, [self.team_a])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(TransientFollowupRequest.objects.count(), before)

    def test_no_menu_entry_without_preview(self):
        client = self.client_for(self.member, preview=False)
        self.assertNotContains(client.get(reverse("dashboard")), "Repeat follow-up")
        self.assertNotContains(client.get(reverse("search") + "?status=New"), 'value="repeat_followup"')

    def test_search_bulk_refuses_action_without_preview(self):
        before = TransientFollowupRequest.objects.count()
        response = self.client_for(self.member, preview=False).post(
            reverse("search_bulk_action"), {"action": "repeat_followup", "transient_id": self.ids()})
        self.assertEqual(response.status_code, 302)
        self.assertIn("bulk_result=", response["Location"])
        self.assertEqual(TransientFollowupRequest.objects.count(), before)

    def test_anonymous_is_sent_to_login(self):
        response = Client().post(reverse(URL_NAME), {"transient_id": self.ids()})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(reverse(URL_NAME), response["Location"].split("?")[0])

    def test_get_is_not_allowed(self):
        response = self.client_for(self.member).get(reverse(URL_NAME))
        self.assertEqual(response.status_code, 405)


class MenuTests(RepeatFollowupBase):
    def test_dashboard_has_control(self):
        response = self.client_for(self.member).get(reverse("dashboard"))
        self.assertContains(response, "Repeat follow-up…")
        self.assertContains(response, 'action="%s"' % reverse(URL_NAME))
        self.assertContains(response, "repeat-followup.js?v=repeat-fu-1")

    def test_search_has_bulk_option(self):
        response = self.client_for(self.member).get(reverse("search") + "?status=New")
        self.assertContains(response, '<option value="repeat_followup">Repeat follow-up…</option>', html=False)

    def test_combined_list_has_control(self):
        response = self.client_for(self.member).get(reverse("combined_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Repeat follow-up…")


class ReviewTests(RepeatFollowupBase):
    def test_review_summarises_and_creates_nothing(self):
        before = TransientFollowupRequest.objects.count()
        response = self.client_for(self.member).post(reverse(URL_NAME), {"transient_id": self.ids()})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "YSE_App/followup/repeat_bulk.html")
        self.assertContains(response, "Repeat for 3 of 4 selected; 1 skipped.")
        self.assertContains(response, bulk_followup_views.SKIP_NO_REQUEST)
        self.assertEqual([row.transient.pk for row in response.context["rows"]], [t.pk for t in self.followed])
        self.assertEqual(TransientFollowupRequest.objects.count(), before)
        # The confirm form carries the CSRF token.
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_defaults_come_from_the_most_recent_requests(self):
        response = self.client_for(self.member).post(reverse(URL_NAME), {"transient_id": self.ids()})
        form = response.context["form"]
        self.assertEqual(form.fields["resource"].initial, "too_resource:%d" % self.too_open.pk)
        self.assertEqual(list(form.fields["audience_groups"].initial), [self.team_a.pk])
        self.assertEqual([row.last_priority for row in response.context["rows"]], [2.0, 3.0, 4.5])

    def test_unauthorized_resource_is_not_offered(self):
        response = self.client_for(self.member).post(reverse(URL_NAME), {"transient_id": self.ids()})
        keys = [key for _label, options in response.context["form"].fields["resource"].choices[1:]
                for key, _text in options]
        self.assertIn("too_resource:%d" % self.too_a.pk, keys)
        self.assertNotIn("too_resource:%d" % self.too_b.pk, keys)

    def test_unlisted_object_is_skipped_without_its_name(self):
        response = self.client_for(self.member).post(
            reverse(URL_NAME), {"transient_id": [self.followed[0].pk, self.unlisted.pk]})
        self.assertContains(response, "Repeat for 1 of 2 selected; 1 skipped.")
        self.assertContains(response, bulk_followup_views.SKIP_NOT_VISIBLE)
        self.assertNotContains(response, self.unlisted.name)

    def test_nothing_eligible(self):
        response = self.client_for(self.member).post(reverse(URL_NAME), {"transient_id": [self.new_target.pk]})
        self.assertContains(response, "Repeat for 0 of 1 selected; 1 skipped.")
        self.assertContains(response, 'id="yse-repeat-fu-none"')
        self.assertNotContains(response, 'id="yse-repeat-fu-form"')

    def test_too_many_objects(self):
        ids = list(range(1, bulk_followup_views.MAX_OBJECTS + 2))
        response = self.client_for(self.member).post(reverse(URL_NAME), {"transient_id": ids})
        self.assertContains(response, 'id="yse-repeat-fu-too-many"')

    def test_next_must_be_local(self):
        response = self.client_for(self.member).post(
            reverse(URL_NAME), {"transient_id": self.ids(), "next": "https://example.invalid/x"})
        self.assertEqual(response.context["next_url"], reverse("dashboard"))

    def test_search_bulk_shows_review(self):
        response = self.client_for(self.member).post(
            reverse("search_bulk_action"), {"action": "repeat_followup", "transient_id": self.ids(), "params": ""})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "YSE_App/followup/repeat_bulk.html")
        self.assertContains(response, "Repeat for 3 of 4 selected; 1 skipped.")
        self.assertTrue(response.context["next_url"].startswith(reverse("search")))


class ConfirmTests(RepeatFollowupBase):
    def test_only_objects_with_requests_get_new_ones(self):
        response = self.confirm(self.client_for(self.member), self.too_a, [self.team_a])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Requested 3 of 4 selected")
        self.assertEqual(len(response.context["created"]), 3)
        self.assertFalse(TransientFollowup.objects.filter(transient=self.new_target).exists())
        for transient in self.followed:
            parent = TransientFollowup.objects.get(transient=transient, too_resource=self.too_a)
            self.assertEqual(parent.modified_by, self.member)
            self.assertEqual(parent.requested_by, self.member)
            self.assertFalse(parent.is_public)
            self.assertEqual(list(parent.groups.all()), [self.team_a])
            child = parent.requests.get()
            self.assertEqual(child.requestor, self.member)
            self.assertEqual(child.modified_by, self.member)

    def test_blank_priority_and_window_keep_each_objects_last(self):
        self.confirm(self.client_for(self.member), self.too_a, [self.team_a])
        for transient, priority, days in zip(self.followed, (2.0, 3.0, 4.5), (3, 5, 2)):
            parent = TransientFollowup.objects.get(transient=transient, too_resource=self.too_a)
            self.assertEqual(parent.requests.get().priority, priority)
            self.assertEqual(parent.valid_stop - parent.valid_start, timedelta(days=days))

    def test_options_apply_to_all(self):
        start = timezone.now().replace(microsecond=0) + timedelta(days=1)
        self.confirm(self.client_for(self.member), self.too_a, [self.team_a], extra={
            "priority": "1.5", "comment": "another epoch",
            "valid_start": start.strftime("%Y-%m-%d %H:%M"),
            "valid_stop": (start + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")})
        for transient in self.followed:
            parent = TransientFollowup.objects.get(transient=transient, too_resource=self.too_a)
            child = parent.requests.get()
            self.assertEqual(child.priority, 1.5)
            self.assertEqual(child.comment, "another epoch")
            self.assertEqual(parent.valid_stop - parent.valid_start, timedelta(days=2))

    def test_same_resource_adds_to_the_open_request(self):
        before = TransientFollowup.objects.filter(transient__in=self.followed).count()
        response = self.confirm(self.client_for(self.member), self.too_open, [self.team_a])
        self.assertEqual(len(response.context["created"]), 3)
        self.assertEqual(TransientFollowup.objects.filter(transient__in=self.followed).count(), before)
        for transient in self.followed:
            parent = TransientFollowup.objects.get(transient=transient, too_resource=self.too_open)
            self.assertEqual(parent.requests.count(), 2)
            self.assertEqual(parent.requests.filter(requestor=self.member).count(), 1)

    def test_ineligible_group_is_refused(self):
        before = TransientFollowupRequest.objects.count()
        # Team C is the member's, but the team-A resource does not allow it (#794).
        response = self.confirm(self.client_for(self.member), self.too_a, [self.team_a, self.team_c])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context.get("step"), "review")
        self.assertIn("audience_groups", response.context["form"].errors)
        self.assertEqual(TransientFollowupRequest.objects.count(), before)

    def test_group_the_user_is_not_in_is_refused(self):
        before = TransientFollowupRequest.objects.count()
        response = self.confirm(self.client_for(self.member), self.too_open, [self.team_b])
        self.assertIn("audience_groups", response.context["form"].errors)
        self.assertEqual(TransientFollowupRequest.objects.count(), before)

    def test_unauthorized_resource_is_refused(self):
        before = TransientFollowupRequest.objects.count()
        response = self.confirm(self.client_for(self.member), self.too_b, [self.team_a])
        self.assertIn("resource", response.context["form"].errors)
        self.assertEqual(TransientFollowupRequest.objects.count(), before)
        self.assertFalse(TransientFollowup.objects.filter(too_resource=self.too_b).exists())

    def test_user_who_cannot_see_the_requests_gets_nothing(self):
        # The outsider cannot see the team-A requests, so every object counts as new.
        before = TransientFollowupRequest.objects.count()
        response = self.client_for(self.outsider).post(reverse(URL_NAME), {"transient_id": self.ids()})
        self.assertContains(response, "Repeat for 0 of 4 selected; 4 skipped.")
        self.confirm(self.client_for(self.outsider), self.too_b, [self.team_b])
        self.assertEqual(TransientFollowupRequest.objects.count(), before)

    def test_unlisted_ids_never_get_requests(self):
        before = TransientFollowup.objects.filter(transient=self.unlisted).count()
        self.confirm(self.client_for(self.member), self.too_a, [self.team_a], ids=[self.unlisted.pk])
        self.assertEqual(TransientFollowup.objects.filter(transient=self.unlisted).count(), before)

    def test_notifications_go_through_the_model_signals(self):
        with mock.patch("YSE_App.services.favorites.on_followup", return_value=[]) as on_followup:
            self.confirm(self.client_for(self.member), self.too_a, [self.team_a])
        created_calls = [c for c in on_followup.call_args_list if c.kwargs.get("created")]
        self.assertEqual(len(created_calls), 3)

    def test_csrf_is_enforced(self):
        client = self.client_for(self.member, enforce_csrf_checks=True)
        before = TransientFollowupRequest.objects.count()
        response = self.confirm(client, self.too_a, [self.team_a])
        self.assertEqual(response.status_code, 403)
        response = client.post(reverse(URL_NAME), {"transient_id": self.ids()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(TransientFollowupRequest.objects.count(), before)
