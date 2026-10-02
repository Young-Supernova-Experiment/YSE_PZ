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

Two tiers share this code:

  * the gating test (YSE_App.tests.test_mag_limited_query_regression) seeds
    SUITE_N_TRANSIENTS x SUITE_N_POINTS so the whole suite stays fast;
  * ``manage.py record_perf_benchmark`` seeds FULL_N_TRANSIENTS x FULL_N_POINTS
    for the trend history and is not a merge gate.

Both are overridable with YSE_PERF_MAG_LIMITED_TRANSIENTS and
YSE_PERF_MAG_LIMITED_POINTS. Every SELECT is capped server-side with MySQL's
``max_execution_time`` so a bad plan cannot hang a CI job (the first CI run of
this benchmark sat in the query for 15+ minutes at 3000 x 20).

The SQL text is the production saved query (explorer_query id 62 in
docker/db_init/YSE_rest_of_tables_insert.sql), verbatim. Migration 0002 turned
TransientPhotData.data_quality into a ManyToMany, so on a migrated schema the
column ``pd2.data_quality_id`` does not exist; ``mag_limited_sample_sql`` picks
the variant that matches the connected schema (the only difference is that one
predicate).
"""

from __future__ import annotations

import datetime
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List, Optional
from unittest.mock import patch

from autoslug.fields import AutoSlugField
from django.core.cache import cache
from django.db import DatabaseError, connection, connections
from django.test import Client, override_settings
from django.utils import timezone
from django.utils.text import slugify

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

# Gating tier (test suite) and full tier (record_perf_benchmark).
SUITE_N_TRANSIENTS = 300
SUITE_N_POINTS = 10
FULL_N_TRANSIENTS = 3000
FULL_N_POINTS = 20
ENV_N_TRANSIENTS = "YSE_PERF_MAG_LIMITED_TRANSIENTS"
ENV_N_POINTS = "YSE_PERF_MAG_LIMITED_POINTS"

TRANSIENT_NAME_PREFIX = "2024perf"  # matches the query's t.name LIKE '202%'
YSE_TAG_NAME = "YSE"
BULK_BATCH = 5000


def configured_n_transients(default: int = SUITE_N_TRANSIENTS) -> int:
    return int(os.environ.get(ENV_N_TRANSIENTS, default))


def configured_n_points(default: int = SUITE_N_POINTS) -> int:
    return int(os.environ.get(ENV_N_POINTS, default))


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


@contextmanager
def statement_time_cap(max_ms: Optional[int]) -> Iterator[None]:
    """
    Cap every SELECT on the default connection at ``max_ms`` (MySQL
    max_execution_time; MariaDB max_statement_time). A capped statement fails
    with a DatabaseError instead of hanging the process. No-op elsewhere.
    """
    conn = connections["default"]
    if not max_ms or conn.vendor != "mysql":
        yield
        return
    is_mariadb = getattr(conn, "mysql_is_mariadb", False)
    try:
        with conn.cursor() as cursor:
            if is_mariadb:
                cursor.execute("SET SESSION max_statement_time = %s", [max_ms / 1000.0])
            else:
                cursor.execute("SET SESSION max_execution_time = %s", [int(max_ms)])
    except DatabaseError:
        yield
        return
    try:
        yield
    finally:
        try:
            with conn.cursor() as cursor:
                if is_mariadb:
                    cursor.execute("SET SESSION max_statement_time = 0")
                else:
                    cursor.execute("SET SESSION max_execution_time = 0")
        except DatabaseError:
            pass


@contextmanager
def _preset_slugs() -> Iterator[None]:
    """Skip AutoSlugField's per-row uniqueness SELECT; slugs are set explicitly."""
    with patch.object(
        AutoSlugField,
        "pre_save",
        lambda self, instance, add: getattr(instance, self.attname),
    ):
        yield


@dataclass
class MagLimitedDataset:
    n_transients: int
    n_points: int
    n_photdata_rows: int
    user_query: UserQuery
    user_query_url: str
    seed_ms: float = 0.0


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
    predicate does real work. Four bulk_create calls; no per-row queries.
    """
    n_transients = n_transients or configured_n_transients()
    n_points = n_points or configured_n_points()
    started = time.perf_counter()
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
        name = f"{TRANSIENT_NAME_PREFIX}{i:05d}"
        transients.append(
            Transient(
                name=name,
                slug=slugify(name),
                ra=rng.uniform(0.0, 360.0),
                dec=rng.uniform(-30.0, 80.0),
                status=status_cycle[i % len(status_cycle)],
                obs_group=obs_group,
                disc_date=now - datetime.timedelta(days=rng.uniform(1, 900)),
                **audit,
            )
        )
    # bulk_create skips post_save (no TESS lookup); _preset_slugs skips autoslug's
    # uniqueness SELECT per row.
    with _preset_slugs():
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
    TransientPhotData.objects.bulk_create(points, batch_size=BULK_BATCH)

    user_query = attach_mag_limited_query(user, sql=sql)
    return MagLimitedDataset(
        n_transients=len(transients),
        n_points=n_points,
        n_photdata_rows=len(points),
        user_query=user_query,
        user_query_url=f"/personaldashboard/section/{user_query.id}/",
        seed_ms=(time.perf_counter() - started) * 1000.0,
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
    """
    Run ``sql`` exactly as _personaldashboard_table_for_user_query does.

    Returns (elapsed_ms, names, error). ``error`` is the DatabaseError text
    when the statement failed (e.g. hit the statement time cap).
    """
    start = time.perf_counter()
    names: List[str] = []
    error = None
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute(sql.replace("%", "%%"), ())
            names = [row[0] for row in cursor.fetchall()]
    except DatabaseError as exc:
        error = str(exc)
    return (time.perf_counter() - start) * 1000.0, names, error


@dataclass
class MagLimitedRun:
    pages: List[PageBenchmark]
    dataset: MagLimitedDataset
    n_rows: int
    sql_error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
def run_mag_limited_benchmark(
    *,
    n_transients: Optional[int] = None,
    n_points: Optional[int] = None,
    user=None,
    max_execution_ms: Optional[int] = None,
    deadline_s: Optional[float] = None,
) -> MagLimitedRun:
    """
    Seed, attach and time.

    ``max_execution_ms`` caps each SELECT server-side; ``deadline_s`` is a
    wall-clock budget for the whole run (seeding included) after which the
    remaining measurements are skipped and noted. Whatever was measured is
    returned so the caller can record it.
    """
    started = time.perf_counter()
    user = user or create_test_user("perf_benchmark_mag_limited")
    dataset = seed_mag_limited_dataset(
        user, n_transients=n_transients, n_points=n_points
    )
    sql = dataset.user_query.query.sql
    run = MagLimitedRun(pages=[], dataset=dataset, n_rows=0)

    def over_deadline() -> bool:
        return deadline_s is not None and (time.perf_counter() - started) > deadline_s

    with statement_time_cap(max_execution_ms):
        sql_ms, names, error = time_raw_sql(sql)
        run.n_rows = len(names)
        run.sql_error = error
        sections = {
            "rows": float(len(names)),
            "transients": float(dataset.n_transients),
            "phot_rows": float(dataset.n_photdata_rows),
            "seed_ms": round(dataset.seed_ms, 1),
        }
        if error:
            sections["failed"] = 1.0
            run.notes.append(f"raw SQL failed after {sql_ms:.0f} ms: {error}")
        run.pages.append(
            PageBenchmark(
                page_key=BENCHMARK_KEY_SQL,
                url=f"explorer:{MAG_LIMITED_SAMPLE_TITLE}",
                ttfb_ms=sql_ms,
                total_ms=sql_ms,
                sql_count=1,
                sections=sections,
            )
        )
        if over_deadline():
            run.notes.append("deadline reached after raw SQL; dashboard fragment skipped")
            return run

        client = Client()
        client.force_login(user)
        with explorer_routed_to_default():
            cache.clear()
            for key in (BENCHMARK_KEY_COLD, BENCHMARK_KEY_WARM):
                if over_deadline():
                    run.notes.append(f"deadline reached; {key} skipped")
                    break
                response, n_queries, ms = _profile_get(client, dataset.user_query_url)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"personaldashboard section returned {response.status_code}"
                    )
                page_sections = None
                if TRANSIENT_NAME_PREFIX.encode() not in response.content:
                    page_sections = {"failed": 1.0}
                    run.notes.append(f"{key}: fragment rendered without result rows")
                run.pages.append(
                    PageBenchmark(
                        page_key=key,
                        url=dataset.user_query_url,
                        ttfb_ms=ms,
                        total_ms=ms,
                        sql_count=n_queries,
                        sections=page_sections,
                    )
                )
    return run


def scale_budget_ms(max_ms: float, *, n_transients: int, reference_n: int) -> float:
    """
    Linear scaling of a budget stated for ``reference_n`` transients.

    Smaller datasets keep the stated budget (fixed costs dominate there).
    """
    if reference_n <= 0:
        return max_ms
    return max_ms * max(1.0, n_transients / reference_n)
