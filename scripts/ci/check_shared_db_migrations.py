#!/usr/bin/env python3
"""Fail on migrations that would break older code sharing the same database.

``/yse_test/`` runs develop's code on the ``YSE_test`` database, and every
experimental deploy applies experimental's migrations to that database. develop
can stay several migrations behind for weeks, so a migration that develop's code
cannot live with breaks ``/yse_test/`` (#395: a NOT NULL column with only a
Python default made develop's INSERTs fail with IntegrityError 1364).

This script parses (with ``ast``; nothing is imported or run) every migration
file that exists in the working tree but not on the base ref (``develop``), and
reports operations that are unsafe while older code shares the database:

* ``RemoveField``, ``RenameField``, ``RenameModel``, ``DeleteModel``,
  ``AlterModelTable`` and ``AlterOrderWithRespectTo`` on a model that exists on
  the base ref;
* ``AddField`` on such a model whose column is NOT NULL and has no real MySQL
  DEFAULT (``db_default=``, or an ``ALTER ... DEFAULT`` string for that column in
  one of the new migrations, as in 0030), or that is unique;
* ``AlterField`` that makes a column NOT NULL, narrows it, changes its type,
  renames its column or makes it unique (or whose previous definition the
  script cannot find);
* unique or check constraints and ``unique_together`` on such a model;
* ``RunPython`` and ``RunSQL`` (they can rewrite existing rows);
* any operation the script does not recognise or cannot parse.

Operations on models created by the new migrations themselves are safe: the
older code never reads or writes those tables. Many-to-many fields add a table,
not a column, so they are safe too.

A reviewed operation is allowed with a comment directly above it, or on any of
its lines::

    # shared-db-safe: only fills names that are empty; develop ignores them
    migrations.RunPython(fill, migrations.RunPython.noop),

The reason after the colon is required. Allowed operations are listed in the
output so the reviewer sees them.

Usage::

    python3 scripts/ci/check_shared_db_migrations.py --base origin/develop
    python3 scripts/ci/check_shared_db_migrations.py --new YSE_App/migrations/0060_fill_account_names.py

Exit status: 0 when nothing is unsafe, 1 when something is, 2 on usage errors.
"""

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize

OPT_OUT_RE = re.compile(r"#\s*shared-db-safe\s*:\s*(\S.*?)\s*$")
OPT_OUT_HINT = "if a reviewer has checked it is safe, add '# shared-db-safe: <reason>' above the operation"

# Operations that never hurt older code on the same database.
SAFE_OPS = {
    "CreateModel", "AddIndex", "RemoveIndex", "RenameIndex", "RemoveConstraint",
    "AlterModelOptions", "AlterModelManagers", "AlterIndexTogether",
}
# Operations that are unsafe on a model the older code knows about.
UNSAFE_ON_EXISTING = {
    "RemoveField": "removes a column that older code still reads and writes",
    "RenameField": "renames a column that older code still uses by its old name",
    "RenameModel": "renames a table that older code still uses by its old name",
    "DeleteModel": "drops a table that older code still uses",
    "AlterModelTable": "renames a table that older code still uses by its old name",
    "AlterOrderWithRespectTo": "adds or drops the NOT NULL _order column",
}
FK_TYPES = {"ForeignKey", "OneToOneField"}
M2M_TYPES = {"ManyToManyField"}
UNKNOWN = object()


class Finding:
    def __init__(self, path, line, op, target, message, reason=None):
        self.path, self.line, self.op = path, line, op
        self.target, self.message, self.reason = target, message, reason

    def __str__(self):
        where = f"{self.path}:{self.line}"
        what = f"{self.op} {self.target}".strip()
        if self.reason is not None:
            return f"{where}: {what}: allowed by shared-db-safe: {self.reason}"
        return f"{where}: {what}: {self.message}"

    def __repr__(self):
        return f"Finding({self})"


# --------------------------------------------------------------------------- parsing helpers

def _call_name(node):
    """'AddField' for migrations.AddField(...) or AddField(...); None otherwise."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _arg(call, index, keyword):
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    if index is not None and len(call.args) > index:
        return call.args[index]
    return None


def _literal(node, default=UNKNOWN):
    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return UNKNOWN


def _str_arg(call, index, keyword):
    value = _literal(_arg(call, index, keyword), None)
    return value if isinstance(value, str) else None


def field_info(node):
    """A dict describing a field from its ``models.XField(...)`` call, or None."""
    name = _call_name(node)
    if name is None:
        return None
    kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    info = {
        "type": name,
        "null": _literal(kwargs.get("null"), False),
        "unique": _literal(kwargs.get("unique"), False) or name == "OneToOneField",
        "primary_key": _literal(kwargs.get("primary_key"), False),
        "max_length": _literal(kwargs.get("max_length"), None),
        "max_digits": _literal(kwargs.get("max_digits"), None),
        "decimal_places": _literal(kwargs.get("decimal_places"), None),
        "db_column": _literal(kwargs.get("db_column"), None),
        "db_default": "db_default" in kwargs,
        "has_default": "default" in kwargs,
    }
    return info


def _list_elements(node):
    """Elements of a literal list/tuple, concatenated through ``+``; None if not literal."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _list_elements(node.left), _list_elements(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def _model_fields(node):
    """(fields dict, complete flag) from a CreateModel ``fields`` argument."""
    fields, complete = {}, True
    parts = [node]
    while parts:
        part = parts.pop()
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Add):
            parts.extend([part.left, part.right])
            continue
        if not isinstance(part, (ast.List, ast.Tuple)):
            complete = False
            continue
        for elt in part.elts:
            if (isinstance(elt, ast.Tuple) and len(elt.elts) == 2
                    and isinstance(elt.elts[0], ast.Constant) and isinstance(elt.elts[0].value, str)):
                fields[elt.elts[0].value.lower()] = field_info(elt.elts[1])
            else:
                complete = False
    return fields, complete


def _comments(source):
    """{line number: comment text} for every comment in ``source``."""
    out = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def _opt_out(node, comments, lines):
    """The shared-db-safe reason for ``node``, or None."""
    candidates = list(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    line = node.lineno - 1
    while line >= 1 and lines[line - 1].strip().startswith("#"):
        candidates.append(line)
        line -= 1
    for number in sorted(candidates):
        match = OPT_OUT_RE.search(comments.get(number, ""))
        if match:
            return match.group(1)
    return None


def _strings(tree):
    """Every string constant, and every tuple/list of string constants, in ``tree``."""
    singles, groups = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            singles.append(node.value)
        elif isinstance(node, (ast.Tuple, ast.List)):
            group = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(group) >= 2:
                groups.append(group)
    return singles, groups


def _has_db_default(table, column, string_pool):
    """True if a new migration sets a MySQL DEFAULT for ``table.column`` (as 0030 does)."""
    singles, groups = string_pool
    word = re.compile(r"(?<![A-Za-z0-9_])`?" + re.escape(column) + r"`?(?![A-Za-z0-9_])")
    table_l = table.lower()
    for text in singles:
        if "default" in text.lower() and table_l in text.lower() and word.search(text):
            return True
    for group in groups:
        lowered = [g.lower() for g in group]
        if table_l in lowered and column in group and any(" default " in f" {g} " for g in lowered):
            return True
    return False


def _operations(tree):
    """The Migration class's ``operations`` list node, or None."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "operations"):
                    return stmt.value
    return None


def _app_label(path):
    parts = os.path.normpath(path).split(os.sep)
    return parts[-3] if len(parts) >= 3 else ""


# --------------------------------------------------------------------------- the checker

class State:
    """Which models the old code knows, which ones the new migrations created, and field definitions."""

    def __init__(self):
        self.fields = {}          # (app, model) -> {field: info or None}
        self.complete = {}        # (app, model) -> all fields known
        self.new_models = set()   # (app, model) created by the new migrations

    def create(self, key, fields, complete, new):
        self.fields[key], self.complete[key] = fields, complete
        if new:
            self.new_models.add(key)

    def delete(self, key):
        self.fields.pop(key, None)
        self.complete.pop(key, None)
        self.new_models.discard(key)

    def rename(self, old, new):
        self.fields[new] = self.fields.pop(old, {})
        self.complete[new] = self.complete.pop(old, False)
        if old in self.new_models:
            self.new_models.discard(old)
            self.new_models.add(new)

    def is_new(self, key):
        return key in self.new_models

    def field(self, key, name):
        return self.fields.get(key, {}).get(name, UNKNOWN)

    def set_field(self, key, name, info):
        self.fields.setdefault(key, {})[name] = info

    def remove_field(self, key, name):
        self.fields.get(key, {}).pop(name, None)


class Checker:
    def __init__(self):
        self.state = State()
        self.findings = []
        self.allowed = []

    # ---- files

    def load_base(self, path, source):
        """Apply a migration that the old code already has (state only, never reported)."""
        tree = ast.parse(source, filename=path)
        ops = _list_elements(_operations(tree)) if _operations(tree) is not None else None
        for op in ops or []:
            self._apply(op, _app_label(path), new=False)

    def check_new(self, path, source, string_pool):
        """Check a migration the old code does not have, then apply it to the state."""
        tree = ast.parse(source, filename=path)
        comments, lines = _comments(source), source.splitlines()
        ops_node = _operations(tree)
        ops = _list_elements(ops_node) if ops_node is not None else None
        if ops is None:
            line = ops_node.lineno if ops_node is not None else 1
            self._report(path, line, "operations", "", "is not a literal list; cannot check it",
                         _opt_out(ops_node, comments, lines) if ops_node is not None else None)
            return
        ctx = (path, comments, lines, string_pool, _app_label(path))
        for op in ops:
            self._check(op, ctx)
            self._apply(op, ctx[4], new=True)

    # ---- state

    def _apply(self, op, app, new):
        name = _call_name(op)
        if name is None:
            return
        if name == "CreateModel":
            model = _str_arg(op, 0, "name")
            fields_node = _arg(op, 1, "fields")
            if model:
                fields, complete = _model_fields(fields_node) if fields_node is not None else ({}, False)
                self.state.create((app, model.lower()), fields, complete, new)
        elif name == "DeleteModel":
            model = _str_arg(op, 0, "name")
            if model:
                self.state.delete((app, model.lower()))
        elif name == "RenameModel":
            old, new_name = _str_arg(op, 0, "old_name"), _str_arg(op, 1, "new_name")
            if old and new_name:
                self.state.rename((app, old.lower()), (app, new_name.lower()))
        elif name in ("AddField", "AlterField"):
            model, field = _str_arg(op, 0, "model_name"), _str_arg(op, 1, "name")
            if model and field:
                self.state.set_field((app, model.lower()), field.lower(), field_info(_arg(op, 2, "field")))
        elif name == "RemoveField":
            model, field = _str_arg(op, 0, "model_name"), _str_arg(op, 1, "name")
            if model and field:
                self.state.remove_field((app, model.lower()), field.lower())
        elif name == "RenameField":
            model = _str_arg(op, 0, "model_name")
            old, new_name = _str_arg(op, 1, "old_name"), _str_arg(op, 2, "new_name")
            if model and old and new_name:
                key = (app, model.lower())
                info = self.state.field(key, old.lower())
                self.state.remove_field(key, old.lower())
                self.state.set_field(key, new_name.lower(), None if info is UNKNOWN else info)
        elif name == "SeparateDatabaseAndState":
            for sub in _list_elements(_arg(op, 1, "state_operations")) or []:
                self._apply(sub, app, new)

    # ---- checks

    def _report(self, path, line, op, target, message, reason):
        finding = Finding(path, line, op, target, message, reason)
        (self.allowed if reason is not None else self.findings).append(finding)

    def _check(self, op, ctx):
        path, comments, lines, string_pool, app = ctx
        reason = _opt_out(op, comments, lines)
        name = _call_name(op)

        def report(target, message):
            self._report(path, op.lineno, name or "operation", target, message, reason)

        if name is None:
            report("", "is not a plain operation call; cannot check it")
            return
        if name in SAFE_OPS:
            return

        model = _str_arg(op, 0, "model_name") or _str_arg(op, 0, "name") or _str_arg(op, 0, "old_name")
        key = (app, model.lower()) if model else None
        existing = key is not None and not self.state.is_new(key)

        if name in UNSAFE_ON_EXISTING:
            if key is None or existing:
                report(model or "", UNSAFE_ON_EXISTING[name])
        elif name == "AddField":
            self._check_add_field(op, key, existing, string_pool, report)
        elif name == "AlterField":
            self._check_alter_field(op, key, existing, report)
        elif name == "AddConstraint":
            constraint = _call_name(_arg(op, 1, "constraint"))
            if (key is None or existing) and constraint != "Deferrable":
                report(model or "", f"adds a {constraint or 'database'} constraint to a table older code writes; "
                                    "its INSERTs and UPDATEs may now be rejected")
        elif name == "AlterUniqueTogether":
            together = _literal(_arg(op, 1, "unique_together"), UNKNOWN)
            if (key is None or existing) and together not in (None, set(), (), [], frozenset()):
                report(model or "", "adds a unique constraint to a table older code writes")
        elif name == "RunPython":
            code = _arg(op, 0, "code")
            if not (isinstance(code, ast.Attribute) and code.attr == "noop"):
                report("", "runs Python against the shared database (it can rewrite existing rows)")
        elif name == "RunSQL":
            sql = _literal(_arg(op, 0, "sql"), UNKNOWN)
            code = _arg(op, 0, "sql")
            if not (isinstance(code, ast.Attribute) and code.attr == "noop") and sql not in ("", [], ()):
                report("", "runs SQL against the shared database (it can rewrite existing rows or columns)")
        elif name == "SeparateDatabaseAndState":
            for sub in _list_elements(_arg(op, 0, "database_operations")) or []:
                self._check(sub, ctx)
                self._apply(sub, app, new=True)
            if _arg(op, 0, "database_operations") is not None and _list_elements(_arg(op, 0, "database_operations")) is None:
                report("", "database_operations is not a literal list; cannot check it")
        else:
            report(model or "", "is not an operation this check knows; review it by hand")

    def _check_add_field(self, op, key, existing, string_pool, report):
        model, field = _str_arg(op, 0, "model_name"), _str_arg(op, 1, "name")
        info = field_info(_arg(op, 2, "field"))
        target = f"{model}.{field}"
        if not existing:
            return
        if info is None:
            report(target, "field definition is not a plain field call; cannot check it")
            return
        if info["type"] in M2M_TYPES:
            return
        nullable = info["null"] is True
        if not nullable:
            column = info["db_column"] if isinstance(info["db_column"], str) else (
                f"{field}_id" if info["type"] in FK_TYPES else field)
            table = f"{key[0]}_{key[1]}"
            if not info["db_default"] and not _has_db_default(table, column, string_pool):
                report(target, "adds a NOT NULL column without a MySQL DEFAULT to an existing table; older code "
                               "leaves it out of its INSERTs and MySQL refuses the row (IntegrityError 1364, #395). "
                               "Use null=True or give the column a real DEFAULT (see 0030)")
        if info["unique"] is True and not nullable:
            report(target, "adds a unique NOT NULL column to an existing table; older code inserts the same value "
                           "into every row")

    def _check_alter_field(self, op, key, existing, report):
        model, field = _str_arg(op, 0, "model_name"), _str_arg(op, 1, "name")
        target = f"{model}.{field}"
        if not existing:
            return
        new = field_info(_arg(op, 2, "field"))
        old = self.state.field(key, field.lower()) if field else UNKNOWN
        if new is None:
            report(target, "field definition is not a plain field call; cannot check it")
            return
        if old is UNKNOWN or old is None:
            report(target, "previous definition not found; cannot tell whether this narrows the column, "
                           "changes its type or makes it NOT NULL")
            return
        problems = []
        if new["type"] != old["type"]:
            problems.append(f"changes the column type ({old['type']} to {new['type']})")
        if old["null"] is True and new["null"] is not True and new["type"] not in M2M_TYPES:
            problems.append("makes the column NOT NULL")
        for size in ("max_length", "max_digits", "decimal_places"):
            if isinstance(old[size], int) and isinstance(new[size], int) and new[size] < old[size]:
                problems.append(f"narrows {size} from {old[size]} to {new[size]}")
            elif old[size] is None and isinstance(new[size], int) and old["type"] == "TextField":
                problems.append(f"adds {size}={new[size]}")
        if new["db_column"] != old["db_column"]:
            problems.append("changes db_column (renames the column)")
        if new["unique"] is True and old["unique"] is not True and not new["primary_key"]:
            problems.append("makes the column unique")
        if problems:
            report(target, "; ".join(problems))


# --------------------------------------------------------------------------- files and git

def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _migration_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "static", "__pycache__")
                       and not d.startswith(".")]
        if os.path.basename(dirpath) != "migrations":
            continue
        for name in filenames:
            if name.endswith(".py") and name != "__init__.py":
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def _base_files(root, ref):
    out = subprocess.run(["git", "-C", root, "ls-tree", "-r", "--name-only", ref],
                         check=True, capture_output=True, text=True).stdout
    return {os.path.normpath(p) for p in out.splitlines()}


def check_sources(base, new):
    """Check migrations. ``base`` and ``new`` are lists of (path, source), each in apply order."""
    checker = Checker()
    for path, source in base:
        checker.load_base(path, source)
    pool_singles, pool_groups = [], []
    for path, source in new:
        singles, groups = _strings(ast.parse(source, filename=path))
        pool_singles.extend(singles)
        pool_groups.extend(groups)
    for path, source in new:
        checker.check_new(path, source, (pool_singles, pool_groups))
    return checker.findings, checker.allowed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--base", help="git ref the older code runs (e.g. origin/develop); every migration "
                                      "file that is not on it is checked")
    group.add_argument("--new", nargs="+", metavar="FILE",
                       help="check these migration files; every other migration file is treated as old")
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    files = _migration_files(root)
    if args.base:
        try:
            on_base = _base_files(root, args.base)
        except subprocess.CalledProcessError as exc:
            print(f"cannot list {args.base}: {exc.stderr.strip()}", file=sys.stderr)
            return 2
        new_paths = [p for p in files if p not in on_base]
    else:
        new_paths = sorted(os.path.normpath(os.path.relpath(os.path.abspath(p), root)) for p in args.new)
        missing = [p for p in new_paths if p not in files]
        if missing:
            print(f"not migration files under {root}: {', '.join(missing)}", file=sys.stderr)
            return 2
    base_paths = [p for p in files if p not in new_paths]

    findings, allowed = check_sources(
        [(p, _read(os.path.join(root, p))) for p in base_paths],
        [(p, _read(os.path.join(root, p))) for p in new_paths],
    )

    print(f"Checked {len(new_paths)} migration file(s) not on the base: {', '.join(new_paths) or 'none'}")
    for item in allowed:
        print(f"  allowed: {item}")
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for item in findings:
        print(f"  UNSAFE:  {item}")
        if annotate:
            print(f"::error file={item.path},line={item.line}::{item.op} {item.target}: {item.message}")
    if findings:
        print(f"\n{len(findings)} operation(s) unsafe for older code on the shared database; {OPT_OUT_HINT}.")
        return 1
    print("OK: nothing unsafe for older code on the shared database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
