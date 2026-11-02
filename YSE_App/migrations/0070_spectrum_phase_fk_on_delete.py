"""ON DELETE CASCADE on the foreign keys of ``YSE_App_spectrumphase``, created by 0069 (#848).

A separate migration: Django adds the foreign-key constraints of a CreateModel as deferred SQL at
the end of its migration, so a RunPython in 0069 would run before they exist. Only the two
constraints of the new table change; no row, column or existing table.
"""

from django.db import migrations

from YSE_App.common import fk_on_delete
from YSE_App.common.fk_on_delete import CASCADE

# (table, column, referenced table, ON DELETE rule); test_spectral_phase.py checks it against the model.
FOREIGN_KEYS = (
    ("YSE_App_spectrumphase", "spectrum_id", "YSE_App_transientspectrum", CASCADE),
    ("YSE_App_spectrumphase", "transient_id", "YSE_App_transient", CASCADE),
)

forwards = fk_on_delete.forwards(FOREIGN_KEYS)
backwards = fk_on_delete.backwards(FOREIGN_KEYS)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ('YSE_App', '0069_spectrum_phase'),
    ]

    operations = [
        # shared-db-safe: only sets ON DELETE CASCADE on the two foreign keys of the table 0069 created, which develop's code never reads or writes; no row or column of an existing table changes (#848)
        migrations.RunPython(forwards, backwards),
    ]
