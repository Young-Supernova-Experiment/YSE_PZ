"""
Personal-dashboard saved queries: the rewrite command, the SQL guard (#258),
the cache warmer and TTL setting (#233), and the index migration (#248).
The SQL equivalence checks are in ``test_sql_rewrites_equivalence.py``.
"""

import json
import os
import tempfile
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App import views as views_module
from YSE_App.common import db_time_cap
from YSE_App.models import Transient, TransientPhotData, UserQuery
from YSE_App.queries import dashboard_saved_queries as dsq
from YSE_App.services import dashboard_cache_warmer as warmer
from YSE_App.services.dashboard_queries import (
    dashboard_sql_is_supported,
    dashboard_sql_rejection_reason,
    explorer_query_cache_seconds,
    strip_leading_sql_comments,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

GOOD = "SELECT name FROM YSE_App_transient WHERE name = 'none'"


def make_query(user, title, sql=GOOD):
    from explorer.models import Query

    return Query.objects.create(title=title, sql=sql, description="", snapshot=False, created_by_user=user)


class _ExplorerToDefault:
    """The saved SQL runs on the 'explorer' alias; in a TestCase only 'default' sees the rows."""

    def __getitem__(self, alias):
        if alias == "explorer":
            return connections["default"]
        return connections[alias]


# --------------------------------------------------------------------------- #
# guard (#258)
# --------------------------------------------------------------------------- #

class DashboardSqlGuardTests(SimpleTestCase):
    def test_plain_select_is_accepted(self):
        self.assertIsNone(dashboard_sql_rejection_reason(GOOD))
        self.assertTrue(dashboard_sql_is_supported(GOOD))

    def test_leading_whitespace_and_newlines_are_accepted(self):
        self.assertTrue(dashboard_sql_is_supported("  \r\n\t" + GOOD))

    def test_leading_comments_are_accepted(self):
        for prefix in ("-- Ryan's query\n", "# mysql style\n", "/* block\n comment */ ", "-- a\n/* b */\n  -- c\n"):
            with self.subTest(prefix=prefix):
                self.assertTrue(dashboard_sql_is_supported(prefix + GOOD))

    def test_with_cte_is_accepted(self):
        sql = "WITH recent AS (SELECT name FROM YSE_App_transient) SELECT name FROM recent"
        self.assertTrue(dashboard_sql_is_supported(sql))
        self.assertTrue(dashboard_sql_is_supported("with recent as (select name from yse_app_transient) select * from recent"))

    def test_non_read_statements_are_refused(self):
        for sql in (
            "DESCRIBE YSE_App_transient",
            "describe YSE_App_hostphotometry_groups;",
            "DELETE FROM YSE_App_transient WHERE name = 'x'",
            "UPDATE YSE_App_transient SET name = 'x'",
            "INSERT INTO YSE_App_transient (name) VALUES ('x')",
            "DROP TABLE YSE_App_transient -- name",
            "-- comment only\nDELETE FROM YSE_App_transient WHERE name = 'x'",
            "selection FROM YSE_App_transient name",
            "WITHOUT SELECT name FROM YSE_App_transient",
            "",
        ):
            with self.subTest(sql=sql):
                self.assertFalse(dashboard_sql_is_supported(sql))
                self.assertIn("SELECT", dashboard_sql_rejection_reason(sql))

    def test_transient_table_and_name_are_still_required(self):
        self.assertIn("YSE_App_transient", dashboard_sql_rejection_reason("SELECT 1"))
        self.assertIn("name", dashboard_sql_rejection_reason("SELECT id FROM YSE_App_transient"))

    def test_strip_leading_comments_keeps_body(self):
        self.assertEqual(strip_leading_sql_comments("  /* a */ -- b\nSELECT 1"), "SELECT 1")
        self.assertEqual(strip_leading_sql_comments("SELECT /* inner */ 1"), "SELECT /* inner */ 1")

    def test_fixture_texts_that_failed_the_old_guard_now_pass(self):
        # docker/db_init fixture ids 54, 207, 208 start with two spaces.
        self.assertTrue(dashboard_sql_is_supported("  SELECT t.name,\r\n       t.ra AS transient_RA\r\nFROM YSE_App_transient t"))
        # ... and id 206 ('describe ...') is still refused.
        self.assertFalse(dashboard_sql_is_supported("describe YSE_App_hostphotometry_groups;\r\n\r\n"))


class PersonalDashboardSectionGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("pdash_guard_user")
        cls.transient = create_minimal_transient(cls.user, name="pdash-guard-1")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        cache.clear()

    def _section(self, sql, title="Guarded"):
        q = make_query(self.user, title, sql)
        uq = UserQuery.objects.create(user=self.user, query=q, **audit_fields(self.user))
        with mock.patch.object(views_module, "connections", _ExplorerToDefault()):
            return self.client.get(reverse("personaldashboard_section", kwargs={"user_query_id": uq.id}))

    def test_query_with_leading_comment_renders_its_table(self):
        response = self._section("-- leading comment\n  SELECT name FROM YSE_App_transient WHERE name = 'pdash-guard-1'")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"pdash-guard-1", response.content)
        self.assertNotIn(b"pdash-section-unsupported", response.content)

    def test_rejected_query_returns_200_with_the_reason_not_204(self):
        response = self._section("describe YSE_App_transient")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"pdash-section-unsupported", response.content)
        self.assertIn(b"must start with SELECT", response.content)

    def test_section_javascript_treats_an_empty_body_as_failure(self):
        response = self.client.get(reverse("personaldashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"if (!html || !$.trim(html))", response.content)


# --------------------------------------------------------------------------- #
# rewrite registry
# --------------------------------------------------------------------------- #

class RewriteRegistryTests(SimpleTestCase):
    def test_every_rewrite_passes_the_dashboard_guard(self):
        for rw in dsq.REWRITES:
            for text in (*rw.originals, *dsq.rewritten_texts(rw)):
                with self.subTest(title=rw.title, text=text[:30]):
                    self.assertTrue(dashboard_sql_is_supported(text))

    def test_classification(self):
        rw = dsq.find_rewrite("fast & young target search")
        self.assertEqual(rw.fixture_id, 254)
        self.assertEqual(dsq.classify_saved_sql(rw, rw.originals[0]), "original")
        self.assertEqual(dsq.classify_saved_sql(rw, "  " + rw.originals[0].replace("\n", "\r\n") + ";\n"), "original")
        self.assertEqual(dsq.classify_saved_sql(rw, rw.rewritten), "rewritten")
        self.assertEqual(dsq.classify_saved_sql(rw, rw.originals[0].replace("0.2", "0.25")), "unknown")
        self.assertIsNone(dsq.find_rewrite("Something else"))

    def test_magnitude_limited_keeps_the_matched_data_quality_predicate(self):
        rw = dsq.find_rewrite("YSE Magnitude-Limited Sample (min mag < 18.6)")
        self.assertIn("data_quality_id", dsq.rewrite_for(rw, dsq.MAG_LIMITED_ORIGINAL))
        self.assertNotIn("data_quality_id", dsq.rewrite_for(rw, dsq.MAG_LIMITED_ORIGINAL_M2M))
        self.assertIn("YSE_App_transientphotdata_data_quality", dsq.rewrite_for(rw, dsq.MAG_LIMITED_ORIGINAL_M2M))
        self.assertIsNone(dsq.rewrite_for(rw, "SELECT name FROM YSE_App_transient"))

    def test_rewrites_keep_the_original_thresholds(self):
        self.assertIn("HAVING MIN(pd2.mag) < 18.6", dsq.MAG_LIMITED_REWRITE)
        self.assertIn("INTERVAL 2 DAY", dsq.FAST_YOUNG_REWRITE)
        self.assertIn("TO_DAYS(latest_detection) < 3", dsq.FAST_YOUNG_REWRITE)
        self.assertIn("INTERVAL 1 DAY", dsq.NEW_TWO_DAYS_REWRITE)
        self.assertIn("TO_DAYS(latest_detection) < 2", dsq.NEW_TWO_DAYS_REWRITE)
        self.assertIn("AND a3.status_id = 1", dsq.INTERESTING_REWRITE)


# --------------------------------------------------------------------------- #
# management command
# --------------------------------------------------------------------------- #

class RewriteDashboardQueriesCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("rewrite_cmd_user")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fast = make_query(self.user, "Fast & Young Target Search", dsq.FAST_YOUNG_ORIGINAL)
        self.interesting = make_query(self.user, "Interesting New Targets", dsq.INTERESTING_ORIGINAL)
        self.edited = make_query(self.user, "New Transients Last Two Days", dsq.NEW_TWO_DAYS_ORIGINAL.replace("0.35", "0.4"))
        self.review = make_query(self.user, "Auto Ignore", "SELECT name FROM YSE_App_transient WHERE status_id = 5")
        self.unrelated = make_query(self.user, "Unrelated", GOOD)

    def run_cmd(self, *args):
        out = StringIO()
        call_command("rewrite_dashboard_queries", *args, "--backup-dir", self.tmp.name, stdout=out)
        return out.getvalue()

    def _reload(self, q):
        return type(q).objects.get(pk=q.pk).sql

    def test_dry_run_is_the_default_and_changes_nothing(self):
        out = self.run_cmd()
        self.assertIn("REWRITE", out)
        self.assertIn("Fast & Young Target Search", out)
        self.assertIn("Dry run", out)
        self.assertIn("-", out)  # a unified diff was printed
        self.assertIn("+AND t.id IN (SELECT tp2.transient_id", out)
        self.assertEqual(self._reload(self.fast), dsq.FAST_YOUNG_ORIGINAL)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_edited_text_is_skipped_and_review_titles_are_printed(self):
        out = self.run_cmd()
        self.assertIn(f"SKIP       {self.edited.id} New Transients Last Two Days", out)
        self.assertIn("not the text this rewrite was proven against", out)
        self.assertIn(f"REVIEW     {self.review.id} Auto Ignore", out)
        self.assertIn("status_id = 5", out)
        self.assertIn("NOT FOUND  'YSE Magnitude-Limited Sample (min mag < 18.6)'", out)
        self.assertNotIn("Unrelated", out)

    def test_apply_rewrites_backs_up_and_is_idempotent(self):
        out = self.run_cmd("--apply")
        self.assertIn("Updated 2 saved queries", out)
        self.assertEqual(self._reload(self.fast), dsq.FAST_YOUNG_REWRITE)
        self.assertEqual(self._reload(self.interesting), dsq.INTERESTING_REWRITE)
        self.assertEqual(self._reload(self.edited), dsq.NEW_TWO_DAYS_ORIGINAL.replace("0.35", "0.4"))
        files = os.listdir(self.tmp.name)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("saved_queries_rewrite_") and files[0].endswith(".json"))
        path = os.path.join(self.tmp.name, files[0])
        self.assertIn(f"Previous SQL saved to {path}", out)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        by_id = {e["id"]: e for e in payload["queries"]}
        self.assertEqual(set(by_id), {self.fast.id, self.interesting.id})
        self.assertEqual(by_id[self.fast.id]["previous_sql"], dsq.FAST_YOUNG_ORIGINAL)
        self.assertEqual(by_id[self.fast.id]["new_sql"], dsq.FAST_YOUNG_REWRITE)
        self.assertEqual(by_id[self.fast.id]["title"], "Fast & Young Target Search")

        again = self.run_cmd("--apply")
        self.assertIn(f"SKIP       {self.fast.id} Fast & Young Target Search: already rewritten", again)
        self.assertIn("Nothing to change", again)
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_title_filter_limits_the_change(self):
        out = self.run_cmd("--apply", "--title", "interesting new targets")
        self.assertIn("Updated 1 saved query", out)
        self.assertEqual(self._reload(self.fast), dsq.FAST_YOUNG_ORIGINAL)
        self.assertEqual(self._reload(self.interesting), dsq.INTERESTING_REWRITE)
        self.assertNotIn("Auto Ignore", out)

    def test_revert_restores_the_backed_up_text(self):
        self.run_cmd("--apply")
        backup = os.path.join(self.tmp.name, os.listdir(self.tmp.name)[0])
        # A query edited after the rewrite is left alone by the revert.
        type(self.interesting).objects.filter(pk=self.interesting.pk).update(sql=dsq.INTERESTING_REWRITE + "\n-- tweaked")

        dry = self.run_cmd("--revert", backup)
        self.assertIn(f"REVERT     {self.fast.id}", dry)
        self.assertIn("SQL changed since the backup was written", dry)
        self.assertIn("Dry run", dry)
        self.assertEqual(self._reload(self.fast), dsq.FAST_YOUNG_REWRITE)

        out = self.run_cmd("--revert", backup, "--apply")
        self.assertIn("Updated 1 saved query", out)
        self.assertEqual(self._reload(self.fast), dsq.FAST_YOUNG_ORIGINAL)
        self.assertEqual(self._reload(self.interesting), dsq.INTERESTING_REWRITE + "\n-- tweaked")
        names = sorted(os.listdir(self.tmp.name))
        self.assertEqual(len(names), 2)
        self.assertTrue(any(n.startswith("saved_queries_revert_") for n in names))

        again = self.run_cmd("--revert", backup, "--apply")
        self.assertIn("already holds the backed-up text", again)

    def test_revert_with_a_bad_file_fails_cleanly(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_cmd("--revert", os.path.join(self.tmp.name, "missing.json"))


# --------------------------------------------------------------------------- #
# cache TTL and warmer (#233)
# --------------------------------------------------------------------------- #

class ExplorerQueryCacheSettingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("cache_ttl_user")
        cls.transient = create_minimal_transient(cls.user, name="cache-ttl-1")
        cls.query = make_query(cls.user, "TTL", "SELECT name FROM YSE_App_transient WHERE name = 'cache-ttl-1'")

    def setUp(self):
        cache.clear()

    def test_default_ttl_is_one_hour_and_configurable(self):
        self.assertEqual(settings.EXPLORER_QUERY_CACHE_SECONDS, 3600)
        self.assertEqual(explorer_query_cache_seconds(), 3600)
        with override_settings(EXPLORER_QUERY_CACHE_SECONDS=7200):
            self.assertEqual(explorer_query_cache_seconds(), 7200)

    @override_settings(EXPLORER_QUERY_CACHE_SECONDS=1234)
    def test_run_explorer_query_cached_uses_the_setting_and_refresh(self):
        with mock.patch.object(views_module, "connections", _ExplorerToDefault()), \
                mock.patch.object(views_module.cache, "set", wraps=views_module.cache.set) as cache_set:
            names = views_module.run_explorer_query_cached(self.query)
            self.assertEqual(names, ["cache-ttl-1"])
            self.assertEqual(cache_set.call_args.kwargs["timeout"], 1234)
            # warm hit: no second cache.set
            views_module.run_explorer_query_cached(self.query)
            self.assertEqual(cache_set.call_count, 1)
            # refresh re-runs and re-stores even though the entry is warm
            views_module.run_explorer_query_cached(self.query, refresh=True, timeout=99)
            self.assertEqual(cache_set.call_count, 2)
            self.assertEqual(cache_set.call_args.kwargs["timeout"], 99)


class ExplorerCapOverrideTests(SimpleTestCase):
    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=20000)
    def test_override_disables_the_cap_inside_the_block_only(self):
        self.assertEqual(db_time_cap.explorer_cap_ms(), 20000)
        with mock.patch.object(db_time_cap, "_close_explorer_connection") as close:
            with db_time_cap.explorer_cap_override(0):
                self.assertEqual(db_time_cap.explorer_cap_ms(), 0)
            self.assertEqual(close.call_count, 2)
        self.assertEqual(db_time_cap.explorer_cap_ms(), 20000)

    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=20000)
    def test_override_is_restored_after_an_exception(self):
        with mock.patch.object(db_time_cap, "_close_explorer_connection"):
            with self.assertRaises(RuntimeError):
                with db_time_cap.explorer_cap_override(0):
                    raise RuntimeError("boom")
        self.assertEqual(db_time_cap.explorer_cap_ms(), 20000)


class DashboardCacheWarmerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("warm_user")
        cls.other = create_test_user("warm_other_user")
        cls.t1 = create_minimal_transient(cls.user, name="warm-1")
        cls.t2 = create_minimal_transient(cls.user, name="warm-2", ra=11.0)
        audit = audit_fields(cls.user)
        cls.q1 = make_query(cls.user, "Warm one", "SELECT name FROM YSE_App_transient WHERE name = 'warm-1'")
        cls.q2 = make_query(cls.user, "Warm two", "SELECT name FROM YSE_App_transient WHERE name LIKE 'warm-%'")
        cls.q_bad = make_query(cls.user, "Warm bad", "describe YSE_App_transient")
        cls.q_error = make_query(cls.user, "Warm error", "SELECT name FROM YSE_App_transient_no_such_table")
        cls.q_unattached = make_query(cls.user, "Not on any dashboard", GOOD)
        # q2 pinned by two users: warmed once.
        UserQuery.objects.create(user=cls.user, query=cls.q1, **audit)
        UserQuery.objects.create(user=cls.user, query=cls.q2, **audit)
        UserQuery.objects.create(user=cls.other, query=cls.q2, **audit)
        UserQuery.objects.create(user=cls.other, query=cls.q_bad, **audit)
        UserQuery.objects.create(user=cls.other, query=cls.q_error, **audit)
        UserQuery.objects.create(user=cls.other, python_query="rising_transient_queryset", **audit)

    def setUp(self):
        cache.clear()

    def test_dashboard_saved_queries_are_distinct_and_attached_only(self):
        self.assertEqual(
            list(warmer.dashboard_saved_queries().values_list("id", flat=True)),
            sorted([self.q1.id, self.q2.id, self.q_bad.id, self.q_error.id]),
        )

    def test_warm_runs_each_query_once_with_the_cap_disabled(self):
        seen_caps = []
        real = views_module.run_explorer_query_cached

        def spy(query, timeout=None, refresh=False):
            seen_caps.append(db_time_cap.explorer_cap_ms())
            return real(query, timeout=timeout, refresh=refresh)

        with override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=20000, EXPLORER_QUERY_CACHE_SECONDS=555), \
                mock.patch.object(views_module, "connections", _ExplorerToDefault()), \
                mock.patch.object(views_module, "run_explorer_query_cached", side_effect=spy), \
                mock.patch.object(db_time_cap, "_close_explorer_connection"), \
                mock.patch.object(views_module.cache, "set", wraps=views_module.cache.set) as cache_set:
            results = warmer.warm_dashboard_saved_queries()
            # the override is lifted once the warm run ends
            self.assertEqual(db_time_cap.explorer_cap_ms(), 20000)

        by_id = {r.query_id: r for r in results}
        self.assertEqual(set(by_id), {self.q1.id, self.q2.id, self.q_bad.id, self.q_error.id})
        self.assertEqual(by_id[self.q1.id].n_names, 1)
        self.assertEqual(by_id[self.q2.id].n_names, 2)
        self.assertIn("SELECT", by_id[self.q_bad.id].skipped)
        self.assertIsNotNone(by_id[self.q_error.id].error)
        self.assertEqual(seen_caps, [0, 0, 0])  # q1, q2, q_error ran uncapped; q_bad skipped before running
        self.assertTrue(all(c.kwargs["timeout"] == 555 for c in cache_set.call_args_list))
        # the entries the views read are now warm
        self.assertEqual(cache.get(views_module.explorer_query_cache_key(self.q1.id)), ["warm-1"])
        self.assertEqual(sorted(cache.get(views_module.explorer_query_cache_key(self.q2.id))), ["warm-1", "warm-2"])

    def test_cron_is_registered_and_is_a_no_op_when_disabled(self):
        from YSE_App.data_ingest.Dashboard_Cache_Warm import WarmDashboardQueries

        self.assertIn("YSE_App.data_ingest.Dashboard_Cache_Warm.WarmDashboardQueries", settings.CRON_CLASSES)
        self.assertFalse(settings.DASHBOARD_CACHE_WARM_ENABLED)
        with mock.patch.object(warmer, "warm_dashboard_saved_queries") as warm:
            self.assertEqual(WarmDashboardQueries().do(), "disabled")
            warm.assert_not_called()

    @override_settings(DASHBOARD_CACHE_WARM_ENABLED=True)
    def test_cron_warms_when_enabled(self):
        from YSE_App.data_ingest import Dashboard_Cache_Warm as cron_module

        with mock.patch.object(cron_module, "warm_dashboard_saved_queries",
                               return_value=[warmer.WarmResult(1, "a", 1.0, n_names=3),
                                             warmer.WarmResult(2, "b", 1.0, error="x"),
                                             warmer.WarmResult(3, "c", 0.0, skipped="y")]) as warm:
            summary = cron_module.WarmDashboardQueries().do()
        warm.assert_called_once_with()
        self.assertEqual(summary, "warmed 1/3 saved queries (1 failed, 1 skipped)")


# --------------------------------------------------------------------------- #
# indexes (#248)
# --------------------------------------------------------------------------- #

class DashboardIndexTests(TestCase):
    EXPECTED = {
        "yse_transient_name_idx": ("transient", ["name"]),
        "yse_transient_disc_date_idx": ("transient", ["disc_date"]),
        "yse_transient_status_disc_idx": ("transient", ["status", "disc_date"]),
        "yse_transient_mod_date_idx": ("transient", ["modified_date"]),
        "yse_transient_ra_idx": ("transient", ["ra"]),
        "yse_transient_dec_idx": ("transient", ["dec"]),
        "yse_photdata_obs_date_idx": ("transientphotdata", ["obs_date"]),
        "yse_photdata_phot_obs_idx": ("transientphotdata", ["photometry", "obs_date"]),
    }
    # Added later for the search filters (#286, migration 0027), not part of the #248 list.
    LATER = {
        "yse_transient_gal_b_idx": ("transient", ["gal_b"]),
    }

    def test_models_declare_exactly_the_recommended_indexes(self):
        declared = {}
        for model in (Transient, TransientPhotData):
            for idx in model._meta.indexes:
                declared[idx.name] = (model._meta.model_name, list(idx.fields))
        self.assertEqual(declared, {**self.EXPECTED, **self.LATER})
        # (photometry_id, mag) is deliberately absent: it slowed the rewritten
        # Magnitude-Limited query 2.7x on the synthetic data (docs/dashboard-performance.md).
        self.assertFalse(any(f == ["photometry", "mag"] for _, f in declared.values()))

    def test_migration_0010_adds_them_and_the_graph_is_consistent(self):
        loader = MigrationLoader(connections["default"])
        migration = loader.get_migration("YSE_App", "0010_dashboard_indexes")
        added = {op.index.name: (op.model_name, list(op.index.fields)) for op in migration.operations}
        self.assertEqual(added, self.EXPECTED)
        self.assertEqual(loader.detect_conflicts(), {})

    def test_indexes_exist_in_the_test_database(self):
        connection = connections["default"]
        with connection.cursor() as cursor:
            names = set()
            for table in ("YSE_App_transient", "YSE_App_transientphotdata"):
                names |= set(connection.introspection.get_constraints(cursor, table))
        self.assertTrue(set(self.EXPECTED) <= names, sorted(set(self.EXPECTED) - names))
