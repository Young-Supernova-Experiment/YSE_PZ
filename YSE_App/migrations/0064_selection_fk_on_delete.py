"""MySQL ON DELETE on the foreign keys of ``YSE_App_selection`` (#841), as 0062 does for 0056-0061.

0063 created the table. Its foreign keys point at ``auth_user``, ``auth_group``, ``explorer_query``
and ``YSE_App_userquery``, which develop deletes from on the shared database without knowing this
table. Each MySQL constraint gets the model's rule (``YSE_App.common.fk_on_delete``). No row or
column of any table changes.
"""

from django.db import migrations

from YSE_App.common import fk_on_delete
from YSE_App.common.fk_on_delete import CASCADE, SET_NULL

# (table, column, referenced table, ON DELETE rule); YSE_App/tests/test_selections.py checks it
# against the model.
FOREIGN_KEYS = (
    ("YSE_App_selection", "owner_id", "auth_user", CASCADE),
    ("YSE_App_selection", "team_id", "auth_group", SET_NULL),
    ("YSE_App_selection", "query_id", "explorer_query", SET_NULL),
    ("YSE_App_selection", "user_query_id", "YSE_App_userquery", SET_NULL),
    ("YSE_App_selection", "modified_by_id", "auth_user", SET_NULL),
)

forwards = fk_on_delete.forwards(FOREIGN_KEYS)
backwards = fk_on_delete.backwards(FOREIGN_KEYS)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ("YSE_App", "0063_selection"),
    ]

    operations = [
        # shared-db-safe: only sets the ON DELETE rule of the foreign keys of YSE_App_selection (created by 0063), which develop's code never reads or writes; no row or column of any other table changes (#841, as 0062)
        migrations.RunPython(forwards, backwards),
    ]
