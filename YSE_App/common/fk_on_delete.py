"""Database-level ON DELETE for foreign keys that older code on the shared database does not know.

``/yse_test/`` runs develop's code on the shared ``YSE_test`` database. Django emulates
``on_delete`` in Python, only for the models and fields it knows, and creates the MySQL foreign
keys without an ``ON DELETE`` clause (that is, RESTRICT). When develop deletes a row that a
newer table or column still references, MySQL rejects the DELETE (error 1451).

Migrations call :func:`set_rules` to re-create such constraints so that the database does what
the model says: ``CASCADE`` becomes ``ON DELETE CASCADE`` and ``SET_NULL`` becomes
``ON DELETE SET NULL``. Constraint names are read from ``information_schema`` at run time and
kept. Running it again does nothing. On other database backends it does nothing. No row or
column is changed.

First used by migration 0062 (#941); moved here when 0064 needed it too.
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
    """Make the ON DELETE of every ``(table, column, referenced table, rule)`` in ``foreign_keys``
    ``target(rule)`` (None means plain). Every referenced column is ``id``."""
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


def apply(schema_editor, foreign_keys):
    """Forwards: the rules listed in ``foreign_keys``."""
    set_rules(schema_editor, foreign_keys, lambda rule: rule)


def revert(schema_editor, foreign_keys):
    """Backwards: plain constraints again."""
    set_rules(schema_editor, foreign_keys, lambda rule: None)
