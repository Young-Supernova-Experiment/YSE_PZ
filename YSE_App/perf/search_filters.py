"""
Benchmark the transient search on the stored stat row and the indexed ``gal_b`` column (#286).

The acceptance search of issue #286 -- ``|b| > 10, peak_mag < 19, num_det >= 3,
last detection within 5 days`` -- must run under one second on a
production-sized table. CI cannot hold the production database, so this module
seeds a deterministic synthetic sample of transients with a ``TransientPhotStat``
row each (two ``bulk_create`` calls, no photometry points: the filters read the
stat row only) and times

  search_acceptance_sql    the FilterSet queryset (id list) as the search page and the API run it
  search_acceptance_page   GET /search/?... : query + COUNT + table render

Two tiers share the code: the gating test
(``YSE_App.tests.test_search_perf_regression``) seeds ``SUITE_N_TRANSIENTS``;
``YSE_PERF_SEARCH_TRANSIENTS`` overrides it for a production-sized local run
(``docs/transient-search.md`` records one). The budgets in
``perf_baselines.json`` are *not* scaled with the size: the whole point of the
indexes is that the search stays flat as the table grows.

Dates are offsets from the database clock with the 5-day boundary well away
from every seeded value (0.5-3 days for the recent tenth, 20-900 days for the
rest), so the result does not depend on the wall-clock date (#381).
"""

from __future__ import annotations

import datetime
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone
from django.utils.text import slugify

from YSE_App.common.galactic import galactic_coords
from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.models import Transient, TransientPhotStat
from YSE_App.models.phot_stat_models import datetime_to_mjd
from YSE_App.perf.benchmark import PASSWORD_HASHERS, PageBenchmark, _profile_get
from YSE_App.perf.mag_limited import _preset_slugs
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_test_user,
    ensure_transient_statuses,
)

BENCHMARK_KEY_SQL = "search_acceptance_sql"
BENCHMARK_KEY_PAGE = "search_acceptance_page"
BENCHMARK_KEYS = (BENCHMARK_KEY_SQL, BENCHMARK_KEY_PAGE)

SUITE_N_TRANSIENTS = 2000
ENV_N_TRANSIENTS = "YSE_PERF_SEARCH_TRANSIENTS"
ENV_EXPLAIN = "YSE_PERF_SEARCH_EXPLAIN"  # print the query plan of the acceptance search
TRANSIENT_NAME_PREFIX = "2024srch"
BULK_BATCH = 5000

# The acceptance search of #286, as a query string.
ACCEPTANCE_PARAMS = {
    "gal_b_abs_min": "10",
    "peak_mag_max": "19",
    "num_det_min": "3",
    "days_since_last_det_max": "5",
}
ACCEPTANCE_QUERY = "&".join(f"{k}={v}" for k, v in ACCEPTANCE_PARAMS.items())


def configured_n_transients(default: int = SUITE_N_TRANSIENTS) -> int:
    return int(os.environ.get(ENV_N_TRANSIENTS, default))


@dataclass
class SearchDataset:
    n_transients: int
    n_expected: int
    seed_ms: float = 0.0


def expected_match(gal_b, peak_mag, num_det, days_since_last_det) -> bool:
    """The acceptance predicate in Python, for the seeded values."""
    return abs(gal_b) >= 10 and peak_mag <= 19 and num_det >= 3 and days_since_last_det <= 5


def seed_search_dataset(user, *, n_transients: Optional[int] = None, seed: int = 20260930) -> SearchDataset:
    """
    Deterministic sample: positions uniform on the sphere (so ~17% sit at |b| < 10),
    peak magnitudes in [16, 22], 1-12 detections, a tenth detected in the last
    0.5-3 days and the rest 20-900 days ago. Returns the number of rows the
    acceptance search must find.
    """
    n_transients = n_transients or configured_n_transients()
    started = time.perf_counter()
    rng = random.Random(seed)
    audit = audit_fields(user)
    statuses = ensure_transient_statuses(user)
    obs_group, _instrument, band = create_instrument_stack(user, obs_group_name="perf-search")
    status_cycle = [statuses[name] for name in ("New", "Following", "Watch", "Ignore")]
    now = timezone.now()

    rows = []
    for i in range(n_transients):
        name = f"{TRANSIENT_NAME_PREFIX}{i:06d}"
        ra = rng.uniform(0.0, 360.0)
        dec = math.degrees(math.asin(rng.uniform(-1.0, 1.0)))
        gal_l, gal_b = galactic_coords(ra, dec)
        peak_mag = rng.uniform(16.0, 22.0)
        num_det = rng.randint(1, 12)
        days = rng.uniform(0.5, 3.0) if i % 10 == 0 else rng.uniform(20.0, 900.0)
        rows.append((name, ra, dec, gal_l, gal_b, peak_mag, num_det, days))

    with _preset_slugs():
        Transient.objects.bulk_create(
            [
                Transient(
                    name=name, slug=slugify(name), ra=ra, dec=dec, gal_l=gal_l, gal_b=gal_b,
                    status=status_cycle[i % len(status_cycle)], obs_group=obs_group,
                    disc_date=now - datetime.timedelta(days=days + 10), **audit,
                )
                for i, (name, ra, dec, gal_l, gal_b, _pm, _nd, days) in enumerate(rows)
            ],
            batch_size=BULK_BATCH,
        )
    ids = dict(Transient.objects.filter(name__startswith=TRANSIENT_NAME_PREFIX).values_list("name", "id"))

    stats = []
    n_expected = 0
    for name, _ra, _dec, _gl, gal_b, peak_mag, num_det, days in rows:
        last = now - datetime.timedelta(days=days)
        first = last - datetime.timedelta(days=2 * num_det)
        stats.append(
            TransientPhotStat(
                transient_id=ids[name], num_obs_global=num_det + 2, num_det_global=num_det, num_limits_global=2,
                last_obs_mjd=datetime_to_mjd(last), last_obs_date=last,
                first_detected_mjd=datetime_to_mjd(first), first_detected_date=first,
                first_detected_mag=peak_mag + 1.0, first_detected_band=band,
                last_detected_mjd=datetime_to_mjd(last), last_detected_date=last,
                last_detected_mag=peak_mag + 0.5, last_detected_band=band,
                peak_mjd=datetime_to_mjd(first + (last - first) / 2), peak_date=first + (last - first) / 2,
                peak_mag=peak_mag, peak_band=band, mean_mag=peak_mag + 0.6, faintest_mag=peak_mag + 1.2,
                rise_rate=0.2, decay_rate=0.05,
            )
        )
        if expected_match(gal_b, peak_mag, num_det, days):
            n_expected += 1
    TransientPhotStat.objects.bulk_create(stats, batch_size=BULK_BATCH)
    return SearchDataset(
        n_transients=len(rows), n_expected=n_expected, seed_ms=(time.perf_counter() - started) * 1000.0
    )


def acceptance_filterset(user, params=None):
    request = RequestFactory().get("/search/", params or ACCEPTANCE_PARAMS)
    request.user = user
    return TransientSearchFilterSet(request.GET, queryset=Transient.objects.all(), request=request)


def time_filterset(user) -> tuple:
    """``(elapsed_ms, names, n_queries)`` for the acceptance FilterSet queryset."""
    from django.test.utils import CaptureQueriesContext

    fs = acceptance_filterset(user)
    if not fs.is_valid():
        raise RuntimeError(f"acceptance search does not validate: {fs.errors}")
    qs = fs.qs.values_list("name", flat=True)
    with CaptureQueriesContext(connection) as ctx:
        start = time.perf_counter()
        names = list(qs)
        elapsed = time.perf_counter() - start
    if os.environ.get(ENV_EXPLAIN, "").lower() in ("1", "true", "yes"):
        print(f"EXPLAIN ({connection.vendor}):", flush=True)
        print(qs.explain(), flush=True)
    return elapsed * 1000.0, names, len(ctx.captured_queries)


@dataclass
class SearchRun:
    pages: List[PageBenchmark]
    dataset: SearchDataset
    n_rows: int
    notes: List[str] = field(default_factory=list)


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
def run_search_benchmark(*, n_transients: Optional[int] = None, user=None) -> SearchRun:
    """Seed and time the acceptance search as a queryset and as the search page."""
    user = user or create_test_user("perf_benchmark_search")
    dataset = seed_search_dataset(user, n_transients=n_transients)
    sql_ms, names, n_queries = time_filterset(user)
    run = SearchRun(pages=[], dataset=dataset, n_rows=len(names))
    run.pages.append(
        PageBenchmark(
            page_key=BENCHMARK_KEY_SQL, url="/search/?" + ACCEPTANCE_QUERY, ttfb_ms=sql_ms, total_ms=sql_ms,
            sql_count=n_queries,
            sections={"rows": float(len(names)), "transients": float(dataset.n_transients),
                      "seed_ms": round(dataset.seed_ms, 1)},
        )
    )
    client = Client()
    client.force_login(user)
    response, page_queries, page_ms = _profile_get(client, "/search/?" + ACCEPTANCE_QUERY)
    if response.status_code != 200:
        raise RuntimeError(f"search page returned {response.status_code}")
    if TRANSIENT_NAME_PREFIX.encode() not in response.content:
        run.notes.append("search page rendered without result rows")
    run.pages.append(
        PageBenchmark(page_key=BENCHMARK_KEY_PAGE, url="/search/?" + ACCEPTANCE_QUERY, ttfb_ms=page_ms,
                      total_ms=page_ms, sql_count=page_queries)
    )
    return run
