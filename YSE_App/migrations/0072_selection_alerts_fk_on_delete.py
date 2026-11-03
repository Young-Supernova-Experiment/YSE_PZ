"""MySQL ON DELETE on the foreign keys of the Selection alert tables (#844), as 0066 does for Selections.

0071 created ``YSE_App_selectionalert`` and ``YSE_App_selectionalertsnapshot``. Their foreign keys
point at ``auth_user`` and ``auth_group``, which develop deletes from on the shared database
without knowing these tables, and at ``YSE_App_selection``, whose rows MySQL deletes by cascade
when develop deletes their owner (0066). Each MySQL constraint gets the model's rule
(``YSE_App.common.fk_on_delete``). A separate migration: Django adds the constraints of a
CreateModel only at the end of its migration (#961). No row or column of any table changes.
"""

from django.db import migrations

from YSE_App.common import fk_on_delete
from YSE_App.common.fk_on_delete import CASCADE

# (table, column, referenced table, ON DELETE rule); YSE_App/tests/test_selection_alerts.py checks it
# against the models.
FOREIGN_KEYS = (
    ("YSE_App_selectionalert", "selection_id", "YSE_App_selection", CASCADE),
    ("YSE_App_selectionalert", "owner_id", "auth_user", CASCADE),
    ("YSE_App_selectionalert", "team_id", "auth_group", CASCADE),
    ("YSE_App_selectionalertsnapshot", "alert_id", "YSE_App_selectionalert", CASCADE),
    ("YSE_App_selectionalertsnapshot", "recipient_id", "auth_user", CASCADE),
)

forwards = fk_on_delete.forwards(FOREIGN_KEYS)
backwards = fk_on_delete.backwards(FOREIGN_KEYS)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ("YSE_App", "0071_selection_alerts"),
    ]

    operations = [
        # shared-db-safe: only sets ON DELETE CASCADE on the foreign keys of the two tables 0071 created, which develop's code never reads or writes; no row or column of any other table changes (#844, as 0066)
        migrations.RunPython(forwards, backwards),
    ]
