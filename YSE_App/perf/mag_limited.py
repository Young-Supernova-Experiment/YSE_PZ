"""
Benchmark the saved SQL Explorer query "YSE Magnitude-Limited Sample (min mag < 18.6)"
as the personal dashboard runs it.

Ryan Foley's requirement (issue #204): the query must return a list on the
personal dashboard in under one minute at production volume, and PRs must not
regress that time badly. CI cannot hold the production database, so this module
seeds a synthetic, deterministic dataset of tagged transients with photometry,
attaches the real saved query to a user's dashboard and times:

  mag_limited_sample_sql               raw SQL through the dashboard's cursor path
  personal_dashboard_mag_limited_cold  /personaldashboard/section/<id>/ with an empty
                                       result cache (SQL + table build + render)
  personal_dashboard_mag_limited_warm  the same fragment with the SQL result cached

The SQL text is the production saved query (explorer_query id 62 in
docker/db_init/YSE_rest_of_tables_insert.sql), verbatim. Migration 0002 turned
TransientPhotData.data_quality into a ManyToMany, so on a migrated schema the
column ``pd2.data_quality_id`` does not exist; ``mag_limited_sample_sql`` picks
the variant that matches the connected schema (the only difference is that one
predicate).

Dataset size is controlled by YSE_PERF_MAG_LIMITED_TRANSIENTS and
YSE_PERF_MAG_LIMITED_POINTS (defaults below); budgets in
YSE_App/tests/perf_baselines.json are stated for the defaults and scaled linearly
by the regression test when the size is overridden.
"""

from __future__ import annotations

import datetime
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, List, Optional
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections
from django.test import Client, override_settings
from django.utils import timezone

from YSE_App.models import (
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientTag,
    UserQuery,
)
from YSE_App.perf.benchmark import PASSWORD_HASHERS, PageBenchmark, _profile_get
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_test_user,
    ensure_transient_statuses,
)

MAG_LIMITED_SAMPLE_TITLE = "YSE Magnitude-Limited Sample (min mag < 18.6)"

# Verbatim from explorer_query id 62 (docker/db_init/YSE_rest_of_tables_insert.sql).
MAG_LIMITED_SAMPLE_SQL_PRODUCTION = """SELECT t.name, pd.mag, t.ra, t.dec
\tFROM YSE_App_transient t, YSE_App_transientphotdata pd, YSE_App_transientphotometry p, YSE_App_transient_tags tt, YSE_App_transienttag tg
    WHERE pd.photometry_id = p.id AND tg.name = 'YSE' AND pd.mag < 18.6 AND
    tt.transient_id = t.id AND tg.id = tt.transienttag_id AND
    tt.transient_id = t.id AND tg.id = tt.transienttag_id AND
    pd.id = (
         SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2
         WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id AND ISNULL(pd2.data_quality_id) = True AND
      ISNULL(pd2.mag) = False AND pd2.flux/pd2.flux_err > 3
         ORDER BY pd2.mag ASC
         LIMIT 1
     )
     AND (t.name LIKE '202%' OR t.name LIKE '201%')"""

_DATA_QUALITY_FK_PREDICATE = "ISNULL(pd2.data_quality_id) = True"
_DATA_QUALITY_M2M_PREDICATE = (
    "NOT EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality dq "
    "WHERE dq.transientphotdata_id = pd2.id)"
)

# Same query for the migrated schema (0002: data_quality is a ManyToMany).
MAG_LIMITED_SAMPLE_SQL_M2M = MAG_LIMITED_SAMPLE_SQL_PRODUCTION.replace(
    _DATA_QUALITY_FK_PREDICATE, _DATA_QUALITY_M2M_PREDICATE
)

BENCHMARK_KEY_SQL = "mag_limited_sample_sql"
BENCHMARK_KEY_COLD = "personal_dashboard_mag_limited_cold"
BENCHMARK_KEY_WARM = "personal_dashboard_mag_limited_warm"
BENCHMARK_KEYS = (BENCHMARK_KEY_SQL, BENCHMARK_KEY_COLD, BENCHMARK_KEY_WARM)

DEFAULT_N_TRANSIENTS = 3000
DEFAULT_N_POINTS = 20
ENV_N_TRANSIENTS = "YSE_PERF_MAG_LIMITED_TRANSIENTS"
ENV_N_POINTS = "YSE_PERF_MAG_LIMITED_POINTS"

TRANSIENT_NAME_PREFIX = "2024perf"  # matches the query's t.name LIKE '202%'
YSE_TAG_NAME = "YSE"
BULK_BATCH = 2000


def configured_n_transients() -> int:
    return int(os.environ.get(ENV_N_TRANSIENTS, DEFAULT_N_TRANSIENTS))


def configured_n_points() -> int:
    return int(os.environ.get(ENV_N_POINTS, DEFAULT_N_POINTS))


def photdata_has_data_quality_column(conn=None) -> bool:
    """True on the pre-0002 schema where data_quality_id is a column."""
    conn = conn or connection
    with conn.cursor() as cursor:
        columns = conn.introspection.get_table_description(
            cursor, TransientPhotData._meta.db_table
        )
    return any(col.name == "data_quality_id" for col in columns)


def mag_limited_sample_sql(conn=None) -> str:
    """The saved query text that runs against the connected schema."""
    if photdata_has_data_quality_column(conn):
        return MAG_LIMITED_SAMPLE_SQL_PRODUCTION
    return MAG_LIMITED_SAMPLE_SQL_M2M


@contextmanager
def explorer_routed_to_default() -> Iterator[None]:
    """
    Route the dashboard's ``connections['explorer']`` to ``default``.

    Tests run inside a transaction on ``default`` only, and the benchmark
    command's test database is created for the ``default`` credentials, so the
    separate explorer connection cannot see the seeded rows. Same pattern as
    test_performance / test_deploy_checklist_flows.
    """
    import YSE_App.views as views_module

    class _Routed:
        def __getitem__(self, alias):
            if alias == "explorer":
                return connections["default"]
            return connections[alias]

    with patch.object(views_module, "connections", _Routed()):
        yield


@dataclass
class MagLimitedDataset:
    n_transients: int
    n_points: int
    n_photdata_rows: int
    user_query: UserQuery
    user_query_url: str


def seed_mag_limited_dataset(
    user,
    *,
    n_transients: Optional[int] = None,
    n_points: Optional[int] = None,
    seed: int = 20260929,
    sql: Optional[str] = None,
) -> MagLimitedDataset:
    """
    Deterministic synthetic sample: YSE-tagged transients named like TNS
    objects, each with one photometry set of ``n_points`` epochs. Peak
    magnitudes are uniform in [17.0, 21.0], so roughly 40% pass the 18.6 cut,
    and the S/N per point is drawn from [1.5, 60] so the flux/flux_err > 3
    predicate does real work.
    """
    n_transients = n_transients or configured_n_transients()
    n_points = n_points or configured_n_points()
    rng = random.Random(seed)
    audit = audit_fields(user)
    statuses = ensure_transient_statuses(user)
    obs_group, instrument, band = create_instrument_stack(
        user, obs_group_name="perf-mag-limited"
    )
    yse_tag, _ = TransientTag.objects.get_or_create(name=YSE_TAG_NAME, defaults=audit)
    status_cycle = [statuses[name] for name in ("New", "Following", "Watch", "Ignore")]
    now = timezone.now()

    transients = []
    for i in range(n_transients):
        disc = now - datetime.timedelta(days=rng.uniform(1, 900))
        transients.append(
            Transient(
                name=f"{TRANSIENT_NAME_PREFIX}{i:05d}",
                ra=rng.uniform(0.0, 360.0),
                dec=rng.uniform(-30.0, 80.0),
                status=status_cycle[i % len(status_cycle)],
                obs_group=obs_group,
                disc_date=disc,
                **audit,
            )
        )
    # bulk_create skips post_save (no TESS lookup) but still runs AutoSlugField.pre_save.
    Transient.objects.bulk_create(transients, batch_size=BULK_BATCH)
    transients = list(
        Transient.objects.filter(name__startswith=TRANSIENT_NAME_PREFIX).order_by("id")
    )

    through = Transient.tags.through
    through.objects.bulk_create(
        [through(transient_id=t.id, transienttag_id=yse_tag.id) for t in transients],
        batch_size=BULK_BATCH,
    )

    TransientPhotometry.objects.bulk_create(
        [
            TransientPhotometry(
                transient=t, instrument=instrument, obs_group=obs_group, **audit
            )
            for t in transients
        ],
        batch_size=BULK_BATCH,
    )
    photometry = list(
        TransientPhotometry.objects.filter(transient__in=transients)
        .order_by("transient_id")
        .select_related("transient")
    )

    points = []
    for phot in photometry:
        peak = rng.uniform(17.0, 21.0)
        start = phot.transient.disc_date
        for j in range(n_points):
            # Simple rise/decline around the peak.
            mag = peak + 0.08 * abs(j - n_points // 3)
            flux = 10 ** (-0.4 * (mag - 27.5))
            snr = rng.uniform(1.5, 60.0)
            points.append(
                TransientPhotData(
                    photometry=phot,
                    band=band,
                    obs_date=start + datetime.timedelta(days=j * 3),
                    mag=mag,
                    mag_err=1.0857 / snr,
                    flux=flux,
                    flux_err=flux / snr,
                    discovery_point=(j == 0),
                    **audit,
                )
            )
        if len(points) >= BULK_BATCH * 5:
            TransientPhotData.objects.bulk_create(points, batch_size=BULK_BATCH)
            points = []
    if points:
        TransientPhotData.objects.bulk_create(points, batch_size=BULK_BATCH)

    user_query = attach_mag_limited_query(user, sql=sql)
    return MagLimitedDataset(
        n_transients=len(transients),
        n_points=n_points,
        n_photdata_rows=len(transients) * n_points,
        user_query=user_query,
        user_query_url=f"/personaldashboard/section/{user_query.id}/",
    )


def attach_mag_limited_query(user, *, sql: Optional[str] = None) -> UserQuery:
    """Save the query in SQL Explorer and pin it to ``user``'s personal dashboard."""
    from explorer.models import Query

    explorer_query, _ = Query.objects.get_or_create(
        title=MAG_LIMITED_SAMPLE_TITLE,
        defaults={
            "sql": sql or mag_limited_sample_sql(),
            "description": "Perf benchmark copy of explorer_query 62",
            "snapshot": False,
            "created_by_user": user,
        },
    )
    user_query, _ = UserQuery.objects.get_or_create(
        user=user, query=explorer_query, defaults=audit_fields(user)
    )
    return user_query


def time_raw_sql(sql: str) -> tuple:
    """Run ``sql`` exactly as _personaldashboard_table_for_user_query does."""
    with connections["default"].cursor() as cursor:
        start = time.perf_counter()
        cursor.execute(sql.replace("%", "%%"), ())
        names = [row[0] for row in cursor.fetchall()]
        elapsed = time.perf_counter() - start
    return elapsed * 1000.0, names


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
def run_mag_limited_benchmark(
    *,
    n_transients: Optional[int] = None,
    n_points: Optional[int] = None,
    user=None,
) -> tuple:
    """
    Seed, attach and time. Returns (List[PageBenchmark], MagLimitedDataset, n_rows).

    ``sections`` on the SQL benchmark carries the matched-row count so the
    history shows the list size alongside the time.
    """
    user = user or create_test_user("perf_benchmark_mag_limited")
    dataset = seed_mag_limited_dataset(
        user, n_transients=n_transients, n_points=n_points
    )
    sql = dataset.user_query.query.sql

    sql_ms, names = time_raw_sql(sql)
    results: List[PageBenchmark] = [
        PageBenchmark(
            page_key=BENCHMARK_KEY_SQL,
            url=f"explorer:{MAG_LIMITED_SAMPLE_TITLE}",
            ttfb_ms=sql_ms,
            total_ms=sql_ms,
            sql_count=1,
            sections={"rows": float(len(names)), "transients": float(dataset.n_transients)},
        )
    ]

    client = Client()
    client.force_login(user)
    with explorer_routed_to_default():
        cache.clear()
        response, n_queries, cold_ms = _profile_get(client, dataset.user_query_url)
        if response.status_code != 200:
            raise RuntimeError(
                f"personaldashboard section returned {response.status_code}"
            )
        results.append(
            PageBenchmark(
                page_key=BENCHMARK_KEY_COLD,
                url=dataset.user_query_url,
                ttfb_ms=cold_ms,
                total_ms=cold_ms,
                sql_count=n_queries,
            )
        )
        response, n_queries, warm_ms = _profile_get(client, dataset.user_query_url)
        if response.status_code != 200:
            raise RuntimeError(
                f"personaldashboard section (warm) returned {response.status_code}"
            )
        results.append(
            PageBenchmark(
                page_key=BENCHMARK_KEY_WARM,
                url=dataset.user_query_url,
                ttfb_ms=warm_ms,
                total_ms=warm_ms,
                sql_count=n_queries,
            )
        )
    return results, dataset, len(names)


def scale_budget_ms(max_ms: float, *, n_transients: int, reference_n: int) -> float:
    """
    Linear scaling of a budget stated for ``reference_n`` transients.

    Smaller datasets keep the stated budget (fixed costs dominate there).
    """
    if reference_n <= 0:
        return max_ms
    return max_ms * max(1.0, n_transients / reference_n)
