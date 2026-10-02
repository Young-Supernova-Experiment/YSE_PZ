"""Database-level defaults for NOT NULL columns that older code inserts without (#395).

Django keeps a field's ``default`` in Python only: ``AddField(default=...)`` fills
existing rows and then drops the DEFAULT from the column. ``yse_test`` (develop)
and ``yse_experimental`` share the ``YSE_test`` database, so once experimental has
added a NOT NULL column, develop's code -- which does not know the field -- leaves
it out of its INSERTs and MySQL strict mode refuses the row::

    IntegrityError (1364, "Field 'usage_hours' doesn't have a default value")

That broke "Add follow-up request" on yse_test. Production has the same window
between ``migrate`` and the Apache restart. This migration gives those columns a
MySQL DEFAULT equal to the model default so rows written by code that predates
them still insert. MySQL only (the CI and Ziggy backend); a no-op elsewhere.

Covered: the NOT NULL columns added by 0011-0029 to tables that existed before
them (new tables are written only by code that knows their fields).
``Telescope.weather`` (a TEXT column, 0022) is left out: MySQL TEXT defaults need
8.0.13 expression syntax, and telescopes are created in the admin, not by
legacy views.
"""

from django.db import migrations

# (table, column, column definition including the new DEFAULT)
COLUMN_DEFAULTS = [
    ("YSE_App_transientfollowup", "usage_hours", "double NOT NULL DEFAULT 0"),
    ("YSE_App_telescope", "weather_url", "varchar(500) NOT NULL DEFAULT ''"),
    ("YSE_App_telescope", "weather_link", "varchar(500) NOT NULL DEFAULT ''"),
    ("YSE_App_telescope", "skycam_url", "varchar(500) NOT NULL DEFAULT ''"),
]


def _alter(schema_editor, with_default):
    if schema_editor.connection.vendor != "mysql":
        return
    for table, column, definition in COLUMN_DEFAULTS:
        if not with_default:
            definition = definition.split(" DEFAULT ")[0]
        schema_editor.execute(f"ALTER TABLE `{table}` MODIFY `{column}` {definition}")


def add_defaults(apps, schema_editor):
    _alter(schema_editor, with_default=True)


def drop_defaults(apps, schema_editor):
    _alter(schema_editor, with_default=False)


class Migration(migrations.Migration):

    dependencies = [
        ("YSE_App", "0029_broker_streams"),
    ]

    operations = [
        migrations.RunPython(add_defaults, drop_defaults),
    ]
