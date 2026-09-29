"""
CI regression: the saved query "YSE Magnitude-Limited Sample (min mag < 18.6)"
must return a list on the personal dashboard within budget (issue #204).

Two tiers
---------
This module is the merge gate and runs inside ``manage.py test YSE_App.tests``,
so it seeds the small tier (perf_baselines.json -> benchmarks.dataset, default
300 transients x 10 points, overridable with YSE_PERF_MAG_LIMITED_TRANSIENTS /
YSE_PERF_MAG_LIMITED_POINTS) and must finish in well under a minute. The
3000 x 20 run lives only in ``record_perf_benchmark`` (non-gating trend
history). Every SELECT here is capped server-side at the SQL budget, so a bad
query plan fails the test instead of hanging the job.

Budget reasoning
----------------
Ryan's requirement is one minute at production volume (order 1e5 transients,
1e6-1e7 photometry points), ~300x this dataset. Scaling 60 s down linearly
gives a fraction of a second, which at this size is runner noise and fixed
Django/template cost rather than the query, so the absolute budgets in
perf_baselines.json are deliberately looser than linear while still catching
the failure mode that matters: the query's per-transient dependent subquery
scales with transients x photometry rows, and a plan that evaluates it per
candidate row takes tens of seconds even at 300 x 10. When the size is
overridden upwards, budgets scale linearly with it.

On top of the absolute budget, once a baseline_ms is recorded from CI, a
measurement above ``regression_factor`` (2x) times the baseline fails, mirroring
the tolerance check the page-load regression test applies.
"""

import json
import os
from pathlib import Path

from django.test import TestCase, override_settings

from YSE_App.perf.mag_limited import (
    BENCHMARK_KEY_SQL,
    BENCHMARK_KEYS,
    MAG_LIMITED_SAMPLE_SQL_M2M,
    MAG_LIMITED_SAMPLE_SQL_PRODUCTION,
    SUITE_N_POINTS,
    SUITE_N_TRANSIENTS,
    configured_n_points,
    configured_n_transients,
    mag_limited_sample_sql,
    photdata_has_data_quality_column,
    run_mag_limited_benchmark,
    scale_budget_ms,
)
from YSE_App.tests.fixtures_minimal import create_test_user

BASELINES_PATH = Path(__file__).with_name("perf_baselines.json")
SKIP_TIMING = os.environ.get("YSE_PERF_SKIP_TIMING", "").lower() in ("1", "true", "yes")


def _load_benchmark_baselines() -> dict:
    with BASELINES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)["benchmarks"]


def _dashboard_accepts(sql: str) -> bool:
    """The guard _personaldashboard_table_for_user_query applies before running SQL."""
    lowered = sql.lower()
    return (
        "yse_app_transient" in lowered
        and "name" in lowered
        and lowered.startswith("select")
    )


class MagLimitedSampleSqlTests(TestCase):
    def test_saved_sql_passes_dashboard_guard(self):
        self.assertTrue(_dashboard_accepts(MAG_LIMITED_SAMPLE_SQL_PRODUCTION))
        self.assertTrue(_dashboard_accepts(MAG_LIMITED_SAMPLE_SQL_M2M))

    def test_schema_variant_matches_connected_database(self):
        sql = mag_limited_sample_sql()
        if photdata_has_data_quality_column():
            self.assertEqual(sql, MAG_LIMITED_SAMPLE_SQL_PRODUCTION)
        else:
            self.assertEqual(sql, MAG_LIMITED_SAMPLE_SQL_M2M)
            self.assertNotIn("data_quality_id", sql)

    def test_suite_tier_matches_baselines_dataset(self):
        dataset = _load_benchmark_baselines()["dataset"]
        self.assertEqual(int(dataset["n_transients"]), SUITE_N_TRANSIENTS)
        self.assertEqual(int(dataset["n_points_per_transient"]), SUITE_N_POINTS)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MagLimitedSampleQueryRegressionTests(TestCase):
    """Seed the small tier, attach the saved query, time it as the dashboard does."""

    def test_mag_limited_sample_within_budget(self):
        baselines = _load_benchmark_baselines()
        reference_n = int(baselines["dataset"]["n_transients"])
        factor = float(baselines.get("regression_factor", 2.0))
        entries = baselines["entries"]
        n_transients = configured_n_transients(SUITE_N_TRANSIENTS)
        n_points = configured_n_points(SUITE_N_POINTS)
        sql_budget_ms = scale_budget_ms(
            float(entries[BENCHMARK_KEY_SQL]["max_ms"]),
            n_transients=n_transients,
            reference_n=reference_n,
        )

        user = create_test_user("perf_mag_limited_regression")
        run = run_mag_limited_benchmark(
            user=user,
            n_transients=n_transients,
            n_points=n_points,
            max_execution_ms=int(sql_budget_ms),
        )
        dataset = run.dataset
        measured = {p.page_key: p for p in run.pages}
        context = (
            f"({dataset.n_transients} transients, {dataset.n_photdata_rows} phot rows, "
            f"seeded in {dataset.seed_ms:.0f} ms)"
        )

        # (1) The query returns a list on the dashboard.
        self.assertIsNone(
            run.sql_error,
            f"saved query failed {context}; statement cap {sql_budget_ms:.0f} ms: {run.sql_error}",
        )
        self.assertGreater(run.n_rows, 0, f"saved query matched no seeded transients {context}")
        self.assertLess(run.n_rows, dataset.n_transients, "mag cut selected everything")
        self.assertEqual(
            run.notes, [], f"dashboard fragment did not list results {context}: {run.notes}"
        )

        # (2) Timing budgets and regression versus recorded baseline.
        if SKIP_TIMING:
            return
        failures = []
        for key in BENCHMARK_KEYS:
            page = measured.get(key)
            if page is None:
                failures.append(f"missing measurement for {key}")
                continue
            spec = entries.get(key)
            if spec is None:
                failures.append(f"perf_baselines.json has no benchmarks.entries.{key}")
                continue
            max_ms = scale_budget_ms(
                float(spec["max_ms"]), n_transients=n_transients, reference_n=reference_n
            )
            if page.ttfb_ms > max_ms:
                failures.append(
                    f"{key}: {page.ttfb_ms:.0f} ms > budget {max_ms:.0f} ms {context}"
                )
            baseline_ms = spec.get("baseline_ms")
            if baseline_ms and page.ttfb_ms > factor * float(baseline_ms):
                failures.append(
                    f"{key}: {page.ttfb_ms:.0f} ms > {factor:g}x baseline {baseline_ms:.0f} ms"
                )

        if failures:
            self.fail("Magnitude-limited sample regression:\n" + "\n".join(failures))
