"""Per-user default site for "Observable tonight" (#959).

Additive only: one nullable column, ``observing_default_site_id``, on ``YSE_App_userpreference``
(a table develop knows). develop's INSERTs leave it NULL. Its MySQL foreign key gets
``ON DELETE SET NULL`` (``YSE_App.common.fk_on_delete``, as in 0062), so develop can still
delete a telescope on the shared database: develop's Django does not know the column and
emulates nothing for it. No existing row is changed.
"""

import django.db.models.deletion
from django.db import migrations, models

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
        ("YSE_App", "0062_shared_db_fk_on_delete"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpreference",
            name="observing_default_site",
            field=models.ForeignKey(
                blank=True,
                help_text="Site that Observable tonight starts from for this user.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="YSE_App.telescope",
            ),
        ),
        # shared-db-safe: changes only the ON DELETE rule of the foreign key of the column added just above (Section 5, as in 0062); no row or other column changes (#959)
        migrations.RunPython(forwards, backwards),
    ]
