"""J/K list stepping and list-context links (#850): the pure parts of list-context.js, run under node.

Skipped when node is not installed. No page, no data: synthetic objects only.
"""

import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "YSE_App", "static", "YSE_App", "list-context.js")

NODE_PROGRAM = r"""
const api = require(process.argv[1]);
function el(tag, attrs, parent) {
  attrs = attrs || {};
  return {nodeType: 1, tagName: tag, parentNode: parent || null, isContentEditable: !!attrs.ce,
          getAttribute: (n) => (n in attrs ? attrs[n] : null)};
}
const body = el('BODY');
const div = el('DIV', {}, body);
const editable = el('DIV', {contenteditable: 'true', ce: true}, body);
const inEditable = el('SPAN', {}, editable);
const ev = (key, target, extra) => Object.assign({key: key, target: target}, extra || {});
const out = {
  j_body: api.stepKey(ev('j', div)),
  k_body: api.stepKey(ev('k', div)),
  J_body: api.stepKey(ev('J', div)),
  x_body: api.stepKey(ev('x', div)),
  j_input: api.stepKey(ev('j', el('INPUT', {}, body))),
  k_textarea: api.stepKey(ev('k', el('TEXTAREA', {}, body))),
  j_select: api.stepKey(ev('j', el('SELECT', {}, body))),
  j_editable: api.stepKey(ev('j', inEditable)),
  j_combobox: api.stepKey(ev('j', el('SPAN', {role: 'combobox'}, body))),
  j_ctrl: api.stepKey(ev('j', div, {ctrlKey: true})),
  j_meta: api.stepKey(ev('j', div, {metaKey: true})),
  j_prevented: api.stepKey(ev('j', div, {defaultPrevented: true})),
  href_dash: api.contextHref('/yse/transient_detail/2026abc/', 'dash', '?dash-ex=x&following-page=2', 'following'),
  href_hash: api.contextHref('/transient_detail/2026abc/#followup_tab', 'search', 'name_contains=a', ''),
  href_other: api.contextHref('/search/?page=2', 'search', '', ''),
  href_has_query: api.contextHref('/transient_detail/2026abc/?ls=tag', 'search', '', ''),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ListContextJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["node", "-e", NODE_PROGRAM, SCRIPT], capture_output=True, text=True, timeout=60, check=True
        )
        cls.out = json.loads(result.stdout)

    def test_j_is_next_and_k_is_previous(self):
        self.assertEqual(self.out["j_body"], "next")
        self.assertEqual(self.out["J_body"], "next")
        self.assertEqual(self.out["k_body"], "prev")
        self.assertIsNone(self.out["x_body"])

    def test_keys_are_ignored_while_typing(self):
        for name in ("j_input", "k_textarea", "j_select", "j_editable", "j_combobox"):
            self.assertIsNone(self.out[name], name)

    def test_keys_with_modifiers_or_already_handled_are_ignored(self):
        for name in ("j_ctrl", "j_meta", "j_prevented"):
            self.assertIsNone(self.out[name], name)

    def test_detail_links_get_the_list_context(self):
        self.assertEqual(
            self.out["href_dash"],
            "/yse/transient_detail/2026abc/?ls=dash&lq=dash-ex%3Dx%26following-page%3D2&lt=following",
        )
        self.assertEqual(
            self.out["href_hash"], "/transient_detail/2026abc/?ls=search&lq=name_contains%3Da#followup_tab"
        )
        self.assertIsNone(self.out["href_other"])
        self.assertIsNone(self.out["href_has_query"])


if __name__ == "__main__":
    unittest.main()
