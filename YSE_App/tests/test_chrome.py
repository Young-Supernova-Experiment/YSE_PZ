"""Chrome redesign smokes: header search, skip link, night theme, nav groups (#123–#130)."""

from unittest.mock import patch

from django.test import Client, TestCase

from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user
from YSE_App.tests.static_asset_utils import static_asset_available


class ChromeSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("chrome_user")
        with patch("YSE_App.models.transient_models.tess_obs", return_value=False):
            cls.transient = create_minimal_transient(cls.user, name="chrome-sn")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _assert_chrome(self, response):
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn('data-yse-theme="dark"', html)
        self.assertIn('id="yse-header-search-q"', html)
        self.assertIn('name="q"', html)
        self.assertIn('href="#yse-main"', html)
        self.assertIn("Skip to content", html)
        self.assertIn("yse-nav-core", html)
        self.assertIn("yse-nav-observing", html)
        self.assertIn("yse-nav-tools", html)
        self.assertIn("yse-nav-docs", html)
        self.assertIn("yse-theme.css", html)
        self.assertIn("yse-chrome.js", html)
        self.assertIn("vendor/adminlte-3.2/css/adminlte.min.css", html)
        self.assertIn("vendor/bootstrap-4.6/css/bootstrap.min.css", html)
        self.assertNotIn("skin-red", html)
        self.assertNotIn("user-scalable=no", html)
        return html

    def test_dashboard_chrome(self):
        html = self._assert_chrome(self.client.get("/dashboard/"))
        self.assertIn('class="nav-header yse-nav-core"', html)

    def test_transient_detail_chrome(self):
        url = f"/transient_detail/{self.transient.slug}/"
        html = self._assert_chrome(self.client.get(url))
        self.assertIn('class="nav-link active"', html)
        self.assertIn("carousel-item", html)
        self.assertIn("collapsed-box", html)
        self.assertIn("Who can see this?", html)
        self.assertIn("yse-audience-toggle-public", html)
        self.assertIn('placeholder="Tag name"', html)
        self.assertNotIn('placeholder="Event Title"', html)
        self.assertNotIn("dataTables.bootstrap.min.css", html)

    def test_theme_css_has_light_and_collapsed_box(self):
        from pathlib import Path

        css_path = Path(__file__).resolve().parents[1] / "static" / "YSE_App" / "yse-theme.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn('html[data-yse-theme="light"]', css)
        self.assertIn('html[data-yse-theme="dark"]', css)
        self.assertIn("#070913", css)
        self.assertIn("collapsed-box", css)
        self.assertIn("carousel-inner > .item", css)
        self.assertIn("width: 33.33333% !important", css)
        self.assertIn("width: 50% !important", css)
        self.assertIn('content: "/"', css)
        self.assertIn(".btn-box-tool", css)
        self.assertIn("yse-page-dashboard .btn-group .btn", css)

    def test_vendored_assets_exist(self):
        for rel in (
            "YSE_App/vendor/adminlte-3.2/css/adminlte.min.css",
            "YSE_App/vendor/adminlte-3.2/js/adminlte.min.js",
            "YSE_App/vendor/bootstrap-4.6/css/bootstrap.min.css",
            "YSE_App/vendor/bootstrap-4.6/js/bootstrap.bundle.min.js",
            "YSE_App/vendor/datatables-bs4/dataTables.bootstrap4.min.css",
            "YSE_App/yse-theme.css",
            "YSE_App/yse-chrome.js",
        ):
            self.assertTrue(static_asset_available(rel), msg=rel)


class LoginChromeTests(TestCase):
    def test_login_has_labels_and_night_theme(self):
        response = Client().get("/login/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8", errors="replace")
        self.assertIn('data-yse-theme="dark"', html)
        self.assertIn('<label for="id_username">Username</label>', html)
        self.assertIn('<label for="id_password">Password</label>', html)
        self.assertIn("yse-theme.css", html)
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="current-password"', html)
