#!/usr/bin/env python3
"""Check Django templates for unbalanced or mis-nested HTML tags on every branch.

A template can be well formed on the path a developer looked at and broken on
another: an ``{% if %}`` branch that opens a ``<div>`` the other branch closes,
or an empty-state branch that forgets a closer.  Browsers repair the markup by
nesting everything after it one level deeper.  On the transient page that put
every tab pane inside Summary, so all tabs showed at once (#1039).

The checker follows every combination of ``{% if %}``/``{% elif %}``/``{% else %}``
branches (an ``{% if %}`` that repeats an earlier condition takes the same branch,
so paired open/close wrappers are fine) and reports:

* a closing tag that does not match the innermost open tag;
* tags left open, or closed without being opened, at the end of a file,
  a ``{% block %}`` or a ``{% for %}`` body.

Each template, include and block must be balanced on its own.  ``<script>``,
``<style>``, comments and ``{% verbatim %}`` are skipped.  Void tags and tags that
are routinely left unclosed (``option``, ``dt``, ``dd``, ``colgroup``) are not
checked; ``p`` and ``li`` are, so write their end tags.

Usage: check_template_markup.py [TEMPLATE_DIR ...]   (default: every templates/ dir)
Exit status 1 when a problem is found.  Opt a file out with a line
``{# markup-check: skip <reason> #}``.
"""

import os
import re
import sys

CHECKED = {
    "a", "abbr", "article", "aside", "b", "blockquote", "button", "canvas", "caption",
    "center", "code", "details", "dialog", "div", "dl", "em", "fieldset", "figure",
    "footer", "font", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "i",
    "iframe", "label", "main", "nav", "ol", "pre", "section", "select", "small",
    "span", "strong", "sub", "summary", "sup", "table", "td", "textarea", "th",
    "thead", "tbody", "tfoot", "time", "tr", "u", "ul", "li", "p",
}
MAX_PATHS = 512

STRIP_RES = [
    re.compile(r"\{%\s*comment\b.*?%\}.*?\{%\s*endcomment\s*%\}", re.S),
    re.compile(r"\{%\s*verbatim\s*%\}.*?\{%\s*endverbatim\s*%\}", re.S),
    re.compile(r"\{#.*?#\}", re.S),
    re.compile(r"<!--.*?-->", re.S),
    re.compile(r"<script\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>.*?</script\s*>", re.S | re.I),
    re.compile(r"<style\b[^>]*>.*?</style\s*>", re.S | re.I),
]
TOKEN_RE = re.compile(
    r"\{%-?\s*(?P<dj>.*?)\s*-?%\}"
    r"|<(?P<close>/)?(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?P<attrs>(?:[^>\"'{]|\"[^\"]*\"|'[^']*'|\{\{.*?\}\}|\{%.*?%\}|\{)*?)(?P<self>/)?>",
    re.S,
)


def _blank(m):
    return re.sub(r"[^\n]", " ", m.group(0))


class Node:
    pass


class Tag(Node):
    def __init__(self, name, close, line):
        self.name, self.close, self.line = name, close, line


class If(Node):
    def __init__(self, line):
        self.line = line
        self.branches = []  # list of (condition key or None, negated, seq)


class For(Node):
    def __init__(self, line):
        self.line, self.body, self.empty = line, [], []


class Block(Node):
    def __init__(self, name, line):
        self.name, self.line, self.body = name, line, []


def parse(text):
    """Return (top-level seq, extends?) for a template source."""
    for rx in STRIP_RES:
        text = rx.sub(_blank, text)
    root = []
    stack = [("root", root, None)]
    extends = False

    def cur():
        return stack[-1][1]

    for m in TOKEN_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        dj = m.group("dj")
        if dj is not None:
            word = dj.split(None, 1)[0] if dj.split() else ""
            rest = dj[len(word):].strip()
            if word == "extends":
                extends = True
            elif word in ("if", "ifchanged", "ifequal", "ifnotequal"):
                node = If(line)
                key, neg = _cond(rest) if word == "if" else (None, False)
                seq = []
                node.branches.append((key, neg, seq))
                cur().append(node)
                stack.append(("if", seq, node))
            elif word in ("elif", "else", "empty"):
                kind, _, node = stack[-1]
                if kind == "if":
                    seq = []
                    key, neg = _cond(rest) if word == "elif" else (None, False)
                    node.branches.append((key, neg, seq) if word == "elif" else ("__else__", False, seq))
                    stack[-1] = ("if", seq, node)
                elif kind == "for" and word == "empty":
                    stack[-1] = ("for", node.empty, node)
            elif word in ("endif", "endifchanged", "endifequal", "endifnotequal"):
                if stack[-1][0] == "if":
                    stack.pop()
            elif word == "for":
                node = For(line)
                cur().append(node)
                stack.append(("for", node.body, node))
            elif word == "endfor":
                if stack[-1][0] == "for":
                    stack.pop()
            elif word == "block":
                node = Block(rest.split()[0] if rest else "?", line)
                cur().append(node)
                stack.append(("block", node.body, node))
            elif word == "endblock":
                if stack[-1][0] == "block":
                    stack.pop()
            continue
        name = m.group("tag").lower()
        if name not in CHECKED or m.group("self"):
            continue
        cur().append(Tag(name, bool(m.group("close")), line))
    return root, extends


def _cond(rest):
    """Normalise a simple condition so `if x` / `if not x` pair up."""
    c = " ".join(rest.split())
    if c.startswith("not ") and " and " not in c and " or " not in c:
        return c[4:], True
    return c, False


# An effect is (closes, opens): tuples of (tag, line) left unmatched.
EMPTY = ((), ())


def compose(a, b, errors, where):
    closes, opens = list(a[0]), list(a[1])
    for tag, line in b[0]:
        if opens:
            otag, oline = opens[-1]
            if otag == tag:
                opens.pop()
            else:
                errors.add(f"{where}line {line}: </{tag}> closes <{otag}> opened on line {oline}")
                # Recover the way a browser would: close up to a matching open tag, if any.
                for i in range(len(opens) - 1, -1, -1):
                    if opens[i][0] == tag:
                        del opens[i:]
                        break
        else:
            closes.append((tag, line))
    opens.extend(b[1])
    return (tuple(closes), tuple(opens))


def tag_effect(t):
    return (((t.name, t.line),), ()) if t.close else ((), ((t.name, t.line),))


class Ctx:
    """Per-file state: conditions worth tracking, units still to check, findings."""

    def __init__(self, shared):
        self.shared = shared  # condition keys used by more than one {% if %}
        self.errors = set()
        self.units = []
        self._unit_ids = set()
        self.hints = {}  # If node line -> description, for branches that disagree

    def add_unit(self, label, seq):
        if id(seq) not in self._unit_ids:
            self._unit_ids.add(id(seq))
            self.units.append((label, seq))


def run(seq, states, ctx):
    """Follow every branch combination. states: set of (env, effect)."""
    for node in seq:
        if isinstance(node, Tag):
            eff = tag_effect(node)
            states = {(env, compose(e, eff, ctx.errors, "")) for env, e in states}
        elif isinstance(node, If):
            new = set()
            for env, e in states:
                envd = dict(env)
                taken = False
                for key, neg, bseq in node.branches:
                    if key == "__else__":
                        new |= run(bseq, {(env, e)}, ctx)
                        taken = True
                        break
                    tracked = key is not None and key in ctx.shared
                    if tracked and key in envd:
                        if envd[key] != neg:
                            new |= run(bseq, {(env, e)}, ctx)
                            taken = True
                            break
                        continue
                    env_t = frozenset(env | {(key, not neg)}) if tracked else env
                    new |= run(bseq, {(env_t, e)}, ctx)
                    if tracked:
                        env = frozenset(env | {(key, neg)})
                        envd[key] = neg
                if not taken:
                    new.add((env, e))
            states = new
            _branch_hint(node, ctx)
        elif isinstance(node, For):
            ctx.add_unit(f"{{% for %}} body on line {node.line}", node.body)
            if node.empty:
                ctx.add_unit(f"{{% empty %}} on line {node.line}", node.empty)
        elif isinstance(node, Block):
            ctx.add_unit(f"{{% block {node.name} %}} on line {node.line}", node.body)
        if len(states) > MAX_PATHS:
            states = set(sorted(states, key=repr)[:MAX_PATHS])
    return states


def _net(seq):
    """Tag-count change of a branch on its own (ignores nested branches' choice)."""
    net = {}
    for node in seq:
        if isinstance(node, Tag):
            net[node.name] = net.get(node.name, 0) + (-1 if node.close else 1)
        elif isinstance(node, If):
            first = node.branches[0][2]
            for k, v in _net(first).items():
                net[k] = net.get(k, 0) + v
    return {k: v for k, v in net.items() if v}


def _branch_hint(node, ctx):
    if node.line in ctx.hints:
        return
    nets = [_net(b[2]) for b in node.branches]
    if len(node.branches) == 1 or node.branches[-1][0] != "__else__":
        nets.append({})  # the "no branch taken" path
    if any(n != nets[0] for n in nets):
        ctx.hints[node.line] = nets


def describe(effect):
    parts = []
    if effect[1]:
        parts.append("left open: " + ", ".join(f"<{t}> (line {l})" for t, l in effect[1]))
    if effect[0]:
        parts.append("closed but not opened: " + ", ".join(f"</{t}> (line {l})" for t, l in effect[0]))
    return "; ".join(parts)


def _shared_conditions(seq, counts):
    for node in seq:
        if isinstance(node, If):
            for key, _, bseq in node.branches:
                if key not in (None, "__else__"):
                    counts[key] = counts.get(key, 0) + 1
                _shared_conditions(bseq, counts)
        elif isinstance(node, For):
            _shared_conditions(node.body, counts)
            _shared_conditions(node.empty, counts)
        elif isinstance(node, Block):
            _shared_conditions(node.body, counts)
    return counts


def check_file(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    if "markup-check: skip" in src:
        return []
    root, extends = parse(src)
    counts = _shared_conditions(root, {})
    ctx = Ctx({k for k, n in counts.items() if n > 1})
    if extends:
        # Django ignores content outside blocks in a child template.
        _collect_blocks(root, ctx)
    else:
        ctx.add_unit("file", root)
    done = 0
    while done < len(ctx.units):
        label, seq = ctx.units[done]
        done += 1
        ctx.hints = {}
        final = run(seq, {(frozenset(), EMPTY)}, ctx)
        bad = sorted({describe(e) for _, e in final if e != EMPTY})
        for b in bad:
            ctx.errors.add(f"{label}: {b}")
        if bad:
            for line, nets in sorted(ctx.hints.items()):
                shown = " vs ".join(
                    ", ".join(f"{k} {v:+d}" for k, v in sorted(n.items())) or "balanced" for n in nets
                )
                ctx.errors.add(f"{label}: hint: branches of the {{% if %}} on line {line} differ ({shown})")
    return sorted(ctx.errors)


def _collect_blocks(seq, ctx):
    for node in seq:
        if isinstance(node, Block):
            ctx.add_unit(f"{{% block {node.name} %}} on line {node.line}", node.body)
        elif isinstance(node, If):
            for _, _, bseq in node.branches:
                _collect_blocks(bseq, ctx)


def main(argv):
    roots = argv[1:] or [
        os.path.join(d, "templates")
        for d in ("YSE_App", "YSE_PZ", ".")
        if os.path.isdir(os.path.join(d, "templates"))
    ]
    failed = 0
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in sorted(files):
                if not f.endswith(".html"):
                    continue
                path = os.path.join(dirpath, f)
                errs = check_file(path)
                if errs:
                    failed += 1
                    print(path)
                    for e in errs:
                        print("   ", e)
    if failed:
        print(f"\n{failed} template(s) with unbalanced or mis-nested tags.")
        return 1
    print("Template markup OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
