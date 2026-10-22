"""SQL view ``yse_transient_photstat_v`` for SQL Explorer users (#270).

One row per ``TransientPhotStat`` with the transient name and band names
joined in, so an Explorer query can read the stored photometry statistics
without repeating the photometry subqueries. Adds no column or table, so it
is safe on the shared ``YSE_test`` database. The Bazin ``extrapolated_*``
columns (0033) are left out: derived values get no new exposure until #607
computes them per visibility scope.

MySQL only (``CREATE OR REPLACE VIEW``); other backends skip it. Creating a
view needs the ``CREATE VIEW`` privilege. When the migrating database user
lacks it, the migration logs a warning and is recorded as applied, so a
deploy never stops on it; the runbook then has the statement to run as a
privileged user. The reverse drops the view.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

VIEW_NAME = 'yse_transient_photstat_v'

CREATE_VIEW_SQL = """
CREATE OR REPLACE VIEW yse_transient_photstat_v AS
SELECT
    ps.transient_id AS transient_id,
    t.name AS transient_name,
    ps.num_obs_global AS num_obs,
    ps.num_det_global AS num_det,
    ps.num_limits_global AS num_limits,
    ps.last_obs_mjd AS last_obs_mjd,
    ps.last_obs_date AS last_obs_date,
    ps.first_detected_mjd AS first_detected_mjd,
    ps.first_detected_date AS first_detected_date,
    ps.first_detected_mag AS first_detected_mag,
    fb.name AS first_detected_band,
    ps.last_detected_mjd AS last_detected_mjd,
    ps.last_detected_date AS last_detected_date,
    ps.last_detected_mag AS last_detected_mag,
    lb.name AS last_detected_band,
    ps.peak_mjd AS peak_mjd,
    ps.peak_date AS peak_date,
    ps.peak_mag AS peak_mag,
    pb.name AS peak_band,
    ps.mean_mag AS mean_mag,
    ps.faintest_mag AS faintest_mag,
    ps.deepest_limit AS deepest_limit,
    ps.deepest_limit_mjd AS deepest_limit_mjd,
    db.name AS deepest_limit_band,
    ps.last_non_detection_mjd AS last_non_detection_mjd,
    nb.name AS last_non_detection_band,
    ps.time_to_non_detection AS time_to_non_detection,
    ps.rise_rate AS rise_rate,
    ps.decay_rate AS decay_rate,
    ps.schema_version AS schema_version,
    ps.last_updated AS last_updated
FROM YSE_App_transientphotstat ps
JOIN YSE_App_transient t ON t.id = ps.transient_id
LEFT JOIN YSE_App_photometricband fb ON fb.id = ps.first_detected_band_id
LEFT JOIN YSE_App_photometricband lb ON lb.id = ps.last_detected_band_id
LEFT JOIN YSE_App_photometricband pb ON pb.id = ps.peak_band_id
LEFT JOIN YSE_App_photometricband db ON db.id = ps.deepest_limit_band_id
LEFT JOIN YSE_App_photometricband nb ON nb.id = ps.last_non_detection_band_id
"""

DROP_VIEW_SQL = 'DROP VIEW IF EXISTS yse_transient_photstat_v'

# MySQL: 1142 command denied, 1227 access denied (missing privilege).
_PRIVILEGE_ERRORS = (1142, 1227)


def _execute(schema_editor, sql):
    if schema_editor.connection.vendor != 'mysql':
        return
    from django.db.utils import OperationalError, ProgrammingError

    try:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)
    except (OperationalError, ProgrammingError) as exc:
        code = exc.args[0] if exc.args else None
        if code not in _PRIVILEGE_ERRORS:
            raise
        logger.warning(
            '%s not changed: the database user lacks the privilege (%s). '
            'See docs/ziggy-runbook.md (photometry statistics view).', VIEW_NAME, exc,
        )


def create_view(apps, schema_editor):
    _execute(schema_editor, CREATE_VIEW_SQL)


def drop_view(apps, schema_editor):
    _execute(schema_editor, DROP_VIEW_SQL)


class Migration(migrations.Migration):

    # The view statement is DDL (an implicit commit on MySQL).
    atomic = False

    dependencies = [
        ('YSE_App', '0033_photstat_extrapolated'),
    ]

    operations = [
        migrations.RunPython(create_view, drop_view),
    ]
