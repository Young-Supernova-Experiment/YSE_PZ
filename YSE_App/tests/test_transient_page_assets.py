"""Every fragment, AJAX endpoint and static asset on the transient detail page must load.

The detail page returns 200 long before its tabs are filled: the Follow-up,
Resources, Photometry and Spectra tabs, the comments box and the Summary
spectrum toolbar are fetched afterwards from fragment endpoints, and the
page pulls scripts and stylesheets from STATIC_URL. A broken fragment (a
template error, a NoReverseMatch, a view exception) leaves the tab empty
while the page itself still passes a plain status check, which is how the
Follow-up tab could stop loading on yse_experimental without CI noticing.

These tests render the page in both modes (deferred fragments and inline),
harvest every same-origin URL the page references, and request each one
as a logged-in user: fragment endpoints must return 200, static assets must
be findable by the staticfiles finders, and the containers the deferred
loader writes into must exist in the page.
"""

import os
import re
from datetime import timedelta
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from YSE_App.models import (
    ClassicalResource,
    FollowupStatus,
    ToOResource,
    TransientFollowup,
    TransientTag,
)
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_minimal_transient,
    create_test_user,
    create_transient_with_synthetic_data,
)

# Views that fetch from MAST, Chandra, PS1, the Legacy Survey or SDSS over
# the network; the CI runner has no route to them and their failure is not
# a page regression.
EXTERNAL_ENDPOINTS = frozenset(
    {
        "get_hst_status",
        "get_chandra_status",
        "get_hst_image",
        "get_chandra_image",
        "get_ps1_image",
        "get_legacy_image",
        "finderchart",
    }
)

FRAGMENT_NAMES = (
    "transient_detail_followup_fragment",
    "transient_detail_followup_classical_fragment",
    "transient_detail_followup_rest_fragment",
    "transient_detail_comments_fragment",
    "transient_detail_gw_fragment",
    "transient_detail_spectra_tab_fragment",
    "transient_detail_summary_spectra_tools_fragment",
    "transient_detail_resources_fragment",
    "transient_detail_photometry_fragment",
)

# Assets the browser fetches while rendering the page.
_ASSET_TAG_RE = re.compile(
    r"""<(?:script|link|img|iframe)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# Fragment / AJAX targets declared on elements.
_DATA_URL_RE = re.compile(r"""\bdata-[a-z-]*(?:url|src|href)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
# GET-style loads issued from inline scripts: $.get("..."), $.getJSON("..."), $el.load("...").
_JS_GET_RE = re.compile(r"""(?:\$\.get|\$\.getJSON|\.load)\(\s*["'](/[^"']+)["']""")
# yseLoadDetailFragment(key, "url", '#container') calls of the deferred shell.
_DEFERRED_LOADER_RE = re.compile(
    r"""yseLoadDetailFragment\(\s*['"][^'"]+['"]\s*,\s*["']([^"']+)["']\s*,\s*['"]#([^'"]+)['"]"""
)
# Any same-origin path literal, used only to find fragment routes wherever they appear.
_ANY_PATH_RE = re.compile(r"""["'](/[A-Za-z0-9_./-]+/?)["']""")
# {% url 'name' -1 -2 %} placeholders that scripts .replace() with real ids at runtime.
_PLACEHOLDER_SEGMENT_RE = re.compile(r"/-\d+(?=/|$|\?)")


def _same_origin_path(url):
    """Return the path (+query) for a same-origin URL, or None for anything else."""
    url = url.strip()
    if not url or url.startswith(("#", "javascript:", "mailto:", "data:", "{")):
        return None
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def harvest_urls(html):
    """Same-origin URLs the page loads, as (static assets, endpoints).

    Only what the browser fetches is harvested: script/link/img/iframe
    sources, data-*url attributes, GET-style loads and the deferred
    loader calls in inline scripts, plus every fragment route the page
    mentions. Links and form actions are deliberately left out: GETting
    them would follow navigation and, for Delete links, mutate data.
    """
    candidates = set(_ASSET_TAG_RE.findall(html))
    candidates.update(_DATA_URL_RE.findall(html))
    for script in _INLINE_SCRIPT_RE.findall(html):
        candidates.update(_JS_GET_RE.findall(script))
        candidates.update(url for url, _container in _DEFERRED_LOADER_RE.findall(script))
        for path in _ANY_PATH_RE.findall(script):
            name = resolve_name(path)
            if name and "fragment" in name:
                candidates.add(path)
    static, endpoints = set(), set()
    for url in candidates:
        path = _same_origin_path(url)
        if path is None or _PLACEHOLDER_SEGMENT_RE.search(path):
            continue
        if path.startswith(settings.STATIC_URL):
            static.add(path)
        else:
            endpoints.add(path)
    return static, endpoints


def resolve_name(path):
    """URL name for a same-origin path, or None when it is not a route."""
    try:
        return resolve(urlsplit(path).path).url_name
    except Resolver404:
        return None


class _RaiseOnMissingVariable(str):
    """string_if_invalid that turns a missing template variable into a failure."""

    def __mod__(self, variable):
        raise AssertionError(f"template references a missing variable: {variable}")


def _strict_templates():
    strict = []
    for engine in settings.TEMPLATES:
        options = dict(engine.get("OPTIONS", {}))
        options["string_if_invalid"] = _RaiseOnMissingVariable("%s")
        strict.append({**engine, "OPTIONS": options})
    return strict


class TransientPageAssetsTests(TestCase):
    """A transient with photometry, spectra, host, comments, tags and follow-ups."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("page_assets_user", is_staff=False)
        cls.other = create_test_user("page_assets_other", is_staff=False)
        cls.staff = create_test_user("page_assets_staff", is_staff=True)
        cls.transient = create_transient_with_synthetic_data(
            cls.user, name="2026assets", n_phot_points=6
        )
        cls.empty_transient = create_minimal_transient(cls.user, name="2026assetsempty")
        audit = audit_fields(cls.user)
        tag = TransientTag.objects.create(name="page-assets-tag", **audit)
        cls.transient.tags.add(tag)

        requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)
        now = timezone.now()
        telescope = cls.transient.transientphotometry_set.first().instrument.telescope
        resource_kwargs = dict(
            telescope=telescope,
            begin_date_valid=now - timedelta(days=1),
            end_date_valid=now + timedelta(days=7),
            creator_only=True,
            **audit,
        )
        classical = ClassicalResource.objects.create(**resource_kwargs)
        too = ToOResource.objects.create(**resource_kwargs)
        window = dict(status=requested, valid_start=now, valid_stop=now + timedelta(days=5))
        # A parent that predates per-request rows: no children at all.
        cls.legacy_followup = TransientFollowup.objects.create(
            transient=cls.transient, too_resource=too, priority=3.0, is_public=True, **window, **audit
        )
        # Two people request the same classical night with their own comments.
        cls.shared_parent, _, _ = create_or_attach_request(
            cls.user, cls.transient, priority=2.0, comment="first requester",
            classical_resource=classical, **window
        )
        create_or_attach_request(
            cls.other, cls.transient, priority=1.0, comment="second requester",
            classical_resource=classical, **window
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.detail_url = reverse("transient_detail", kwargs={"slug": self.transient.slug})

    def _detail_html(self, defer):
        env = {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}
        with mock.patch.dict(os.environ, env):
            response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200, f"detail page defer={defer}")
        return response.content.decode("utf-8", errors="replace")

    def _assert_endpoint_loads(self, path, label):
        name = resolve_name(path)
        if name is None or name in EXTERNAL_ENDPOINTS:
            return
        response = self.client.get(path)
        status = response.status_code
        if 300 <= status < 400:
            target = response.get("Location", "")
            self.assertNotIn(
                reverse("login"), target, f"{label}: {path} ({name}) redirected to login"
            )
            return
        # 405 means a POST-only action was harvested from a form or a $.ajax call.
        self.assertIn(status, (200, 405), f"{label}: {path} ({name}) returned {status}")
        if "fragment" in name:
            self.assertEqual(status, 200, f"{label}: fragment {path} returned {status}")

    def _assert_static_exists(self, path):
        relative = urlsplit(path).path[len(settings.STATIC_URL):]
        collected = os.path.join(settings.STATIC_ROOT, relative)
        self.assertTrue(
            finders.find(relative) or os.path.exists(collected),
            f"static asset referenced by the detail page is missing: {path}",
        )

    def test_every_referenced_url_loads(self):
        for defer in (True, False):
            with self.subTest(defer=defer):
                html = self._detail_html(defer)
                static, endpoints = harvest_urls(html)
                self.assertTrue(static, "no static assets harvested; the harvester is broken")
                self.assertTrue(endpoints, "no endpoints harvested; the harvester is broken")
                for path in sorted(static):
                    self._assert_static_exists(path)
                for path in sorted(endpoints):
                    self._assert_endpoint_loads(path, f"defer={defer}")

    def test_deferred_loader_targets_exist_and_load(self):
        """Each yseLoadDetailFragment(key, url, '#id') call names a container in the page."""
        html = self._detail_html(defer=True)
        calls = _DEFERRED_LOADER_RE.findall(html)
        self.assertGreaterEqual(len(calls), 5, "deferred loader calls not found in the page")
        for url, container_id in calls:
            with self.subTest(container=container_id):
                self.assertIn(f'id="{container_id}"', html, f"missing container #{container_id}")
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, f"{url} -> {response.status_code}")

    def test_every_fragment_endpoint_returns_200(self):
        for transient in (self.transient, self.empty_transient):
            for name in FRAGMENT_NAMES:
                url = reverse(name, args=[transient.id])
                for user in (self.user, self.staff):
                    self.client.force_login(user)
                    with self.subTest(fragment=name, transient=transient.name, user=user.username):
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 200, url)

    def test_followup_fragment_lists_every_request(self):
        url = reverse("transient_detail_followup_fragment", args=[self.transient.id])
        for user in (self.user, self.other, self.staff):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode("utf-8", errors="replace")
                self.assertEqual(html.count('class="followupbox'), 2)
                self.assertEqual(html.count('class="followup-request-row"'), 2)
                self.assertIn("first requester", html)
                self.assertIn("second requester", html)
                self.assertIn("No individual requests recorded.", html)
                self.assertIn(reverse("delete_followup", args=[self.legacy_followup.id]), html)

    def test_inline_page_includes_followups_and_fragment_reload_url(self):
        html = self._detail_html(defer=False)
        for parent in (self.legacy_followup, self.shared_parent):
            self.assertEqual(html.count(f"Follow-up (id: {parent.id})"), 1)
        self.assertIn(
            reverse("transient_detail_followup_fragment", args=[self.transient.id]), html
        )

    @override_settings(TEMPLATES=_strict_templates())
    def test_followup_fragments_reference_no_missing_variables(self):
        """The follow-up tab templates must not depend on context the view can forget to set."""
        for name in (
            "transient_detail_followup_fragment",
            "transient_detail_followup_classical_fragment",
        ):
            for transient in (self.transient, self.empty_transient):
                for user in (self.user, self.staff):
                    self.client.force_login(user)
                    with self.subTest(fragment=name, transient=transient.name, user=user.username):
                        response = self.client.get(reverse(name, args=[transient.id]))
                        self.assertEqual(response.status_code, 200)
