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

from django.db import migrations

# The helper moved to YSE_App.common.fk_on_delete when 0064 needed it too; the names stay
# importable from here for the tests.
from YSE_App.common.fk_on_delete import (  # noqa: F401
    CASCADE,
    PLAIN_RULES,
    SET_NULL,
    apply,
    current_constraints,
    revert,
)

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


def forwards(apps, schema_editor):
    apply(schema_editor, FOREIGN_KEYS)


def backwards(apps, schema_editor):
    revert(schema_editor, FOREIGN_KEYS)


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
