"""Saved Explorer SQL runs under a statement time cap on a cache miss (#233)."""

from contextlib import contextmanager
from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError, OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from YSE_App.common import db_time_cap
from YSE_App.common.db_time_cap import QueryTimeout, statement_time_cap
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user


class _FakeCursor:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise DatabaseError("no privilege")
        self.log.append((sql, list(params) if params is not None else None))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, vendor="mysql", mariadb=False, fail_on=None):
        self.vendor = vendor
        self.mysql_is_mariadb = mariadb
        self.executed = []
        self.fail_on = fail_on

    def cursor(self):
        return _FakeCursor(self.executed, self.fail_on)


@contextmanager
def _fake_connections(conn):
    with mock.patch.object(db_time_cap, "connections", {"explorer": conn}):
        yield conn


class StatementTimeCapUnitTests(SimpleTestCase):
    def test_mysql_sets_and_resets_max_execution_time(self):
        with _fake_connections(_FakeConnection()) as conn:
            with statement_time_cap("explorer", 20000):
                conn.executed.append(("SELECT 1", None))
        self.assertEqual(
            conn.executed,
            [
                ("SET SESSION max_execution_time = %s", [20000]),
                ("SELECT 1", None),
                ("SET SESSION max_execution_time = 0", None),
            ],
        )

    def test_mariadb_uses_max_statement_time_in_seconds(self):
        with _fake_connections(_FakeConnection(mariadb=True)) as conn:
            with statement_time_cap("explorer", 1500):
                pass
        self.assertEqual(conn.executed[0], ("SET SESSION max_statement_time = %s", [1.5]))
        self.assertEqual(conn.executed[-1], ("SET SESSION max_statement_time = 0", None))

    def test_reset_runs_even_when_the_body_raises(self):
        with _fake_connections(_FakeConnection()) as conn:
            with self.assertRaises(ValueError):
                with statement_time_cap("explorer", 100):
                    raise ValueError("boom")
        self.assertEqual(conn.executed[-1], ("SET SESSION max_execution_time = 0", None))

    def test_timeout_error_is_translated(self):
        with _fake_connections(_FakeConnection()):
            with self.assertRaises(QueryTimeout) as ctx:
                with statement_time_cap("explorer", 250):
                    raise OperationalError(3024, "Query execution was interrupted")
        self.assertIn("250 ms", str(ctx.exception))
        self.assertIsInstance(ctx.exception, DatabaseError)

    def test_other_database_errors_pass_through_unchanged(self):
        with _fake_connections(_FakeConnection()):
            with self.assertRaises(OperationalError) as ctx:
                with statement_time_cap("explorer", 250):
                    raise OperationalError(1146, "Table does not exist")
        self.assertNotIsInstance(ctx.exception, QueryTimeout)

    def test_zero_cap_and_non_mysql_are_no_ops(self):
        for conn in (_FakeConnection(vendor="sqlite"), _FakeConnection()):
            with _fake_connections(conn):
                with statement_time_cap("explorer", 0 if conn.vendor == "mysql" else 5000):
                    pass
            self.assertEqual(conn.executed, [])

    def test_set_session_failure_still_runs_the_body(self):
        with _fake_connections(_FakeConnection(fail_on="SET SESSION")) as conn:
            ran = []
            with statement_time_cap("explorer", 100):
                ran.append(True)
        self.assertEqual(ran, [True])
        self.assertEqual(conn.executed, [])


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

        self.user = create_test_user("time_cap_user")
        transient = create_minimal_transient(self.user, name="time-cap-sn")
        self.query = Query.objects.create(
            title="time cap query",
            sql="SELECT name FROM YSE_App_transient WHERE name = '%s'" % transient.name,
            created_by_user=self.user,
        )

    def tearDown(self):
        cache.clear()

    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=12345)
    def test_cache_miss_runs_under_the_configured_cap(self):
        from YSE_App import views

        with mock.patch.object(views, "statement_time_cap", wraps=statement_time_cap) as cap:
            names = views.run_explorer_query_cached(self.query)
        cap.assert_called_once_with("explorer", 12345)
        self.assertEqual(names, ["time-cap-sn"])

    @override_settings(EXPLORER_QUERY_MAX_EXECUTION_MS=12345)
    def test_cache_hit_skips_the_database(self):
        from YSE_App import views

        views.run_explorer_query_cached(self.query)
        with mock.patch.object(views, "statement_time_cap") as cap:
            names = views.run_explorer_query_cached(self.query)
        cap.assert_not_called()
        self.assertEqual(names, ["time-cap-sn"])

    def test_default_cap_setting_is_twenty_seconds(self):
        from django.conf import settings

        self.assertEqual(settings.EXPLORER_QUERY_MAX_EXECUTION_MS, 20000)
