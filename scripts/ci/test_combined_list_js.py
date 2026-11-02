"""Combined candidate list (#849): the remembered-sources logic of combined-list.js, run under node.

Skipped when node is not installed. No page, no data: synthetic query strings only.
"""

import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "YSE_App", "static", "YSE_App", "combined-list.js")

NODE_PROGRAM = r"""
const api = require(process.argv[1]);
const out = {
  restore: api.plan('', false, 'src=1&nights=3', 'src=1&tag=4'),
  first_visit: api.plan('', false, 'src=1&nights=3', null),
  save: api.plan('?src=1&tag=4&page=2', true, 'src=1&tag=4', 'src=1&tag=9'),
  forget: api.plan('?src=default', false, 'src=1&nights=3', 'src=1&tag=4'),
  paged_defaults: api.plan('?page=2', false, 'src=1&nights=3', 'src=1&tag=4'),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CombinedListJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["node", "-e", NODE_PROGRAM, SCRIPT], capture_output=True, text=True, timeout=60, check=True
        )
        cls.out = json.loads(result.stdout)

    def test_bare_url_reopens_the_remembered_sources(self):
        self.assertEqual(self.out["restore"], {"action": "restore", "query": "src=1&tag=4"})
        self.assertIsNone(self.out["first_visit"])

    def test_an_explicit_choice_is_remembered_without_page_state(self):
        self.assertEqual(self.out["save"], {"action": "save", "query": "src=1&tag=4"})

    def test_defaults_forget_and_paging_keeps_the_url(self):
        self.assertEqual(self.out["forget"], {"action": "forget"})
        self.assertIsNone(self.out["paged_defaults"])


if __name__ == "__main__":
    unittest.main()
