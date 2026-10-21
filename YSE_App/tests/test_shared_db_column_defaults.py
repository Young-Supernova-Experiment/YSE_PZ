"""0030: NOT NULL columns added since 0011 keep a MySQL DEFAULT (#395).

yse_test (develop) shares the YSE_test database with yse_experimental, so code
that predates a column inserts rows without it; without a DB default MySQL
refuses them ("Field 'usage_hours' doesn't have a default value").
"""

import importlib
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

defaults = importlib.import_module("YSE_App.migrations.0030_shared_db_column_defaults")


@skipUnless(connection.vendor == "mysql", "DB-level defaults are MySQL-only")
class SharedDbColumnDefaultsTests(TestCase):
    def _column_default(self, table, column):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_DEFAULT, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                [table, column],
            )
            return cursor.fetchone()

    def test_columns_have_database_defaults(self):
        expected = {"usage_hours": "0", "weather_url": "", "weather_link": "", "skycam_url": ""}
        for table, column, _definition in defaults.COLUMN_DEFAULTS:
            with self.subTest(column=column):
                row = self._column_default(table, column)
                self.assertIsNotNone(row, f"{table}.{column} missing")
                column_default, is_nullable = row
                self.assertEqual(is_nullable, "NO")
                self.assertEqual(column_default, expected[column])

    def test_followup_insert_without_usage_hours_succeeds(self):
        """The statement develop's code issues: no usage_hours in the column list."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'YSE_App_transientfollowup' "
                "AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL AND EXTRA NOT LIKE '%%auto_increment%%'"
            )
            required = {r[0] for r in cursor.fetchall()}
        self.assertNotIn("usage_hours", required)
