"""Grouped sidebar (#858), glossary and in-context help (#862), behind the UI preview.

Synthetic users only. The preview is the per-user cookie from
``YSE_App.common.ui_preview``; without it every page must look as before.
"""

from django.contrib.auth.models import AnonymousUser, Group
from django.template import Context, Template
from django.test import Client, TestCase
from django.urls import get_script_prefix, set_script_prefix

from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.tests.fixtures_minimal import create_test_user

NEW_GROUPS = (
    "yse-nav-find", "yse-nav-observe", "yse-nav-discover", "yse-nav-analyze",
    "yse-nav-share", "yse-nav-help",
)
OLD_GROUPS = ("yse-nav-core", "yse-nav-observing", "yse-nav-tools", "yse-nav-docs")


class NavPreviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_test_user("nav_member", is_staff=False)
        cls.staff = create_test_user("nav_staff", is_staff=True)

    def _get(self, user, path, preview, **extra):
        client = Client()
        if user is not None:
            client.force_login(user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        response = client.get(path, **extra)
        self.assertEqual(response.status_code, 200, path)
        return response.content.decode("utf-8", errors="replace")

    def test_preview_off_keeps_current_sidebar_and_menu(self):
        html = self._get(self.staff, "/dashboard/", preview=False)
        for cls in OLD_GROUPS:
            self.assertIn(cls, html)
        for cls in NEW_GROUPS + ("yse-nav-staff", "yse-dashboard-tour", "yse-glossary-help"):
            self.assertNotIn(cls, html)
        self.assertIn('class="dropdown-item" href="/jobs/">Background jobs</a>', html)
        self.assertNotIn('href="/glossary/"', html)

    def test_preview_on_member_sees_task_groups_without_staff(self):
        html = self._get(self.member, "/dashboard/", preview=True)
        for cls in NEW_GROUPS:
            self.assertIn(cls, html)
        for cls in OLD_GROUPS:
            self.assertNotIn(cls, html)
        self.assertNotIn('class="nav-item yse-nav-staff', html)
        self.assertNotIn('href="/jobs/"', html)
        self.assertNotIn('href="/feeds/"', html)
        self.assertNotIn('href="/service_runs/"', html)
        self.assertNotIn('href="/allocations/"', html)
        # New entries for pages that already exist.
        for href in ('href="/search/"', 'href="/my/favorites/"', 'href="/data_access/"', 'href="/glossary/"'):
            self.assertIn(href, html)
        # Acknowledgements: sidebar and footer.
        self.assertGreaterEqual(html.count('href="/acknowledgements/"'), 2)
        # Personal items stay in the user menu (the favorites/DAR scripts read these ids).
        self.assertIn('id="yse-fav-menu-link"', html)
        self.assertIn('id="yse-dar-menu-link"', html)

    def test_preview_on_staff_group_and_menu(self):
        html = self._get(self.staff, "/dashboard/", preview=True)
        self.assertIn('class="nav-item yse-nav-staff', html)
        staff_block = html[html.index('class="nav-item yse-nav-staff'):]
        for href in ('href="/admin/"', 'href="/jobs/"', 'href="/feeds/"',
                     'href="/service_runs/"', 'href="/allocations/"'):
            self.assertIn(href, staff_block)
        # Staff pages left the user menu.
        self.assertNotIn('class="dropdown-item" href="/jobs/"', html)
        # Collapsed unless the current page is a staff page.
        self.assertNotIn("yse-nav-staff menu-open", html)
        jobs_html = self._get(self.staff, "/jobs/", preview=True)
        self.assertIn("yse-nav-staff menu-open", jobs_html)

    def test_yse_group_sees_planning(self):
        group, _ = Group.objects.get_or_create(name="YSE")
        self.member.groups.add(group)
        html = self._get(self.member, "/dashboard/", preview=True)
        self.assertIn("yse-nav-yse", html)
        self.assertIn('href="/yse_planning/"', html)
        self.member.groups.remove(group)
        html = self._get(self.member, "/dashboard/", preview=True)
        self.assertNotIn('href="/yse_planning/"', html)

    def test_anonymous_never_gets_preview_and_keeps_acknowledgements(self):
        html = self._get(None, "/acknowledgements/", preview=True)
        self.assertIn("yse-nav-docs", html)
        self.assertNotIn("yse-nav-find", html)
        self.assertGreaterEqual(html.count('href="/acknowledgements/"'), 2)

    def test_links_keep_script_prefix(self):
        # Under mod_wsgi the stack's mount point becomes the script prefix; the test
        # client does not set it, so set it as WSGIHandler would.
        self.addCleanup(set_script_prefix, get_script_prefix())
        set_script_prefix("/yse_test/")
        html = self._get(self.staff, "/dashboard/", preview=True)
        for href in ('href="/yse_test/search/"', 'href="/yse_test/data_access/"',
                     'href="/yse_test/glossary/"', 'href="/yse_test/acknowledgements/"',
                     'href="/yse_test/jobs/"'):
            self.assertIn(href, html)
        self.assertNotIn('href="/glossary/"', html)


class DashboardTourTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("tour_user", is_staff=False)

    def _html(self, path, preview):
        client = Client()
        client.force_login(self.user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client.get(path).content.decode("utf-8", errors="replace")

    def test_tour_only_on_dashboard_with_preview(self):
        html = self._html("/dashboard/", preview=True)
        self.assertIn('id="yse-dashboard-tour"', html)
        self.assertIn('"yse-tour-dashboard"', html)  # localStorage key
        self.assertIn("hidden", html[html.index('id="yse-dashboard-tour"'):][:200])
        self.assertNotIn('id="yse-dashboard-tour"', self._html("/dashboard/", preview=False))
        self.assertNotIn('id="yse-dashboard-tour"', self._html("/glossary/", preview=True))


class GlossaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("glossary_user", is_staff=False)

    def test_page_lists_every_term(self):
        client = Client()
        client.force_login(self.user)
        response = client.get("/glossary/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        for anchor in ("status", "real-bogus", "quality-flags", "mjd", "magnitudes", "phase", "bazin-salt"):
            self.assertIn('id="%s"' % anchor, html)
        # Definitions come from the code.
        self.assertIn("Follow-up requested", html)
        self.assertIn("Someone asked for follow-up observations.", html)
        self.assertIn("<code>51</code> REJECTED", html)
        self.assertIn("<code>23</code> BAD_SUBTRACTION", html)

    def test_anonymous_redirected_to_login(self):
        response = Client().get("/glossary/")
        self.assertEqual(response.status_code, 302)


class GlossaryHelpTagTests(TestCase):
    def _render(self, src, **ctx):
        return Template("{% load glossary_extras %}" + src).render(Context(ctx))

    def test_only_with_preview_and_known_terms(self):
        self.assertEqual(self._render("{% glossary_help 'mjd' %}", ui_preview=False), "")
        self.assertEqual(self._render("{% glossary_help 'mjd' %}", user=AnonymousUser()), "")
        self.assertEqual(self._render("{% glossary_help 'nope' %}", ui_preview=True), "")
        out = self._render("{% glossary_help 'mjd' 'MJD' %}", ui_preview=True)
        self.assertIn('href="/glossary/#mjd"', out)
        self.assertIn('title="What does MJD mean?"', out)

    def test_search_field_mapping(self):
        out = self._render("{% glossary_help_for_field 'status' 'Status' %}", ui_preview=True)
        self.assertIn('href="/glossary/#status"', out)
        self.assertEqual(self._render("{% glossary_help_for_field 'ra' 'RA' %}", ui_preview=True), "")


class KeyPageHelpLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("help_link_user", is_staff=False)

    def _html(self, path, preview):
        client = Client()
        client.force_login(self.user)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        response = client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.content.decode("utf-8", errors="replace")

    def test_candidates_and_search(self):
        for path, anchor in (("/candidates/", "#real-bogus"), ("/search/", "#status")):
            self.assertIn('href="/glossary/%s"' % anchor, self._html(path, preview=True))
            self.assertNotIn("yse-glossary-help", self._html(path, preview=False))
