"""Saved Explorer SQL runs under a statement time cap on a cache miss (#233)."""

from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError, OperationalError
from django.db.backends.signals import connection_created
from django.test import SimpleTestCase, TestCase, override_settings

from YSE_App.common.db_time_cap import (
    QueryTimeout,
    apply_statement_time_cap,
    cap_explorer_connection,
    explorer_cap_ms,
    translate_query_timeout,
)
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user


class _FakeCursor:
    def __init__(self, log, fail=False):
        self.log = log
        self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise DatabaseError("no privilege")
        self.log.append((sql, list(params) if params is not None else None))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, alias="explorer", vendor="mysql", mariadb=False, fail=False):
        self.alias = alias
        self.vendor = vendor
        self.mysql_is_mariadb = mariadb
        self.executed = []
        self.fail = fail

    def cursor(self):
        return _FakeCursor(self.executed, self.fail)


class ApplyStatementTimeCapTests(SimpleTestCase):
    def test_mysql_sets_max_execution_time(self):
        conn = _FakeConnection()
        self.assertTrue(apply_statement_time_cap(conn, 20000))
        self.assertEqual(conn.executed, [("SET SESSION max_execution_time = %s", [20000])])

    def test_mariadb_uses_max_statement_time_in_seconds(self):
        conn = _FakeConnection(mariadb=True)
        self.assertTrue(apply_statement_time_cap(conn, 1500))
        self.assertEqual(conn.executed, [("SET SESSION max_statement_time = %s", [1.5])])

    def test_zero_cap_and_non_mysql_are_no_ops(self):
        sqlite = _FakeConnection(vendor="sqlite")
        self.assertFalse(apply_statement_time_cap(sqlite, 5000))
        mysql = _FakeConnection()
        self.assertFalse(apply_statement_time_cap(mysql, 0))
        self.assertEqual(sqlite.executed + mysql.executed, [])

    def test_failing_set_is_logged_not_raised(self):
        conn = _FakeConnection(fail=True)
        with self.assertLogs("YSE_App.common.db_time_cap", level="WARNING"):
            self.assertFalse(apply_statement_time_cap(conn, 100))


class ExplorerConnectionSignalTests(SimpleTestCase):
    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=4321)
    def test_receiver_caps_only_the_explorer_alias(self):
        explorer, default = _FakeConnection(alias="explorer"), _FakeConnection(alias="default")
        cap_explorer_connection(sender=None, connection=explorer)
        cap_explorer_connection(sender=None, connection=default)
        self.assertEqual(explorer.executed, [("SET SESSION max_execution_time = %s", [4321])])
        self.assertEqual(default.executed, [])

    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=4321)
    def test_receiver_is_wired_to_connection_created(self):
        conn = _FakeConnection(alias="explorer")
        connection_created.send(sender=_FakeConnection, connection=conn)
        self.assertEqual(conn.executed, [("SET SESSION max_execution_time = %s", [4321])])

    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=0)
    def test_zero_disables_the_cap(self):
        conn = _FakeConnection(alias="explorer")
        connection_created.send(sender=_FakeConnection, connection=conn)
        self.assertEqual(conn.executed, [])

    def test_default_cap_setting_is_twenty_seconds(self):
        self.assertEqual(explorer_cap_ms(), 20000)


class TranslateQueryTimeoutTests(SimpleTestCase):
    def test_timeout_error_is_translated(self):
        with self.assertRaises(QueryTimeout) as ctx:
            with translate_query_timeout(250):
                raise OperationalError(3024, "Query execution was interrupted")
        self.assertIn("250 ms", str(ctx.exception))
        self.assertIsInstance(ctx.exception, DatabaseError)

    def test_mariadb_timeout_error_is_translated(self):
        with self.assertRaises(QueryTimeout):
            with translate_query_timeout(250):
                raise OperationalError(1969, "Query execution was interrupted (max_statement_time exceeded)")

    def test_other_errors_pass_through_unchanged(self):
        with self.assertRaises(OperationalError) as ctx:
            with translate_query_timeout(250):
                raise OperationalError(1146, "Table does not exist")
        self.assertNotIsInstance(ctx.exception, QueryTimeout)
        with self.assertRaises(ValueError):
            with translate_query_timeout(250):
                raise ValueError("not a database error")


class RunExplorerQueryCachedTests(TestCase):
    """Route the 'explorer' alias at the test database (as the perf tests do)."""

    def setUp(self):
        cache.clear()
        from django.db import connections
        from explorer.models import Query

        from YSE_App import views

        router = mock.patch.object(views, "connections", {"explorer": connections["default"]})
        router.start()
        self.addCleanup(router.stop)
        self.addCleanup(cache.clear)

        self.user = create_test_user("time_cap_user")
        transient = create_minimal_transient(self.user, name="time-cap-sn")
        self.query = Query.objects.create(
            title="time cap query",
            sql="SELECT name FROM YSE_App_transient WHERE name = '%s'" % transient.name,
            created_by_user=self.user,
        )

    def test_cache_miss_runs_the_sql_and_caches_the_names(self):
        from YSE_App import views

        with mock.patch.object(views, "translate_query_timeout", wraps=translate_query_timeout) as guard:
            names = views.run_explorer_query_cached(self.query)
        guard.assert_called_once_with(20000)
        self.assertEqual(names, ["time-cap-sn"])
        self.assertEqual(cache.get(views.explorer_query_cache_key(self.query.id)), ["time-cap-sn"])

    def test_cache_hit_skips_the_database(self):
        from YSE_App import views

        views.run_explorer_query_cached(self.query)
        with mock.patch.object(views, "connections", {}):
            names = views.run_explorer_query_cached(self.query)
        self.assertEqual(names, ["time-cap-sn"])

    def test_timeout_surfaces_as_query_timeout_and_is_not_cached(self):
        from YSE_App import views

        cursor = mock.MagicMock()
        cursor.execute.side_effect = OperationalError(3024, "Query execution was interrupted")
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        with mock.patch.object(views, "connections", {"explorer": conn}):
            with self.assertRaises(QueryTimeout) as ctx:
                views.run_explorer_query_cached(self.query)
        self.assertIn("20000 ms", str(ctx.exception))
        cursor.close.assert_called_once()
        self.assertIsNone(cache.get(views.explorer_query_cache_key(self.query.id)))

    def test_query_errors_propagate_and_are_not_cached(self):
        from explorer.models import Query

        from YSE_App import views

        broken = Query.objects.create(
            title="broken", sql="SELECT name FROM no_such_table_here", created_by_user=self.user
        )
        with self.assertRaises(DatabaseError):
            views.run_explorer_query_cached(broken)
        self.assertIsNone(cache.get(views.explorer_query_cache_key(broken.id)))
