"""Client-side DB timeouts (#770): parsing, settings wiring, and a real MySQL read timeout."""

import copy
import time
from typing import Dict, Optional

from django.db import DEFAULT_DB_ALIAS, OperationalError, connections
from django.test import SimpleTestCase, TestCase

from YSE_App.tests.test_settings_hardening import _load_settings
from YSE_PZ.db_timeouts import timeout_options


def _ini(values: Dict[str, str]):
    """Return an ``ini_get`` callable backed by a dict (missing keys give None)."""
    def get(key: str) -> Optional[str]:
        return values.get(key)
    return get


class TimeoutOptionsTests(SimpleTestCase):
    def test_nothing_configured_means_no_options(self) -> None:
        self.assertEqual(timeout_options(_ini({}), environ={}), {})
        self.assertEqual(timeout_options(_ini({}), environ={}, explorer=True), {})

    def test_ini_values(self) -> None:
        ini = _ini({"DB_CONNECT_TIMEOUT": "10", "DB_READ_TIMEOUT": "120", "DB_WRITE_TIMEOUT": "60"})
        self.assertEqual(timeout_options(ini, environ={}),
                         {"connect_timeout": 10, "read_timeout": 120, "write_timeout": 60})

    def test_env_wins_over_ini(self) -> None:
        ini = _ini({"DB_READ_TIMEOUT": "120"})
        self.assertEqual(timeout_options(ini, environ={"YSE_DB_READ_TIMEOUT": "30"}), {"read_timeout": 30})

    def test_zero_and_blank_mean_no_timeout(self) -> None:
        ini = _ini({"DB_CONNECT_TIMEOUT": "0", "DB_READ_TIMEOUT": " "})
        self.assertEqual(timeout_options(ini, environ={}), {})
        # an explicit 0 in the environment turns off a timeout set in the ini (one-off commands)
        self.assertEqual(timeout_options(_ini({"DB_READ_TIMEOUT": "120"}), environ={"YSE_DB_READ_TIMEOUT": "0"}), {})

    def test_bad_value_fails_loudly_naming_the_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "DB_READ_TIMEOUT"):
            timeout_options(_ini({"DB_READ_TIMEOUT": "2m"}), environ={})
        with self.assertRaisesRegex(ValueError, "YSE_DB_CONNECT_TIMEOUT"):
            timeout_options(_ini({}), environ={"YSE_DB_CONNECT_TIMEOUT": "-1"})

    def test_explorer_read_timeout_replaces_the_common_one(self) -> None:
        ini = _ini({"DB_CONNECT_TIMEOUT": "10", "DB_READ_TIMEOUT": "120", "EXPLORER_DB_READ_TIMEOUT": "30"})
        self.assertEqual(timeout_options(ini, environ={}, explorer=True), {"connect_timeout": 10, "read_timeout": 30})
        self.assertEqual(timeout_options(ini, environ={})["read_timeout"], 120)

    def test_explorer_falls_back_and_explicit_zero_disables(self) -> None:
        ini = _ini({"DB_READ_TIMEOUT": "120"})
        self.assertEqual(timeout_options(ini, environ={}, explorer=True), {"read_timeout": 120})
        ini = _ini({"DB_READ_TIMEOUT": "120", "EXPLORER_DB_READ_TIMEOUT": "0"})
        self.assertEqual(timeout_options(ini, environ={}, explorer=True), {})


class SettingsWiringTests(SimpleTestCase):
    def test_unset_keeps_todays_options(self) -> None:
        mod = _load_settings(debug="True")
        self.assertEqual(mod.DATABASES["default"]["OPTIONS"], {"ssl": {"ssl_disabled": True}})
        self.assertNotIn("OPTIONS", mod.DATABASES["explorer"])

    def test_ini_timeouts_reach_both_aliases(self) -> None:
        mod = _load_settings(debug="True", database_extra=(
            "DB_CONNECT_TIMEOUT: 10\nDB_READ_TIMEOUT: 120\nEXPLORER_DB_READ_TIMEOUT: 30"))
        self.assertEqual(mod.DATABASES["default"]["OPTIONS"],
                         {"ssl": {"ssl_disabled": True}, "connect_timeout": 10, "read_timeout": 120})
        self.assertEqual(mod.DATABASES["explorer"]["OPTIONS"], {"connect_timeout": 10, "read_timeout": 30})

    def test_env_timeout(self) -> None:
        mod = _load_settings(debug="True", env={"YSE_DB_READ_TIMEOUT": "45"})
        self.assertEqual(mod.DATABASES["default"]["OPTIONS"]["read_timeout"], 45)
        self.assertEqual(mod.DATABASES["explorer"]["OPTIONS"], {"read_timeout": 45})


class MysqlReadTimeoutTests(TestCase):
    """Against the real test database: a statement slower than read_timeout fails instead of hanging."""

    def _probe_connection(self, **options: int):
        """Open a separate connection to the test DB with extra OPTIONS.

        Parameters:
            options: mysqlclient options to add (e.g. read_timeout=1).

        Returns:
            A Django DatabaseWrapper the caller must close.
        """
        base = connections[DEFAULT_DB_ALIAS]
        if base.vendor != "mysql":
            self.skipTest("needs MySQL")
        settings_dict = copy.deepcopy(base.settings_dict)
        settings_dict["OPTIONS"] = dict(settings_dict.get("OPTIONS") or {}, **options)
        conn = type(base)(settings_dict, alias="timeout_probe")
        self.addCleanup(conn.close)
        return conn

    def test_fast_statement_passes(self) -> None:
        conn = self._probe_connection(read_timeout=2)
        with conn.cursor() as cursor:
            cursor.execute("SELECT SLEEP(0), 1")
            self.assertEqual(cursor.fetchone()[1], 1)

    def test_slow_statement_raises_within_three_timeouts(self) -> None:
        conn = self._probe_connection(read_timeout=1)
        start = time.monotonic()
        with self.assertRaises(OperationalError):
            with conn.cursor() as cursor:
                cursor.execute("SELECT SLEEP(20)")
        # libmysqlclient retries a timed-out read, so up to ~3 x read_timeout
        self.assertLess(time.monotonic() - start, 10)
