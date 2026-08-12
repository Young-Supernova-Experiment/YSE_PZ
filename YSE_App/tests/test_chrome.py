"""Chrome redesign smokes: header search, skip link, night theme, nav groups (#123–#130)."""

from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import Client, TestCase

from YSE_App.models import TransientTag, WebAppColor
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

    def test_search_by_tag_lists_available_tags(self):
        audit = {"created_by": self.user, "modified_by": self.user}
        color, _ = WebAppColor.objects.get_or_create(color="red", defaults=audit)
        TransientTag.objects.get_or_create(
            name="chrome-tag-young",
            defaults={"color": color, **audit},
        )
        html = self._assert_chrome(self.client.get("/transient_tags/"))
        self.assertIn("Available Tags", html)
        self.assertIn("Selected Tags", html)
        self.assertIn("Matching Transients", html)
        self.assertIn("chrome-tag-young", html)
        self.assertIn("external-event", html)
        self.assertIn("bg-red", html)
        self.assertIn('id="external-events"', html)
        self.assertIn('id="associated-events"', html)

    def test_dashboard_chrome(self):
        html = self._assert_chrome(self.client.get("/dashboard/"))
        self.assertIn('class="nav-header yse-nav-core"', html)
        self.assertIn("layout-footer-not-fixed", html)

    def test_comment_audience_lists_all_user_groups(self):
        group_public, _ = Group.objects.get_or_create(name="Public")
        group_a, _ = Group.objects.get_or_create(name="sec-group-a")
        group_b, _ = Group.objects.get_or_create(name="sec-group-b")
        user_ab = create_test_user("sec_user_ab", is_staff=False)
        user_ab.groups.add(group_public, group_a, group_b)
        self.client.force_login(user_ab)
        html = self._assert_chrome(
            self.client.get(f"/transient_detail/{self.transient.slug}/")
        )
        self.assertEqual(html.count(">Public</span>"), 1)
        self.assertIn("sec-group-a", html)
        self.assertIn("sec-group-b", html)
        self.assertNotIn('id="id_audience_groups_%s"' % group_public.pk, html)
        self.assertNotRegex(
            html,
            r'<input(?=[^>]*\bid="id_is_public")(?=[^>]*\bchecked\b)[^>]*>',
        )
        self.assertRegex(
            html,
            r'<input(?=[^>]*\bid="id_audience_groups_%s")(?=[^>]*\bchecked\b)[^>]*>'
            % group_a.pk,
        )
        self.assertRegex(
            html,
            r'<input(?=[^>]*\bid="id_audience_groups_%s")(?=[^>]*\bchecked\b)[^>]*>'
            % group_b.pk,
        )

    def test_transient_detail_chrome(self):
        group = Group.objects.create(name="chrome-collab")
        self.user.groups.add(group)
        url = f"/transient_detail/{self.transient.slug}/"
        html = self._assert_chrome(self.client.get(url))
        self.assertIn('class="nav-link active"', html)
        self.assertIn("carousel-item", html)
        self.assertIn("collapsed-box", html)
        self.assertIn("Who can see this?", html)
        self.assertIn('id="id_is_public"', html)
        self.assertIn('type="checkbox"', html)
        self.assertIn("yse-audience-native", html)
        self.assertIn(">Public</span>", html)
        self.assertIn("chrome-collab", html)
        followup_at = html.find("add_transient_followup_btn")
        self.assertGreater(followup_at, 0)
        self.assertNotIn("collapsed-box", html[max(0, followup_at - 500):followup_at])
        self.assertNotIn('class="box-body collapsed-box"', html)
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
        self.assertIn(".box.collapsed-box > .box-body", css)
        self.assertNotIn(".collapsed-box.box-body", css)
        self.assertIn("carousel-inner > .item", css)
        self.assertIn(".yse-page-transient-summary .form-group br", css)
        self.assertIn("yse-audience-native", css)
        self.assertIn("color-scheme: dark", css)
        self.assertIn("position: static !important", css)
        self.assertIn("yse-audience-label", css)
        self.assertIn("width: 33.33333% !important", css)
        self.assertIn("width: 50% !important", css)
        self.assertIn('content: "/"', css)
        self.assertIn(".btn-box-tool", css)
        self.assertIn("yse-page-dashboard .btn-group .btn", css)
        self.assertIn(".external-event", css)
        self.assertIn(".bg-red", css)
        self.assertIn(".bg-light-blue", css)

    def test_observing_calendar_fits_viewport(self):
        from pathlib import Path

        js_path = Path(__file__).resolve().parents[1] / "static" / "YSE_App" / "yse-chrome.js"
        js = js_path.read_text(encoding="utf-8")
        self.assertIn("fitYseCalendar", js)
        self.assertIn("yseCalendarHeight", js)
        html = self._assert_chrome(self.client.get("/observing_calendar/"))
        self.assertIn('id="calendar"', html)
        self.assertIn("$(window).height() - 180", html)

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
