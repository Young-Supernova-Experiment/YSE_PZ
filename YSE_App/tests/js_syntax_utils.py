"""Cheap structural checks for inline <script> blocks in rendered templates.

Regression guard for the transient detail page: a stray ``});`` left in
``transient_detail.html`` was a SyntaxError that silently disabled every handler
in that block (tags, CSRF header, spectra plots, status changes). We cannot run
a JS parser in the Django test suite, so this strips comments and string
literals and checks that (), [] and {} are balanced per inline block.
"""

import re

_INLINE_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>(?:\s[^>]*)?)>(?P<body>.*?)</script>", re.S | re.I
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_STRING_RE = re.compile(r'"(?:\\.|[^"\\\n])*"' r"|'(?:\\.|[^'\\\n])*'" r"|`(?:\\.|[^`\\])*`", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_TEMPLATE_COMMENT_RE = re.compile(r"{#.*?#}", re.S)

_PAIRS = {"(": ")", "[": "]", "{": "}"}


def inline_script_bodies(html):
    """Return the bodies of all inline (non-``src``) <script> blocks."""
    bodies = []
    for match in _INLINE_SCRIPT_RE.finditer(html):
        attrs = match.group("attrs") or ""
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        if re.search(r'type\s*=\s*["\'](?!(?:text/|application/)?(?:java|ecma)script)', attrs, re.I):
            continue  # JSON / template blocks
        bodies.append(match.group("body"))
    return bodies


def strip_js_literals(js):
    """Remove comments and string literals so only structural characters remain."""
    js = _TEMPLATE_COMMENT_RE.sub("", js)
    js = _BLOCK_COMMENT_RE.sub("", js)
    out = []
    for line in js.split("\n"):
        line = _STRING_RE.sub('""', line)
        line = _LINE_COMMENT_RE.sub("", line)
        out.append(line)
    return "\n".join(out)


def bracket_imbalance(js):
    """Return a list of human-readable problems; empty when brackets balance."""
    stack = []
    problems = []
    for lineno, line in enumerate(strip_js_literals(js).split("\n"), start=1):
        for ch in line:
            if ch in _PAIRS:
                stack.append((ch, lineno))
            elif ch in _PAIRS.values():
                if stack and _PAIRS[stack[-1][0]] == ch:
                    stack.pop()
                else:
                    problems.append(f"unexpected '{ch}' at script line {lineno}")
    for ch, lineno in stack:
        problems.append(f"unclosed '{ch}' opened at script line {lineno}")
    return problems


def html_script_problems(html):
    """Bracket problems across every inline <script> block, prefixed by block index."""
    problems = []
    for idx, body in enumerate(inline_script_bodies(html)):
        for problem in bracket_imbalance(body):
            problems.append(f"script block {idx}: {problem}")
    return problems
