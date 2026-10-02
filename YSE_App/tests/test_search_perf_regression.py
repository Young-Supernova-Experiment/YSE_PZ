"""
CI regression: the acceptance search of issue #286 (``|b| > 10, peak_mag < 19,
num_det >= 3, last detection within 5 days``) must stay within the budgets in
perf_baselines.json -> benchmarks.entries.search_acceptance_*.

The gating tier seeds SUITE_N_TRANSIENTS (2000) transients with a stat row each
and is a few seconds of seeding; ``YSE_PERF_SEARCH_TRANSIENTS=100000`` runs the
production-sized tier locally (see docs/transient-search.md for a recorded
run). The budgets are not scaled with the size: the indexes are supposed to
keep the search flat. ``YSE_PERF_SKIP_TIMING=1`` keeps the correctness checks
and skips the budgets (slow runners).
"""

import json
import os
from pathlib import Path

from django.test import TestCase

from YSE_App.perf.search_filters import (
    ACCEPTANCE_PARAMS,
    BENCHMARK_KEYS,
    SUITE_N_TRANSIENTS,
    configured_n_transients,
    expected_match,
    run_search_benchmark,
)

BASELINES_PATH = Path(__file__).with_name("perf_baselines.json")
SKIP_TIMING = os.environ.get("YSE_PERF_SKIP_TIMING", "").lower() in ("1", "true", "yes")


def _entries() -> dict:
    with BASELINES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)["benchmarks"]["entries"]


class AcceptancePredicateTests(TestCase):
    def test_predicate_mirrors_the_parameters(self):
        self.assertEqual(ACCEPTANCE_PARAMS["gal_b_abs_min"], "10")
        self.assertTrue(expected_match(-12.0, 18.9, 3, 4.9))
        self.assertFalse(expected_match(9.9, 18.9, 3, 4.9))
        self.assertFalse(expected_match(-12.0, 19.1, 3, 4.9))
        self.assertFalse(expected_match(-12.0, 18.9, 2, 4.9))
        self.assertFalse(expected_match(-12.0, 18.9, 3, 5.1))


class SearchAcceptanceRegressionTests(TestCase):
    def test_acceptance_search_returns_the_expected_rows_within_budget(self):
        n_transients = configured_n_transients()
        run = run_search_benchmark(n_transients=n_transients)
        measured = {page.page_key: page for page in run.pages}
        context = f"(n_transients={n_transients}, suite default {SUITE_N_TRANSIENTS})"
        for key in BENCHMARK_KEYS:
            print(f"{key}: {measured[key].ttfb_ms:.1f} ms {context}", flush=True)

        # (1) Correctness: the stored columns and the stat row give exactly the Python predicate's rows.
        self.assertEqual(run.notes, [], run.notes)
        self.assertGreater(run.dataset.n_expected, 0, "seed produced no matching rows")
        self.assertEqual(run.n_rows, run.dataset.n_expected, context)
        self.assertEqual(measured["search_acceptance_sql"].sql_count, 1, "the search is one query")

        # (2) Budgets and regression against the recorded baseline.
        if SKIP_TIMING:
            return
        entries = _entries()
        failures = []
        for key in BENCHMARK_KEYS:
            spec = entries.get(key)
            if spec is None:
                failures.append(f"perf_baselines.json has no benchmarks.entries.{key}")
                continue
            ms = measured[key].ttfb_ms
            if ms > float(spec["max_ms"]):
                failures.append(f"{key}: {ms:.0f} ms > budget {spec['max_ms']} ms {context}")
        if failures:
            self.fail("Search acceptance regression:\n" + "\n".join(failures))
