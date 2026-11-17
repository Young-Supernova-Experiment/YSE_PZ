"""Chrome redesign smokes: header search, skip link, night theme, nav groups (#123–#130)."""


from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings

from YSE_App.models import TransientTag, WebAppColor
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user
from YSE_App.tests.static_asset_utils import static_asset_available


class ChromeSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("chrome_user")
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
        # AdminLTE 3.2's stylesheet already contains Bootstrap 4.6 (#819).
        self.assertNotIn("vendor/bootstrap-4.6/css/bootstrap.min.css", html)
        self.assertIn("vendor/bootstrap-4.6/js/bootstrap.bundle.min.js", html)
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
        # CheckboxSelectMultiple ids are choice indices (id_audience_groups_1), not PKs.
        self.assertNotIn(
            'name="audience_groups" value="%s"' % group_public.pk, html
        )
        self.assertNotRegex(
            html,
            r'<input(?=[^>]*\bid="id_is_public")(?=[^>]*\bchecked\b)[^>]*>',
        )
        self.assertRegex(
            html,
            r'<input(?=[^>]*\bname="audience_groups")(?=[^>]*\bvalue="%s")(?=[^>]*\bchecked\b)[^>]*>'
            % group_a.pk,
        )
        self.assertRegex(
            html,
            r'<input(?=[^>]*\bname="audience_groups")(?=[^>]*\bvalue="%s")(?=[^>]*\bchecked\b)[^>]*>'
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

    def test_light_mode_switches_the_sidebar(self):
        """The toggle swaps AdminLTE's sidebar classes and the light theme has a light sidebar."""
        import re
        from pathlib import Path

        static = Path(__file__).resolve().parents[1] / "static" / "YSE_App"
        js = (static / "yse-chrome.js").read_text(encoding="utf-8")
        self.assertIn("sidebar-light-primary", js)
        self.assertIn("sidebar-dark-primary", js)
        css = (static / "yse-theme.css").read_text(encoding="utf-8")
        light = css[css.index('html[data-yse-theme="light"] {'):]
        light = light[:light.index("}")]
        bg = re.search(r"--yse-sidebar-bg:\s*#([0-9a-fA-F]{6})", light).group(1)
        self.assertGreater(sum(int(bg[i:i + 2], 16) for i in (0, 2, 4)) / 3, 200, bg)

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


@override_settings(ONBOARDING_GATE_ENABLED=False)
class ChromeNavTests(TestCase):
    """Sidebar links, asset loading, OS theme and sidebar state (#801 #802 #819 #821 #823 #839)."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("chrome_nav_staff", is_staff=True)
        cls.member = create_test_user("chrome_nav_member", is_staff=False)

    def _html(self, user, url="/dashboard/"):
        client = Client()
        client.force_login(user)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8", errors="replace")

    def test_sidebar_has_search_and_favorites(self):
        from django.urls import reverse

        html = self._html(self.member)
        self.assertIn('href="%s"' % reverse("search"), html)
        self.assertIn("<p>Transient Search</p>", html)
        self.assertIn('href="%s"' % reverse("my_favorites"), html)
        self.assertIn("<p>Favorites</p>", html)
        # Acknowledgements stay in the sidebar and the footer for everyone.
        self.assertIn("<p>Acknowledgements</p>", html)
        self.assertIn('class="main-footer yse-footer"', html)

    @override_settings(EXPLORER_RESTRICTED_ACCESS=False, EXPLORER_OPEN_TO_ALL_USERS=False)
    def test_admin_and_explorer_links_only_for_allowed_users(self):
        from django.urls import reverse

        html = self._html(self.staff)
        self.assertIn("<p>Admin</p>", html)
        self.assertIn("<p>Query Explorer</p>", html)
        self.assertIn('href="%s"' % reverse("explorer_index"), html)
        html = self._html(self.member)
        self.assertNotIn("<p>Admin</p>", html)
        self.assertNotIn("<p>Query Explorer</p>", html)

    @override_settings(EXPLORER_OPEN_TO_ALL_USERS=True, EXPLORER_QUERY_SCOPE="enforce")
    def test_explorer_link_for_open_explorer(self):
        html = self._html(self.member)
        self.assertNotIn("<p>Admin</p>", html)
        self.assertIn("<p>Query Explorer</p>", html)

    def test_unused_assets_not_loaded(self):
        html = self._html(self.member)
        for gone in ("morris.css", "jquery-jvectormap.css", "bootstrap3-wysihtml5.min.css",
                     "datatables.net-bs/js/dataTables.bootstrap.min.js"):
            self.assertNotIn(gone, html)
        self.assertEqual(html.count("adminlte.min.css"), 1)
        self.assertIn("vendor/datatables-bs4/dataTables.bootstrap4.min.js", html)
        # Still used by date pickers on several pages.
        self.assertIn("bootstrap-datepicker.min.css", html)
        self.assertIn("daterangepicker.css", html)
        self.assertNotIn('form-control select2"', html)

    def test_sidebar_starts_expanded_and_theme_follows_os(self):
        import re
        from pathlib import Path

        html = self._html(self.member)
        body = re.search(r"<body class=\"([^\"]*)\"", html).group(1).split()
        self.assertIn("sidebar-mini", body)
        self.assertNotIn("sidebar-collapse", body)
        self.assertIn('localStorage.getItem("yse-sidebar")', html)
        self.assertIn("prefers-color-scheme: light", html)
        js = (Path(__file__).resolve().parents[1] / "static" / "YSE_App" / "yse-chrome.js").read_text(encoding="utf-8")
        self.assertIn("prefers-color-scheme: light", js)
        self.assertIn('"yse-sidebar"', js)
        self.assertIn("collapsed.lte.pushmenu shown.lte.pushmenu", js)
        self.assertIn("navbar-light", js)
        self.assertIn("btn-outline-secondary", js)
        # The OS default is applied without being stored; only the toggle stores a choice.
        self.assertIn("apply(currentTheme(), false)", js)
        self.assertIn('apply(currentTheme() === "dark" ? "light" : "dark", true)', js)
        # The explicit choice lives in a new key; a legacy "yse-theme" only migrates "light".
        self.assertIn('"yse-theme-choice"', js)
        self.assertIn('localStorage.getItem("yse-theme-choice")', html)
        self.assertNotIn('localStorage.setItem("yse-theme",', html)
        self.assertNotIn("setItem(LEGACY_KEY", js)
        # The sidebar state is stored only after a click on the toggle, and width is
        # measured like AdminLTE (clientWidth, scrollbar excluded).
        self.assertIn("userToggle && isWide()", js)
        self.assertNotIn("innerWidth", js)
        self.assertIn("document.documentElement.clientWidth > 992", html)

    def test_standalone_pages_follow_os_theme_and_bust_cache(self):
        """Pages outside base.html use the same theme rule and the new asset versions (#821)."""
        from pathlib import Path

        templates = Path(__file__).resolve().parents[1] / "templates"
        paths = [templates / "YSE_App" / ("%s.html" % n) for n in (
            "login", "signup", "signup_done", "signup_verify", "logout_confirm")]
        paths += [templates / "YSE_App" / "staff_mfa" / "base.html", templates / "explorer" / "yse_bare.html"]
        for path in paths:
            src = path.read_text(encoding="utf-8")
            self.assertIn('localStorage.getItem("yse-theme-choice")', src, path.name)
            self.assertIn("prefers-color-scheme: light", src, path.name)
            self.assertNotIn('getItem("yse-theme") || "dark"', src, path.name)
            self.assertIn("yse-theme.css' %}?v=chrome-nav-1", src, path.name)
            if "yse-chrome.js" in src:
                self.assertIn("yse-chrome.js' %}?v=chrome-nav-1", src, path.name)
