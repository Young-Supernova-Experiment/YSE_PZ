"""Data-access pages in the preview layout (#863): the current rules as plain tables, each row edited in one modal.

The modal's forms must post the same fields to the same endpoints as the current per-row forms, permission
checks must not change, and with the preview off both pages stay as they are. Synthetic data only.
"""

import datetime
from html.parser import HTMLParser

from django.test import Client, override_settings
from django.urls import reverse

from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.models import Allocation, DataAccessRequest, StandingAccessGrant, TransientPhotometry
from YSE_App.services import access_actions
from YSE_App.tests import test_access_request_revoke as revoke_tests
from YSE_App.tests import test_access_ui as ui_tests
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user

R = ui_tests.R
ON = override_settings(DATA_ACCESS_GRANTS_ENABLED=True, UI_PREVIEW_DEFAULT=False)


class _PostForms(HTMLParser):
    """The POST forms of a page: their URL, whether they sit in a row editor <template>, and their fields
    (hidden and text inputs with their value; selects with their selected or first option)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self._form, self._select, self._template = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "template":
            self._template += 1
        elif tag == "form" and (a.get("method") or "").lower() == "post":
            self._form = {"url": a.get("action") or "", "in_editor": self._template > 0, "fields": {}}
        elif self._form is None:
            return
        elif tag == "input" and a.get("name") and a.get("type") not in ("submit", "button"):
            self._form["fields"].setdefault(a["name"], a.get("value") or "")
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            self._form["fields"][self._select] = None
        elif tag == "option" and self._select:
            if self._form["fields"][self._select] is None or "selected" in a:
                self._form["fields"][self._select] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "template":
            self._template -= 1
        elif tag == "select":
            self._select = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def access_forms(html):
    """The page's access forms (those posting an ``action`` or revoking a request)."""
    parser = _PostForms()
    parser.feed(html)
    return [f for f in parser.forms if "action" in f["fields"] or "/revoke" in f["url"]]


def signatures(forms):
    """What each form posts: (URL, action, field names), without the CSRF token."""
    return {(f["url"], f["fields"].get("action"), frozenset(set(f["fields"]) - {"csrfmiddlewaretoken"}))
            for f in forms}


def editor_form(forms, **match):
    for f in forms:
        if all(f["fields"].get(k) == str(v) for k, v in match.items()):
            return dict(f["fields"], csrfmiddlewaretoken="")
    raise AssertionError("no editor form with %r" % (match,))


def _client(user, preview):
    client = Client()
    client.force_login(user)
    if preview:
        client.cookies[PREVIEW_COOKIE] = "1"
    return client


@ON
class TransientRulesPreviewTests(ui_tests._Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        access_actions.dataset_action(cls.pi_user, cls.sets[0], effect="allow", grantee_group=cls.o)
        access_actions.dataset_action(cls.pi_user, cls.spec, effect="deny", grantee_user=cls.outsider)

    def setUp(self):
        self.url = reverse("access_transient_rules", args=[self.transient.pk])

    def page(self, preview, user=None):
        resp = _client(user or self.pi_user, preview).get(self.url)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_preview_off_is_unchanged(self):
        html = self.page(preview=False)
        self.assertNotIn("yse-access-editor", html)
        self.assertNotIn("yse-access-edit", html)
        self.assertNotIn('data-layout="preview"', html)
        forms = access_forms(html)
        self.assertTrue(forms and not any(f["in_editor"] for f in forms))
        # one form per night, per dataset and per rule action, as before
        self.assertEqual(html.count('name="action" value="dataset"'), 3)
        self.assertEqual(html.count('name="action" value="remove_allow"'), 1)
        self.assertEqual(html.count('name="action" value="lift"'), 1)

    def test_preview_shows_rules_table_first_and_one_editor(self):
        html = self.page(preview=True)
        self.assertEqual(html.count('id="yse-access-editor"'), 1)
        rules, nights, datasets = (html.index('id="yse-access-rules"'), html.index('id="yse-access-nights"'),
                                   html.index('id="yse-access-datasets"'))
        self.assertLess(rules, nights)
        self.assertLess(nights, datasets)
        # the scope-of-action wording stays
        self.assertIn("all of this object's points from the night, in any filter, and that night's spectra", html)
        forms = access_forms(html)
        self.assertTrue(forms and all(f["in_editor"] for f in forms))  # no form outside the editor
        self.assertNotIn("photometry set", html.lower())

    def test_editor_forms_post_the_same_fields_as_today(self):
        self.assertEqual(signatures(access_forms(self.page(preview=True))),
                         signatures(access_forms(self.page(preview=False))))

    def test_edit_through_the_editor(self):
        forms = access_forms(self.page(preview=True))
        client = _client(self.pi_user, preview=True)
        post = editor_form(forms, action="dataset", kind="photometry", dataset_id=self.sets[1].pk)
        post.update(effect="allow", grantee_kind="group", group=str(self.o.pk))
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "may now see 2 new items")
        self.assertEqual(R.objects.filter(photometry=self.sets[1], grantee_group=self.o).count(), 2)

        post = editor_form(forms, action="remove_allow", kind="photometry", dataset_id=self.sets[0].pk)
        post.update(direction="backward")
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "removed backwards from 2 items")
        self.assertFalse(R.objects.filter(photometry=self.sets[0]).exists())

        post = editor_form(forms, action="lift", kind="spectrum", dataset_id=self.spec.pk)
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "Restriction lifted")
        self.assertFalse(R.objects.filter(transient_spectrum=self.spec).exists())

        nights = [f for f in forms if f["fields"].get("action") == "night"]
        post = dict(nights[0]["fields"], effect="allow", grantee_kind="user", username=self.outsider.username)
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "may now see")

    def test_permission_denials_unchanged(self):
        html = self.page(preview=True, user=self.outsider)
        self.assertIn("You may not set access", html)
        self.assertNotIn('id="yse-access-editor"', html)
        forms = access_forms(self.page(preview=True))
        post = editor_form(forms, action="remove_allow", kind="photometry", dataset_id=self.sets[0].pk)
        post.update(direction="backward")
        for preview in (True, False):
            resp = _client(self.outsider, preview).post(self.url, post, follow=True)
            self.assertContains(resp, "Not changed")
        self.assertEqual(R.objects.filter(photometry=self.sets[0], grantee_group=self.o).count(), 2)

    def test_hidden_when_access_rules_are_off(self):
        with override_settings(DATA_ACCESS_GRANTS_ENABLED=False):
            self.assertEqual(_client(self.pi_user, True).get(self.url).status_code, 404)


@ON
class DashboardPreviewTests(revoke_tests._RevokeBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.alloc = Allocation.objects.create(
            name="dap863 A", telescope=cls.instrument.telescope, instrument=cls.instrument,
            principal_investigator=cls.pi, start_date=cls.now - datetime.timedelta(days=30),
            end_date=cls.now + datetime.timedelta(days=30), **audit_fields(cls.staff))
        TransientPhotometry.objects.filter(pk=cls.phot.pk).update(allocation=cls.alloc)
        cls.grantee = create_test_user("dap863_grantee", is_staff=False)
        cls.grant, _ = access_actions.program_action(cls.pi_user, cls.alloc, effect="allow", grantee_user=cls.grantee)
        access_actions.dataset_action(cls.pi_user, cls.spec, effect="deny", grantee_user=cls.member)

    def setUp(self):
        super().setUp()
        self.req = self.accepted()
        self.url = reverse("access_dashboard")

    def page(self, preview):
        resp = _client(self.pi_user, preview).get(self.url)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_preview_off_is_unchanged(self):
        html = self.page(preview=False)
        self.assertNotIn("yse-access-editor", html)
        self.assertNotIn('data-layout="preview"', html)
        forms = access_forms(html)
        self.assertTrue(forms and not any(f["in_editor"] for f in forms))
        self.assertIn('data-grant-id="%d"' % self.grant.pk, html)
        self.assertIn('data-request-id="%d"' % self.req.pk, html)

    def test_preview_is_plain_tables_with_one_editor(self):
        html = self.page(preview=True)
        self.assertEqual(html.count('id="yse-access-editor"'), 1)
        for table in ("yse-dashboard-grants", "yse-dashboard-requests", "yse-dashboard-datasets"):
            self.assertIn('id="%s" data-layout="preview"' % table, html)
        forms = access_forms(html)
        self.assertTrue(forms and all(f["in_editor"] for f in forms))
        self.assertIn('data-request-id="%d"' % self.req.pk, html)
        self.assertNotIn("photometry set", html.lower())

    def test_editor_forms_post_the_same_fields_as_today(self):
        on, off = signatures(access_forms(self.page(preview=True))), signatures(access_forms(self.page(False)))
        self.assertEqual(on, off)
        actions = {sig[1] for sig in on}
        self.assertTrue({"stop", "remove_group", "remove_allow", "lift", "autopublish", None} <= actions)

    def test_edit_through_the_editor(self):
        forms = access_forms(self.page(preview=True))
        client = _client(self.pi_user, preview=True)
        resp = client.post(self.url, editor_form(forms, action="stop", grant_id=self.grant.pk), follow=True)
        self.assertContains(resp, "Stopped going forward")
        self.grant.refresh_from_db()
        self.assertIsNotNone(self.grant.stopped_at)

        post = editor_form(forms, action="autopublish", kind="photometry", dataset_id=self.phot.pk)
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "Automatic publishing allowed")

        post = editor_form(forms, action="lift", kind="spectrum", dataset_id=self.spec.pk)
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "Restriction lifted")

        post = editor_form(forms, action="remove_group", kind="photometry", dataset_id=self.phot.pk,
                           group_id=self.o.pk)
        post.update(direction="backward")
        resp = client.post(self.url, post, follow=True)
        self.assertContains(resp, "including data taken earlier")
        self.assertFalse(self.phot.groups.filter(pk=self.o.pk).exists())

        revoke = next(f for f in forms if "/revoke" in f["url"])
        client.post(revoke["url"], dict(revoke["fields"], csrfmiddlewaretoken=""))
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, DataAccessRequest.STATUS_REVOKED)
        self.assertEqual(self.req.revoke_direction, "forward")

    def test_permission_denials_unchanged(self):
        forms = access_forms(self.page(preview=True))
        for preview in (True, False):
            client = _client(self.member, preview)
            resp = client.post(self.url, editor_form(forms, action="stop", grant_id=self.grant.pk), follow=True)
            self.assertContains(resp, "Not changed")
            post = editor_form(forms, action="remove_group", kind="photometry", dataset_id=self.phot.pk,
                               group_id=self.o.pk)
            resp = client.post(self.url, post, follow=True)
            self.assertContains(resp, "Not changed")
            revoke = next(f for f in forms if "/revoke" in f["url"])
            client.post(revoke["url"], revoke["fields"])
        self.assertIsNone(StandingAccessGrant.objects.get(pk=self.grant.pk).stopped_at)
        self.assertTrue(self.phot.groups.filter(pk=self.o.pk).exists())
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, DataAccessRequest.STATUS_ACCEPTED)
