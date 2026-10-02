# SQL rewrites, 2026-09-29 (issue #332)

Ryan Foley asked for a sweep of the SQL the app runs: find the inefficient statements, rewrite them to be
faster **only where the output is identical**, and log every change so it can be reverted. This file is that log.

Rule applied to every candidate: `EXPLAIN ANALYZE` old and new on the synthetic docker MySQL 8.0.25 dataset
(35 000 transients, 40 000 photometry rows, 800 000 photometry points, 28 000 YSE-tagged, 128 MB buffer pool),
once with today's production index set (primary and foreign keys only) and once with the eight indexes migration
`0010_dashboard_indexes` adds; diff the full result lists (sorted, and in returned order); keep the rewrite only
when the plan improves and both diffs are empty. The script is `docs/perf/sql_sweep_measure.py`
and its full output (every plan, old and new, before and after the indexes) is `docs/perf/sql_sweep_plans.md`; the same checks run
as tests in `YSE_App/tests/test_sql_rewrites_equivalence.py` on a fixture that exercises every predicate.
Anything that could not be proven identical is listed under **Not changed** with the reason.

## Summary

| # | query | before: old ms | before: new ms | after 0010: old ms | after 0010: new ms | rows | identical |
|---|---|---|---|---|---|---|---|
| 1 | saved: YSE Magnitude-Limited Sample (min mag < 18.6) | 3559 | 730 | 4549 | 2403 | 27998 | yes |
| 2 | saved: Fast & Young Target Search | 1656 | 417 | 2606 | 349 | 255 | yes |
| 3 | saved: New Transients Last Two Days | 1600 | 411 | 2579 | 300 | 451 | yes |
| 4 | saved: Interesting New Targets | 3201 | 2746 | 4147 | 3763 | 799 | yes |
| 5 | code: recent_mag RawSQL (12 copies), 2000 rows | 466 | 309 | 183 | 135 | 2000 | yes |
| 6 | code: days_since_disc RawSQL (5 copies), 2000 rows | 18 | 15 | 22 | 18 | 2000 | yes |
| 7 | code: rising-transient last_mag_query (24 annotations), 10 of them on 400 rows | 442 | 141 | 466 | 208 | 400 | yes |

"before" = today's indexes, "after 0010" = with the indexes in migration `0010_dashboard_indexes`. Wall time of the
statement itself, best of two warm runs. The synthetic dataset is 1/10-1/50 of production photometry and fits in
RAM, so these are seconds where Ziggy showed minutes (216-299 s per saved query in Ryan's HAR); the plan shapes
are what carry over. Row 1's "after" numbers are slower than its "before" numbers for both texts because the
`(photometry_id, obs_date)` index tempts the optimizer into an index-ordered join for this query; the rewrite is
still 1.9x faster than the original there and 4.9x without that index. The production `EXPLAIN` in
`docs/dashboard-performance.md` decides the final index list.

## Saved queries (Explorer `explorer_query` rows, changed by `manage.py rewrite_dashboard_queries`)

The command matches rows by title and changes a row only when its current text is exactly (whitespace aside)
the "Old SQL" below; `--apply` writes the previous text of every changed row to a timestamped JSON file first.
The constants and the equivalence notes are in `YSE_App/queries/dashboard_saved_queries.py`.

## 1. YSE Magnitude-Limited Sample (min mag < 18.6)

**Location:** saved query (fixture id 62); Ryan's dashboard section 472

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 3559 ms -> 730 ms; with migration 0010's indexes 4549 ms -> 2403 ms
(27998 rows; identical row set and identical order).

**Why it is faster:** the old text runs one dependent subquery per YSE-tagged transient (`pd.id = (SELECT pd2.id ... ORDER BY pd2.mag ASC LIMIT 1)`, 84k executions for 28k transients, each a filesort of that transient's light curve). The new text computes MIN(mag) per transient in one GROUP BY over the clean points (`HAVING MIN(pd2.mag) < 18.6`) and joins transient/tags/name pattern afterwards. Plan: `Select #2 (subquery in condition; dependent) ... Sort: pd2.mag, limit input to 1 row(s) per chunk (loops=83998)` becomes one `Aggregate using temporary table` over an index lookup on the photometry rows.

**Equivalence check:** the brightest clean point's `pd.mag` is by definition `MIN(pd2.mag)` under the same filters (`mag IS NOT NULL`, `flux/flux_err > 3`, no data-quality flag); the YSE tag is unique per transient so both texts return one row per qualifying transient. Sorted rows identical on the synthetic data (27 998 rows) and on the test fixture (flagged brightest point, low S/N bright point, untagged, non-matching name, only-flagged, no photometry). No ORDER BY in the original, so order is undefined in both. The fixture dump's text uses `ISNULL(pd2.data_quality_id) = True` (pre-migration schema); the command carries whichever data-quality predicate the saved row uses into the rewrite (`MAG_LIMITED_REWRITE` / `MAG_LIMITED_REWRITE_M2M`).

**Old SQL:**
```sql
SELECT t.name, pd.mag, t.ra, t.dec
	FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p, YSE_App_transient_tags tt, YSE_App_transienttag tg
    WHERE pd.photometry_id = p.id AND tg.name = 'YSE' AND pd.mag < 18.6 AND 
    tt.transient_id = t.id AND tg.id = tt.transienttag_id AND
    tt.transient_id = t.id AND tg.id = tt.transienttag_id AND
    pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id AND NOT EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality dq WHERE dq.transientphotdata_id = pd2.id) AND 
      ISNULL(pd2.mag) = False AND pd2.flux/pd2.flux_err > 3
         ORDER BY pd2.mag ASC
         LIMIT 1
     )
     AND (t.name LIKE '202%' OR t.name LIKE '201%')
```

**New SQL:**
```sql
SELECT t.name, g.min_mag AS mag, t.ra, t.dec
FROM (SELECT p2.transient_id, MIN(pd2.mag) AS min_mag
      FROM YSE_App_transientphotdata pd2
      JOIN YSE_App_transientphotometry p2 ON pd2.photometry_id = p2.id
      WHERE pd2.mag IS NOT NULL AND pd2.flux/pd2.flux_err > 3 AND NOT EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality dq WHERE dq.transientphotdata_id = pd2.id)
      GROUP BY p2.transient_id
      HAVING MIN(pd2.mag) < 18.6) g
JOIN YSE_App_transient t ON t.id = g.transient_id
JOIN YSE_App_transient_tags tt ON tt.transient_id = t.id
JOIN YSE_App_transienttag tg ON tg.id = tt.transienttag_id AND tg.name = 'YSE'
WHERE (t.name LIKE '202%' OR t.name LIKE '201%')
```

**Revert:** `python manage.py rewrite_dashboard_queries --revert <backup.json> --apply` (the JSON file `--apply` printed; the dry run shows the diff first). Or paste the old SQL above into the Explorer editor for that saved query.


## 2. Fast & Young Target Search

**Location:** saved query (fixture id 254); Ryan's dashboard section 480

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 1656 ms -> 417 ms; with migration 0010's indexes 2606 ms -> 349 ms
(255 rows; identical row set and identical order).

**Why it is faster:** the derived table `g` aggregated MIN/MAX/COUNT(obs_date) over every photometry row of every transient (full scan, `Aggregate using temporary table` over 794k rows) to keep the few transients whose latest detection is within 3 days. The new text adds `AND t.id IN (SELECT ... WHERE pd2.obs_date >= CURDATE() - INTERVAL 2 DAY)` to the aggregate, so only transients with a point in that window are aggregated (1 656 of 35 000 here; far fewer on production). With the `obs_date` index the prefilter is an index range scan.

**Equivalence check:** the added predicate is a necessary condition of the outer `TO_DAYS(CURDATE()) - TO_DAYS(latest_detection) < 3`: `latest_detection` is the MAX obs_date of the transient's qualifying points, so a transient passing the outer filter has a point dated on/after CURDATE() - 2 days; for every kept transient the aggregate runs over exactly the same rows, so first/latest detection and the count are unchanged. Everything else is the original text. Ordered rows identical (255 rows synthetic; fixture covers the 8-day/3-day/2-detection/SN Ia boundaries).

**Old SQL:**
```sql
SELECT DISTINCT	t.name,
				t.TNS_spec_class AS `classification`,
                g.first_detection AS `first_detection`,
                g.latest_detection AS `latest_detection`,
                g.number_of_detection AS `number_of_detection`,
                og.name AS `group_name`
FROM (SELECT DISTINCT	t.id,
                MIN(pd.obs_date) AS `first_detection`,
                MAX(pd.obs_date) AS `latest_detection`,
      			COUNT(pd.obs_date) AS `number_of_detection`
FROM YSE_App_transient t
INNER JOIN YSE_App_transientphotometry tp ON tp.transient_id = t.id
INNER JOIN YSE_App_transientphotdata pd ON pd.photometry_id = tp.id
WHERE (pd.flux/pd.flux_err > 2 OR pd.mag_err< 0.2 OR ((pd.mag_err IS NULL) AND (pd.mag IS NOT NULL)))
GROUP BY t.id) g
INNER JOIN YSE_App_transient t ON t.id=g.id
INNER JOIN YSE_App_observationgroup og ON og.id = t.obs_group_id
WHERE t.TNS_spec_class IS NULL
-- AND t.name LIKE '2021%' OR t.name LIKE '%YSE%'
AND TO_DAYS(CURDATE())- TO_DAYS(first_detection) < 8
AND TO_DAYS(CURDATE())- TO_DAYS(latest_detection) < 3
AND TO_DAYS(latest_detection) - TO_DAYS(first_detection) > 0.01
AND number_of_detection > 2
ORDER BY g.first_detection DESC
```

**New SQL:**
```sql
SELECT DISTINCT	t.name,
				t.TNS_spec_class AS `classification`,
                g.first_detection AS `first_detection`,
                g.latest_detection AS `latest_detection`,
                g.number_of_detection AS `number_of_detection`,
                og.name AS `group_name`
FROM (SELECT DISTINCT	t.id,
                MIN(pd.obs_date) AS `first_detection`,
                MAX(pd.obs_date) AS `latest_detection`,
      			COUNT(pd.obs_date) AS `number_of_detection`
FROM YSE_App_transient t
INNER JOIN YSE_App_transientphotometry tp ON tp.transient_id = t.id
INNER JOIN YSE_App_transientphotdata pd ON pd.photometry_id = tp.id
WHERE (pd.flux/pd.flux_err > 2 OR pd.mag_err< 0.2 OR ((pd.mag_err IS NULL) AND (pd.mag IS NOT NULL)))
AND t.id IN (SELECT tp2.transient_id FROM YSE_App_transientphotometry tp2
             INNER JOIN YSE_App_transientphotdata pd2 ON pd2.photometry_id = tp2.id
             WHERE pd2.obs_date >= CURDATE() - INTERVAL 2 DAY)
GROUP BY t.id) g
INNER JOIN YSE_App_transient t ON t.id=g.id
INNER JOIN YSE_App_observationgroup og ON og.id = t.obs_group_id
WHERE t.TNS_spec_class IS NULL
-- AND t.name LIKE '2021%' OR t.name LIKE '%YSE%'
AND TO_DAYS(CURDATE())- TO_DAYS(first_detection) < 8
AND TO_DAYS(CURDATE())- TO_DAYS(latest_detection) < 3
AND TO_DAYS(latest_detection) - TO_DAYS(first_detection) > 0.01
AND number_of_detection > 2
ORDER BY g.first_detection DESC
```

**Revert:** `python manage.py rewrite_dashboard_queries --revert <backup.json> --apply` (the JSON file `--apply` printed; the dry run shows the diff first). Or paste the old SQL above into the Explorer editor for that saved query.


## 3. New Transients Last Two Days

**Location:** saved query (fixture id 217); Ryan's dashboard section 483

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 1600 ms -> 411 ms; with migration 0010's indexes 2579 ms -> 300 ms
(451 rows; identical row set and identical order).

**Why it is faster:** same shape as Fast & Young with a 15-day / 2-day window and `mag_err < 0.35`; the prefilter uses `INTERVAL 1 DAY`.

**Equivalence check:** as for Fast & Young with N = 2 (a point on/after CURDATE() - 1 day). Ordered rows identical (451 rows synthetic; fixture).

**Old SQL:**
```sql
SELECT DISTINCT	t.name,
				t.TNS_spec_class AS `classification`,
                g.first_detection AS `first_detection`,
                g.latest_detection AS `latest_detection`,
                g.number_of_detection AS `number_of_detection`,
                og.name AS `group_name`
FROM (SELECT DISTINCT	t.id,
                MIN(pd.obs_date) AS `first_detection`,
                MAX(pd.obs_date) AS `latest_detection`,
      			COUNT(pd.obs_date) AS `number_of_detection`
FROM YSE_App_transient t
INNER JOIN YSE_App_transientphotometry tp ON tp.transient_id = t.id
INNER JOIN YSE_App_transientphotdata pd ON pd.photometry_id = tp.id
WHERE (pd.flux/pd.flux_err > 2 OR pd.mag_err< 0.35 OR ((pd.mag_err IS NULL) AND (pd.mag IS NOT NULL)))
GROUP BY t.id) g

INNER JOIN YSE_App_transient t ON t.id=g.id
INNER JOIN YSE_App_observationgroup og ON og.id = t.obs_group_id
WHERE (t.TNS_spec_class != 'SN Ia' OR t.TNS_spec_class IS NULL)
AND TO_DAYS(CURDATE())- TO_DAYS(first_detection) < 15
AND TO_DAYS(CURDATE())- TO_DAYS(latest_detection) < 2
AND number_of_detection > 2

ORDER BY g.first_detection DESC
```

**New SQL:**
```sql
SELECT DISTINCT	t.name,
				t.TNS_spec_class AS `classification`,
                g.first_detection AS `first_detection`,
                g.latest_detection AS `latest_detection`,
                g.number_of_detection AS `number_of_detection`,
                og.name AS `group_name`
FROM (SELECT DISTINCT	t.id,
                MIN(pd.obs_date) AS `first_detection`,
                MAX(pd.obs_date) AS `latest_detection`,
      			COUNT(pd.obs_date) AS `number_of_detection`
FROM YSE_App_transient t
INNER JOIN YSE_App_transientphotometry tp ON tp.transient_id = t.id
INNER JOIN YSE_App_transientphotdata pd ON pd.photometry_id = tp.id
WHERE (pd.flux/pd.flux_err > 2 OR pd.mag_err< 0.35 OR ((pd.mag_err IS NULL) AND (pd.mag IS NOT NULL)))
AND t.id IN (SELECT tp2.transient_id FROM YSE_App_transientphotometry tp2
             INNER JOIN YSE_App_transientphotdata pd2 ON pd2.photometry_id = tp2.id
             WHERE pd2.obs_date >= CURDATE() - INTERVAL 1 DAY)
GROUP BY t.id) g

INNER JOIN YSE_App_transient t ON t.id=g.id
INNER JOIN YSE_App_observationgroup og ON og.id = t.obs_group_id
WHERE (t.TNS_spec_class != 'SN Ia' OR t.TNS_spec_class IS NULL)
AND TO_DAYS(CURDATE())- TO_DAYS(first_detection) < 15
AND TO_DAYS(CURDATE())- TO_DAYS(latest_detection) < 2
AND number_of_detection > 2

ORDER BY g.first_detection DESC
```

**Revert:** `python manage.py rewrite_dashboard_queries --revert <backup.json> --apply` (the JSON file `--apply` printed; the dry run shows the diff first). Or paste the old SQL above into the Explorer editor for that saved query.


## 4. Interesting New Targets

**Location:** saved query (fixture id 11); Ryan's dashboard section 481

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 3201 ms -> 2746 ms; with migration 0010's indexes 4147 ms -> 3763 ms
(799 rows; identical row set and identical order).

**Why it is faster:** `g1` computed MIN(mag) over every transient's photometry and the `t.status_id = 1` filter was applied only outside. The new text repeats `AND a3.status_id = 1` inside `g1`, so only status-1 transients' photometry is aggregated. The gain is modest on the synthetic data because 55% of its transients are status 1; on production `New` is a small fraction of all transients, so the aggregate shrinks accordingly.

**Equivalence check:** `g1` groups by transient and the outer join needs `g1.transient_id = t.id AND t.status_id = 1`, so groups of other statuses could never contribute a row; the remaining groups aggregate the same rows (the predicate is on the transient). SELECT list, other predicates and ORDER BY unchanged. Ordered rows identical (799 rows synthetic; fixture covers bright/status-1, faint-but-nearby-host, other status, mw_ebv >= 0.5, two points sharing the MIN mag).

**Old SQL:**
```sql
SELECT DISTINCT t.name,
       t.id,
       t.ra,
       t.dec,
       t.disc_date,
       t.status_id,
       pb.name AS `filter`,
       g1.min_mag AS 'Recent mag',
       pd.obs_date
FROM YSE_App_transientphotdata pd
INNER JOIN YSE_App_transientphotometry tp ON pd.photometry_id = tp.id
INNER JOIN YSE_App_transient t ON tp.transient_id = t.id
INNER JOIN YSE_App_photometricband pb ON pb.id = pd.band_id
INNER JOIN YSE_App_host h ON h.id = t.host_id
INNER JOIN
  (SELECT a3.id AS transient_id,
          MIN(a1.mag) AS min_mag
   FROM YSE_App_transientphotdata a1
   INNER JOIN YSE_App_transientphotometry a2 ON a1.photometry_id = a2.id
   INNER JOIN YSE_App_transient a3 ON a2.transient_id = a3.id
   WHERE mag IS NOT NULL
   GROUP BY a3.id) AS g1 ON g1.min_mag = pd.mag
AND g1.transient_id = t.id
WHERE t.status_id = 1
  AND t.mw_ebv < 0.5
  AND (g1.min_mag < 17
  OR  ( COALESCE(t.redshift, h.redshift) > 0
  AND COALESCE(t.redshift, h.redshift) <= 0.01
  AND (ACOS(SIN(RADIANS(t.`dec`))*SIN(RADIANS(h.`dec`)) + COS(RADIANS(t.`dec`))*COS(RADIANS(h.`dec`))*COS(RADIANS(ABS(t.ra - h.ra))))*(3e+5*COALESCE(t.redshift, h.redshift)/73)/POW((1.0 + COALESCE(t.redshift, h.redshift)), 2)*1000) < 40 ) )
ORDER BY t.name DESC,
         pd.obs_date ASC;
```

**New SQL:**
```sql
SELECT DISTINCT t.name,
       t.id,
       t.ra,
       t.dec,
       t.disc_date,
       t.status_id,
       pb.name AS `filter`,
       g1.min_mag AS 'Recent mag',
       pd.obs_date
FROM YSE_App_transientphotdata pd
INNER JOIN YSE_App_transientphotometry tp ON pd.photometry_id = tp.id
INNER JOIN YSE_App_transient t ON tp.transient_id = t.id
INNER JOIN YSE_App_photometricband pb ON pb.id = pd.band_id
INNER JOIN YSE_App_host h ON h.id = t.host_id
INNER JOIN
  (SELECT a3.id AS transient_id,
          MIN(a1.mag) AS min_mag
   FROM YSE_App_transientphotdata a1
   INNER JOIN YSE_App_transientphotometry a2 ON a1.photometry_id = a2.id
   INNER JOIN YSE_App_transient a3 ON a2.transient_id = a3.id
   WHERE mag IS NOT NULL
     AND a3.status_id = 1
   GROUP BY a3.id) AS g1 ON g1.min_mag = pd.mag
AND g1.transient_id = t.id
WHERE t.status_id = 1
  AND t.mw_ebv < 0.5
  AND (g1.min_mag < 17
  OR  ( COALESCE(t.redshift, h.redshift) > 0
  AND COALESCE(t.redshift, h.redshift) <= 0.01
  AND (ACOS(SIN(RADIANS(t.`dec`))*SIN(RADIANS(h.`dec`)) + COS(RADIANS(t.`dec`))*COS(RADIANS(h.`dec`))*COS(RADIANS(ABS(t.ra - h.ra))))*(3e+5*COALESCE(t.redshift, h.redshift)/73)/POW((1.0 + COALESCE(t.redshift, h.redshift)), 2)*1000) < 40 ) )
ORDER BY t.name DESC,
         pd.obs_date ASC;
```

**Revert:** `python manage.py rewrite_dashboard_queries --revert <backup.json> --apply` (the JSON file `--apply` printed; the dry run shows the diff first). Or paste the old SQL above into the Explorer editor for that saved query.


## Code SQL (`RawSQL` fragments; constants in `YSE_App/queries/raw_sql.py`)

Each fragment is a scalar subquery correlated on `YSE_App_transient.id`. The legacy texts joined
`YSE_App_transient t` to the outer row by primary key and then looked the chosen photometry row up again by
`pd.id = (SELECT pd2.id ... ORDER BY ... LIMIT 1)`: three to five extra primary-key lookups per outer row per
annotation on top of the one dependent subquery that does the work. The new texts select the wanted column
from that ordered subquery directly; candidate rows, ORDER BY key and LIMIT/OFFSET are unchanged, so the same
row is picked and the value is identical (including the undefined choice between two points with the same
obs_date, which neither version pins down).

## 5. recent_mag column sorter

**Location:** `YSE_App/table_utils.py` (`order_recent_mag` in TransientTable, FieldTransientTable, AdjustFieldTransientTable, YSETransientTable, YSEFullTransientTable, YSERisingTransientTable, NewTransientTable: 7 copies), `YSE_App/yse_views.py` (`recent_mag_raw_query`, 4 copies in yse_sky / yse_planning), `YSE_App/views.py` (`transient_summary` `last_mag` sort)

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 466 ms -> 309 ms; with migration 0010's indexes 183 ms -> 135 ms
(2000 rows; identical row set and identical order).

**Why it is faster:** per outer row the plan drops the `t` primary-key lookup, the `pd` lookup by the subquery's id and the `p` lookup (`Single-row index lookup on t using PRIMARY`, `... on pd using PRIMARY (id=(select #N))`, `... on p using PRIMARY`) and keeps only the ordered index lookup on the transient's photometry rows.

**Equivalence check:** same candidate rows (`pd2.photometry_id = p2.id AND p2.transient_id = <outer id>`), same `ORDER BY pd2.obs_date DESC LIMIT 1`; the legacy text returned `pd.mag` of the row with that id, the new text returns `pd2.mag` of that row. 2 000 annotated rows identical in value and order on the synthetic data; test fixture covers two photometry rows per transient, flagged data (included by both, as before) and no photometry (NULL).

**Old SQL:**
```sql
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
```

**New SQL:**
```sql
SELECT pd2.mag
   FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
   WHERE pd2.photometry_id = p2.id AND p2.transient_id = YSE_App_transient.id
   ORDER BY pd2.obs_date DESC
   LIMIT 1
```

**Revert:** in `table_utils.py`, `yse_views.py` and `views.py` change the constant back to its `*_LEGACY` neighbour in `YSE_App/queries/raw_sql.py` (the replaced text is kept there verbatim), or `git revert` the sweep commit of the PR that merged issue #332.


## 6. days_since_disc annotation

**Location:** `YSE_App/yse_views.py` (`days_from_disc_query`: yse_sky, yse_planning x3, yse_fields)

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 18 ms -> 15 ms; with migration 0010's indexes 22 ms -> 18 ms
(2000 rows; identical row set and identical order).

**Why it is faster:** the legacy text was a self-join on the primary key to read a column that is already on the outer row; the new text is the expression itself, so the dependent subquery disappears from the plan.

**Equivalence check:** `YSE_App_transient.id = t.id` matches exactly one row (the outer row), so `t.disc_date` is `YSE_App_transient.disc_date`; `DATEDIFF(curdate(), ...)` unchanged. 2 000 rows identical (values and order). MySQL-only functions, so the test runs on MySQL.

**Old SQL:**
```sql
SELECT DATEDIFF(curdate(), t.disc_date) as days_since_disc
FROM YSE_App_transient t WHERE YSE_App_transient.id = t.id
```

**New SQL:**
```sql
DATEDIFF(CURDATE(), YSE_App_transient.disc_date)
```

**Revert:** in `yse_views.py` change the constant back to its `*_LEGACY` neighbour in `YSE_App/queries/raw_sql.py` (the replaced text is kept there verbatim), or `git revert` the sweep commit of the PR that merged issue #332.


## 7. rising-transient band annotations

**Location:** `YSE_App/queries/yse_python_queries.py` (`last_mag_query` in `annotate_rising_transient_qs`, used by rising_transient_queryset, fastrising_transient_queryset, tns_yse_rising_transient_queryset, recent_rising_transient_queryset: 24 annotations per row)

**Measured (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows; wall time, warm cache):**
today's indexes 442 ms -> 141 ms; with migration 0010's indexes 466 ms -> 208 ms
(400 rows; identical row set and identical order).

**Why it is faster:** per annotation and outer row the legacy plan performed five primary-key lookups (t, pd, p, pb, i) after the dependent subquery; the new plan is the subquery alone. Ten of the 24 annotations on 400 rows: 442 ms -> 141 ms (3.1x).

**Equivalence check:** candidate rows identical (`p.transient_id = <outer id>`, band in the list, instrument not Gaia-Photometric), same `ORDER BY pd.obs_date DESC LIMIT 1 OFFSET n`; the selected expression refers to the chosen row as `pd` in both texts. All twelve column/band/offset combinations identical on the synthetic data and on the fixture (Gaia point excluded, g-ZTF as second-most-recent g, i band, missing offset -> NULL).

**Old SQL:**
```sql
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
```

**New SQL:**
```sql
SELECT %s
   FROM YSE_App_transientphotdata pd, YSE_App_transientphotometry p, YSE_App_photometricband pb, YSE_App_instrument i
   WHERE pd.photometry_id = p.id AND p.transient_id = YSE_App_transient.id AND
   pd.band_id = pb.id AND pb.instrument_id = i.id AND i.name != 'Gaia-Photometric' AND
   pb.name IN %s
   ORDER BY pd.obs_date DESC
   LIMIT 1 OFFSET %i
```

**Revert:** in `yse_python_queries.py` change the constant back to its `*_LEGACY` neighbour in `YSE_App/queries/raw_sql.py` (the replaced text is kept there verbatim), or `git revert` the sweep commit of the PR that merged issue #332.


## Not changed (and why)

| query | location | reason |
|---|---|---|
| YSE Volume-Limited Sample (z < 0.06) (fixture id 61; Ryan's section 470) | saved query | touches no photometry: one pass over transient x host x tags, 175 ms synthetic (141 ms with 0010). Its 255 s in the HAR is most plausibly I/O contention with the four photometry queries running at the same time; confirm with a solo `EXPLAIN ANALYZE` on Ziggy before touching it. |
| YSE Forced Phot Only, Auto Ignore (Ryan's sections 468, 469) | saved queries | text exists only in the production `explorer_query` table (not in the fixture dump), so nothing could be measured or proven. See the dump command below. |
| transients with YSE forced photometry / YSE forced photometry for non-YSE(, non-Antares) transients (fixture ids 83-85) | saved queries | `t2.id IN (SELECT DISTINCT t.id ... JOIN transientdiffimage ... GROUP BY t.id HAVING count(t.id) >= 1)` is an `EXISTS` in disguise and a candidate, but the synthetic dataset has no `transientdiffimage` rows, so neither the plan gain nor the equivalence could be measured. Second pass, with production-shaped data. |
| the other ~300 saved queries in the fixture dump | saved queries | not on a dashboard (no `UserQuery` in the dump points at them) and not exercised in the HAR; out of scope for this pass. |
| `box_search`, `sne_15deg_from_yse`, `sne_in_yse_field*` (`data_utils.py`, `yse_python_queries.py`) | code, `cursor.execute` | `SELECT t.name, ACOS(...) AS sep FROM YSE_App_transient t WHERE <ra/dec box> HAVING sep < N`: the box predicate becomes an index range with 0010's `ra`/`dec` indexes; moving the `HAVING` alias into `WHERE` would evaluate the same trigonometry per row and change nothing measurable. |
| `_recent_phot_subqueries` (`table_utils.py`), `annotate_dashboard_transient_fields` | code, ORM `Subquery` | already the direct shape (`WHERE photometry__transient = OuterRef ORDER BY -obs_date LIMIT 1`); benefits from `(photometry_id, obs_date)` without a text change. PR #257's page-first slice already limits it to the page rows. |
| `order_recent_magdate` (`table_utils.py`) | code, ORM `Max()` | ORM aggregate, not raw SQL; not measured as slow. |
| `Transient.recent_mag()` and friends (`models/transient_models.py`) | code, ORM | per-row ORM queries on the detail page; ORM, not raw SQL; out of scope. |
| `YSE_App/perf/mag_limited.py` benchmark SQL | perf harness | intentionally the old shape: it measures it. |
| `docs/example_queries.rst` examples 3 and 4 | documentation | not executed by the app. Example 3 still references `pd2.data_quality_id`, a column the migrated schema no longer has; follow-up to update the examples to the shapes above (or to the PhotStat table, #270). |

## Saved queries whose SQL could not be seen

The text of the `explorer_query` rows on Ziggy is not in the repository. Ryan's sections 468 (`YSE Forced Phot
Only`) and 469 (`Auto Ignore`) are known only by title, and 470/472/480/481/483 may have drifted from the fixture
dump (the command reports a row whose text differs from the expected original and leaves it alone). To dump them
for a second pass (replace `$DB`, `$USER`):

```bash
mysql -u $USER -p $DB -e "SELECT q.id, q.title, q.sql FROM explorer_query q JOIN YSE_App_userquery uq ON uq.query_id = q.id GROUP BY q.id, q.title, q.sql\G" > dashboard_saved_queries.txt
# or every saved query:
mysql -u $USER -p $DB -e "SELECT id, title, sql FROM explorer_query\G" > all_saved_queries.txt
```

`python manage.py rewrite_dashboard_queries --dry-run` also prints the current text of the REVIEW titles
(468/469/470) so no separate dump is needed for those three.
