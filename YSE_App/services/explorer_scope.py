"""SQL Explorer for everyone: every query reads only its viewer's data (#723).

Ryan (2026-10-18): SQL Explorer should be available to everyone, while every
query sees only the data its viewer can access. Filtering results after a query
has run cannot do that (aggregates, joins and ``WHERE`` clauses reveal hidden
data without returning a hidden row), so queries are rewritten *before* they
run:

1. **Parse** with ``sqlglot`` (MySQL dialect). Refused: anything that does not
   parse, more than one statement, anything but a ``SELECT`` (or ``WITH`` /
   ``UNION`` of them), ``SELECT ... INTO``, locking reads, MySQL executable
   comments, session variables, functions that are not built-in MySQL
   functions (stored functions), and functions that read files, sleep or lock.
2. **Resolve every table reference** (in joins, subqueries, derived tables,
   common table expressions, ``UNION`` branches, with or without aliases,
   backticks, case or a database prefix). A reference is to a ``WITH`` name
   only when it has no database prefix and matches one; a ``WITH`` name may
   not be the name of a real table, so the two can never be confused. Other
   databases (``information_schema``, ``mysql``, ...), the personal-data tables
   (``EXPLORER_PERSONAL_DATA_TABLES``), account, session, token and credential
   tables, and unknown tables are refused.
3. **Replace each data table** with a derived table of the viewer's visible
   rows (``services.explorer_scope_rows``, built from the per-point rules).
   Reference tables pass unchanged. Staff and superusers also read the
   remaining collaboration and operations tables unchanged (as before, for
   them), and ``auth_user`` only as id and names; other viewers are refused
   those tables.
4. **Derived values** (``EXPLORER_DERIVED_VALUE_COLUMNS`` and the views in
   ``PAGE_QUERY_EXTRA_ALLOWED_TABLES``) may be used to select and sort rows,
   but every output column computed from them is returned as NULL.
5. **Limits**: at most ``EXPLORER_SCOPED_MAX_ROWS`` rows, and
   ``EXPLORER_SCOPED_MAX_EXECUTION_MS`` per statement (:func:`viewer_time_cap`).

The query that runs is the one generated from the checked syntax tree (with
identifiers quoted and comments dropped), never the text as typed, and it is
parsed and checked again before the derived tables are spliced in.

Service readers (``visibility.user_sees_everything``) are not rewritten. The
setting ``EXPLORER_QUERY_SCOPE: off`` is the rollback switch.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from contextlib import contextmanager
from typing import Dict, FrozenSet, Iterator, List, NamedTuple, Optional, Set

from django.conf import settings

logger = logging.getLogger(__name__)

POLICY_ENFORCE = 'enforce'
POLICY_OFF = 'off'

TIER_SERVICE = 'service'
TIER_STAFF = 'staff'
TIER_MEMBER = 'member'

MAX_SQL_CHARS = 200_000

_SYSTEM_SCHEMAS = frozenset({'information_schema', 'mysql', 'performance_schema', 'sys'})

# Staff read these account tables as they are (group names, memberships); everything else
# under the excluded prefixes is refused for every scoped viewer.
_STAFF_ACCOUNT_TABLES = frozenset({'auth_group', 'auth_user_groups'})

# Result files of analysis runs are data results (#706): refused for every scoped viewer.
_ALWAYS_REFUSED_TABLES = frozenset({'yse_app_analysisresultfile'})

#: Built-in derived-value columns ``{table: {column, ...}}`` (lower case). None are stored
#: today (fit values live in the photometry statistics, whose rows are already limited per
#: viewer); ``EXPLORER_DERIVED_VALUE_COLUMNS`` and ``PAGE_QUERY_EXTRA_ALLOWED_TABLES`` add more.
DERIVED_VALUE_COLUMNS: Dict[str, FrozenSet[str]] = {}

#: Columns of a derived-value view that identify rows and are returned as they are.
IDENTIFYING_COLUMNS = frozenset(
    {'id', 'name', 'transient_id', 'transient_name', 'ra', 'dec', 'transient_ra', 'transient_dec'})

# MySQL built-in functions that sqlglot parses as unknown ("anonymous") functions. Any
# other unknown name could be a stored function, which runs with its definer's rights.
_BUILTIN_FUNCTIONS = frozenset("""
abs acos adddate addtime aes_decrypt any_value ascii asin atan atan2 avg bin bit_and bit_count
bit_length bit_or bit_xor cast ceil ceiling char char_length character_length charset coalesce
collation compress concat concat_ws conv convert convert_tz cos cot count crc32 cume_dist curdate
current_date current_time current_timestamp curtime date date_add date_format date_sub datediff
day dayname dayofmonth dayofweek dayofyear degrees dense_rank elt exp export_set extract field
find_in_set first_value floor format from_base64 from_days from_unixtime get_format greatest
group_concat hex hour if ifnull inet6_aton inet6_ntoa inet_aton inet_ntoa insert instr interval
is_ipv4 is_ipv6 isnull json_array json_arrayagg json_contains json_contains_path json_depth
json_extract json_keys json_length json_merge_patch json_merge_preserve json_object
json_objectagg json_overlaps json_pretty json_quote json_search json_type json_unquote
json_valid json_value lag last_day last_value lcase lead least left length ln localtime
localtimestamp locate log log10 log2 lower lpad ltrim make_set makedate maketime max md5
microsecond mid min minute mod month monthname now nth_value ntile nullif oct octet_length ord
percent_rank period_add period_diff pi point position pow power quarter quote radians rand rank
regexp_instr regexp_like regexp_replace regexp_substr repeat replace reverse right round
row_number rpad rtrim sec_to_time second sha sha1 sha2 sign sin soundex space sqrt st_astext
st_distance st_distance_sphere st_geomfromtext st_x st_y std stddev stddev_pop stddev_samp
str_to_date strcmp subdate substr substring substring_index subtime sum sysdate tan time
time_format time_to_sec timediff timestamp timestampadd timestampdiff to_base64 to_days
to_seconds trim truncate ucase uncompress unhex unix_timestamp upper utc_date utc_time
utc_timestamp uuid var_pop var_samp variance week weekday weekofyear weight_string year yearweek
""".split())

# Never allowed, whatever sqlglot calls them: files, sleeping, locks, replication waits,
# and the server's identity and state.
_FORBIDDEN_FUNCTIONS = frozenset("""
load_file sleep benchmark get_lock release_lock release_all_locks is_free_lock is_used_lock
master_pos_wait source_pos_wait wait_for_executed_gtid_set wait_until_sql_thread_after_gtids
sys_eval sys_exec user current_user session_user system_user current_role version database
schema connection_id last_insert_id found_rows row_count name_const uuid_short
statement_digest statement_digest_text icu_version roles_graphml ps_current_thread_id
ps_thread_id format_bytes format_pico_time current_schema current_database current_catalog
""".split())


class ExplorerQueryRefused(ValueError):
    """The query cannot be scoped to its viewer (reason in ``args[0]``)."""


class ScopedQuery(NamedTuple):
    sql: str
    tier: str
    rewritten: bool
    time_cap_ms: int = 0


def scope_policy() -> str:
    value = str(getattr(settings, 'EXPLORER_QUERY_SCOPE', POLICY_ENFORCE) or POLICY_ENFORCE).strip().lower()
    return POLICY_OFF if value == POLICY_OFF else POLICY_ENFORCE


def scope_enforced() -> bool:
    return scope_policy() == POLICY_ENFORCE


def viewer_tier(user) -> Optional[str]:
    """``service`` (not rewritten), ``staff``, ``member``, or None (refused: not signed in)."""
    if user is None or not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return None
    from YSE_App.services.visibility import user_sees_everything

    if user_sees_everything(user):
        return TIER_SERVICE
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return TIER_STAFF
    return TIER_MEMBER


def max_rows() -> int:
    try:
        return max(0, int(getattr(settings, 'EXPLORER_SCOPED_MAX_ROWS', 100000) or 0))
    except (TypeError, ValueError):
        return 100000


def max_execution_ms() -> int:
    try:
        return max(0, int(getattr(settings, 'EXPLORER_SCOPED_MAX_EXECUTION_MS', 120000) or 0))
    except (TypeError, ValueError):
        return 120000


# ------------------------------------------------------------------ catalog

_TABLE_CACHE_SECONDS = 300.0
_table_cache = {'at': 0.0, 'names': None}


def _database_tables() -> Dict[str, str]:
    """``{lower-case name: name}`` of the application database's tables and views."""
    now = time.monotonic()
    names = _table_cache['names']
    if names is None or now - _table_cache['at'] > _TABLE_CACHE_SECONDS:
        from django.db import connection

        found = set()
        with connection.cursor() as cursor:
            found.update(connection.introspection.table_names(cursor, include_views=True))
        names = {}
        for name in found:
            names.setdefault(name.lower(), set()).add(name)
        _table_cache.update(at=now, names=names)
    return {low: (sorted(real)[0] if len(real) == 1 else None) for low, real in names.items()}


def clear_table_cache() -> None:
    _table_cache.update(at=0.0, names=None)


def _database_names() -> Set[str]:
    from django.db import connections

    out = set()
    for alias in ('default', 'explorer'):
        try:
            name = connections.databases.get(alias, {}).get('NAME')
        except Exception:  # pragma: no cover - misconfigured alias
            name = None
        if name:
            out.add(str(name).lower())
    from django.db import connection

    if connection.settings_dict.get('NAME'):
        out.add(str(connection.settings_dict['NAME']).lower())
    return out


def _lower_set(values) -> FrozenSet[str]:
    if isinstance(values, str):
        values = re.split(r'[\s,]+', values)
    return frozenset(str(v).strip().lower() for v in (values or ()) if str(v).strip())


def _personal_tables() -> FrozenSet[str]:
    return _lower_set(getattr(settings, 'EXPLORER_PERSONAL_DATA_TABLES', ()))


def _excluded_prefixes():
    return tuple(p.lower() for p in getattr(settings, 'EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES', ()) or ())


def extra_derived_tables() -> FrozenSet[str]:
    """Views offered for selection only (``PAGE_QUERY_EXTRA_ALLOWED_TABLES``, #715)."""
    return _lower_set(getattr(settings, 'PAGE_QUERY_EXTRA_ALLOWED_TABLES', ()))


def _derived_columns_setting() -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {t: set(c) for t, c in DERIVED_VALUE_COLUMNS.items()}
    for item in _lower_set(getattr(settings, 'EXPLORER_DERIVED_VALUE_COLUMNS', '')):
        table, _dot, column = item.rpartition('.')
        if table and column:
            out.setdefault(table, set()).add(column)
    return out


class Decision(NamedTuple):
    kind: str  # 'pass', 'data', 'sql', 'refuse'
    real_name: str = ''
    rule: object = None
    sql: str = ''
    reason: str = ''


def classify(name: str, tier: str) -> Decision:
    """What a scoped viewer of ``tier`` gets for the table ``name`` (any case)."""
    from YSE_App.services import explorer_scope_rows as rows

    low = name.lower()
    real = _database_tables()
    if low not in real:
        return Decision('refuse', reason='unknown table %s' % name)
    real_name = real[low]
    if real_name is None:
        return Decision('refuse', reason='ambiguous table name %s' % name)
    if low in _personal_tables() or low in _ALWAYS_REFUSED_TABLES:
        return Decision('refuse', reason='%s holds personal or restricted data and cannot be queried' % real_name)
    rule = rows.row_rules().get(low)
    if rule is not None:
        return Decision('data', real_name, rule=rule)
    if low in extra_derived_tables():
        return Decision('derived', real_name)
    if low in rows.open_tables():
        return Decision('pass', real_name)
    staff_sql = rows.staff_only_tables().get(low)
    if staff_sql is not None:
        if tier == TIER_STAFF:
            return Decision('sql', real_name, sql=staff_sql)
        return Decision('refuse', reason='%s is not available to you in SQL Explorer' % real_name)
    if low.startswith(_excluded_prefixes()):
        if tier == TIER_STAFF and low in _STAFF_ACCOUNT_TABLES:
            return Decision('pass', real_name)
        return Decision('refuse', reason='%s holds account, session or credential data and cannot be queried'
                        % real_name)
    if tier == TIER_STAFF:
        return Decision('pass', real_name)
    return Decision('refuse', reason='%s is not available to you in SQL Explorer' % real_name)


def tables_for_viewer(user) -> Optional[FrozenSet[str]]:
    """Lower-case names of the tables ``user`` may read (None: every table; empty: none)."""
    tier = viewer_tier(user)
    if tier == TIER_SERVICE or not scope_enforced():
        return None
    if tier is None:
        return frozenset()
    return frozenset(low for low in _database_tables() if classify(low, tier).kind != 'refuse')


# ------------------------------------------------------------------ parsing


def _refuse(reason: str):
    raise ExplorerQueryRefused(reason)


def _parse(sql: str):
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    if not sql or not sql.strip():
        _refuse('the query is empty')
    if len(sql) > MAX_SQL_CHARS:
        _refuse('the query is longer than %d characters' % MAX_SQL_CHARS)
    if '\x00' in sql:
        _refuse('the query contains a NUL character')
    if re.search(r'/\*(!|\+|M!)', sql):
        _refuse('MySQL executable comments and optimizer hints are not allowed')
    try:
        statements = sqlglot.parse(sql, read='mysql')
    except SqlglotError as exc:
        _refuse('the query could not be parsed (%s)' % str(exc).splitlines()[0][:200])
    except RecursionError:
        _refuse('the query is nested too deeply')
    statements = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1:
        _refuse('only one statement may be run at a time')
    root = statements[0]
    inner = root
    while isinstance(inner, exp.Subquery):
        inner = inner.this
    if not isinstance(inner, (exp.Select, exp.SetOperation)):
        _refuse('only SELECT queries may be run')
    return root


def _forbidden_node_types():
    from sqlglot import exp

    names = ('Into', 'Lock', 'SessionParameter', 'Command', 'DDL', 'DML', 'Insert', 'Update', 'Delete',
             'Merge', 'Create', 'Drop', 'Alter', 'Show', 'Describe', 'Use', 'Set', 'Transaction',
             'Commit', 'Rollback', 'Pragma', 'LoadData', 'Copy', 'CurrentUser', 'Analyze', 'Grant',
             'Cache', 'Uncache', 'Kill', 'Execute')
    return tuple(getattr(exp, n) for n in names if isinstance(getattr(exp, n, None), type))


def _function_name(node) -> str:
    from sqlglot import exp

    if isinstance(node, exp.Anonymous):
        return str(node.name or '').lower()
    try:
        name = str(node.sql_name() or '').lower()
    except Exception:  # pragma: no cover - sqlglot internals
        name = ''
    return name or str(node.key).lower()


def _check_nodes(root) -> None:
    from sqlglot import exp

    forbidden = _forbidden_node_types()
    for node in root.walk():
        if isinstance(node, forbidden):
            _refuse('%s is not allowed' % node.key.upper())
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in _FORBIDDEN_FUNCTIONS or str(node.key).lower() in _FORBIDDEN_FUNCTIONS:
                _refuse('the function %s is not allowed' % name.upper())
            if isinstance(node, exp.Anonymous):
                if isinstance(node.parent, exp.Dot) or name not in _BUILTIN_FUNCTIONS:
                    _refuse('the function %s is not a MySQL built-in function' % (node.name or '?'))
    for hint in list(root.find_all(exp.Hint)):
        hint.pop()


def _cte_names(root) -> Set[str]:
    from sqlglot import exp

    return {str(cte.alias_or_name).lower() for cte in root.find_all(exp.CTE)}


# ------------------------------------------------------------------ derived values


def _derived_for_query(referenced: Dict[str, str]) -> Dict[str, Set[str]]:
    """``{table: derived columns}`` for the tables this query reads."""
    configured = _derived_columns_setting()
    out: Dict[str, Set[str]] = {}
    for low in referenced:
        if low in configured:
            out[low] = set(configured[low])
        if low in extra_derived_tables():
            out.setdefault(low, set()).update(_view_columns(referenced[low]) - IDENTIFYING_COLUMNS)
    return {t: c for t, c in out.items() if c}


def _view_columns(real_name: str) -> Set[str]:
    from django.db import connection

    with connection.cursor() as cursor:
        return {str(row[0]).lower() for row in connection.introspection.get_table_description(cursor, real_name)}


def _outer_selects(root):
    from sqlglot import exp

    node = root
    while isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.SetOperation):
        return _outer_selects(node.this) + _outer_selects(node.expression)
    return []


def _strip_derived_outputs(root, derived: Dict[str, Set[str]]) -> List[str]:
    """Return every output column computed from a derived value as NULL; the names stripped."""
    from sqlglot import exp

    if not derived:
        return []
    for star in root.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            _refuse('list the columns explicitly (no *) in a query that reads derived values')
    for node in root.find_all(exp.Parameter, exp.PropertyEQ, exp.JSONTable, exp.Values):
        _refuse('user variables, JSON_TABLE and VALUES are not allowed in a query that reads derived values')
    for alias in root.find_all(exp.TableAlias):
        if alias.args.get('columns'):
            _refuse('column lists on table or WITH names are not allowed in a query that reads derived values')

    tainted: Set[str] = set()
    for columns in derived.values():
        tainted |= columns

    def is_tainted(expression) -> bool:
        return any(str(c.name).lower() in tainted for c in expression.find_all(exp.Column))

    changed = True
    while changed:
        changed = False
        for select in root.find_all(exp.Select):
            for projection in select.expressions:
                name = str(projection.alias_or_name).lower()
                if name and name not in tainted and is_tainted(projection):
                    tainted.add(name)
                    changed = True
    outer = _outer_selects(root)
    positions = set()
    for select in outer:
        for i, projection in enumerate(select.expressions):
            if is_tainted(projection):
                positions.add(i)
    stripped = []
    for select in outer:
        projections = list(select.expressions)
        for i in sorted(positions):
            if i < len(projections):
                name = projections[i].alias_or_name or 'col%d' % (i + 1)
                projections[i] = exp.alias_(exp.Null(), name, quoted=True)
                stripped.append(name)
        select.set('expressions', projections)
    return sorted(set(stripped))


# ------------------------------------------------------------------ rewrite


def _cap_rows(root, cap: int) -> None:
    from sqlglot import exp

    if not cap:
        return
    limit = root.args.get('limit')
    if isinstance(limit, exp.Limit):
        value = limit.args.get('expression')
        if isinstance(value, exp.Literal) and not value.is_string:
            try:
                if int(str(value.this)) <= cap:
                    return
            except ValueError:
                pass
        limit.set('expression', exp.Literal.number(cap))
        return
    root.set('limit', exp.Limit(expression=exp.Literal.number(cap)))


def rewrite(sql: str, user, *, row_cap: Optional[int] = None) -> ScopedQuery:
    """``sql`` rewritten so it reads only ``user``'s rows (raises :class:`ExplorerQueryRefused`).

    Service readers get ``sql`` unchanged. ``row_cap`` defaults to
    ``EXPLORER_SCOPED_MAX_ROWS``.
    """
    from sqlglot import exp

    tier = viewer_tier(user)
    if tier is None:
        _refuse('sign in to run queries')
    if tier == TIER_SERVICE:
        return ScopedQuery(sql, tier, False)

    root = _parse(sql)
    _check_nodes(root)
    real = _database_tables()
    ctes = _cte_names(root)
    for name in ctes:
        if name in real:
            _refuse('a WITH name may not be the name of a table (%s)' % name)
    databases = _database_names()

    from YSE_App.services.explorer_scope_rows import ViewerRows

    viewer_rows = ViewerRows(user)
    nonce = secrets.token_hex(8)
    splices: Dict[str, str] = {}
    sql_by_table: Dict[str, str] = {}
    passed: Set[str] = set()
    referenced: Dict[str, str] = {}

    for table in list(root.find_all(exp.Table)):
        this = table.this
        if isinstance(this, exp.JSONTable):
            continue
        if not isinstance(this, exp.Identifier):
            _refuse('unsupported table expression %s' % table.sql(dialect='mysql')[:80])
        if table.args.get('catalog') is not None:
            _refuse('three-part table names are not allowed')
        name = str(table.name)
        db = str(table.db or '')
        if db:
            if db.lower() in _SYSTEM_SCHEMAS:
                _refuse('system schemas (%s) cannot be queried' % db)
            if db.lower() not in databases:
                _refuse('only the application database can be queried (not %s)' % db)
        elif name.lower() in ctes:
            continue
        if not name or not name.isascii() or not re.fullmatch(r'[A-Za-z0-9_$]+', name):
            _refuse('unsupported table name %r' % name)
        if not db and name.lower() == 'dual':
            select = table.parent.parent if isinstance(table.parent, exp.From) else None
            if isinstance(select, exp.Select) and not select.args.get('joins'):
                select.set('from', None)
                continue
            _refuse('DUAL may only be used alone')
        decision = classify(name, tier)
        if decision.kind == 'refuse':
            _refuse(decision.reason)
        referenced[decision.real_name.lower()] = decision.real_name
        if decision.kind == 'pass':
            table.set('this', exp.to_identifier(decision.real_name, quoted=True))
            table.set('db', None)
            passed.add(decision.real_name.lower())
            continue
        low = decision.real_name.lower()
        if low not in sql_by_table:
            if decision.kind == 'data':
                sql_by_table[low] = viewer_rows.table_sql(decision.rule)
            elif decision.kind == 'sql':
                sql_by_table[low] = decision.sql
            else:  # derived-value view: rows of listed transients when it has a transient_id
                from django.db import connection

                qn = connection.ops.quote_name
                if 'transient_id' in _view_columns(decision.real_name):
                    sql_by_table[low] = 'SELECT `__yse_b`.* FROM %s `__yse_b` WHERE `__yse_b`.`transient_id` IN (%s)' % (
                        qn(decision.real_name), viewer_rows.sql('listed_transients'))
                else:
                    sql_by_table[low] = 'SELECT `__yse_b`.* FROM %s `__yse_b`' % qn(decision.real_name)
        token = '__yse_scope_%s_%d' % (nonce, len(splices))
        splices[token] = sql_by_table[low]
        alias = table.args.get('alias')
        if alias is None:
            alias = exp.TableAlias(this=exp.to_identifier(name, quoted=True))
        replacement = exp.Table(this=exp.to_identifier(token, quoted=True), alias=alias.copy())
        table.replace(replacement)

    _strip_derived_outputs(root, _derived_for_query(referenced))
    _cap_rows(root, max_rows() if row_cap is None else row_cap)
    text = root.sql(dialect='mysql', comments=False, identify=True)
    _verify(text, splices, passed, ctes)
    for token, sub in splices.items():
        text = text.replace('`%s`' % token, '(%s)' % sub)
    return ScopedQuery(text, tier, True, max_execution_ms())


def _verify(text: str, splices: Dict[str, str], passed: Set[str], ctes: Set[str]) -> None:
    """Parse the generated query again: only placeholders, passed tables and WITH names remain."""
    import sqlglot
    from sqlglot import exp

    try:
        again = sqlglot.parse(text, read='mysql')
    except Exception:  # pragma: no cover - sqlglot generated what it cannot read
        logger.warning('explorer scope: generated SQL did not parse again')
        _refuse('the query could not be rewritten safely')
    again = [s for s in again if s is not None]
    if len(again) != 1:
        _refuse('the query could not be rewritten safely')
    seen: Dict[str, int] = {}
    for table in again[0].find_all(exp.Table):
        if isinstance(table.this, exp.JSONTable):
            continue
        name = str(table.name)
        if name in splices:
            seen[name] = seen.get(name, 0) + 1
            continue
        low = name.lower()
        if table.db or (low not in passed and low not in ctes):
            _refuse('the query could not be rewritten safely')
    for token in list(splices):
        # A placeholder may have gone with an output column that was returned as NULL.
        if seen.get(token, 0) != text.count('`%s`' % token) or seen.get(token, 0) > 1:
            _refuse('the query could not be rewritten safely')
        if not seen.get(token):
            del splices[token]


# ------------------------------------------------------------------ running


def scope_for_viewer(sql: str, user) -> ScopedQuery:
    """The SQL to run for ``user`` (unchanged with the rollback switch or for service readers)."""
    if not scope_enforced():
        return ScopedQuery(sql, viewer_tier(user) or '', False)
    return rewrite(sql, user)


def _set_cap(connection, ms: int) -> None:
    from django.db import DatabaseError

    try:
        with connection.cursor() as cursor:
            if getattr(connection, 'mysql_is_mariadb', False):
                cursor.execute('SET SESSION max_statement_time = %s', [ms / 1000.0])
            else:
                cursor.execute('SET SESSION max_execution_time = %s', [int(ms)])
    except DatabaseError as exc:
        logger.warning('explorer scope: could not set the statement time cap: %s', exc)


@contextmanager
def viewer_time_cap(connection, ms: int) -> Iterator[None]:
    """Run the block with the statement time cap at ``ms`` (never above the connection's own cap)."""
    if not ms or getattr(connection, 'vendor', None) != 'mysql':
        yield
        return
    from YSE_App.common.db_time_cap import explorer_cap_ms

    base = explorer_cap_ms() if getattr(connection, 'alias', None) == 'explorer' else 0
    cap = min(ms, base) if base else ms
    _set_cap(connection, cap)
    try:
        yield
    finally:
        _set_cap(connection, base)


def viewer_cache_suffix(user) -> str:
    """Cache-key suffix for results scoped to ``user``: per viewer, and new when their groups or
    access rules change. Empty for unscoped callers."""
    if user is None or viewer_tier(user) in (None, TIER_SERVICE) or not scope_enforced():
        return ''
    from YSE_App.services.visibility import group_access_plot_cache_token

    return '_v%s_%s' % (user.pk, group_access_plot_cache_token(user))
