"""
CI regression: the saved query "YSE Magnitude-Limited Sample (min mag < 18.6)"
must return a list on the personal dashboard within budget (issue #204).

Budget reasoning
----------------
Ryan's requirement is one minute at production volume (order 1e5 transients,
1e6-1e7 photometry points). CI seeds a much smaller synthetic dataset
(perf_baselines.json -> benchmarks.dataset, default 3000 transients x 20 points)
so the docker MySQL run stays under a few minutes. Scaling the 60 s budget
linearly to that size would give ~1-2 s, which at CI scale is dominated by
fixed costs (Django table build, template render, a cold MySQL in a shared
runner) rather than by the query, so the absolute budgets in
perf_baselines.json are deliberately looser than linear: they still catch the
failure mode that matters (a dependent-subquery blow-up scales with
transients x photometry rows and would take tens of seconds even here) without
failing on runner noise. When YSE_PERF_MAG_LIMITED_TRANSIENTS overrides the
size, budgets scale linearly with it.

On top of the absolute budget, once a baseline_ms is recorded from CI, a
measurement above ``regression_factor`` (2x) times the baseline fails, mirroring
the tolerance check the page-load regression test applies.
"""

import json
import os
from pathlib import Path

from django.test import TestCase, override_settings

from YSE_App.perf.mag_limited import (
    BENCHMARK_KEYS,
    MAG_LIMITED_SAMPLE_SQL_M2M,
    MAG_LIMITED_SAMPLE_SQL_PRODUCTION,
    TRANSIENT_NAME_PREFIX,
    configured_n_transients,
    explorer_routed_to_default,
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


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MagLimitedSampleQueryRegressionTests(TestCase):
    """Seed, attach the saved query, time it as the dashboard does, compare to budgets."""

    def test_mag_limited_sample_within_budget(self):
        baselines = _load_benchmark_baselines()
        reference_n = int(baselines["dataset"]["n_transients"])
        factor = float(baselines.get("regression_factor", 2.0))
        entries = baselines["entries"]

        user = create_test_user("perf_mag_limited_regression")
        results, dataset, n_rows = run_mag_limited_benchmark(user=user)
        measured = {p.page_key: p for p in results}

        # (1) The query returns a list on the dashboard.
        self.assertGreater(n_rows, 0, "saved query matched no seeded transients")
        self.assertLess(n_rows, dataset.n_transients, "mag cut selected everything")
        client = self.client
        client.force_login(user)
        with explorer_routed_to_default():
            response = client.get(dataset.user_query_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(TRANSIENT_NAME_PREFIX.encode(), response.content)

        # (2) Timing budgets and regression versus recorded baseline.
        if SKIP_TIMING:
            return
        n_transients = configured_n_transients()
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
                    f"{key}: {page.ttfb_ms:.0f} ms > budget {max_ms:.0f} ms "
                    f"({dataset.n_transients} transients, {dataset.n_photdata_rows} phot rows)"
                )
            baseline_ms = spec.get("baseline_ms")
            if baseline_ms and page.ttfb_ms > factor * float(baseline_ms):
                failures.append(
                    f"{key}: {page.ttfb_ms:.0f} ms > {factor:g}x baseline {baseline_ms:.0f} ms"
                )

        if failures:
            self.fail("Magnitude-limited sample regression:\n" + "\n".join(failures))
