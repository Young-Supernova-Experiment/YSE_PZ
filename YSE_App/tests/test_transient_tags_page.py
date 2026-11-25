"""Search by Tag without drag and drop (#866), behind the UI preview switch.

Covers the /get_transient_tags/ any/all matching and which template /transient_tags/ renders
with the preview off (unchanged drag-and-drop page) and on (labelled multi-select).

Synthetic data only; no external calls.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.models import TransientTag
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user
from YSE_App.tests.js_syntax_utils import html_script_problems


@override_settings(ONBOARDING_GATE_ENABLED=False)
class TagMatchingTests(TestCase):
    """``tagid`` repeated = any of the tags (today); ``match=all`` = every tag."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("tag866_user", is_staff=False, is_superuser=False)
        # TNS-style names, so the listing rule shows them to any signed-in user.
        cls.both = create_minimal_transient(cls.user, name="2026tga", obs_group_name="tag866")
        cls.red_only = create_minimal_transient(cls.user, name="2026tgb", obs_group_name="tag866")
        cls.blue_only = create_minimal_transient(cls.user, name="2026tgc", obs_group_name="tag866")
        cls.untagged = create_minimal_transient(cls.user, name="2026tgd", obs_group_name="tag866")
        cls.red = TransientTag.objects.create(name="tag866-red", **audit_fields(cls.user))
        cls.blue = TransientTag.objects.create(name="tag866-blue", **audit_fields(cls.user))
        cls.both.tags.add(cls.red, cls.blue)
        cls.red_only.tags.add(cls.red)
        cls.blue_only.tags.add(cls.blue)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def names(self, params):
        response = self.client.get(reverse("get_transient_tags"), params)
        self.assertEqual(response.status_code, 200)
        return sorted(t["name"] for t in response.json().values())

    def test_any_is_the_default(self):
        self.assertEqual(self.names({"tagid": [self.red.id, self.blue.id]}),
                         ["2026tga", "2026tgb", "2026tgc"])

    def test_any_lists_a_transient_once(self):
        response = self.client.get(reverse("get_transient_tags"), {"tagid": [self.red.id, self.blue.id]})
        self.assertEqual(len(response.json()), 3)

    def test_explicit_any_and_unknown_modes_match_any(self):
        for mode in ("any", "ALL", "bogus"):
            with self.subTest(mode=mode):
                self.assertEqual(self.names({"tagid": [self.red.id, self.blue.id], "match": mode}),
                                 ["2026tga", "2026tgb", "2026tgc"])

    def test_all_needs_every_tag(self):
        self.assertEqual(self.names({"tagid": [self.red.id, self.blue.id], "match": "all"}), ["2026tga"])

    def test_all_with_one_tag_equals_any(self):
        self.assertEqual(self.names({"tagid": [self.red.id], "match": "all"}), ["2026tga", "2026tgb"])
        self.assertEqual(self.names({"tagid": [self.red.id]}), ["2026tga", "2026tgb"])

    def test_all_with_a_repeated_tag(self):
        self.assertEqual(self.names({"tagid": [self.red.id, self.red.id], "match": "all"}),
                         ["2026tga", "2026tgb"])

    def test_no_tags_matches_nothing(self):
        self.assertEqual(self.names({}), [])
        self.assertEqual(self.names({"match": "all"}), [])

    def test_login_still_required(self):
        response = Client().get(reverse("get_transient_tags"), {"tagid": [self.red.id]})
        self.assertEqual(response.status_code, 302)


@override_settings(ONBOARDING_GATE_ENABLED=False)
class TagPagePreviewTests(TestCase):
    """The current page is unchanged with preview off; preview users get the picker."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("tag866_page_user", is_staff=False, is_superuser=False)
        cls.tag = TransientTag.objects.create(name="tag866-young", **audit_fields(cls.user))

    def get_page(self, preview):
        client = Client()
        client.force_login(self.user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        response = client.get(reverse("transient_tags"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def test_preview_off_keeps_the_drag_and_drop_page(self):
        html = self.get_page(preview=False)
        self.assertIn('id="external-events"', html)
        self.assertIn(".draggable(", html)
        self.assertNotIn('id="tag-picker"', html)

    def test_preview_on_shows_a_labelled_multiselect(self):
        html = self.get_page(preview=True)
        self.assertNotIn(".draggable(", html)
        self.assertNotIn('id="external-events"', html)
        self.assertIn('<label for="tag-picker">Tags</label>', html)
        self.assertIn('<select id="tag-picker" name="tags" multiple', html)
        self.assertIn('aria-describedby="tag-picker-help"', html)
        self.assertIn('value="tag866-young" data-tag-id="%d"' % self.tag.id, html)
        self.assertIn("select2/dist/js/select2.full.min.js", html)
        self.assertIn('<label class="form-check-label" for="tag-match-any">', html)
        self.assertIn('<label class="form-check-label" for="tag-match-all">', html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertIn(reverse("get_transient_tags"), html)
        self.assertIn('href="%s"' % reverse("search"), html)
        self.assertEqual(html_script_problems(html), [])

    def test_focus_order_follows_the_markup(self):
        html = self.get_page(preview=True)
        order = [html.index(marker) for marker in
                 ('id="tag-picker"', 'id="tag-match-any"', 'id="tag-match-all"', 'id="tag-search-link"',
                  'id="transient_tag_tbl"')]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("tabindex", html.split('id="tag-search-form"', 1)[1].split("</form>", 1)[0])

    def test_anonymous_users_are_sent_to_login(self):
        client = Client()
        client.cookies[PREVIEW_COOKIE] = "1"
        response = client.get(reverse("transient_tags"))
        self.assertEqual(response.status_code, 302)
