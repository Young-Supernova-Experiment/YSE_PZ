"""Inline scripts read the CSRF cookie by its configured name (per-stack CSRF_COOKIE_NAME, #637)."""

import re
from pathlib import Path

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from YSE_App.context_processors import csrf_cookie_name
from YSE_App.tests.fixtures_minimal import create_test_user

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
HARD_CODED = re.compile(r"""(getCookie|Cookies\.get|\$\.cookie)\(\s*['"]csrftoken['"]\s*\)|/[^/\n]*csrftoken=""")


class NoHardCodedCsrfCookieTests(SimpleTestCase):
    def test_templates_never_hard_code_the_cookie_name(self):
        offenders = []
        for path in TEMPLATES.rglob("*.html"):
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if HARD_CODED.search(line):
                    offenders.append("%s:%d" % (path.relative_to(TEMPLATES), n))
        self.assertEqual(offenders, [])

    def test_context_processor(self):
        request = RequestFactory().get("/")
        with override_settings(CSRF_COOKIE_NAME="yse_test_csrftoken"):
            self.assertEqual(csrf_cookie_name(request), {"csrf_cookie_name": "yse_test_csrftoken"})
        self.assertEqual(csrf_cookie_name(request), {"csrf_cookie_name": "csrftoken"})


class CsrfCookieNameRenderTests(TestCase):
    @override_settings(CSRF_COOKIE_NAME="yse_exp_csrftoken")
    def test_page_script_uses_the_configured_name(self):
        self.client.force_login(create_test_user("csrf_cookie_name_user"))
        html = self.client.get("/dashboard/").content.decode()
        self.assertIn("getCookie('yse_exp_csrftoken')", html)
        self.assertNotIn("getCookie('csrftoken')", html)
