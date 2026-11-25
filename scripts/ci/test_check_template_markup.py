"""Unit tests for check_template_markup.py (synthetic templates, plus the repo's own).

Run: python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_template_markup as chk

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TemplateMarkupTests(unittest.TestCase):
    def errors(self, src):
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
            fh.write(textwrap.dedent(src))
        try:
            return chk.check_file(fh.name)
        finally:
            os.unlink(fh.name)

    def assertClean(self, src):
        self.assertEqual(self.errors(src), [])

    def assertFlagged(self, src, fragment):
        errs = self.errors(src)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_balanced_template_passes(self):
        self.assertClean("""
            <div class="row"><div class="col-6"><p>Hi <a href="#">x</a></p></div></div>
            <ul>{% for x in xs %}<li>{{ x }}</li>{% endfor %}</ul>
            <img src="a.png"><br><input type="text"/>
        """)

    def test_branch_that_skips_a_closer_is_flagged(self):
        # The #1039 case: the else branch closes the row, the if branch does not.
        self.assertFlagged("""
            <div class="tab-pane">
              <div class="row">
              {% if not host %}
                <div class="col-12">No host</div>
              {% else %}
                <div class="col-4">{{ host }}</div>
              </div>
              <div class="row">
                <div class="col-4">near</div>
              </div>
              {% endif %}
            </div>
        """, "left open: <div>")

    def test_matching_branches_pass(self):
        self.assertClean("""
            <div class="row">
            {% if a %}<div>a</div>{% elif b %}<span>b</span>{% else %}<p>c</p>{% endif %}
            </div>
        """)

    def test_paired_conditions_follow_the_same_branch(self):
        self.assertClean("""
            {% if wrap %}<div class="wrap">{% endif %}
              <p>body</p>
            {% if wrap %}</div>{% endif %}
            {% if not boxed %}<section>{% else %}<div>{% endif %}
            {% if boxed %}</div>{% else %}</section>{% endif %}
        """)

    def test_mismatched_closer_is_flagged(self):
        self.assertFlagged("<div><h3>Title</div>", "</div> closes <h3>")

    def test_stray_closer_is_flagged(self):
        self.assertFlagged("<div></div></div>", "closed but not opened: </div>")

    def test_unbalanced_loop_body_is_flagged(self):
        self.assertFlagged("{% for x in xs %}<div class='box'>{% endfor %}", "{% for %} body")

    def test_child_blocks_must_balance(self):
        self.assertFlagged("""
            {% extends 'base.html' %}
            {% block content %}<section class="content"><div>x</div>{% endblock %}
        """, "{% block content %}")

    def test_scripts_comments_and_attributes_are_ignored(self):
        self.assertClean("""
            <script>var html = "<div><span>";</script>
            <!-- <div class="old"> -->
            {# <div> #}
            {% comment %}<div>{% endcomment %}
            <a title="<div>" href="{% url 'x' %}">x</a>
            <div {% if a %}class="a"{% endif %} data-x="{{ y }}">z</div>
        """)

    def test_skip_marker(self):
        self.assertClean("{# markup-check: skip fragment opened in header.html #}<div>")

    def test_repository_templates_are_balanced(self):
        roots = [os.path.join(REPO, "YSE_App", "templates")]
        problems = {}
        for root in roots:
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if f.endswith(".html"):
                        path = os.path.join(dirpath, f)
                        errs = chk.check_file(path)
                        if errs:
                            problems[os.path.relpath(path, REPO)] = errs
        self.assertEqual(problems, {}, "run scripts/ci/check_template_markup.py for details")


if __name__ == "__main__":
    unittest.main()
