"""
Rewrite saved SQL that still uses the dropped ``data_quality_id`` column.

Migration 0002 turned ``data_quality`` on TransientPhotData, HostPhotData,
TransientSpectrum and HostSpectrum from a ForeignKey (column
``data_quality_id``) into a ManyToMany (join table ``<table>_data_quality``).
Saved Explorer queries written before that still say, for example,
``ISNULL(pd2.data_quality_id) = True``; on the migrated schema they fail with
"Unknown column".

:func:`rewrite_data_quality_sql` replaces each reference with the join-table
form that gives the same rows:

* ``ISNULL(x.data_quality_id) = True`` / ``x.data_quality_id IS NULL``
  -> ``NOT EXISTS (SELECT 1 FROM <table>_data_quality dq WHERE dq.<model>_id = x.id)``
* ``ISNULL(x.data_quality_id) = False`` / ``x.data_quality_id IS NOT NULL``
  -> ``EXISTS (...)``
* ``x.data_quality_id`` in a SELECT list
  -> ``(SELECT MIN(dq.dataquality_id) FROM ... ) AS data_quality_id``
  (same column name; one flag id, or NULL when the point has none)

It deliberately does not translate to ``quality_mask``: the mask also carries
bits that never came from a DataQuality row (NOT_PUBLISHED, NOT_FORCED, ...),
so ``quality_mask = 0`` would drop rows the original kept. The transient
photometry form is exactly ``dashboard_saved_queries._DQ_M2M``, so a rewritten
"YSE Magnitude-Limited Sample" is still recognised by
``manage.py rewrite_dashboard_queries``.

A reference is rewritten only when its alias is bound to one of the four
tables in the same query text; anything else is left as it is and reported in
``RewriteResult.leftover`` so a person can look at it. Django-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

#: Lower-case table name -> model part of the join-table FK column.
DQ_TABLES: Dict[str, str] = {
    "yse_app_transientphotdata": "transientphotdata",
    "yse_app_hostphotdata": "hostphotdata",
    "yse_app_transientspectrum": "transientspectrum",
    "yse_app_hostspectrum": "hostspectrum",
}

_COLUMN = "data_quality_id"
_ANY_REF_RE = re.compile(r"\bdata_quality_id\b", re.I)

# alias.data_quality_id with optional spaces around the dot
_REF = r"(?P<alias>`?\w+`?)\s*\.\s*`?data_quality_id`?"

_PREDICATES = (
    # ISNULL(x.data_quality_id) = True / = 1   -> no flag
    (re.compile(r"ISNULL\s*\(\s*" + _REF + r"\s*\)\s*=\s*(?:TRUE|1)\b", re.I), "NOT EXISTS"),
    # ISNULL(x.data_quality_id) = False / = 0  -> has a flag
    (re.compile(r"ISNULL\s*\(\s*" + _REF + r"\s*\)\s*=\s*(?:FALSE|0)\b", re.I), "EXISTS"),
    (re.compile(_REF + r"\s+IS\s+NOT\s+NULL\b", re.I), "EXISTS"),
    (re.compile(_REF + r"\s+IS\s+NULL\b", re.I), "NOT EXISTS"),
)

# alias.data_quality_id as a SELECT-list item: followed by ',', 'FROM' or 'AS'.
_SELECT_ITEM_RE = re.compile(_REF + r"(?=\s*(?P<after>,|\bFROM\b|\bAS\b))", re.I)


@dataclass(frozen=True)
class RewriteResult:
    sql: str
    changed: int
    #: Context snippets of ``data_quality_id`` references left untouched.
    leftover: Tuple[str, ...]

    @property
    def state(self) -> str:
        """'clean' (nothing to do), 'rewrite' (fully rewritten) or 'review'."""
        if self.leftover:
            return "review"
        return "rewrite" if self.changed else "clean"


def _alias_tables(sql: str) -> Dict[str, str]:
    """alias (lower case) -> table name as written, for the four DQ tables."""
    out: Dict[str, str] = {}
    for m in re.finditer(r"`?\b(YSE_App_(?:transient|host)(?:photdata|spectrum))\b`?"
                         r"(?:\s+(?:AS\s+)?`?(\w+)`?)?", sql, re.I):
        table = m.group(1)
        out[table.lower()] = table          # the bare table name works as its own alias
        alias = m.group(2)
        if alias and alias.upper() not in _SQL_WORDS:
            out[alias.lower()] = table
    return out


_SQL_WORDS = {
    "ON", "WHERE", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "JOIN", "STRAIGHT_JOIN",
    "GROUP", "ORDER", "LIMIT", "AND", "OR", "SET", "UNION", "HAVING", "USING", "NATURAL",
    "WINDOW", "FOR", "LOCK", "INTO", "VALUES", "SELECT", "FROM", "AS",
}


_CLAUSE_RE = re.compile(r"\b(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|ON|USING)\b", re.I)


def _in_select_list(sql: str, pos: int) -> bool:
    """True when the nearest clause keyword before ``pos`` is SELECT."""
    last = None
    for last in _CLAUSE_RE.finditer(sql, 0, pos):
        pass
    return bool(last) and last.group(1).upper() == "SELECT"


def _join_table(table: str) -> Tuple[str, str]:
    return f"{table}_data_quality", f"{DQ_TABLES[table.lower()]}_id"


def _subquery(table: str, alias: str, what: str = "1") -> str:
    join, fk = _join_table(table)
    return f"SELECT {what} FROM {join} dq WHERE dq.{fk} = {alias}.id"


def rewrite_data_quality_sql(sql: str) -> RewriteResult:
    """Rewrite every ``data_quality_id`` reference whose alias resolves; see module doc."""
    text = sql or ""
    if not _ANY_REF_RE.search(text):
        return RewriteResult(text, 0, ())
    tables = _alias_tables(text)
    changed = 0

    def resolve(m) -> Optional[Tuple[str, str]]:
        alias = m.group("alias").strip("`")
        table = tables.get(alias.lower())
        return (table, alias) if table else None

    for pattern, op in _PREDICATES:
        def repl(m, op=op):
            nonlocal changed
            hit = resolve(m)
            if not hit:
                return m.group(0)
            changed += 1
            return f"{op} ({_subquery(*hit)})"
        text = pattern.sub(repl, text)

    def select_repl(m):
        nonlocal changed
        hit = resolve(m)
        if not hit or not _in_select_list(m.string, m.start()):
            return m.group(0)
        changed += 1
        sub = f"({_subquery(*hit, what='MIN(dq.dataquality_id)')})"
        return sub if m.group("after").upper() == "AS" else f"{sub} AS {_COLUMN}"

    text = _SELECT_ITEM_RE.sub(select_repl, text)

    flat = re.sub(r"\s+", " ", text)
    leftover = tuple(
        flat[max(0, m.start() - 40):m.end() + 20].strip()
        for m in _ANY_REF_RE.finditer(flat)
        # "AS data_quality_id" is the alias this rewrite adds, not a column reference
        if not re.search(r"\bAS\s+`?$", flat[:m.start()], re.I)
    )
    return RewriteResult(text, changed, leftover)


# --------------------------------------------------------------------------- #
# mysqldump INSERT rows (the docker seed data), for tests and the fixture rewrite
# --------------------------------------------------------------------------- #

_UNESCAPE = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b"}


def _parse_value(body: str, i: int) -> Tuple[object, int, int]:
    """(value, start, end) of the value at ``body[i]``; strings are unescaped."""
    if body[i] == "'":
        j, out = i + 1, []
        while True:
            c = body[j]
            if c == "\\":
                out.append(_UNESCAPE.get(body[j + 1], body[j + 1]))
                j += 2
            elif c == "'":
                if body[j + 1:j + 2] == "'":
                    out.append("'")
                    j += 2
                else:
                    return "".join(out), i, j + 1
            else:
                out.append(c)
                j += 1
    m = re.compile(r"[^,)]+").match(body, i)
    return m.group(0), i, m.end()


def iter_dump_rows(line: str) -> Iterator[List[Tuple[object, int, int]]]:
    """Yield each row of one ``INSERT INTO `t` VALUES (...),(...);`` line as
    ``[(value, start, end), ...]`` with offsets into ``line``."""
    i = line.index(" VALUES ") + len(" VALUES ")
    n = len(line.rstrip().rstrip(";"))
    while i < n:
        assert line[i] == "(", f"unexpected {line[i]!r} at {i}"
        i += 1
        row = []
        while True:
            value, start, end = _parse_value(line, i)
            row.append((value, start, end))
            i = end
            if line[i] == ",":
                i += 1
                continue
            if line[i] == ")":
                i += 1
                break
        yield row
        if i < n and line[i] == ",":
            i += 1


def mysql_escape(value: str) -> str:
    """Quote ``value`` the way mysqldump does."""
    out = (value.replace("\\", "\\\\").replace("\0", "\\0").replace("\n", "\\n")
           .replace("\r", "\\r").replace("\x1a", "\\Z").replace("'", "\\'").replace('"', '\\"'))
    return f"'{out}'"


def explorer_rows_from_dump(text: str) -> List[Tuple[int, str, str]]:
    """``[(id, title, sql), ...]`` from the ``explorer_query`` INSERT in a dump."""
    for line in text.splitlines():
        if line.startswith("INSERT INTO `explorer_query` VALUES "):
            return [(int(r[0][0]), r[1][0], r[2][0]) for r in iter_dump_rows(line)]
    return []
