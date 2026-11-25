"""ON DELETE CASCADE on the foreign keys of the fit tables created by 0063 (#846).

A separate migration: Django adds the foreign-key constraints of a CreateModel as deferred SQL
at the end of its migration, so a RunPython in 0063 would run before they exist. Only the four
constraints of the two new tables change; no row, column or existing table.
"""

from django.db import migrations

from YSE_App.common import fk_on_delete
from YSE_App.common.fk_on_delete import CASCADE

# (table, column, referenced table, ON DELETE rule); test_fit_tables.py checks it against the models.
FOREIGN_KEYS = (
    ("YSE_App_bazinfit", "run_id", "YSE_App_externalservicerun", CASCADE),
    ("YSE_App_bazinfit", "transient_id", "YSE_App_transient", CASCADE),
    ("YSE_App_saltfit", "run_id", "YSE_App_externalservicerun", CASCADE),
    ("YSE_App_saltfit", "transient_id", "YSE_App_transient", CASCADE),
)

forwards = fk_on_delete.forwards(FOREIGN_KEYS)
backwards = fk_on_delete.backwards(FOREIGN_KEYS)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ('YSE_App', '0063_fit_tables'),
    ]

    operations = [
        # shared-db-safe: only sets ON DELETE CASCADE on the foreign keys of the two tables 0063 created, which develop's code never reads or writes; no row or column of an existing table changes (#846)
        migrations.RunPython(forwards, backwards),
    ]
