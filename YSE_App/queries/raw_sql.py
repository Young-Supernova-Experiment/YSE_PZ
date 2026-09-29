"""
Raw SQL fragments the ORM code annotates transient querysets with, each with
the text it replaced (``*_LEGACY``) so the change can be reverted or diffed.

Every fragment is a scalar subquery correlated on ``YSE_App_transient.id``
(the outer queryset's table). The legacy texts joined ``YSE_App_transient t``
to the outer row by primary key and then looked the chosen photometry row up
again by ``pd.id = (SELECT pd2.id ... ORDER BY ... LIMIT 1)``: three to five
extra primary-key lookups per outer row per annotation, on top of the one
dependent ``ORDER BY ... LIMIT`` subquery that does the real work. The
replacements select the wanted column from that ordered subquery directly.
The candidate rows, the ORDER BY key and the LIMIT/OFFSET are unchanged, so
the row each fragment picks is the same row and the value is identical
(including the undefined choice between two points with the same obs_date,
which neither version pins down). Measured plans and the row-for-row checks:
``docs/sql-rewrites-2026-09-29.md`` and
``YSE_App/tests/test_sql_rewrites_equivalence.py``.
"""

# ---- most recent magnitude of a transient (any band, flagged data included) -
# Used by the ``order_recent_mag`` column sorters in table_utils, the yse_views
# field tables and the transient_summary ``last_mag`` sort.

RECENT_MAG_SQL_LEGACY = """
SELECT pd.mag
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id
         ORDER BY pd2.obs_date DESC
         LIMIT 1
     )
"""

RECENT_MAG_SQL = """
SELECT pd2.mag
   FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
   WHERE pd2.photometry_id = p2.id AND p2.transient_id = YSE_App_transient.id
   ORDER BY pd2.obs_date DESC
   LIMIT 1
"""

# ---- days since discovery ---------------------------------------------------
# yse_views annotates ``days_since_disc`` with a self-join on the primary key;
# the column is on the outer row already.

DAYS_SINCE_DISC_SQL_LEGACY = """SELECT DATEDIFF(curdate(), t.disc_date) as days_since_disc
FROM YSE_App_transient t WHERE YSE_App_transient.id = t.id"""

DAYS_SINCE_DISC_SQL = "DATEDIFF(CURDATE(), YSE_App_transient.disc_date)"

# ---- n-th most recent point in a band group (rising-transient queries) ------
# ``% (column_expression, band_name_tuple_sql, offset)``. The column expression
# refers to the chosen photometry row as ``pd`` in both versions.

LAST_MAG_BY_BAND_SQL_LEGACY = """
SELECT %s
   FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p, YSE_App_photometricband pb, YSE_App_instrument i
   WHERE pd.photometry_id = p.id AND
   YSE_App_transient.id = t.id AND
   pb.instrument_id	= i.id AND
   pd.band_id = pb.id AND
   pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2 , YSE_App_photometricband pb2, YSE_App_instrument i2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id AND pd2.band_id = pb2.id AND pb2.instrument_id = i2.id AND i2.name != 'Gaia-Photometric' AND
         pb2.name IN %s
         ORDER BY pd2.obs_date DESC
         LIMIT 1 OFFSET %i
     )
"""

LAST_MAG_BY_BAND_SQL = """
SELECT %s
   FROM YSE_App_transientphotdata pd, YSE_App_transientphotometry p, YSE_App_photometricband pb, YSE_App_instrument i
   WHERE pd.photometry_id = p.id AND p.transient_id = YSE_App_transient.id AND
   pd.band_id = pb.id AND pb.instrument_id = i.id AND i.name != 'Gaia-Photometric' AND
   pb.name IN %s
   ORDER BY pd.obs_date DESC
   LIMIT 1 OFFSET %i
"""
