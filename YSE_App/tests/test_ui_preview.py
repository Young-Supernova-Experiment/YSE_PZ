"""Per-user UI preview switch (#947). Synthetic users and transients only."""

import base64
import copy
import re

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import get_script_prefix, reverse, set_script_prefix

from YSE_App.common.ui_preview import PREVIEW_COOKIE, ui_preview_enabled
from YSE_App.context_processors import ui_preview as ui_preview_context
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

MENU_PARTIAL = "YSE_App/partials/ui_preview_menu.html"
BANNER_CLASS = "yse-ui-preview-banner"
_CSRF_RE = re.compile(r'(name="csrfmiddlewaretoken" value=")[^"]*(")')
_MENU_RE = re.compile(r'<form method="post" action="[^"]*" class="m-0" id="yse-ui-preview-form">.*?</form>\n?')


def _normalise(html):
    # CSRF form tokens are masked differently on every render.
    return _CSRF_RE.sub(r"\1x\2", html)


def _templates_without_preview():
    """TEMPLATES as they were before #947: no ui_preview context processor and an empty
    menu partial, so base.html renders exactly the pre-#947 markup."""
    templates = copy.deepcopy(settings.TEMPLATES)
    for engine in templates:
        options = engine.setdefault("OPTIONS", {})
        options["context_processors"] = [
            cp for cp in options.get("context_processors", []) if cp != "YSE_App.context_processors.ui_preview"
        ]
        engine["APP_DIRS"] = False
        options["loaders"] = [
            ("django.template.loaders.locmem.Loader", {MENU_PARTIAL: ""}),
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ]
    return templates


@override_settings(ONBOARDING_GATE_ENABLED=False, UI_PREVIEW_DEFAULT=False)
class UIPreviewSwitchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("uipreview_user")
        cls.transient = create_minimal_transient(cls.user, name="uipreview-sn")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.switch_url = reverse("ui_preview_switch")
        self.dashboard_url = reverse("dashboard")
        self.transient_url = f"/transient_detail/{self.transient.slug}/"

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode("utf-8")

    # ----- helper -----

    def _request(self, user, cookie=None, auth=None):
        request = RequestFactory().get("/dashboard/")
        request.user = user
        if cookie is not None:
            request.COOKIES[PREVIEW_COOKIE] = cookie
        if auth:
            request.META["HTTP_AUTHORIZATION"] = auth
        return request

    def test_helper_cookie_on_off(self):
        self.assertTrue(ui_preview_enabled(self._request(self.user, cookie="1")))
        self.assertFalse(ui_preview_enabled(self._request(self.user, cookie="0")))
        self.assertFalse(ui_preview_enabled(self._request(self.user, cookie="")))
        self.assertFalse(ui_preview_enabled(self._request(self.user)))
        self.assertFalse(ui_preview_enabled(None))

    def test_helper_anonymous_never(self):
        self.assertFalse(ui_preview_enabled(self._request(AnonymousUser(), cookie="1")))
        with override_settings(UI_PREVIEW_DEFAULT=True):
            self.assertFalse(ui_preview_enabled(self._request(AnonymousUser(), cookie="1")))

    def test_helper_basic_and_token_auth_never(self):
        basic = "Basic " + base64.b64encode(b"synthetic:synthetic").decode("ascii")
        self.assertFalse(ui_preview_enabled(self._request(self.user, cookie="1", auth=basic)))
        self.assertFalse(ui_preview_enabled(self._request(self.user, cookie="1", auth="Token abc123")))
        with override_settings(UI_PREVIEW_DEFAULT=True):
            self.assertFalse(ui_preview_enabled(self._request(self.user, auth=basic)))

    def test_helper_site_default(self):
        with override_settings(UI_PREVIEW_DEFAULT=True):
            self.assertTrue(ui_preview_enabled(self._request(self.user)))

    # ----- view and pages -----

    def test_switch_on_then_off(self):
        response = self.client.post(self.switch_url, {"on": "1", "next": self.transient_url})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.transient_url)
        cookie = response.cookies[PREVIEW_COOKIE]
        self.assertEqual(cookie.value, "1")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(int(cookie["max-age"]), 180 * 24 * 60 * 60)
        self.assertFalse(cookie["secure"])

        for url in (self.dashboard_url, self.transient_url):
            response, html = self._html(url)
            self.assertIs(response.context["ui_preview"], True)
            self.assertIn(BANNER_CLASS, html)
            self.assertIn("You're trying the new layout.", html)
            self.assertIn("Back to the current layout", html)
            self.assertNotIn("Try the new layout (preview)", html)

        response = self.client.post(self.switch_url, {"on": "0", "next": self.dashboard_url})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[PREVIEW_COOKIE].value, "")
        self.assertEqual(int(response.cookies[PREVIEW_COOKIE]["max-age"]), 0)
        response, html = self._html(self.dashboard_url)
        self.assertIs(response.context["ui_preview"], False)
        self.assertNotIn(BANNER_CLASS, html)
        self.assertIn("Try the new layout (preview)", html)

    def test_secure_cookie_on_https(self):
        response = self.client.post(self.switch_url, {"on": "1", "next": "/dashboard/"}, secure=True)
        self.assertTrue(response.cookies[PREVIEW_COOKIE]["secure"])

    def test_switch_requires_post(self):
        response = self.client.get(self.switch_url)
        self.assertEqual(response.status_code, 405)

    def test_switch_anonymous_sets_nothing(self):
        response = Client().post(self.switch_url, {"on": "1", "next": "/dashboard/"})
        self.assertIn(response.status_code, (302, 403))
        self.assertNotIn(PREVIEW_COOKIE, response.cookies)

    def test_context_processor(self):
        basic = "Basic " + base64.b64encode(b"synthetic:synthetic").decode("ascii")
        self.assertEqual(ui_preview_context(self._request(self.user, cookie="1")), {"ui_preview": True})
        self.assertEqual(ui_preview_context(self._request(self.user)), {"ui_preview": False})
        self.assertEqual(ui_preview_context(self._request(AnonymousUser(), cookie="1")), {"ui_preview": False})
        self.assertEqual(ui_preview_context(self._request(self.user, cookie="1", auth=basic)), {"ui_preview": False})

    def test_next_must_be_same_host(self):
        for unsafe in ("https://elsewhere.example/", "//elsewhere.example/x", "javascript:alert(1)", ""):
            response = self.client.post(self.switch_url, {"on": "1", "next": unsafe})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], self.dashboard_url, unsafe)

    def test_cookie_path_is_stack_prefix(self):
        self.addCleanup(set_script_prefix, get_script_prefix())
        with override_settings(FORCE_SCRIPT_NAME="/yse_test"):
            response = self.client.post("/ui-preview/", {"on": "1", "next": "/yse_test/dashboard/"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], "/yse_test/dashboard/")
            self.assertEqual(response.cookies[PREVIEW_COOKIE]["path"], "/yse_test/")
            response = self.client.post("/ui-preview/", {"on": "0", "next": "/yse_test/dashboard/"})
            self.assertEqual(response.cookies[PREVIEW_COOKIE]["path"], "/yse_test/")

    # ----- preview off: pages unchanged -----

    def test_preview_off_pages_match_pre_preview_markup(self):
        """With the preview off, the dashboard and a transient page are byte-identical to
        the pre-#947 render apart from the one new user-menu entry."""
        for url in (self.dashboard_url, self.transient_url):
            _, current = self._html(url)
            with override_settings(TEMPLATES=_templates_without_preview()):
                _, before = self._html(url)
            current, before = _normalise(current), _normalise(before)
            self.assertNotIn("yse-ui-preview-form", before)
            self.assertEqual(len(_MENU_RE.findall(current)), 1, url)
            self.assertIn("Try the new layout (preview)", _MENU_RE.search(current).group(0))
            self.assertNotIn(BANNER_CLASS, current)
            self.assertEqual(_MENU_RE.sub("", current, count=1), before, url)
