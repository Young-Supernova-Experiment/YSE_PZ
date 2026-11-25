"""Every tab pane on the transient page must be a direct child of .tab-content.

Regression (develop, 2026-11-25): the Summary tab's "No host galaxy" branch left a
<div class="row"> open, so for host-less transients every later .tab-pane nested
inside Summary. Bootstrap's `.tab-content > .tab-pane {display: none}` then missed
them: all tabs showed at once and clicking a tab did nothing visible.
"""

import os
from html.parser import HTMLParser
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    create_minimal_transient,
    create_test_user,
)

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}


class _TabPaneParents(HTMLParser):
    """Record each .tab-pane id with the classes of its parent element."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.panes = {}

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if "tab-pane" in classes:
            parent = self.stack[-1][1] if self.stack else []
            self.panes[attrs.get("id")] = parent
        self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        # Pop to the matching open tag (tolerates unclosed <p>/<li> etc.).
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


class TransientDetailTabStructureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("tab_structure_user")
        cls.no_host = create_minimal_transient(cls.user, name="2026tabsnohost")
        cls.with_host = create_minimal_transient(cls.user, name="2026tabshost")
        attach_synthetic_host(cls.user, cls.with_host)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _panes(self, transient, defer):
        url = reverse("transient_detail", kwargs={"slug": transient.slug})
        with mock.patch.dict(os.environ, {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        parser = _TabPaneParents()
        parser.feed(response.content.decode("utf-8", errors="replace"))
        return parser.panes

    def test_tab_panes_are_direct_children_of_tab_content(self):
        for transient in (self.no_host, self.with_host):
            for defer in (True, False):
                with self.subTest(transient=transient.name, defer=defer):
                    panes = self._panes(transient, defer)
                    self.assertIn("summary_tab", panes)
                    self.assertIn("history_tab", panes)
                    nested = [pid for pid, parent in panes.items() if "tab-content" not in parent]
                    self.assertEqual(nested, [], "tab panes not directly inside .tab-content")
