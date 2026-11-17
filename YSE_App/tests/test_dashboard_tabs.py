"""Tabbed main dashboard for UI preview users (#859).

Covers both preview states (preview off keeps today's page), the tab counts and
default tab, the shared filter, the column set, the bulk status path the page's
script uses (one session-auth PATCH per row, CSRF enforced, ``modified_by``
recorded, unlisted rows refused) and the query counts of the shell and of one tab.

Synthetic users and rows only.
"""

import re

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from YSE_App import dashboard_preview
from YSE_App.models import Transient
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)

PREVIEW_COOKIE = "yse-ui-preview"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def _tns_name(prefix, i):
    """A TNS-style synthetic name (listed for every signed-in user): 2026 + letters only."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    return "2026%s%s%s" % (prefix, letters[i // 26], letters[i % 26])


def _seed(user, counts):
    """``counts``: {status name: rows}; TNS-style names so a non-staff user sees them listed."""
    made = []
    for s_idx, (status_name, n) in enumerate(sorted(counts.items())):
        for i in range(n):
            made.append(create_minimal_transient(
                user, name=_tns_name("dt" + "abcdefgh"[s_idx], i), status_name=status_name,
                ra=10.0 + i * 0.01, dec=20.0))
    return made


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS, DATA_SERVICE_READER_USERNAMES=[], API_SERVICE_USERNAMES=[],
                   UI_PREVIEW_DEFAULT=False)
class DashboardTabsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("dashtabs_owner")
        cls.viewer = create_test_user("dashtabs_viewer", is_staff=False)
        cls.statuses = ensure_transient_statuses(cls.owner)

    def client_for(self, user, preview=True, **kwargs):
        client = Client(**kwargs)
        client.force_login(user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client


class PreviewOffTests(DashboardTabsBase):
    """Without the preview switch the dashboard is today's page."""

    def test_dashboard_is_unchanged(self):
        _seed(self.owner, {"Following": 1})
        response = self.client_for(self.viewer, preview=False).get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "YSE_App/dashboard.html")
        self.assertTemplateNotUsed(response, "YSE_App/dashboard_v2.html")
        self.assertNotContains(response, 'id="yse-dash2"')

    def test_section_is_unchanged(self):
        _seed(self.owner, {"Following": 1})
        response = self.client_for(self.viewer, preview=False).get(reverse("dashboard_section", args=["following"]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "YSE_App/dashboard_section.html")
        self.assertNotContains(response, "yse-dash-select")


class TabsTests(DashboardTabsBase):
    def test_tabs_with_counts(self):
        _seed(self.owner, {"Following": 3, "Watch": 2, "Ignore": 4})
        response = self.client_for(self.viewer).get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "YSE_App/dashboard_v2.html")
        tabs = {tab["key"]: tab["count"] for tab in response.context["tabs"]}
        self.assertEqual(list(tabs), ["new", "followuprequested", "following", "interesting", "watch",
                                      "followupfinished", "needstemplate"])
        self.assertEqual(tabs["following"], 3)
        self.assertEqual(tabs["watch"], 2)
        self.assertEqual(tabs["new"], 0)
        self.assertContains(response, 'data-dash-tab="following"')
        self.assertContains(response, '<span class="badge badge-secondary yse-dash-count">3</span>', html=False)
        # one table slot per tab, loaded by the script
        self.assertEqual(response.content.decode().count('class="yse-dash-slot"'), 7)

    def test_counts_leave_out_unlisted_rows(self):
        _seed(self.owner, {"Following": 2})
        create_minimal_transient(self.owner, name="private-dashtab-1", status_name="Following")
        response = self.client_for(self.viewer).get(reverse("dashboard"))
        tabs = {tab["key"]: tab["count"] for tab in response.context["tabs"]}
        self.assertEqual(tabs["following"], 2)

    def test_default_tab_following_when_it_has_rows(self):
        _seed(self.owner, {"Following": 1, "FollowupRequested": 1})
        response = self.client_for(self.viewer).get(reverse("dashboard"))
        self.assertEqual(response.context["default_tab"], "following")
        self.assertContains(response, 'data-default-tab="following"')

    def test_default_tab_followup_requested_otherwise(self):
        _seed(self.owner, {"New": 2})
        response = self.client_for(self.viewer).get(reverse("dashboard"))
        self.assertEqual(response.context["default_tab"], "followuprequested")

    def test_default_tab_helper(self):
        self.assertEqual(dashboard_preview.default_tab({"following": 4, "followuprequested": 2}), "following")
        self.assertEqual(dashboard_preview.default_tab({"following": 0, "followuprequested": 0}), "followuprequested")
        self.assertEqual(dashboard_preview.default_tab({}), "followuprequested")


class FilterTests(DashboardTabsBase):
    def test_one_filter_applies_to_counts_and_tables(self):
        rows = _seed(self.owner, {"Following": 3, "Watch": 1})
        target = rows[0]
        client = self.client_for(self.viewer)
        response = client.get(reverse("dashboard"), {"dash-ex": target.name})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertEqual(content.count('name="dash-ex"'), 1)
        tabs = {tab["key"]: tab["count"] for tab in response.context["tabs"]}
        self.assertEqual(tabs["following"], 1)
        self.assertEqual(tabs["watch"], 0)

        section = client.get(reverse("dashboard_section", args=["following"]), {"dash-ex": target.name})
        self.assertContains(section, target.name)
        self.assertNotContains(section, rows[1].name)


class ColumnTests(DashboardTabsBase):
    EXPECTED_DEFAULT = ("favorite", "disc_date_string", "recent_mag", "peak_mag", "best_spec_class",
                        "best_redshift", "status_string")

    def test_eight_default_columns(self):
        # name is always shown, so it plus these seven make the eight; the tick box is the selection control.
        self.assertEqual(dashboard_preview.DEFAULT_COLUMNS, self.EXPECTED_DEFAULT)

    def test_section_renders_every_column_tagged(self):
        _seed(self.owner, {"Following": 2})
        response = self.client_for(self.viewer).get(reverse("dashboard_section", args=["following"]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        names = ["select", "name_string"] + [name for name, _, _ in dashboard_preview.COLUMN_CHOICES]
        for name in names:
            self.assertIn('<th scope="col" data-col="%s"' % name, content)
        self.assertEqual(content.count('class="yse-dash-select"'), 2)
        self.assertIn("yse-dash-select-all", content)
        # no per-row status dropdowns
        self.assertNotIn("transientStatusChange", content)
        header_order = re.findall(r'<th scope="col" data-col="([a-z_]+)"', content)
        self.assertEqual(header_order, names)

    def test_columns_menu_lists_optional_columns(self):
        response = self.client_for(self.viewer).get(reverse("dashboard"))
        for name, _, shown in dashboard_preview.COLUMN_CHOICES:
            box = 'class="yse-dash-col-toggle" value="%s"%s>' % (name, " checked" if shown else "")
            self.assertContains(response, box)
        self.assertContains(response, 'data-default-columns="%s"' % " ".join(self.EXPECTED_DEFAULT))

    @override_settings(DATA_SERVICE_READER_USERNAMES=["dashtabs_owner"])
    def test_last_mag_shows_age(self):
        from YSE_App.services.photstat import recompute_many

        (row,) = _seed(self.owner, {"Following": 1})
        attach_synthetic_photometry(self.owner, row, n_points=3)
        recompute_many([row.pk])
        response = self.client_for(self.owner).get(reverse("dashboard_section", args=["following"]))
        self.assertContains(response, "yse-dash-age")

    def test_age_text(self):
        self.assertEqual(dashboard_preview._age_text(0.2), "<1d")
        self.assertEqual(dashboard_preview._age_text(3.7), "3d")


class BulkStatusTests(DashboardTabsBase):
    """The page's bulk control PATCHes each ticked row through the transient API, as its script does."""

    def _page_targets(self, client):
        page = client.get(reverse("dashboard")).content.decode()
        api_url = re.search(r'data-api-url="([^"]+)"', page).group(1)
        csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
        options = dict((label.strip(), url) for url, label in
                       re.findall(r'<option value="([^"]+)" title="[^"]*">([^<]+)</option>', page))
        return api_url, csrf, options

    def _patch(self, client, api_url, csrf, transient_id, status_url):
        return client.patch(api_url.replace("/0/", "/%d/" % transient_id), "status=" + status_url,
                            content_type="application/x-www-form-urlencoded", HTTP_X_CSRFTOKEN=csrf,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_bulk_status_sets_status_and_modified_by(self):
        rows = _seed(self.owner, {"Following": 3})
        client = self.client_for(self.viewer, enforce_csrf_checks=True)
        api_url, csrf, options = self._page_targets(client)
        self.assertEqual(api_url, reverse("transient-detail", args=[0]))
        watch_url = reverse("transientstatus-detail", args=[self.statuses["Watch"].pk])
        self.assertIn(watch_url, options.values())
        for row in rows:
            response = self._patch(client, api_url, csrf, row.pk, watch_url)
            self.assertEqual(response.status_code, 200, response.content)
        for row in Transient.objects.filter(pk__in=[r.pk for r in rows]):
            self.assertEqual(row.status_id, self.statuses["Watch"].pk)
            self.assertEqual(row.modified_by_id, self.viewer.pk)

    def test_bulk_status_needs_csrf_token(self):
        (row,) = _seed(self.owner, {"Following": 1})
        client = self.client_for(self.viewer, enforce_csrf_checks=True)
        api_url, _, _ = self._page_targets(client)
        watch_url = reverse("transientstatus-detail", args=[self.statuses["Watch"].pk])
        response = self._patch(client, api_url, "not-a-token", row.pk, watch_url)
        self.assertEqual(response.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status_id, self.statuses["Following"].pk)

    def test_bulk_status_refuses_unlisted_rows(self):
        private = create_minimal_transient(self.owner, name="private-dashtab-2", status_name="Following")
        client = self.client_for(self.viewer, enforce_csrf_checks=True)
        api_url, csrf, _ = self._page_targets(client)
        watch_url = reverse("transientstatus-detail", args=[self.statuses["Watch"].pk])
        response = self._patch(client, api_url, csrf, private.pk, watch_url)
        self.assertEqual(response.status_code, 404)
        private.refresh_from_db()
        self.assertEqual(private.status_id, self.statuses["Following"].pk)

    def test_page_has_confirming_bulk_control(self):
        response = self.client_for(self.viewer).get(reverse("dashboard"))
        self.assertContains(response, "yse-dash-bulk-apply")
        self.assertContains(response, "dashboard-tabs.js?v=")
        from django.contrib.staticfiles import finders

        script = open(finders.find("YSE_App/dashboard-tabs.js"), encoding="utf-8").read()
        self.assertIn("window.confirm(", script)
        self.assertIn("method: 'PATCH'", script)


class QueryCountTests(DashboardTabsBase):
    """The counts are one GROUP BY; neither the shell nor a tab grows with the number of rows."""

    def _queries(self, client, url, params=None):
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url, params or {})
        self.assertEqual(response.status_code, 200)
        return [q["sql"] for q in ctx.captured_queries]

    STATUS_GROUP = re.compile(r'GROUP BY [`"]?YSE_APP_TRANSIENT[`"]?\.[`"]?STATUS_ID')

    def _count_queries(self, sqls):
        """COUNTs grouped on the transient status: the tab counts."""
        return [s for s in sqls if "COUNT(" in s.upper() and self.STATUS_GROUP.search(s.upper())]

    def _transient_counts(self, sqls):
        """Every COUNT over the transient table (a per-tab count would show up here)."""
        return [s for s in sqls if "COUNT(" in s.upper() and re.search(r'FROM [`"]?YSE_APP_TRANSIENT[`"]?\s', s.upper())]

    def test_shell_counts_are_one_query(self):
        _seed(self.owner, {name: 2 for name in ("New", "Following", "Watch")})
        client = self.client_for(self.viewer)
        sqls = self._queries(client, reverse("dashboard"))
        self.assertEqual(len(self._count_queries(sqls)), 1, sqls)
        self.assertEqual(len(self._transient_counts(sqls)), 1, self._transient_counts(sqls))
        self.assertLessEqual(len(sqls), 20)

    def test_shell_and_tab_do_not_grow_with_rows(self):
        client = self.client_for(self.viewer)
        _seed(self.owner, {"Following": 2, "Watch": 1})
        shell_small = len(self._queries(client, reverse("dashboard")))
        tab_small = len(self._queries(client, reverse("dashboard_section", args=["following"])))
        _seed(self.owner, {"Following": 9, "Watch": 6})
        shell_large = len(self._queries(client, reverse("dashboard")))
        tab_large = len(self._queries(client, reverse("dashboard_section", args=["following"])))
        self.assertEqual(shell_small, shell_large)
        self.assertEqual(tab_small, tab_large)
        self.assertLessEqual(tab_large, 15)

    def test_filtered_counts_stay_one_query(self):
        rows = _seed(self.owner, {"Following": 2})
        client = self.client_for(self.viewer)
        sqls = self._queries(client, reverse("dashboard"), {"dash-ex": rows[0].name})
        self.assertEqual(len(self._count_queries(sqls)), 1)
