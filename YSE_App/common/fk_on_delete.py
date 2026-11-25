"""Database-level ON DELETE for foreign keys of tables that develop's code does not know (#941).

``/yse_test/`` runs develop's code on the shared ``YSE_test`` database. Django emulates
``on_delete`` in Python, only for the models it knows, and creates the MySQL foreign keys
without an ``ON DELETE`` clause (that is, RESTRICT). So when develop deletes a row that a
table it does not know still references, MySQL rejects the DELETE (error 1451).

A migration that creates such a table lists its foreign keys as
``(table, column, referenced table, rule)`` and runs ``RunPython(forwards(KEYS), backwards(KEYS))``
after the ``CreateModel``: the MySQL constraints are re-created so that the database does what
the model says (``CASCADE`` -> ``ON DELETE CASCADE``, ``SET_NULL`` -> ``ON DELETE SET NULL``).
Django keeps emulating both, which is harmless. Constraint names are read from
``information_schema`` at run time and kept; running it again does nothing; on other database
backends it does nothing. The reverse puts the plain constraints back.

First written inside migration 0062 (#946); moved here when 0063 (#846) needed it too.
"""

import hashlib

CASCADE = "CASCADE"
SET_NULL = "SET NULL"

# What MySQL reports for a constraint created without ON DELETE.
PLAIN_RULES = ("RESTRICT", "NO ACTION")

_CONSTRAINTS_SQL = """
    SELECT kcu.CONSTRAINT_NAME, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME, rc.DELETE_RULE
      FROM information_schema.KEY_COLUMN_USAGE kcu
      JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
        ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
       AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
       AND rc.TABLE_NAME = kcu.TABLE_NAME
     WHERE kcu.TABLE_SCHEMA = DATABASE()
       AND kcu.TABLE_NAME = %s
       AND kcu.COLUMN_NAME = %s
       AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
"""


def current_constraints(connection, table, column):
    """[(name, referenced table, referenced column, delete rule)] for ``table.column`` on MySQL."""
    with connection.cursor() as cursor:
        cursor.execute(_CONSTRAINTS_SQL, [table, column])
        return [tuple(row) for row in cursor.fetchall()]


def _table_exists(connection, table):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                       [table])
        return cursor.fetchone() is not None


def _fallback_name(table, column):
    digest = hashlib.md5(f"{table}.{column}".encode()).hexdigest()[:8]
    return f"{table}_{column}"[:52] + f"_fk_{digest}"


def _rule_matches(current, rule):
    return current in PLAIN_RULES if rule is None else current == rule


def _add(schema_editor, table, name, column, ref_table, ref_column, rule):
    q = schema_editor.quote_name
    on_delete = f" ON DELETE {rule}" if rule else ""
    schema_editor.execute(
        f"ALTER TABLE {q(table)} ADD CONSTRAINT {q(name)} FOREIGN KEY ({q(column)}) "
        f"REFERENCES {q(ref_table)} ({q(ref_column)}){on_delete}"
    )


def set_rules(schema_editor, foreign_keys, target):
    """Make every listed constraint's ON DELETE ``target(rule)`` (None means plain)."""
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    q = schema_editor.quote_name
    for table, column, ref_table, rule in foreign_keys:
        rule = target(rule)
        if not _table_exists(connection, table):
            continue
        found = current_constraints(connection, table, column)
        if not found:
            # Only after an earlier run dropped the constraint and failed before re-adding it.
            _add(schema_editor, table, _fallback_name(table, column), column, ref_table, "id", rule)
            continue
        for name, found_table, found_column, current in found:
            if found_table != ref_table or _rule_matches(current, rule):
                continue
            schema_editor.execute(f"ALTER TABLE {q(table)} DROP FOREIGN KEY {q(name)}")
            # The constraint was in force a moment ago, so every row satisfies it: skip the
            # re-check, which lets MySQL add it in place instead of copying the table.
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION foreign_key_checks = 0")
            try:
                _add(schema_editor, table, name, column, found_table, found_column, rule)
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION foreign_key_checks = 1")


def forwards(foreign_keys):
    """A ``RunPython`` function that puts the listed ON DELETE rules on the constraints."""
    def apply(apps, schema_editor):
        set_rules(schema_editor, foreign_keys, lambda rule: rule)
    return apply


def backwards(foreign_keys):
    """The reverse of :func:`forwards`: plain constraints again."""
    def revert(apps, schema_editor):
        set_rules(schema_editor, foreign_keys, lambda rule: None)
    return revert
