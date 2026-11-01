"""SQL Explorer renders inside the YSE-PZ site shell (#450).

Access is unchanged: common.authz.explorer_access_allowed decides (all staff by
default; superusers plus EXPLORER_ACCESS_GROUP when EXPLORER_RESTRICTED_ACCESS
is on). Users without access get 403, anonymous users the site login.
"""

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

SHELL_MARKERS = (
    'class="main-header navbar',      # site navbar
    'yse-sidebar',                    # site sidebar
    "YSE_App/yse-theme.css",          # site theme
    "YSE_App/yse-chrome.js",          # dark/light toggle
    'id="yse-explorer"',              # Explorer wrapper
    "YSE_App/explorer-theme.css",
    "YSE_App/explorer-shell.js",
)
# Explorer's own Bootstrap 3 and second jQuery are not loaded inside the shell.
NOT_IN_SHELL = (
    "explorer/css/bootstrap.min.css",
    "explorer/js/bootstrap.min.js",
    "explorer/js/jquery.min.js",
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExplorerShellTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.staff = User.objects.create_user("explorer_shell_staff", is_staff=True)
        cls.member = User.objects.create_user("explorer_shell_member")
        cls.group_member = User.objects.create_user("explorer_shell_group_member")
        cls.group_member.groups.add(Group.objects.get(name=settings.EXPLORER_ACCESS_GROUP))
        cls.query = Query.objects.create(title="Shell test query", sql="SELECT 1", created_by_user=cls.staff)

    def _client(self, user=None):
        client = Client()
        if user is not None:
            client.force_login(user)
        return client

    def _urls(self):
        return (
            reverse("explorer_index"),
            reverse("explorer_playground"),
            reverse("query_create"),
            reverse("explorer_logs"),
            # show=0: render the page without running the SQL on the explorer alias.
            reverse("query_detail", args=[self.query.id]) + "?show=0",
        )

    def _assert_in_shell(self, response, url):
        self.assertEqual(response.status_code, 200, url)
        html = response.content.decode()
        for marker in SHELL_MARKERS:
            self.assertIn(marker, html, f"{marker!r} missing on {url}")
        for asset in NOT_IN_SHELL:
            self.assertNotIn(asset, html, f"{asset!r} loaded on {url}")
        self.assertEqual(html.count("<html"), 1, url)

    @override_settings(EXPLORER_RESTRICTED_ACCESS=False)
    def test_staff_pages_render_in_shell(self):
        client = self._client(self.staff)
        for url in self._urls():
            self._assert_in_shell(client.get(url), url)

    @override_settings(EXPLORER_RESTRICTED_ACCESS=False)
    def test_query_page_keeps_explorer_markup(self):
        html = self._client(self.staff).get(self._urls()[-1]).content.decode()
        self.assertIn("Shell test query", html)
        self.assertIn('id="id_sql"', html)
        self.assertIn("new ExplorerEditor(queryId)", html)
        self.assertIn('queryId = "%d"' % self.query.id, html)
        # Explorer's own scripts still load, after the shell's jQuery.
        self.assertLess(html.index("bower_components/jquery/dist/jquery.min.js"),
                        html.index("explorer/js/explorer.js"))
        self.assertLess(html.index("explorer/js/explorer.js"), html.index("YSE_App/explorer-shell.js"))
        self.assertLess(html.index("YSE_App/explorer-shell.js"), html.index("new ExplorerEditor(queryId)"))

    @override_settings(EXPLORER_RESTRICTED_ACCESS=True)
    def test_restricted_group_member_renders_in_shell(self):
        client = self._client(self.group_member)
        for url in self._urls():
            self._assert_in_shell(client.get(url), url)

    @override_settings(EXPLORER_RESTRICTED_ACCESS=False)
    def test_non_staff_forbidden(self):
        client = self._client(self.member)
        for url in self._urls():
            response = client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertNotIn('id="yse-explorer"', response.content.decode())
        self.assertEqual(client.post(reverse("explorer_playground"), {"sql": "SELECT 1"}).status_code, 403)

    @override_settings(EXPLORER_RESTRICTED_ACCESS=True)
    def test_restricted_staff_outside_group_forbidden(self):
        client = self._client(self.staff)
        for url in self._urls():
            self.assertEqual(client.get(url).status_code, 403, url)

    def test_anonymous_redirected_to_login(self):
        for url in self._urls():
            response = self._client().get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("next=", response["Location"])

    @override_settings(EXPLORER_RESTRICTED_ACCESS=False)
    def test_schema_pane_is_bare(self):
        schema = [("YSE_App_transient", [("name", "varchar")])]
        with mock.patch("explorer.views.schema.schema_info", return_value=schema):
            response = self._client(self.staff).get(reverse("explorer_schema", args=["explorer"]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("YSE_App_transient", html)
        self.assertIn("yse-explorer-bare", html)
        self.assertIn("YSE_App/explorer-theme.css", html)
        self.assertNotIn("yse-sidebar", html)
        self.assertNotIn('class="main-header navbar', html)

    @override_settings(EXPLORER_RESTRICTED_ACCESS=False)
    def test_schema_pane_forbidden_for_non_staff(self):
        response = self._client(self.member).get(reverse("explorer_schema", args=["explorer"]))
        self.assertEqual(response.status_code, 403)

    def test_access_settings_unchanged(self):
        self.assertEqual(settings.EXPLORER_NO_PERMISSION_VIEW, "YSE_App.explorer_shell.explorer_no_permission")
        self.assertIn("YSE_App_encryptedcredential", settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)
        self.assertIn("auth_", settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)
        self.assertTrue(hasattr(settings, "EXPLORER_QUERY_MAX_EXECUTION_MS"))
        self.assertIn("YSE_App.login_required.LoginRequiredMiddleware", settings.MIDDLEWARE)
