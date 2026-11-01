"""Follow-up tab (#857) and stepping through a list from the transient page (#850).

Both only with the UI preview on; with it off the current pages are unchanged.
Synthetic users, transients and resources only.
"""

import os
import re
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.list_context import list_step_context, parse_list_params
from YSE_App.models import ClassicalResource, FollowupStatus, Transient, TransientFollowupRequest, TransientTag
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.js_syntax_utils import html_script_problems

LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _name(i):
    """TNS-style synthetic names (2026 + letters), listed for every signed-in user."""
    return "2026ls%s%s" % (LETTERS[i // 26], LETTERS[i % 26])


@override_settings(UI_PREVIEW_DEFAULT=False)
class ListStepsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("list_steps_user")
        now = timezone.now()
        cls.rows = []
        # Eleven "Following" transients, newest first on the dashboard (ORDER BY -disc_date).
        for i in range(11):
            t = create_minimal_transient(cls.user, name=_name(i), status_name="Following", ra=10.0 + i * 0.01)
            Transient.objects.filter(pk=t.pk).update(disc_date=now - timedelta(days=i))
            cls.rows.append(Transient.objects.get(pk=t.pk))
        cls.other = create_minimal_transient(cls.user, name="2026lszzz", status_name="Watch")

    def client_for(self, preview=True):
        client = Client()
        client.force_login(self.user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client

    def detail(self, transient, params=None, preview=True, defer=True):
        url = reverse("transient_detail", kwargs={"slug": transient.slug})
        with mock.patch.dict(os.environ, {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}):
            response = self.client_for(preview).get(url, params or {})
        self.assertEqual(response.status_code, 200)
        return response

    def step_context(self, transient, params):
        request = RequestFactory().get("/", params)
        request.user = self.user
        request.session = {}
        return list_step_context(request, transient)


class PreviewOffUnchangedTests(ListStepsBase):
    def test_transient_page_ignores_list_context(self):
        response = self.detail(self.rows[3], {"ls": "dash", "lt": "following", "lq": ""}, preview=False)
        self.assertTemplateUsed(response, "YSE_App/transient_detail.html")
        self.assertTemplateNotUsed(response, "YSE_App/transient_detail_v2.html")
        self.assertNotIn("list_step", response.context)
        html = response.content.decode()
        for marker in ("yse-v2-list-stepper", "yse-v2-request-modal", "list-context.js", "data-default-comment"):
            self.assertNotIn(marker, html)

    def test_followup_fragments_are_the_current_ones(self):
        client = self.client_for(preview=False)
        response = client.get(reverse("transient_detail_followup_fragment", args=[self.rows[0].id]))
        self.assertTemplateUsed(response, "YSE_App/transient_detail_followup_boxes.html")
        self.assertTemplateNotUsed(response, "YSE_App/transient_detail/followup_existing_v2.html")
        response = client.get(reverse("transient_detail_followup_rest_fragment", args=[self.rows[0].id]))
        self.assertTemplateUsed(response, "YSE_App/transient_detail_followup_rest.html")
        self.assertTemplateNotUsed(response, "YSE_App/transient_detail/followup_new_v2.html")
        html = response.content.decode()
        self.assertIn("Add Transient Follow-up", html)
        self.assertIn('id="facility_request_box"', html)
        self.assertNotIn("yse-v2-", html)

    def test_list_pages_add_no_list_context(self):
        client = self.client_for(preview=False)
        for url in (reverse("search") + "?name_contains=2026ls", reverse("transient_tags"), reverse("dashboard")):
            html = client.get(url).content.decode()
            self.assertNotIn("data-yse-list-source", html, url)
            self.assertNotIn("list-context.js", html, url)


class FollowupTabTests(ListStepsBase):
    """#857: existing requests first, then facility requests, one form with a type selector, then Advanced."""

    def test_non_deferred_tab_order(self):
        html = self.detail(self.rows[0], defer=False).content.decode()
        order = [
            'id="yse-v2-followup-existing"',
            'id="followup_container"',
            'id="yse-v2-followup-empty"',
            'id="yse-v2-facility-requests"',
            'id="facility_requests_slot"',
            'id="yse-v2-new-request"',
            'name="yse-v2-request-type"',
            'id="add_transient_followup"',
            'id="yse-v2-followup-advanced"',
            'id="add_classical_resource"',
            'id="add_too_resource"',
            'id="add_transient_observation_task"',
            'id="automated_spectrum_request"',
        ]
        positions = [html.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        # One form each, no box chrome around the follow-up form, no second classical slot.
        self.assertEqual(html.count('id="add_transient_followup"'), 1)
        self.assertNotIn("Add Transient Follow-up", html)
        self.assertNotIn('id="followup_classical_slot"', html)
        self.assertEqual(html_script_problems(html), [])

    def test_deferred_tab_loads_the_preview_fragments(self):
        html = self.detail(self.rows[0]).content.decode()
        self.assertLess(html.index('id="followup_container"'), html.index('id="followup_rest_slot"'))
        self.assertNotIn('id="followup_classical_slot"', html)
        self.assertNotIn('id="add_transient_followup"', html)

        client = self.client_for()
        response = client.get(reverse("transient_detail_followup_fragment", args=[self.rows[0].id]))
        self.assertTemplateUsed(response, "YSE_App/transient_detail/followup_existing_v2.html")
        self.assertContains(response, "No follow-up has been requested for this transient yet.")

        response = client.get(reverse("transient_detail_followup_rest_fragment", args=[self.rows[0].id]))
        self.assertTemplateUsed(response, "YSE_App/transient_detail/followup_new_v2.html")
        frag = response.content.decode()
        box = frag.index('id="facility_request_box"')
        for marker in ('id="yse-v2-facility-requests"', 'id="yse-v2-new-request"', 'id="add_transient_followup"',
                       'id="yse-v2-followup-advanced"', 'id="add_classical_resource"', 'id="automated_spectrum_request"'):
            self.assertGreater(frag.index(marker), box, marker)
        # Tasks need a follow-up picked first: like today's deferred tab, not in the fragment.
        self.assertNotIn('id="add_transient_observation_task"', frag)
        self.assertNotIn("Add Transient Follow-up", frag)
        # No facility allocation for this user: the facility type is offered but disabled.
        self.assertRegex(frag, r'id="yse-v2-request-type-facility" value="facility"\s+disabled')
        self.assertEqual(html_script_problems(frag), [])

    def test_existing_requests_replace_the_empty_state(self):
        now = timezone.now()
        _group, instrument, _band = create_instrument_stack(self.user, obs_group_name="ls-resource")
        resource = ClassicalResource.objects.create(
            telescope=instrument.telescope, begin_date_valid=now - timedelta(days=1),
            end_date_valid=now + timedelta(days=5), creator_only=True, **audit_fields(self.user))
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(self.user))
        response = self.client_for().post(reverse("add_transient_followup"), {
            "status": status.id, "classical_resource": resource.id, "priority": 3.0,
            "comment": "synthetic", "transient": self.rows[0].id,
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        response = self.client_for().get(reverse("transient_detail_followup_fragment", args=[self.rows[0].id]))
        self.assertNotContains(response, 'id="yse-v2-followup-empty"')
        self.assertContains(response, "followupbox")


class NeighbourTests(ListStepsBase):
    """#850: neighbours from the list's own filter and sort, a window around the current object only."""

    def test_dashboard_tab_position_and_neighbours(self):
        ctx = self.step_context(self.rows[3], {"ls": "dash", "lt": "following", "lq": ""})
        self.assertEqual((ctx["position"], ctx["count"]), (4, 11))
        self.assertIn("Dashboard", ctx["label"])
        self.assertIn("/%s/" % self.rows[2].slug, ctx["prev_url"])
        self.assertIn("/%s/" % self.rows[4].slug, ctx["next_url"])
        self.assertEqual(ctx["back_url"], reverse("dashboard") + "#following")
        self.assertEqual(ctx["default_comment"], "In list: " + ctx["label"])

    def test_dashboard_steps_across_a_page_boundary(self):
        # Ten rows per page: the tenth row's next is the first row of page 2, and back again.
        ctx = self.step_context(self.rows[9], {"ls": "dash", "lt": "following", "lq": ""})
        self.assertEqual(ctx["position"], 10)
        nxt = parse_qs(urlparse(ctx["next_url"]).query)
        self.assertIn("/%s/" % self.rows[10].slug, ctx["next_url"])
        self.assertEqual(nxt["lq"], ["following-page=2"])
        ctx = self.step_context(self.rows[10], {"ls": "dash", "lt": "following", "lq": nxt["lq"][0]})
        self.assertEqual((ctx["position"], ctx["count"]), (11, 11))
        self.assertIsNone(ctx["next_url"])
        prev = parse_qs(urlparse(ctx["prev_url"]).query)
        self.assertNotIn("lq", prev)  # page 1 needs no page parameter
        self.assertEqual(ctx["back_url"], reverse("dashboard") + "?following-page=2#following")

    def test_dashboard_sort_and_filter_come_from_the_list(self):
        # Ascending discovery date reverses the order.
        ctx = self.step_context(self.rows[3], {"ls": "dash", "lt": "following", "lq": "following-sort=disc_date_string"})
        self.assertEqual(ctx["position"], 8)
        self.assertIn("/%s/" % self.rows[4].slug, ctx["prev_url"])
        # The shared filter narrows the list and is named in the label.
        ctx = self.step_context(self.rows[3], {"ls": "dash", "lt": "following", "lq": "dash-ex=%s" % self.rows[3].name})
        self.assertEqual((ctx["position"], ctx["count"]), (1, 1))
        self.assertIn(self.rows[3].name, ctx["label"])

    def test_search_results(self):
        ctx = self.step_context(self.rows[0], {"ls": "search", "lq": "name_contains=2026ls"})
        # Search sorts newest first; the "Watch" transient (no disc_date) sorts after the eleven.
        self.assertEqual((ctx["position"], ctx["count"]), (1, 12))
        self.assertIsNone(ctx["prev_url"])
        self.assertIn("/%s/" % self.rows[1].slug, ctx["next_url"])
        self.assertIn("Name contains", ctx["label"])
        self.assertEqual(ctx["back_url"], reverse("search") + "?name_contains=2026ls")

    def test_tag_list(self):
        tag = TransientTag.objects.create(name="SynthTag", **audit_fields(self.user))
        for t in (self.rows[1], self.rows[5], self.rows[8]):
            t.tags.add(tag)
        ctx = self.step_context(self.rows[5], {"ls": "tag", "lq": "tagid=%d" % tag.id})
        self.assertEqual((ctx["position"], ctx["count"]), (2, 3))
        self.assertIn("/%s/" % self.rows[1].slug, ctx["prev_url"])
        self.assertIn("/%s/" % self.rows[8].slug, ctx["next_url"])
        self.assertIn("SynthTag", ctx["label"])

    def test_object_no_longer_in_the_list(self):
        ctx = self.step_context(self.other, {"ls": "dash", "lt": "following", "lq": ""})
        self.assertIsNone(ctx["position"])
        self.assertIsNone(ctx["prev_url"])
        self.assertIsNone(ctx["next_url"])
        self.assertEqual(ctx["count"], 11)

    def test_bad_or_missing_context_is_ignored(self):
        for params in ({}, {"ls": "nope", "lq": ""}, {"ls": "dash", "lq": ""}, {"ls": "dash", "lt": "nosuchtab"},
                       {"ls": "tag", "lq": "tagid=x"}, {"ls": "search", "lq": "a=" + "x" * 3000},
                       {"ls": "search", "lq": "a=1#frag"}):
            self.assertIsNone(self.step_context(self.rows[0], params), params)
        self.assertIsNone(parse_list_params({"ls": "dash", "lt": ""}))

    def test_work_is_capped(self):
        with CaptureQueriesContext(connection) as queries:
            self.step_context(self.rows[3], {"ls": "dash", "lt": "following", "lq": ""})
        # Status lookup, COUNT, the page-plus-one window: never one query per row.
        self.assertLessEqual(len(queries), 8, [q["sql"] for q in queries])
        # The window: the transients ordered like the tab, at most a page plus one row either side.
        window = [q["sql"] for q in queries if "ORDER BY" in q["sql"].upper() and "LIMIT" in q["sql"].upper() and "disc_date" in q["sql"]]
        self.assertEqual(len(window), 1, window)
        self.assertLessEqual(int(re.search(r"LIMIT\s+(\d+)", window[0], re.I).group(1)), 12, window)


class StepperOnPageTests(ListStepsBase):
    def test_header_shows_position_buttons_and_back_link(self):
        response = self.detail(self.rows[3], {"ls": "dash", "lt": "following", "lq": ""})
        html = response.content.decode()
        stepper = html[html.index('id="yse-v2-list-stepper"'):html.index('id="yse-v2-request-status"')]
        self.assertIn("4 of 11", stepper)
        self.assertIn('rel="prev"', stepper)
        self.assertIn('rel="next"', stepper)
        self.assertIn('aria-keyshortcuts="J"', stepper)
        self.assertIn("Back to list", stepper)
        self.assertIn('data-default-comment="In list: Dashboard', stepper)
        self.assertIn("list-context.js?v=list-steps-1", html)
        self.assertEqual(html_script_problems(html), [])

    def test_no_context_no_stepper_and_no_default_comment(self):
        html = self.detail(self.rows[3]).content.decode()
        self.assertIn('data-yse-hook="list-stepping"', html)
        self.assertNotIn("data-default-comment", html)
        self.assertNotIn("Back to list", html)
        self.assertIn('id="yse-v2-request-modal"', html)

    def test_list_pages_carry_the_context_for_preview_users(self):
        client = self.client_for()
        html = client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-yse-list-source="dash"', html)
        self.assertIn('data-yse-list-tab="following"', html)
        self.assertIn("list-context.js", html)
        html = client.get(reverse("search") + "?name_contains=2026ls").content.decode()
        self.assertIn('data-yse-list-source="search"', html)
        html = client.get(reverse("transient_tags")).content.decode()
        self.assertIn('data-yse-list-source="tag"', html)
        self.assertIn("list-context.js", html)


class DefaultCommentTests(ListStepsBase):
    """The request form gets "In list: ..." only when the list context says why the object is here."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        _group, instrument, _band = create_instrument_stack(cls.user, obs_group_name="ls-comment-resource")
        cls.resource = ClassicalResource.objects.create(
            telescope=instrument.telescope, begin_date_valid=now - timedelta(days=1),
            end_date_valid=now + timedelta(days=5), creator_only=True, **audit_fields(cls.user))
        cls.requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(cls.user))

    def _default_comment(self, params):
        html = self.detail(self.rows[2], params).content.decode()
        match = re.search(r'data-default-comment="([^"]*)"', html)
        return match.group(1).replace("&middot;", "·").replace("&quot;", '"') if match else ""

    def _request(self, comment):
        # The panel posts the Follow-up tab's own form to the same endpoint.
        response = self.client_for().post(reverse("add_transient_followup"), {
            "status": self.requested.id, "classical_resource": self.resource.id, "priority": 3.0,
            "comment": comment, "transient": self.rows[2].id,
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        return TransientFollowupRequest.objects.filter(followup__transient=self.rows[2]).latest("id")

    def test_with_list_context(self):
        comment = self._default_comment({"ls": "dash", "lt": "following", "lq": ""})
        self.assertTrue(comment.startswith("In list: Dashboard"), comment)
        self.assertEqual(self._request(comment).comment, comment)
        # The form itself is rendered empty: the default is applied in the browser, and stays editable.
        frag = self.client_for().get(reverse("transient_detail_followup_rest_fragment", args=[self.rows[2].id]))
        self.assertNotContains(frag, "In list:")

    def test_without_list_context(self):
        self.assertEqual(self._default_comment({}), "")
        self.assertEqual(self._request("").comment, "")
