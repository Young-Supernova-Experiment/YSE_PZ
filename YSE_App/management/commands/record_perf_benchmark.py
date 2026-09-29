"""
Record page-load benchmarks, append metrics_history.json, and regenerate plots.

  python manage.py record_perf_benchmark
  python manage.py record_perf_benchmark --label baseline-pre-pfp
"""

from __future__ import annotations

import subprocess

from django.core.management.base import BaseCommand

from YSE_App.perf import plots
from YSE_App.perf.benchmark import run_primary_pages
from YSE_App.perf.mag_limited import (
    FULL_N_POINTS,
    FULL_N_TRANSIENTS,
    configured_n_points,
    configured_n_transients,
    run_mag_limited_benchmark,
)
from YSE_App.perf.test_db import benchmark_test_database
from YSE_App.perf.waterfall_collect import collect_all_timelines


class Command(BaseCommand):
    help = "Measure primary page loads and update perf metrics history + plots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--label",
            default="",
            help="Iteration label (default: git short SHA).",
        )
        parser.add_argument(
            "--branch",
            default="",
            help="Branch name (default: current git branch).",
        )
        parser.add_argument(
            "--skip-plots",
            action="store_true",
            help="Only append JSON history; do not write PNGs.",
        )
        parser.add_argument(
            "--skip-mag-limited",
            action="store_true",
            help="Skip the magnitude-limited sample saved-query benchmark.",
        )
        parser.add_argument(
            "--mag-limited-max-seconds",
            type=float,
            default=600.0,
            help=(
                "Wall-clock cap for the magnitude-limited benchmark (seeding + "
                "queries); each SELECT is capped at half of it server-side."
            ),
        )

    def handle(self, *args, **options):
        label = options["label"] or _git_short_sha() or "manual"
        branch = options["branch"] or _git_branch() or "unknown"
        commit = _git_short_sha() or "unknown"

        self.stdout.write("Using migrated test database (same as YSE_App.tests)...")
        with benchmark_test_database():
            self.stdout.write("Running primary page benchmarks...")
            pages = run_primary_pages()
            for p in pages:
                self.stdout.write(
                    f"  {p.page_key}: {p.ttfb_ms:.1f} ms, {p.sql_count} queries ({p.url})"
                )

            sections_by_page = {}
            if not options["skip_mag_limited"]:
                ml_pages = self._run_mag_limited(options, sections_by_page)
                pages = pages + ml_pages

            self.stdout.write("Collecting per-resource waterfalls (timeline)...")
            timelines = collect_all_timelines()
            waterfalls = {k: t.resources for k, t in timelines.items()}
            for page_key, tl in timelines.items():
                self.stdout.write(
                    f"  {page_key}: page load {tl.page_load_total_ms:.0f} ms "
                    f"({len(tl.resources)} resources)"
                )
                self.stdout.write(f"    schedule: {tl.schedule_note}")
                for r in tl.resources:
                    self.stdout.write(
                        f"    {r.name}: [{r.start_ms:.0f}, {r.end_ms:.0f}] ms "
                        f"(dur {r.total_ms:.0f})"
                    )

            run = plots.append_run(
                label=label,
                commit=commit,
                branch=branch,
                pages=pages,
                sections_by_page=sections_by_page or None,
                waterfalls=waterfalls,
                timelines=timelines,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Appended iteration {run['iteration_index']} ({label}) to "
                    "metrics_history.json"
                )
            )

            if not options["skip_plots"]:
                wf = plots.write_waterfall_plots(
                    pages, waterfalls=waterfalls, timelines=timelines
                )
                tr = plots.write_trend_plots()
                for path in wf + tr:
                    self.stdout.write(f"  wrote {path}")


    def _run_mag_limited(self, options, sections_by_page) -> list:
        """
        Full-tier saved-query benchmark (non-gating). Bounded by
        --mag-limited-max-seconds: SELECTs are capped server-side and the
        remaining measurements are skipped once the wall clock is spent.
        Any failure is reported and the primary-page run is still recorded.
        """
        n_transients = configured_n_transients(FULL_N_TRANSIENTS)
        n_points = configured_n_points(FULL_N_POINTS)
        max_seconds = float(options["mag_limited_max_seconds"])
        self.stdout.write(
            "Running magnitude-limited sample query benchmark "
            f"({n_transients} transients x {n_points} points, cap {max_seconds:.0f} s)..."
        )
        try:
            run = run_mag_limited_benchmark(
                n_transients=n_transients,
                n_points=n_points,
                max_execution_ms=int(max_seconds * 1000 / 2),
                deadline_s=max_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never block the primary-page record
            self.stderr.write(
                self.style.WARNING(f"  magnitude-limited benchmark failed: {exc!r}")
            )
            return []
        self.stdout.write(
            f"  seeded {run.dataset.n_transients} transients / "
            f"{run.dataset.n_photdata_rows} photometry rows in {run.dataset.seed_ms:.0f} ms"
        )
        for p in run.pages:
            self.stdout.write(
                f"  {p.page_key}: {p.ttfb_ms:.1f} ms, {p.sql_count} queries ({p.url})"
            )
            if p.sections:
                sections_by_page[p.page_key] = p.sections
        self.stdout.write(
            f"  query matched {run.n_rows} of {run.dataset.n_transients} transients"
        )
        for note in run.notes:
            self.stderr.write(self.style.WARNING(f"  {note}"))
        return run.pages


def _git_short_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _git_branch() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
