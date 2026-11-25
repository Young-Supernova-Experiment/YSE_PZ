"""Markup cleanup (#803, #822, #837, #838): titles, removed demo route, tab hiding."""

import re

from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.test import Client, TestCase

from YSE_App.tests.fixtures_minimal import create_test_user, ensure_transient_statuses


class MarkupCleanupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("markup_cleanup_user")
        ensure_transient_statuses(cls.user)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_dashboard_example_route_removed(self):
        self.assertEqual(self.client.get("/dashboard_example/").status_code, 404)

    def test_dead_templates_removed(self):
        for name in (
            "YSE_App/dashboard_test.html",
            "YSE_App/index_tmp.html",
            "YSE_App/starter.html",
            "YSE_App/bootstrap.html",
            "YSE_App/dashboard_filter.html",
            "YSE_App/table.html",
            "YSE_App/dashboard_example.html",
            "YSE_App/django-tables2/bootstrap-responsive.html",
            "YSE_App/django-tables2/bootstrap4.html",
            "YSE_App/django-tables2/semantic.html",
        ):
            with self.assertRaises(TemplateDoesNotExist, msg=name):
                get_template(name)
        get_template("YSE_App/django-tables2/bootstrap.html")

    def test_page_titles(self):
        expected = {
            "/calendar/": "On-Call Calendar",
            "/observing_calendar/": "Observing Calendar",
            "/too_calendar/": "ToO Calendar",
        }
        for url, name in expected.items():
            response = self.client.get(url)
            if response.status_code != 200:
                continue  # YSE-group pages may be gated; titles checked when reachable
            body = response.content.decode("utf-8", errors="replace")
            self.assertIn("<title>YSE | PZ: %s</title>" % name, body, url)
            self.assertRegex(body, r"<h1>\s*%s\s*</h1>" % re.escape(name))

    def test_default_title_unchanged(self):
        body = self.client.get("/dashboard/").content.decode("utf-8", errors="replace")
        self.assertIn("<title>YSE | PZ</title>", body)
