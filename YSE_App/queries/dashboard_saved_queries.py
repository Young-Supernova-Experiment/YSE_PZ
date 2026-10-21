"""
Faster, result-identical rewrites of the saved Explorer queries pinned to the
personal dashboard (``explorer_query`` rows, matched by title).

Each :class:`SavedQueryRewrite` carries the exact original text(s) the rewrite
is proven against and the replacement text. ``manage.py
rewrite_dashboard_queries`` only touches a saved query whose current SQL is
one of the known originals (whitespace-normalised, see :func:`normalize_sql`);
anything else is reported and left alone, so a query someone has edited by
hand is never overwritten. The module is Django-free so the constants can be
measured against a bare MySQL connection.

Why these queries are slow (EXPLAIN ANALYZE on MySQL 8.0.25, 35k transients /
800k photometry rows; full plans in ``docs/sql-rewrites-2026-09-29.md``): every
one rebuilds a per-transient photometry summary from the raw
``YSE_App_transientphotdata`` table on each run, either as one dependent
``ORDER BY ... LIMIT 1`` subquery per transient or as a ``GROUP BY`` over the
whole table. The rewrites keep the same predicates and the same result rows
but let MySQL do one pass over the rows that can possibly qualify.

Equivalence arguments are in each rewrite's ``note``; the row-for-row checks
are ``YSE_App/tests/test_sql_rewrites_equivalence.py`` (MySQL) and the
synthetic-data diff recorded in the docs file above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")


def normalize_sql(sql: str) -> str:
    """Collapse whitespace and drop a trailing semicolon so cosmetic edits
    (indentation, CRLF, a final ``;``) do not defeat the original-text match."""
    text = _WS_RE.sub(" ", (sql or "").replace("\r\n", "\n")).strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


@dataclass(frozen=True)
class SavedQueryRewrite:
    title: str
    originals: Tuple[str, ...]
    rewritten: str
    note: str
    fixture_id: Optional[int] = None
    # Row-set/ordering guarantee the rewrite makes, for the docs and tests.
    order_preserved: bool = True
    extra_titles: Tuple[str, ...] = field(default_factory=tuple)

    def classify(self, sql: str) -> str:
        """'rewritten' | 'original' | 'unknown' for the saved text ``sql``."""
        norm = normalize_sql(sql)
        if norm == normalize_sql(self.rewritten):
            return "rewritten"
        if any(norm == normalize_sql(o) for o in self.originals):
            return "original"
        return "unknown"

    def matches_title(self, title: str) -> bool:
        wanted = {self.title.strip().lower(), *(t.strip().lower() for t in self.extra_titles)}
        return (title or "").strip().lower() in wanted


# --------------------------------------------------------------------------- #
# 62  YSE Magnitude-Limited Sample (min mag < 18.6)
# --------------------------------------------------------------------------- #

# The data-quality predicate differs between the pre-migration schema (a
# ``data_quality_id`` column on transientphotdata) and the migrated schema (a
# ``..._data_quality`` join table). The rewrite keeps whichever predicate the
# saved text uses, so the result is identical on either schema.
_DQ_COLUMN = "ISNULL(pd2.data_quality_id) = True"
_DQ_M2M = (
    "NOT EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality dq "
    "WHERE dq.transientphotdata_id = pd2.id)"
)

_MAG_LIMITED_ORIGINAL_TEMPLATE = """SELECT t.name, pd.mag, t.ra, t.dec
	FROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p, YSE_App_transient_tags tt, YSE_App_transienttag tg
    WHERE pd.photometry_id = p.id AND tg.name = 'YSE' AND pd.mag < 18.6 AND 
    tt.transient_id = t.id AND tg.id = tt.transienttag_id AND
    tt.transient_id = t.id AND tg.id = tt.transienttag_id AND
    pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id AND {dq} AND 
      ISNULL(pd2.mag) = False AND pd2.flux/pd2.flux_err > 3
         ORDER BY pd2.mag ASC
         LIMIT 1
     )
     AND (t.name LIKE '202%' OR t.name LIKE '201%')"""

_MAG_LIMITED_REWRITE_TEMPLATE = """SELECT t.name, g.min_mag AS mag, t.ra, t.dec
FROM (SELECT p2.transient_id, MIN(pd2.mag) AS min_mag
      FROM YSE_App_transientphotdata pd2
      JOIN YSE_App_transientphotometry p2 ON pd2.photometry_id = p2.id
      WHERE pd2.mag IS NOT NULL AND pd2.flux/pd2.flux_err > 3 AND {dq}
      GROUP BY p2.transient_id
      HAVING MIN(pd2.mag) < 18.6) g
JOIN YSE_App_transient t ON t.id = g.transient_id
JOIN YSE_App_transient_tags tt ON tt.transient_id = t.id
JOIN YSE_App_transienttag tg ON tg.id = tt.transienttag_id AND tg.name = 'YSE'
WHERE (t.name LIKE '202%' OR t.name LIKE '201%')"""

MAG_LIMITED_ORIGINAL = _MAG_LIMITED_ORIGINAL_TEMPLATE.format(dq=_DQ_COLUMN)
MAG_LIMITED_ORIGINAL_M2M = _MAG_LIMITED_ORIGINAL_TEMPLATE.format(dq=_DQ_M2M)
MAG_LIMITED_REWRITE = _MAG_LIMITED_REWRITE_TEMPLATE.format(dq=_DQ_COLUMN)
MAG_LIMITED_REWRITE_M2M = _MAG_LIMITED_REWRITE_TEMPLATE.format(dq=_DQ_M2M)

MAG_LIMITED_NOTE = """\
Original: for every YSE-tagged transient the dependent subquery sorts that
transient's whole light curve by mag to find the brightest clean (flux/flux_err
> 3, mag not NULL, no data-quality flag) point, then the outer query keeps the
transient when that point is < 18.6. MySQL ran the subquery 84k times for 28k
transients (5.4 s synthetic; minutes on Ziggy).
Rewrite: one GROUP BY over the clean points gives MIN(mag) per transient
(HAVING < 18.6), then joins transient/tags/name pattern. Same predicates,
applied once. The selected columns are the same: the original's pd.mag is the
mag of the brightest clean point, which is exactly MIN(pd2.mag) under the same
filters (a row per transient in both; the YSE tag is unique per transient).
The original has no ORDER BY, so row order is undefined in both versions."""

# --------------------------------------------------------------------------- #
# 254 Fast & Young Target Search / 217 New Transients Last Two Days
# --------------------------------------------------------------------------- #

_AGGREGATE_TEMPLATE = """SELECT DISTINCT	t.name,
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
WHERE (pd.flux/pd.flux_err > 2 OR pd.mag_err< {mag_err} OR ((pd.mag_err IS NULL) AND (pd.mag IS NOT NULL))){prefilter}
GROUP BY t.id) g
{between}INNER JOIN YSE_App_transient t ON t.id=g.id
INNER JOIN YSE_App_observationgroup og ON og.id = t.obs_group_id
WHERE {outer_where}
AND TO_DAYS(CURDATE())- TO_DAYS(first_detection) < {first_days}
AND TO_DAYS(CURDATE())- TO_DAYS(latest_detection) < {latest_days}
{tail}AND number_of_detection > 2
{blank}ORDER BY g.first_detection DESC"""


def _recent_point_prefilter(latest_days: int) -> str:
    # Necessary condition of the outer ``TO_DAYS(CURDATE()) - TO_DAYS(latest_detection) < N``:
    # the transient has at least one photometry point dated on/after CURDATE() - (N-1) days.
    return (
        "\nAND t.id IN (SELECT tp2.transient_id FROM YSE_App_transientphotometry tp2"
        "\n             INNER JOIN YSE_App_transientphotdata pd2 ON pd2.photometry_id = tp2.id"
        "\n             WHERE pd2.obs_date >= CURDATE() - INTERVAL %d DAY)" % (latest_days - 1)
    )


def _fast_young(prefilter: bool) -> str:
    return _AGGREGATE_TEMPLATE.format(
        mag_err="0.2",
        prefilter=_recent_point_prefilter(3) if prefilter else "",
        between="",
        outer_where="t.TNS_spec_class IS NULL\n-- AND t.name LIKE '2021%' OR t.name LIKE '%YSE%'",
        first_days=8,
        latest_days=3,
        tail="AND TO_DAYS(latest_detection) - TO_DAYS(first_detection) > 0.01\n",
        blank="",
    )


def _new_two_days(prefilter: bool) -> str:
    return _AGGREGATE_TEMPLATE.format(
        mag_err="0.35",
        prefilter=_recent_point_prefilter(2) if prefilter else "",
        between="\n",
        outer_where="(t.TNS_spec_class != 'SN Ia' OR t.TNS_spec_class IS NULL)",
        first_days=15,
        latest_days=2,
        tail="",
        blank="\n",
    )


FAST_YOUNG_ORIGINAL = _fast_young(False)
FAST_YOUNG_REWRITE = _fast_young(True)
NEW_TWO_DAYS_ORIGINAL = _new_two_days(False)
NEW_TWO_DAYS_REWRITE = _new_two_days(True)

_AGGREGATE_NOTE = """\
Original: the derived table g aggregates MIN/MAX/COUNT(obs_date) over every
photometry row of every transient (full scan of transientphotdata) and only
then keeps the transients whose latest detection is within the last {n} days.
Rewrite: the inner aggregate is restricted to transients that have at least
one photometry point with obs_date >= CURDATE() - INTERVAL {m} DAY. That is a
necessary condition of the outer filter TO_DAYS(CURDATE()) - TO_DAYS(latest_detection) < {n}
(latest_detection is the MAX obs_date of the transient's qualifying points, so
a transient passing the outer filter has a qualifying point, hence a point, in
that window), so no transient that passes the original filter is dropped, and
for every kept transient the aggregate runs over exactly the same rows, so
first_detection / latest_detection / number_of_detection are unchanged.
Everything else (SELECT list, joins, remaining predicates, ORDER BY) is the
original text. With an index on transientphotdata(obs_date) the prefilter is
an index range scan instead of a table scan."""

FAST_YOUNG_NOTE = _AGGREGATE_NOTE.format(n=3, m=2)
NEW_TWO_DAYS_NOTE = _AGGREGATE_NOTE.format(n=2, m=1)

# --------------------------------------------------------------------------- #
# 11  Interesting New Targets
# --------------------------------------------------------------------------- #

_INTERESTING_TEMPLATE = """SELECT DISTINCT t.name,
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
   WHERE mag IS NOT NULL{pushdown}
   GROUP BY a3.id) AS g1 ON g1.min_mag = pd.mag
AND g1.transient_id = t.id
WHERE t.status_id = 1
  AND t.mw_ebv < 0.5
  AND (g1.min_mag < 17
  OR  ( COALESCE(t.redshift, h.redshift) > 0
  AND COALESCE(t.redshift, h.redshift) <= 0.01
  AND (ACOS(SIN(RADIANS(t.`dec`))*SIN(RADIANS(h.`dec`)) + COS(RADIANS(t.`dec`))*COS(RADIANS(h.`dec`))*COS(RADIANS(ABS(t.ra - h.ra))))*(3e+5*COALESCE(t.redshift, h.redshift)/73)/POW((1.0 + COALESCE(t.redshift, h.redshift)), 2)*1000) < 40 ) )
ORDER BY t.name DESC,
         pd.obs_date ASC;"""

INTERESTING_ORIGINAL = _INTERESTING_TEMPLATE.format(pushdown="")
INTERESTING_REWRITE = _INTERESTING_TEMPLATE.format(pushdown="\n     AND a3.status_id = 1")

INTERESTING_NOTE = """\
Original: g1 computes MIN(mag) over every photometry row of every transient,
then joins back to the photometry table on the magnitude value; the
``t.status_id = 1`` filter is applied only outside.
Rewrite: the same ``status_id = 1`` predicate is added inside g1. g1 groups by
transient, and the outer join requires g1.transient_id = t.id AND
t.status_id = 1, so groups for transients with any other status can never
contribute a row; the groups that remain aggregate exactly the same rows
(the predicate is on the transient, not on the photometry rows). SELECT list,
remaining predicates and ORDER BY are unchanged, so both the row set and the
order are identical. MySQL then reads only the status-1 transients'
photometry instead of the whole table."""

# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

REWRITES: Sequence[SavedQueryRewrite] = (
    SavedQueryRewrite(
        title="YSE Magnitude-Limited Sample (min mag < 18.6)",
        fixture_id=62,
        originals=(MAG_LIMITED_ORIGINAL, MAG_LIMITED_ORIGINAL_M2M),
        rewritten=MAG_LIMITED_REWRITE,  # replaced per matched original, see rewrite_for()
        note=MAG_LIMITED_NOTE,
        order_preserved=False,  # original has no ORDER BY
    ),
    SavedQueryRewrite(
        title="Fast & Young Target Search",
        fixture_id=254,
        originals=(FAST_YOUNG_ORIGINAL,),
        rewritten=FAST_YOUNG_REWRITE,
        note=FAST_YOUNG_NOTE,
    ),
    SavedQueryRewrite(
        title="New Transients Last Two Days",
        fixture_id=217,
        originals=(NEW_TWO_DAYS_ORIGINAL,),
        rewritten=NEW_TWO_DAYS_REWRITE,
        note=NEW_TWO_DAYS_NOTE,
    ),
    SavedQueryRewrite(
        title="Interesting New Targets",
        fixture_id=11,
        originals=(INTERESTING_ORIGINAL,),
        rewritten=INTERESTING_REWRITE,
        note=INTERESTING_NOTE,
    ),
)

# Ryan's dashboard titles whose SQL exists only in the production explorer_query
# table (not in the fixture dump). The command prints their current text for a
# second pass instead of guessing. Section 470 (Volume-Limited) is listed too:
# it touches no photometry and needs no rewrite (175 ms synthetic).
REVIEW_TITLES: Tuple[str, ...] = (
    "YSE Forced Phot Only",
    "Auto Ignore",
    "YSE Volume-Limited Sample (z < 0.06)",
)


def find_rewrite(title: str) -> Optional[SavedQueryRewrite]:
    for rw in REWRITES:
        if rw.matches_title(title):
            return rw
    return None


def rewrite_for(rw: SavedQueryRewrite, current_sql: str) -> Optional[str]:
    """The replacement text for ``current_sql``, or None when the text is not a
    known original. For the Magnitude-Limited query the data-quality predicate
    of the matched original is carried into the rewrite."""
    if rw.classify(current_sql) != "original":
        return None
    if rw.fixture_id == 62:
        if normalize_sql(current_sql) == normalize_sql(MAG_LIMITED_ORIGINAL_M2M):
            return MAG_LIMITED_REWRITE_M2M
        return MAG_LIMITED_REWRITE
    return rw.rewritten


def rewritten_texts(rw: SavedQueryRewrite) -> Tuple[str, ...]:
    """Every text this rewrite may install (used to recognise 'already rewritten')."""
    if rw.fixture_id == 62:
        return (MAG_LIMITED_REWRITE, MAG_LIMITED_REWRITE_M2M)
    return (rw.rewritten,)


def classify_saved_sql(rw: SavedQueryRewrite, sql: str) -> str:
    norm = normalize_sql(sql)
    if any(norm == normalize_sql(t) for t in rewritten_texts(rw)):
        return "rewritten"
    return rw.classify(sql)
