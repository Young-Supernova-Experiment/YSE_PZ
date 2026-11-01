"""Database-level ``ON DELETE`` for foreign keys of experimental's new tables (#941, #946).

``/yse_test/`` runs develop's code on the shared ``YSE_test`` database, and develop does not
know the tables that experimental's migrations create. Django emulates ``on_delete`` in Python,
only for the models it knows, and creates the MySQL foreign keys without an ``ON DELETE`` clause
(that is, RESTRICT). So when develop deletes a row that one of these tables still references,
MySQL rejects the DELETE (error 1451).

A migration that creates such a table (in the same migration or an earlier one) lists its
foreign keys as
``(table, column, referenced table, rule)`` and runs::

    migrations.RunPython(fk_on_delete.forwards_for(FOREIGN_KEYS),
                         fk_on_delete.backwards_for(FOREIGN_KEYS))

with ``atomic = False``. The forward re-creates each listed MySQL constraint with the model's
rule (``CASCADE`` -> ``ON DELETE CASCADE``, ``SET_NULL`` -> ``ON DELETE SET NULL``), keeping its
name; the reverse puts the plain constraints back. Only the listed constraints change, never a
row. Running it again does nothing. On other database backends it does nothing.

Moved here from ``0062_shared_db_fk_on_delete`` (which now imports it) so that every
section 5 migration shares one copy.
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
    # A CreateModel earlier in the same migration leaves its foreign keys in deferred_sql until
    # the migration ends. Run them now, so the constraints exist before their rules are set
    # (otherwise the plain constraints would be added afterwards, next to ours).
    if schema_editor.deferred_sql:
        for statement in list(schema_editor.deferred_sql):
            schema_editor.execute(statement)
        schema_editor.deferred_sql.clear()
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


def forwards_for(foreign_keys):
    """A ``RunPython`` forward that gives ``foreign_keys`` their ON DELETE rules."""
    def forwards(apps, schema_editor):
        set_rules(schema_editor, foreign_keys, lambda rule: rule)
    return forwards


def backwards_for(foreign_keys):
    """The matching reverse: plain constraints again."""
    def backwards(apps, schema_editor):
        set_rules(schema_editor, foreign_keys, lambda rule: None)
    return backwards
