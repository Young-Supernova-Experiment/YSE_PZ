"""``quality_mask`` and friends on photometry and spectra (#80, #81).

Adds:

* ``quality_mask`` (``BIGINT UNSIGNED NOT NULL DEFAULT 0``) to
  TransientPhotData, HostPhotData, TransientSpectrum and HostSpectrum;
* ``pixel_mask`` (``SMALLINT UNSIGNED NOT NULL DEFAULT 0``) to
  TransientSpecData and HostSpecData;
* nullable ``wavelength_medium`` (both spectrum tables), ``host_subtraction``
  (TransientSpectrum) and ``phot_type`` (HostPhotData).

Shared ``YSE_test`` (#395): the DEFAULTs are real MySQL column defaults, so
``develop``'s INSERTs, which do not name these columns, still work (the
``0030_shared_db_column_defaults`` lesson). Django's own ``AddField`` would
drop the DEFAULT again, so on MySQL the columns are added with explicit SQL
(``SeparateDatabaseAndState``) and no CHECK constraint (UNSIGNED already
refuses negatives; adding a CHECK would make MySQL validate every row).

Size: TransientSpecData is very large. Each ALTER asks MySQL for
``ALGORITHM=INSTANT`` (8.0.12+: a metadata-only change, seconds whatever the
table size). If MySQL refuses INSTANT for a table it says why; the migration
then logs a warning and repeats the ALTER without the clause, which may copy
the table (see the runbook). ``lock_wait_timeout`` is 60 s so a long-running
query on a table makes the ALTER fail instead of queueing the site behind it.

Then a data step fills ``quality_mask`` for every row that has a DataQuality
(a few seconds: only the through tables' rows are read) and sets
WAVELENGTH_MEDIUM_UNKNOWN on every spectrum (one UPDATE per spectrum table).
``python manage.py backfill_quality_mask`` re-checks everything and should
report nothing to change afterwards.

``atomic = False``: MySQL cannot roll DDL back. Re-running after a partial
failure skips columns that already exist. The reverse drops the columns.
"""

import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)

WAVELENGTH_MEDIUM_CHOICES = [('vacuum', 'Vacuum'), ('air', 'Air'), ('unknown', 'Unknown')]
HOST_SUBTRACTION_CHOICES = [
    ('local', 'Local'), ('archival', 'Archival'), ('template', 'Template'),
    ('none', 'None'), ('unknown', 'Unknown'),
]
PHOT_TYPE_CHOICES = [
    ('total', 'Total'), ('r25', 'R25'), ('kron', 'Kron'), ('petrosian', 'Petrosian'),
    ('aperture', 'Aperture'), ('model', 'Model'), ('psf', 'PSF'), ('other', 'Other'),
]

MASK = ('bigint UNSIGNED NOT NULL DEFAULT 0', 'bigint NOT NULL DEFAULT 0')
PIXEL = ('smallint UNSIGNED NOT NULL DEFAULT 0', 'smallint NOT NULL DEFAULT 0')
CHOICE = ('varchar(16) NULL', 'varchar(16) NULL')

# table -> [(column, (mysql definition, generic definition))]
COLUMNS = {
    'YSE_App_transientphotdata': [('quality_mask', MASK)],
    'YSE_App_hostphotdata': [('quality_mask', MASK), ('phot_type', CHOICE)],
    'YSE_App_transientspectrum': [
        ('quality_mask', MASK), ('wavelength_medium', CHOICE), ('host_subtraction', CHOICE),
    ],
    'YSE_App_hostspectrum': [('quality_mask', MASK), ('wavelength_medium', CHOICE)],
    'YSE_App_transientspecdata': [('pixel_mask', PIXEL)],
    'YSE_App_hostspecdata': [('pixel_mask', PIXEL)],
}

# MySQL: "ALGORITHM=INSTANT is not supported for this operation" (1845, 1846).
_INSTANT_REFUSED = {1845, 1846}


def _existing_columns(schema_editor, table):
    with schema_editor.connection.cursor() as cursor:
        return {c.name for c in schema_editor.connection.introspection.get_table_description(cursor, table)}


def _mysql_alter(schema_editor, table, clauses):
    sql = 'ALTER TABLE `%s` %s' % (table, ', '.join(clauses))
    try:
        schema_editor.execute(sql + ', ALGORITHM=INSTANT')
    except Exception as exc:  # OperationalError / NotSupportedError, by driver
        code = exc.args[0] if exc.args else None
        # also "Maximum row versions reached ... cannot be added or dropped instantly" (8.0.29+)
        if code not in _INSTANT_REFUSED and 'INSTANT' not in str(exc).upper():
            raise
        logger.warning('0036: MySQL refused ALGORITHM=INSTANT on %s (%s); retrying with the default algorithm',
                       table, exc)
        schema_editor.execute(sql)


def add_columns(apps, schema_editor):
    mysql = schema_editor.connection.vendor == 'mysql'
    if mysql:
        schema_editor.execute('SET SESSION lock_wait_timeout = 60')
    for table, columns in COLUMNS.items():
        have = _existing_columns(schema_editor, table)
        todo = [(name, defs) for name, defs in columns if name not in have]
        if not todo:
            continue
        if mysql:
            _mysql_alter(schema_editor, table, ['ADD COLUMN `%s` %s' % (name, defs[0]) for name, defs in todo])
        else:
            for name, defs in todo:
                schema_editor.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, name, defs[1]))


def drop_columns(apps, schema_editor):
    mysql = schema_editor.connection.vendor == 'mysql'
    for table, columns in COLUMNS.items():
        have = _existing_columns(schema_editor, table)
        todo = [name for name, _ in columns if name in have]
        if not todo:
            continue
        if mysql:
            _mysql_alter(schema_editor, table, ['DROP COLUMN `%s`' % name for name in todo])
        else:
            for name in todo:
                schema_editor.execute('ALTER TABLE "%s" DROP COLUMN "%s"' % (table, name))


def backfill(apps, schema_editor):
    from YSE_App.common import quality_mask_sync

    for name in ('TransientPhotData', 'HostPhotData', 'TransientSpectrum', 'HostSpectrum'):
        model = apps.get_model('YSE_App', name)
        n = quality_mask_sync.backfill_flagged(model)
        logger.info('0036: quality_mask set on %d flagged %s row(s)', n, name)
    for name in ('TransientSpectrum', 'HostSpectrum'):
        model = apps.get_model('YSE_App', name)
        n = quality_mask_sync.backfill_wavelength_medium(model)
        logger.info('0036: WAVELENGTH_MEDIUM_UNKNOWN set on %d %s row(s)', n, name)


def _mask_field():
    return models.PositiveBigIntegerField(default=0)


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('YSE_App', '0035_feedback'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_columns, drop_columns)],
            state_operations=[
                migrations.AddField(model_name='transientphotdata', name='quality_mask', field=_mask_field()),
                migrations.AddField(model_name='hostphotdata', name='quality_mask', field=_mask_field()),
                migrations.AddField(model_name='transientspectrum', name='quality_mask', field=_mask_field()),
                migrations.AddField(model_name='hostspectrum', name='quality_mask', field=_mask_field()),
                migrations.AddField(
                    model_name='hostphotdata', name='phot_type',
                    field=models.CharField(blank=True, choices=PHOT_TYPE_CHOICES, max_length=16, null=True),
                ),
                migrations.AddField(
                    model_name='transientspectrum', name='wavelength_medium',
                    field=models.CharField(blank=True, choices=WAVELENGTH_MEDIUM_CHOICES, default='unknown',
                                           max_length=16, null=True),
                ),
                migrations.AddField(
                    model_name='hostspectrum', name='wavelength_medium',
                    field=models.CharField(blank=True, choices=WAVELENGTH_MEDIUM_CHOICES, default='unknown',
                                           max_length=16, null=True),
                ),
                migrations.AddField(
                    model_name='transientspectrum', name='host_subtraction',
                    field=models.CharField(blank=True, choices=HOST_SUBTRACTION_CHOICES, max_length=16, null=True),
                ),
                migrations.AddField(
                    model_name='transientspecdata', name='pixel_mask',
                    field=models.PositiveSmallIntegerField(default=0),
                ),
                migrations.AddField(
                    model_name='hostspecdata', name='pixel_mask',
                    field=models.PositiveSmallIntegerField(default=0),
                ),
            ],
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
