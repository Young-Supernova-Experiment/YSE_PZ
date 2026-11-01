"""Database-level ON DELETE SET NULL for ``UserPreference.observing_default_site`` (#959).

The column was added in 0063. develop's Django does not know it and emulates nothing for it, so
without this rule develop could not delete a telescope on the shared database (error 1451).
Uses ``YSE_App.common.fk_on_delete`` as 0062 does. A separate migration because Django creates
0063's constraint from deferred SQL at the end of that migration. No row or column changes.
"""

from django.db import migrations

from YSE_App.common.fk_on_delete import SET_NULL, apply, revert

# (table, column, referenced table, ON DELETE rule); the referenced column is ``id``.
FOREIGN_KEYS = (
    ("YSE_App_userpreference", "observing_default_site_id", "YSE_App_telescope", SET_NULL),
)


def forwards(apps, schema_editor):
    apply(schema_editor, FOREIGN_KEYS)


def backwards(apps, schema_editor):
    revert(schema_editor, FOREIGN_KEYS)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ("YSE_App", "0063_userpreference_observing_default_site"),
    ]

    operations = [
        # shared-db-safe: changes only the ON DELETE rule of the foreign key of the column added in 0063 (Section 5, as in 0062); no row or other column changes (#959)
        migrations.RunPython(forwards, backwards),
    ]
