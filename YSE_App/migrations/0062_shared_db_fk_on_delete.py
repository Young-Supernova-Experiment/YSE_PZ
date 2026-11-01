"""Database-level ON DELETE on the foreign keys of experimental's new tables (#941).

``/yse_test/`` runs develop's code on the shared ``YSE_test`` database, and develop does not
know the tables that 0056-0061 created. Django emulates ``on_delete`` in Python, only for the
models it knows, and creates the MySQL foreign keys without an ``ON DELETE`` clause (that is,
RESTRICT). So when develop deletes a spectrum, transient, host, user or group that one of these
tables still references, MySQL rejects the DELETE (error 1451).

This migration re-creates those MySQL constraints so that the database does what the model
says: ``CASCADE`` becomes ``ON DELETE CASCADE`` and ``SET_NULL`` becomes ``ON DELETE SET NULL``.
Django keeps emulating both, which is harmless. Foreign keys between the new tables themselves
(for example a catalogue quantity's entry) are included too, because MySQL's cascade from a
develop table runs through them. PROTECT foreign keys (the ``created_by`` / ``modified_by``
audit columns of the catalogue tables) are left as they are.

Only constraints on tables created by 0056-0061 are touched; no table that develop's migrations
created, and no row, is changed. Constraint names are read from ``information_schema`` at run
time and kept. Running it again does nothing. On other database backends it does nothing.
The reverse puts the plain constraints back.
"""

import hashlib

from django.db import migrations

CASCADE = "CASCADE"
SET_NULL = "SET NULL"

# (table, column, referenced table, ON DELETE rule). Every referenced column is ``id``.
# Enumerated from migrations 0056, 0057, 0059 and 0061 (0058 and 0060 create no table);
# YSE_App/tests/test_shared_db_fk_on_delete.py checks this list against the models.
FOREIGN_KEYS = (
    # 0056_spectrum_arrays
    ("YSE_App_transientspectrumarrays", "spectrum_id", "YSE_App_transientspectrum", CASCADE),
    ("YSE_App_hostspectrumarrays", "spectrum_id", "YSE_App_hostspectrum", CASCADE),
    # 0057_catalog_import
    ("YSE_App_catalogentry", "transient_id", "YSE_App_transient", CASCADE),
    ("YSE_App_catalogentrysource", "entry_id", "YSE_App_catalogentry", CASCADE),
    ("YSE_App_catalogentrysource", "source_id", "YSE_App_catalogsource", CASCADE),
    ("YSE_App_transientcatalogquantity", "entry_id", "YSE_App_catalogentry", CASCADE),
    ("YSE_App_transientcatalogquantity", "transient_id", "YSE_App_transient", CASCADE),
    ("YSE_App_transientcatalogquantity_sources", "transientcatalogquantity_id",
     "YSE_App_transientcatalogquantity", CASCADE),
    ("YSE_App_transientcatalogquantity_sources", "catalogsource_id", "YSE_App_catalogsource", CASCADE),
    ("YSE_App_transientphotometrycataloginfo", "entry_id", "YSE_App_catalogentry", CASCADE),
    ("YSE_App_transientphotometrycataloginfo", "photometry_id", "YSE_App_transientphotometry", CASCADE),
    ("YSE_App_transientphotometrycataloginfo_sources", "transientphotometrycataloginfo_id",
     "YSE_App_transientphotometrycataloginfo", CASCADE),
    ("YSE_App_transientphotometrycataloginfo_sources", "catalogsource_id", "YSE_App_catalogsource", CASCADE),
    ("YSE_App_transientphotdatacataloginfo", "phot_data_id", "YSE_App_transientphotdata", CASCADE),
    ("YSE_App_transientspectrumcataloginfo", "entry_id", "YSE_App_catalogentry", CASCADE),
    ("YSE_App_transientspectrumcataloginfo", "spectrum_id", "YSE_App_transientspectrum", CASCADE),
    ("YSE_App_transientspectrumcataloginfo_sources", "transientspectrumcataloginfo_id",
     "YSE_App_transientspectrumcataloginfo", CASCADE),
    ("YSE_App_transientspectrumcataloginfo_sources", "catalogsource_id", "YSE_App_catalogsource", CASCADE),
    ("YSE_App_catalogdatamatch", "catalog_phot_data_id", "YSE_App_transientphotdata", CASCADE),
    ("YSE_App_catalogdatamatch", "catalog_spectrum_id", "YSE_App_transientspectrum", CASCADE),
    ("YSE_App_catalogdatamatch", "entry_id", "YSE_App_catalogentry", CASCADE),
    ("YSE_App_catalogdatamatch", "existing_phot_data_id", "YSE_App_transientphotdata", CASCADE),
    ("YSE_App_catalogdatamatch", "existing_spectrum_id", "YSE_App_transientspectrum", CASCADE),
    ("YSE_App_cataloginstrumentalias", "yse_instrument_id", "YSE_App_instrument", CASCADE),
    # 0059_team_deputies_requests
    ("YSE_App_groupprofile_deputies", "groupprofile_id", "YSE_App_groupprofile", CASCADE),
    ("YSE_App_groupprofile_deputies", "user_id", "auth_user", CASCADE),
    # 0061_duplicate_data_decision
    ("YSE_App_duplicatedatadecision", "kept_phot_data_id", "YSE_App_transientphotdata", SET_NULL),
    ("YSE_App_duplicatedatadecision", "kept_spectrum_id", "YSE_App_transientspectrum", SET_NULL),
    ("YSE_App_duplicatedatadecision", "phot_data_id", "YSE_App_transientphotdata", CASCADE),
    ("YSE_App_duplicatedatadecision", "spectrum_id", "YSE_App_transientspectrum", CASCADE),
    ("YSE_App_duplicatedatadecision", "transient_id", "YSE_App_transient", CASCADE),
)

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


def _set_rules(schema_editor, target):
    """Make every listed constraint's ON DELETE ``target(rule)`` (None means plain)."""
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    q = schema_editor.quote_name
    for table, column, ref_table, rule in FOREIGN_KEYS:
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


def forwards(apps, schema_editor):
    _set_rules(schema_editor, lambda rule: rule)


def backwards(apps, schema_editor):
    _set_rules(schema_editor, lambda rule: None)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ("YSE_App", "0061_duplicate_data_decision"),
    ]

    operations = [
        # shared-db-safe: changes only the ON DELETE rule of foreign keys on tables created by 0056-0061, which develop's code never reads or writes; no row or column changes (#941)
        migrations.RunPython(forwards, backwards),
    ]
