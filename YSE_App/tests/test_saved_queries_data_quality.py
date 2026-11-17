"""
Saved SQL that used the dropped ``data_quality_id`` column (issue #939).

* The rewrite helper (``YSE_App/queries/data_quality_sql.py``) on synthetic SQL.
* The docker seed data (``docker/db_init/YSE_rest_of_tables_insert.sql``) and
  ``docs/example_queries.rst`` hold no reference to the column, and every
  repo-defined query that uses the replacement join table names only tables and
  columns the models define; on MySQL (CI) each one also goes through EXPLAIN.
* The rewritten predicate keeps exactly the points with no DataQuality flag.
* ``manage.py rewrite_data_quality_queries`` on synthetic Explorer rows.

Synthetic data only.
"""

import json
import os
import re
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase

from YSE_App.models import DataQuality, TransientPhotData
from YSE_App.queries import dashboard_saved_queries as dsq
from YSE_App.queries.data_quality_sql import (
    explorer_rows_from_dump,
    iter_dump_rows,
    mysql_escape,
    rewrite_data_quality_sql,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

REPO = Path(__file__).resolve().parents[2]
SEED = REPO / "docker" / "db_init" / "YSE_rest_of_tables_insert.sql"
EXAMPLES = REPO / "docs" / "example_queries.rst"
COLUMN_REF = re.compile(r"\b\w+\s*\.\s*`?data_quality_id\b", re.I)
NOT_EXISTS_PD2 = ("NOT EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality dq "
                  "WHERE dq.transientphotdata_id = pd2.id)")


def _seed_text():
    if not SEED.exists():
        raise AssertionError(f"{SEED} is missing")
    return SEED.read_bytes().decode("latin-1")


def _example_queries():
    """The SQL blocks of docs/example_queries.rst (indented literal blocks after '::')."""
    blocks, current = [], None
    for line in EXAMPLES.read_text(encoding="utf-8").splitlines():
        if line.strip() == "::":
            current = []
            blocks.append(current)
        elif current is not None:
            if line.strip() and not line[:1].isspace():
                current = None
            else:
                current.append(line)
    return [s for s in ("\n".join(b).strip() for b in blocks) if s.upper().startswith("SELECT")]


def repo_queries():
    """[(label, sql)] for every repo-defined query that uses a data-quality join table."""
    out = [(f"seed explorer_query {qid} ({title})", sql)
           for qid, title, sql in explorer_rows_from_dump(_seed_text())
           if "_data_quality dq" in sql]
    out += [(f"docs/example_queries.rst #{n}", sql)
            for n, sql in enumerate(_example_queries(), 1) if "_data_quality" in sql]
    out += [("dashboard_saved_queries.MAG_LIMITED_ORIGINAL_M2M", dsq.MAG_LIMITED_ORIGINAL_M2M),
            ("dashboard_saved_queries.MAG_LIMITED_REWRITE_M2M", dsq.MAG_LIMITED_REWRITE_M2M)]
    return out


# --------------------------------------------------------------------------- #
# helper
# --------------------------------------------------------------------------- #

class RewriteHelperTests(SimpleTestCase):
    def test_isnull_true_becomes_not_exists_on_the_join_table(self):
        sql = ("SELECT pd2.id FROM YSE_App_transientphotdata pd2, YSE_App_transientphotometry p2\r\n"
               "WHERE pd2.photometry_id = p2.id AND ISNULL(pd2.data_quality_id) = True\r\nORDER BY pd2.mag")
        r = rewrite_data_quality_sql(sql)
        self.assertEqual(r.state, "rewrite")
        self.assertEqual(r.changed, 1)
        self.assertEqual(r.sql, sql.replace("ISNULL(pd2.data_quality_id) = True", NOT_EXISTS_PD2))
        self.assertIn("\r\n", r.sql)  # untouched text keeps its line endings

    def test_other_predicate_spellings(self):
        base = "SELECT 1 FROM YSE_App_transientphotdata AS pd3 WHERE "
        exists = ("EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality dq "
                  "WHERE dq.transientphotdata_id = pd3.id)")
        cases = {
            "isnull(pd3.data_quality_id)=TRUE": "NOT " + exists,
            "ISNULL( pd3.data_quality_id ) = 1": "NOT " + exists,
            "ISNULL(pd3.data_quality_id) = False": exists,
            "pd3.data_quality_id IS NULL": "NOT " + exists,
            "pd3.data_quality_id is not null": exists,
        }
        for pred, want in cases.items():
            with self.subTest(pred=pred):
                self.assertEqual(rewrite_data_quality_sql(base + pred).sql, base + want)

    def test_host_and_spectrum_tables_use_their_own_join_table(self):
        r = rewrite_data_quality_sql(
            "SELECT 1 FROM YSE_App_hostphotdata hp JOIN YSE_App_transientspectrum ts ON 1=1 "
            "WHERE ISNULL(hp.data_quality_id) = True AND ts.data_quality_id IS NULL")
        self.assertIn("FROM YSE_App_hostphotdata_data_quality dq WHERE dq.hostphotdata_id = hp.id", r.sql)
        self.assertIn("FROM YSE_App_transientspectrum_data_quality dq WHERE dq.transientspectrum_id = ts.id", r.sql)
        self.assertEqual(r.changed, 2)

    def test_select_list_column_keeps_its_name(self):
        r = rewrite_data_quality_sql(
            "SELECT DISTINCT t.name, pd.mag, pd.data_quality_id, h.photo_z\nFROM YSE_App_transient t "
            "INNER JOIN YSE_App_transientphotometry tp ON tp.transient_id = t.id "
            "INNER JOIN YSE_App_transientphotdata pd ON pd.photometry_id = tp.id")
        self.assertIn("(SELECT MIN(dq.dataquality_id) FROM YSE_App_transientphotdata_data_quality dq "
                      "WHERE dq.transientphotdata_id = pd.id) AS data_quality_id, h.photo_z", r.sql)
        self.assertEqual(r.state, "rewrite")
        aliased = rewrite_data_quality_sql("SELECT pd.data_quality_id AS dq_flag FROM YSE_App_transientphotdata pd")
        self.assertIn("= pd.id) AS dq_flag FROM", aliased.sql)

    def test_unresolved_alias_and_other_clauses_are_left_for_review(self):
        for sql in (
            "SELECT 1 FROM (SELECT * FROM YSE_App_transientphotdata) x WHERE ISNULL(x.data_quality_id) = True",
            "SELECT pd.id FROM YSE_App_transientphotdata pd ORDER BY pd.data_quality_id, pd.id",
            "SELECT pd.id FROM YSE_App_transientphotdata pd WHERE COALESCE(pd.data_quality_id, 0) = 0",
        ):
            with self.subTest(sql=sql):
                r = rewrite_data_quality_sql(sql)
                self.assertEqual(r.state, "review")
                self.assertEqual(r.sql, sql)
                self.assertTrue(r.leftover)

    def test_clean_sql_and_idempotence(self):
        self.assertEqual(rewrite_data_quality_sql("SELECT name FROM YSE_App_transient").state, "clean")
        once = rewrite_data_quality_sql(
            "SELECT pd.data_quality_id FROM YSE_App_transientphotdata pd WHERE ISNULL(pd.data_quality_id) = True")
        self.assertEqual(once.changed, 2)
        twice = rewrite_data_quality_sql(once.sql)
        self.assertEqual((twice.state, twice.sql), ("clean", once.sql))

    def test_magnitude_limited_dashboard_query_stays_recognised(self):
        rw = next(r for r in dsq.REWRITES if r.fixture_id == 62)
        self.assertEqual(rewrite_data_quality_sql(dsq.MAG_LIMITED_ORIGINAL).sql, dsq.MAG_LIMITED_ORIGINAL_M2M)
        self.assertEqual(rewrite_data_quality_sql(dsq.MAG_LIMITED_REWRITE).sql, dsq.MAG_LIMITED_REWRITE_M2M)
        self.assertEqual(dsq.classify_saved_sql(rw, rewrite_data_quality_sql(dsq.MAG_LIMITED_ORIGINAL).sql), "original")
        self.assertEqual(dsq.classify_saved_sql(rw, rewrite_data_quality_sql(dsq.MAG_LIMITED_REWRITE).sql), "rewritten")

    def test_dump_parser_round_trips(self):
        line = ("INSERT INTO `explorer_query` VALUES (7,'a \\'b\\'','SELECT 1\\r\\nFROM x\\\\y',NULL),"
                "(8,'c','d',NULL);")
        rows = list(iter_dump_rows(line))
        self.assertEqual([r[0][0] for r in rows], ["7", "8"])
        self.assertEqual(rows[0][1][0], "a 'b'")
        self.assertEqual(rows[0][2][0], "SELECT 1\r\nFROM x\\y")
        for row in rows:
            for value, start, end in row:
                if line[start] == "'":
                    self.assertEqual(mysql_escape(value), line[start:end])
        self.assertEqual(explorer_rows_from_dump("x\n" + line + "\n"), [(7, "a 'b'", "SELECT 1\r\nFROM x\\y"), (8, "c", "d")])


# --------------------------------------------------------------------------- #
# repo-defined queries
# --------------------------------------------------------------------------- #

def _model_columns():
    tables = {}
    for model in apps.get_models(include_auto_created=True):
        cols = {f.column.lower() for f in model._meta.local_concrete_fields}
        tables.setdefault(model._meta.db_table.lower(), set()).update(cols)
    return tables


_NOT_ALIAS = {"ON", "WHERE", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "JOIN", "GROUP", "ORDER",
              "LIMIT", "AND", "OR", "UNION", "HAVING", "USING", "NATURAL", "SET", "AS"}


def unknown_columns(sql, tables):
    """``alias.column`` references whose alias names a model table but whose
    column that table does not have (comments stripped, backticks ignored)."""
    text = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text).replace("`", "")
    aliases = {}
    for table, alias in re.findall(r"\b(\w+)\s+(?:AS\s+)?(\w+)\b", text, re.I):
        if table.lower() in tables and alias.upper() not in _NOT_ALIAS:
            aliases.setdefault(alias.lower(), set()).add(table.lower())
    for table in tables:
        aliases.setdefault(table, set()).add(table)
    bad = set()
    for alias, col in re.findall(r"\b(\w+)\s*\.\s*(\w+)\b", text):
        owners = aliases.get(alias.lower())
        if owners and not any(col.lower() in tables[t] for t in owners):
            bad.add(f"{alias}.{col}")
    return sorted(bad)


class RepoDefinedQueriesTests(TestCase):
    def test_seed_data_and_docs_no_longer_use_the_column(self):
        rows = explorer_rows_from_dump(_seed_text())
        self.assertGreater(len(rows), 300)
        stale = [qid for qid, _title, sql in rows if COLUMN_REF.search(sql)]
        self.assertEqual(stale, [])
        self.assertEqual([qid for qid, _t, sql in rows if rewrite_data_quality_sql(sql).state != "clean"], [])
        # the 43 seed queries that used the column now use the join table
        self.assertEqual(sum("_data_quality dq" in sql for _q, _t, sql in rows), 43)
        self.assertFalse(COLUMN_REF.search(EXAMPLES.read_text(encoding="utf-8")))

    def test_join_table_and_columns_exist_on_the_models(self):
        tables = _model_columns()
        self.assertIn("transientphotdata_id", tables["yse_app_transientphotdata_data_quality"])
        self.assertIn("dataquality_id", tables["yse_app_transientphotdata_data_quality"])
        self.assertNotIn("data_quality_id", tables["yse_app_transientphotdata"])
        queries = repo_queries()
        self.assertGreaterEqual(len(queries), 46)
        for label, sql in queries:
            with self.subTest(query=label):
                self.assertEqual(unknown_columns(sql, tables), [])

    def test_each_query_passes_explain_on_mysql(self):
        if connection.vendor != "mysql":
            self.skipTest("the saved queries are MySQL SQL; CI runs them on MySQL")
        failures = []
        with connection.cursor() as cursor:
            for label, sql in repo_queries():
                try:
                    cursor.execute("EXPLAIN " + sql.strip().rstrip(";"))
                    cursor.fetchall()
                except Exception as exc:  # report every failing query, not just the first
                    failures.append(f"{label}: {exc}")
        self.assertEqual(failures, [])


class RewrittenPredicateResultTests(TestCase):
    """The join-table predicate keeps exactly the points with no DataQuality flag."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("dq_sql_user")
        transient = create_minimal_transient(cls.user, name="2026dqsql")
        phot = attach_synthetic_photometry(cls.user, transient, n_points=6)
        cls.points = list(TransientPhotData.objects.filter(photometry=phot).order_by("id"))
        bad = DataQuality.objects.get_or_create(name="Bad", defaults=audit_fields(cls.user))[0]
        sat = DataQuality.objects.get_or_create(name="Saturation", defaults=audit_fields(cls.user))[0]
        cls.points[1].data_quality.add(bad)
        cls.points[3].data_quality.add(bad, sat)
        cls.photometry_id = phot.id

    def _ids(self, sql):
        with connection.cursor() as cursor:
            cursor.execute(rewrite_data_quality_sql(sql).sql, [self.photometry_id])
            return [row for row in cursor.fetchall()]

    def test_unflagged_and_flagged_rows(self):
        base = "SELECT pd2.id FROM YSE_App_transientphotdata pd2 WHERE pd2.photometry_id = %s AND "
        clean = {p.id for p in self.points} - {self.points[1].id, self.points[3].id}
        self.assertEqual({r[0] for r in self._ids(base + "ISNULL(pd2.data_quality_id) = True")}, clean)
        self.assertEqual({r[0] for r in self._ids(base + "pd2.data_quality_id IS NOT NULL")},
                         {self.points[1].id, self.points[3].id})

    def test_select_list_column(self):
        rows = dict(self._ids(
            "SELECT pd.id, pd.data_quality_id FROM YSE_App_transientphotdata pd WHERE pd.photometry_id = %s"))
        self.assertIsNone(rows[self.points[0].id])
        self.assertEqual(rows[self.points[1].id], DataQuality.objects.get(name="Bad").id)
        self.assertEqual(rows[self.points[3].id],
                         min(DataQuality.objects.filter(name__in=["Bad", "Saturation"]).values_list("id", flat=True)))


# --------------------------------------------------------------------------- #
# management command
# --------------------------------------------------------------------------- #

def make_query(user, title, sql):
    from explorer.models import Query

    return Query.objects.create(title=title, sql=sql, description="", snapshot=False, created_by_user=user)


OLD = ("SELECT t.name FROM YSE_App_transient t, YSE_App_transientphotometry p2, YSE_App_transientphotdata pd2\n"
       "WHERE pd2.photometry_id = p2.id AND p2.transient_id = t.id AND ISNULL(pd2.data_quality_id) = True")
NEW = OLD.replace("ISNULL(pd2.data_quality_id) = True", NOT_EXISTS_PD2)
ODD = "SELECT pd.id FROM YSE_App_transientphotdata pd WHERE COALESCE(pd.data_quality_id, 0) = 0"


class RewriteDataQualityQueriesCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("dq_cmd_user")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = make_query(self.user, "synthetic old", OLD)
        self.other = make_query(self.user, "synthetic other", OLD.replace("t.name", "t.ra"))
        self.odd = make_query(self.user, "synthetic odd", ODD)
        self.clean = make_query(self.user, "synthetic clean", "SELECT name FROM YSE_App_transient")

    def run_cmd(self, *args):
        out = StringIO()
        call_command("rewrite_data_quality_queries", *args, "--backup-dir", self.tmp.name, stdout=out)
        return out.getvalue()

    def _sql(self, q):
        return type(q).objects.get(pk=q.pk).sql

    def test_dry_run_is_the_default(self):
        out = self.run_cmd()
        self.assertIn(f"REWRITE    {self.old.id} synthetic old", out)
        self.assertIn("+" + NEW.splitlines()[1], out)
        self.assertIn(f"REVIEW     {self.odd.id} synthetic odd", out)
        self.assertIn("COALESCE(pd.data_quality_id", out)
        self.assertNotIn("synthetic clean", out)
        self.assertIn("Dry run", out)
        self.assertEqual(self._sql(self.old), OLD)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_apply_backs_up_rewrites_and_is_idempotent(self):
        out = self.run_cmd("--apply")
        self.assertIn("Updated 2 saved queries", out)
        self.assertEqual(self._sql(self.old), NEW)
        self.assertEqual(self._sql(self.odd), ODD)
        files = os.listdir(self.tmp.name)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("saved_queries_data_quality_rewrite_"))
        with open(os.path.join(self.tmp.name, files[0]), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["command"], "manage.py rewrite_data_quality_queries")
        self.assertEqual({e["id"] for e in payload["queries"]}, {self.old.id, self.other.id})
        self.assertIn("Revert with: manage.py rewrite_data_quality_queries --revert", out)
        again = self.run_cmd("--apply")
        self.assertIn("Nothing to change", again)
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_id_filter_and_revert(self):
        self.run_cmd("--apply", "--id", str(self.old.id))
        self.assertEqual(self._sql(self.old), NEW)
        self.assertEqual(self._sql(self.other), OLD.replace("t.name", "t.ra"))
        backup = os.path.join(self.tmp.name, os.listdir(self.tmp.name)[0])
        out = self.run_cmd("--revert", backup, "--apply")
        self.assertIn(f"REVERT     {self.old.id} synthetic old", out)
        self.assertEqual(self._sql(self.old), OLD)

    def test_refuses_to_apply_while_the_column_still_exists(self):
        target = "YSE_App.management.commands.rewrite_data_quality_queries.photdata_has_data_quality_column"
        with mock.patch(target, return_value=True):
            out = self.run_cmd()
            self.assertIn("still has the data_quality_id column", out)
            self.assertIn("REWRITE", out)
            with self.assertRaises(CommandError):
                self.run_cmd("--apply")
        self.assertEqual(self._sql(self.old), OLD)

    def test_migrated_test_database_has_no_column(self):
        from YSE_App.management.commands.rewrite_data_quality_queries import photdata_has_data_quality_column

        self.assertFalse(photdata_has_data_quality_column())
