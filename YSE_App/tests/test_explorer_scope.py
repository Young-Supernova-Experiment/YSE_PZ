"""SQL Explorer queries read only the viewer's data (#723).

Every Explorer query, and every saved query that feeds a page, is rewritten so
each data table it reads holds only the rows its viewer may see (per point).
These tests compare what the rewritten SQL returns with what the application's
own read rules give the same viewer, for queries written many different ways,
and check that statements the layer cannot check are refused. Service readers
are not rewritten.

Synthetic users, groups and transients only.
"""

import csv
import io
import unittest
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group
from django.core.cache import cache
from django.db import connection, connections
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.models import (
    DataAccessRule,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
    TransientSpectrum,
)
from YSE_App.perf.access import MEMBER_GROUP_NAME, OTHER_GROUP_NAME, create_non_staff_user, seed_access_transients
from YSE_App.services import explorer_scope
from YSE_App.services.explorer_scope import ExplorerQueryRefused, rewrite
from YSE_App.services.visibility import filter_listed_transients, visible_photometry_ids_q
from YSE_App.tests import test_search_save as _search_save  # the module, so its tests are not collected twice
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user

PREFIX = 'xsc'
OWNER = 'xsc_owner'
MYSQL = unittest.skipUnless(connection.vendor == 'mysql', 'the rewritten SQL is MySQL')

PHOT_FROM = (
    "FROM YSE_App_transientphotdata pd "
    "JOIN YSE_App_transientphotometry p ON p.id = pd.photometry_id "
    "JOIN YSE_App_transient t ON t.id = p.transient_id "
    "WHERE t.name LIKE 'xsc%'"
)


def run(sql, user):
    """Rows of ``sql`` rewritten for ``user``, on the test database."""
    plan = rewrite(sql, user)
    with connection.cursor() as cursor:
        cursor.execute(plan.sql.replace('%', '%%'), ())
        return [tuple(r) for r in cursor.fetchall()]


def scalar(sql, user):
    return run(sql, user)[0][0]


@override_settings(DATA_SERVICE_READER_USERNAMES=[OWNER])
class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user(OWNER, is_staff=True)
        cls.dataset = seed_access_transients(cls.owner, prefix=PREFIX)
        cls.member = create_non_staff_user('xsc_member', (MEMBER_GROUP_NAME,))
        cls.outsider = create_non_staff_user('xsc_outsider', ())
        cls.staff = create_test_user('xsc_staff', is_staff=True)
        # A listed transient (member photometry) that also has a hidden header with a
        # very bright point and a hidden spectrum, which is its best spectrum.
        cls.mixed = cls.dataset.by_suffix['memberphot']
        visible_header = TransientPhotometry.objects.get(transient=cls.mixed)
        audit = audit_fields(cls.owner)
        hidden_header = TransientPhotometry.objects.create(
            transient=cls.mixed, instrument=visible_header.instrument, obs_group=visible_header.obs_group, **audit)
        hidden_header.groups.add(Group.objects.get(name=OTHER_GROUP_NAME))
        point = TransientPhotData.objects.filter(photometry=visible_header).first()
        cls.bright = TransientPhotData.objects.create(
            photometry=hidden_header, band=point.band, obs_date=point.obs_date, mag=5.0, mag_err=0.01, **audit)
        cls.hidden_spectrum = TransientSpectrum.objects.create(
            transient=cls.mixed, instrument=visible_header.instrument, obs_group=visible_header.obs_group,
            ra=cls.mixed.ra, dec=cls.mixed.dec, obs_date=point.obs_date, redshift=0.05, **audit)
        cls.hidden_spectrum.groups.add(Group.objects.get(name=OTHER_GROUP_NAME))
        Transient.objects.filter(pk=cls.mixed.pk).update(best_spectrum=cls.hidden_spectrum)
        for suffix in ('memberphot', 'pubphot'):
            TransientPhotStat.objects.update_or_create(
                transient=cls.dataset.by_suffix[suffix], defaults={'peak_mag': 5.0, 'num_obs_global': 4})

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        explorer_scope.clear_table_cache()

    def visible_points(self, user):
        qs = TransientPhotData.objects.filter(photometry__transient__name__startswith=PREFIX)
        q = visible_photometry_ids_q(user)
        return qs if q is None else qs.filter(q)

    def listed_names(self, user):
        qs = Transient.objects.filter(name__startswith=PREFIX)
        return set(filter_listed_transients(user, qs).values_list('name', flat=True))


@MYSQL
class SameAnswerAsTheApplicationTests(_Fixture):
    def test_count_and_extremes_use_only_visible_points(self):
        for user in (self.member, self.outsider, self.staff):
            with self.subTest(user=user.username):
                visible = self.visible_points(user)
                self.assertEqual(scalar("SELECT COUNT(*) " + PHOT_FROM, user), visible.count())
                values = sorted(visible.values_list('mag', flat=True))
                low, high = run("SELECT MIN(pd.mag), MAX(pd.mag) " + PHOT_FROM, user)[0]
                self.assertEqual((low, high), (values[0], values[-1]) if values else (None, None))
                self.assertNotEqual(low, 5.0)
        # the hidden bright point is there for a service reader
        self.assertEqual(scalar("SELECT MIN(pd.mag) " + PHOT_FROM, self.owner), 5.0)

    def test_queries_written_many_ways_count_the_same_points(self):
        expected = self.visible_points(self.member).count()
        db = connection.settings_dict['NAME']
        inner = "SELECT pd.id " + PHOT_FROM
        forms = [
            "SELECT COUNT(*) FROM YSE_App_transientphotdata WHERE photometry_id IN (SELECT id FROM "
            "YSE_App_transientphotometry WHERE transient_id IN (SELECT id FROM YSE_App_transient "
            "WHERE name LIKE 'xsc%'))",
            "WITH pts AS (%s) SELECT COUNT(*) FROM pts" % inner,
            "WITH a AS (%s), b AS (SELECT * FROM a) SELECT COUNT(*) FROM b" % inner,
            "SELECT COUNT(*) FROM (%s) AS x" % inner,
            "SELECT COUNT(*) FROM (%s UNION ALL SELECT pd.id %s AND 1 = 0) AS u" % (inner, PHOT_FROM),
            "SELECT COUNT(*) FROM `%s`.`YSE_App_transientphotdata` d JOIN `%s`.YSE_App_transientphotometry p "
            "ON p.id = d.photometry_id JOIN YSE_App_transient t ON t.id = p.transient_id WHERE t.name LIKE 'xsc%%'"
            % (db, db),
            "select count(*) from yse_app_transientphotdata pd join YSE_APP_TRANSIENTPHOTOMETRY p on "
            "p.id = pd.photometry_id join `YSE_App_transient` `t` on t.id = p.transient_id where t.name like 'xsc%'",
            "SELECT /* a comment */ COUNT(*) -- another\n " + PHOT_FROM + " # and a third",
            "SELECT COALESCE((SELECT COUNT(*) " + PHOT_FROM + "), -1)",
            "SELECT COUNT(*) FROM YSE_App_transientphotdata AS YSE_App_transientphotometry WHERE "
            "YSE_App_transientphotometry.photometry_id IN (SELECT p.id FROM YSE_App_transientphotometry p "
            "JOIN YSE_App_transient t ON t.id = p.transient_id WHERE t.name LIKE 'xsc%')",
            "SELECT SUM(n) FROM (SELECT p.id, (SELECT COUNT(*) FROM YSE_App_transientphotdata x "
            "WHERE x.photometry_id = p.id) AS n FROM YSE_App_transientphotometry p JOIN YSE_App_transient t "
            "ON t.id = p.transient_id WHERE t.name LIKE 'xsc%') AS per_header",
            "SELECT COUNT(*) FROM YSE_App_transient t, LATERAL (SELECT pd.id FROM YSE_App_transientphotdata pd "
            "JOIN YSE_App_transientphotometry p ON p.id = pd.photometry_id WHERE p.transient_id = t.id) AS l "
            "WHERE t.name LIKE 'xsc%'",
        ]
        for sql in forms:
            with self.subTest(sql=sql):
                self.assertEqual(int(scalar(sql, self.member)), expected)

    def test_transients_are_the_listed_ones(self):
        for user in (self.member, self.outsider):
            with self.subTest(user=user.username):
                names = {r[0] for r in run("SELECT name FROM YSE_App_transient WHERE name LIKE 'xsc%'", user)}
                self.assertEqual(names, self.listed_names(user))
        hidden = self.dataset.by_suffix['otherphot-otherspec'].name
        self.assertEqual(scalar("SELECT COUNT(*) FROM YSE_App_transient WHERE name = '%s'" % hidden, self.member), 0)
        self.assertEqual(scalar(
            "SELECT COUNT(*) FROM YSE_App_transientphotometry p WHERE p.transient_id IN "
            "(SELECT id FROM YSE_App_transient WHERE name = '%s')" % hidden, self.member), 0)

    def test_spectra_and_best_spectrum(self):
        visible = TransientSpectrum.objects.filter(transient__name__startswith=PREFIX)
        from YSE_App.services.visibility import spectrum_visible_q

        expected = visible.filter(spectrum_visible_q(self.member)).distinct().count()
        sql = ("SELECT COUNT(*) FROM YSE_App_transientspectrum s JOIN YSE_App_transient t "
               "ON t.id = s.transient_id WHERE t.name LIKE 'xsc%'")
        self.assertEqual(scalar(sql, self.member), expected)
        best = "SELECT best_spectrum_id FROM YSE_App_transient WHERE id = %d" % self.mixed.pk
        self.assertIsNone(scalar(best, self.member))
        self.assertEqual(scalar(best, self.owner), self.hidden_spectrum.pk)
        self.assertEqual(scalar(
            "SELECT COUNT(*) FROM YSE_App_transient WHERE best_spectrum_id = %d" % self.hidden_spectrum.pk,
            self.member), 0)

    def test_photometry_statistics(self):
        mixed, public = self.mixed.pk, self.dataset.by_suffix['pubphot'].pk
        sql = "SELECT transient_id FROM YSE_App_transientphotstat WHERE transient_id IN (%d, %d)" % (mixed, public)
        # the member may not see all of the mixed transient's photometry: its all-data row is hidden
        self.assertEqual({r[0] for r in run(sql, self.member)}, {public})
        self.assertEqual({r[0] for r in run(sql, self.owner)}, {mixed, public})

    @override_settings(DATA_ACCESS_GRANTS_ENABLED=True)
    def test_per_point_rules(self):
        # One point of the member's own (group-shared) header is denied to them personally:
        # access is per point, so its header and the other points stay visible.
        header = TransientPhotometry.objects.filter(transient=self.mixed, groups__name=MEMBER_GROUP_NAME).first()
        point = TransientPhotData.objects.filter(photometry=header).first()
        DataAccessRule.objects.create(transient_phot_data=point, grantee_user=self.member, effect='deny',
                                      granted_as='pi')
        member = type(self.member).objects.get(pk=self.member.pk)
        expected = set(self.visible_points(member).values_list('pk', flat=True))
        self.assertNotIn(point.pk, expected)
        self.assertTrue(TransientPhotData.objects.filter(photometry=header, pk__in=expected).exists())
        got = {r[0] for r in run("SELECT pd.id " + PHOT_FROM, member)}
        self.assertEqual(got, expected)
        self.assertEqual(scalar("SELECT COUNT(*) FROM YSE_App_transientphotometry WHERE id = %d" % header.pk,
                                member), 1)

    def test_service_reader_is_not_rewritten(self):
        sql = "SELECT COUNT(*) " + PHOT_FROM
        plan = rewrite(sql, self.owner)
        self.assertFalse(plan.rewritten)
        self.assertEqual(plan.sql, sql)

    def test_row_and_time_limits(self):
        with override_settings(EXPLORER_SCOPED_MAX_ROWS=2):
            self.assertEqual(len(run("SELECT pd.id " + PHOT_FROM, self.member)), 2)
            self.assertEqual(len(run("SELECT pd.id " + PHOT_FROM + " LIMIT 1", self.member)), 1)
        with explorer_scope.viewer_time_cap(connection, 1234):
            with connection.cursor() as cursor:
                cursor.execute('SELECT @@SESSION.max_execution_time')
                self.assertEqual(int(cursor.fetchone()[0]), 1234)
        with connection.cursor() as cursor:
            cursor.execute('SELECT @@SESSION.max_execution_time')
            self.assertEqual(int(cursor.fetchone()[0]), 0)

    def test_derived_values_select_but_are_not_returned(self):
        sql = ("SELECT name, ra, ra * 2 AS twice FROM YSE_App_transient WHERE name LIKE 'xsc%' AND ra > 10.035 "
               "ORDER BY ra")
        with override_settings(EXPLORER_DERIVED_VALUE_COLUMNS='YSE_App_transient.ra'):
            rows = run(sql, self.member)
            expected = {n for n in self.listed_names(self.member)
                        if Transient.objects.get(name=n).ra > 10.035}
            self.assertEqual({r[0] for r in rows}, expected)
            self.assertTrue(rows)
            self.assertTrue(all(r[1] is None and r[2] is None for r in rows))
            self.assertIsNone(scalar("SELECT MAX(ra) FROM YSE_App_transient", self.member))
            with self.assertRaises(ExplorerQueryRefused):
                rewrite("SELECT * FROM YSE_App_transient", self.member)
        self.assertTrue(all(r[1] is not None for r in run(sql, self.member)))

    def test_staff_read_collaboration_tables_and_only_user_names(self):
        self.assertEqual(scalar("SELECT COUNT(*) FROM YSE_App_log WHERE 1 = 0", self.staff), 0)
        self.assertEqual(scalar("SELECT username FROM auth_user WHERE id = %d" % self.staff.pk, self.staff),
                         self.staff.username)
        from django.db import DatabaseError

        with self.assertRaises(DatabaseError):
            run("SELECT email FROM auth_user", self.staff)
        self.assertEqual(scalar("SELECT COUNT(*) FROM YSE_App_tooresource WHERE 1 = 0", self.staff), 0)
        for sql in ("SELECT COUNT(*) FROM YSE_App_tooresource", "SELECT username FROM auth_user"):
            with self.subTest(sql=sql), self.assertRaises(ExplorerQueryRefused):
                rewrite(sql, self.member)


class RefusedStatementTests(_Fixture):
    def assertRefused(self, sql, user=None):
        with self.assertRaises(ExplorerQueryRefused, msg=sql):
            rewrite(sql, user or self.member)

    def test_statements_that_cannot_be_checked(self):
        for sql in (
            "", "   ", "SELECT name FROM", "SELECT 1; SELECT 2",
            "SELECT name FROM YSE_App_transient; DELETE FROM YSE_App_transient",
            "SHOW TABLES", "EXPLAIN SELECT * FROM YSE_App_transientphotdata", "DESCRIBE YSE_App_transientphotdata",
            "TABLE YSE_App_transientphotdata", "VALUES ROW(1)", "CALL some_procedure()", "DO 1",
            "INSERT INTO YSE_App_transientstatus (name) VALUES ('x')", "UPDATE YSE_App_transient SET name = 'x'",
            "DELETE FROM YSE_App_transient", "DROP TABLE YSE_App_transient", "SET @a = 1",
            "HANDLER YSE_App_transientphotdata OPEN",
            "SELECT * FROM YSE_App_transientphotdata INTO OUTFILE '/tmp/x'",
            "SELECT mag INTO @m FROM YSE_App_transientphotdata LIMIT 1",
            "SELECT * FROM YSE_App_transientphotdata FOR UPDATE",
            "SELECT * FROM YSE_App_transientphotdata LOCK IN SHARE MODE",
        ):
            with self.subTest(sql=sql):
                self.assertRefused(sql)

    def test_system_schemas_and_other_databases(self):
        for sql in (
            "SELECT table_name FROM information_schema.tables",
            "SELECT * FROM `INFORMATION_SCHEMA`.`COLUMNS`",
            "SELECT name FROM YSE_App_transient UNION SELECT table_name FROM information_schema.tables",
            "SELECT user FROM mysql.user", "SELECT * FROM performance_schema.threads", "SELECT * FROM sys.version",
            "SELECT * FROM other_database.YSE_App_transientphotdata",
            "SELECT * FROM a.b.YSE_App_transientphotdata",
        ):
            with self.subTest(sql=sql):
                self.assertRefused(sql)
                self.assertRefused(sql, self.staff)

    def test_personal_account_and_unknown_tables(self):
        tables = list(settings.EXPLORER_PERSONAL_DATA_TABLES) + [
            'auth_user', 'authtoken_token', 'django_session', 'YSE_App_encryptedcredential',
            'YSE_App_analysisresultfile', 'YSE_App_no_such_table']
        for table in tables:
            for sql in ("SELECT * FROM %s" % table, "SELECT * FROM `%s` x" % table.lower(),
                        "SELECT name FROM YSE_App_transient WHERE id IN (SELECT 1 FROM %s)" % table.upper()):
                with self.subTest(sql=sql):
                    self.assertRefused(sql)
                    if table != 'auth_user':
                        self.assertRefused(sql, self.staff)

    def test_tricks(self):
        for sql in (
            # executable comments and hints
            "SELECT name FROM YSE_App_transient /*! UNION SELECT name FROM YSE_App_transientphotdata */",
            "SELECT /*+ MAX_EXECUTION_TIME(1) */ name FROM YSE_App_transient",
            # a WITH name that is also a table name
            "WITH YSE_App_transientphotdata AS (SELECT 1 AS mag) SELECT mag FROM YSE_App_transientphotdata",
            "WITH yse_app_transient AS (SELECT 1) SELECT * FROM yse_app_transient",
            # functions that read files, wait, lock or reveal the server, and stored functions
            "SELECT LOAD_FILE('/etc/hostname')", "SELECT SLEEP(5)", "SELECT BENCHMARK(1000000, MD5('x'))",
            "SELECT GET_LOCK('x', 1)", "SELECT VERSION()", "SELECT CURRENT_USER()", "SELECT DATABASE()",
            "SELECT some_stored_function(1)", "SELECT other_db.some_function(1)",
            "SELECT name FROM YSE_App_transient WHERE some_stored_function(id) > 0",
            # session variables
            "SELECT @@datadir", "SELECT @@GLOBAL.version",
            # odd table names
            "SELECT * FROM `YSE_App_transient `",
        ):
            with self.subTest(sql=sql):
                self.assertRefused(sql)

    def test_signed_out_viewers_are_refused(self):
        self.assertRefused("SELECT name FROM YSE_App_transient", AnonymousUser())
        inactive = create_test_user('xsc_inactive', is_staff=False, is_active=False)
        self.assertRefused("SELECT name FROM YSE_App_transient", inactive)

    def test_reference_tables_pass_unchanged(self):
        plan = rewrite("SELECT name FROM YSE_App_photometricband", self.member)
        self.assertIn('FROM `YSE_App_photometricband`', plan.sql)
        self.assertNotIn('__yse', plan.sql)

    def test_rollback_switch(self):
        with override_settings(EXPLORER_QUERY_SCOPE='off'):
            plan = explorer_scope.scope_for_viewer("SELECT * FROM YSE_App_transientphotdata", self.member)
            self.assertFalse(plan.rewritten)


class _ExplorerToDefault:
    def __getitem__(self, alias):
        return connections['default' if alias == 'explorer' else alias]


@override_settings(EXPLORER_OPEN_TO_ALL_USERS=True, ONBOARDING_GATE_ENABLED=False, EXPLORER_RESTRICTED_ACCESS=False)
class ExplorerPagesTests(_Fixture):
    """Explorer itself, opened to everyone: runs, exports, schema, saved queries."""

    def setUp(self):
        super().setUp()
        for target in ('explorer.models.get_valid_connection', 'explorer.schema.get_valid_connection'):
            patcher = mock.patch(target, side_effect=lambda alias: connections['default'])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client()
        self.client.force_login(self.member)

    def _download(self, sql):
        return self.client.post(reverse('download_sql'), {'sql': sql, 'connection': 'explorer'})

    @MYSQL
    def test_playground_export_is_scoped(self):
        response = self._download("SELECT COUNT(*) AS n, MIN(pd.mag) AS m " + PHOT_FROM)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(int(rows[1][0]), self.visible_points(self.member).count())
        self.assertNotEqual(float(rows[1][1]), 5.0)

    def test_refused_query_says_why(self):
        response = self._download("SELECT * FROM information_schema.tables")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b'Query refused', response.content)

    def test_closed_by_default(self):
        with override_settings(EXPLORER_OPEN_TO_ALL_USERS=False):
            self.assertEqual(self.client.get(reverse('explorer_playground')).status_code, 403)
        self.assertEqual(self.client.get(reverse('explorer_playground')).status_code, 200)

    def test_open_users_do_not_see_logs_or_edit_other_queries(self):
        from explorer.models import Query

        self.assertEqual(self.client.get(reverse('explorer_logs')).status_code, 403)
        theirs = Query.objects.create(title='xsc staff query', sql='SELECT 1', created_by_user=self.staff)
        self.client.post(reverse('query_detail', args=[theirs.id]), {'title': 'changed', 'sql': 'SELECT 2'})
        theirs.refresh_from_db()
        self.assertEqual((theirs.title, theirs.sql), ('xsc staff query', 'SELECT 1'))
        self.assertEqual(self.client.post(reverse('query_delete', args=[theirs.id])).status_code, 403)
        self.assertTrue(Query.objects.filter(pk=theirs.pk).exists())

    def test_open_users_save_their_own_queries_under_new_titles(self):
        from explorer.models import Query

        Query.objects.create(title='xsc taken', sql='SELECT 1', created_by_user=self.staff)
        self.client.post(reverse('query_create'), {'title': 'xsc taken', 'sql': 'SELECT 2', 'connection': 'explorer'})
        self.assertEqual(Query.objects.filter(title__iexact='xsc taken').count(), 1)
        self.client.post(reverse('query_create'), {'title': 'xsc mine', 'sql': 'SELECT 2', 'connection': 'explorer'})
        mine = Query.objects.get(title='xsc mine')
        self.assertEqual(mine.created_by_user_id, self.member.pk)

    def test_schema_lists_only_readable_tables(self):
        response = self.client.get(reverse('explorer_schema', kwargs={'connection': 'explorer'}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('>YSE_App_transientphotdata</h4>', html)
        self.assertIn('>YSE_App_photometricband</h4>', html)
        for table in ('YSE_App_tooresource', 'auth_user', 'YSE_App_analysisresultfile') + tuple(
                settings.EXPLORER_PERSONAL_DATA_TABLES):
            self.assertNotIn('>%s</h4>' % table, html)
        self.client.force_login(self.staff)
        cache.clear()
        html = self.client.get(reverse('explorer_schema', kwargs={'connection': 'explorer'})).content.decode()
        self.assertIn('>YSE_App_tooresource</h4>', html)

    def test_queries_outside_an_explorer_request_are_refused(self):
        from django.db import DatabaseError
        from explorer.models import Query

        with self.assertRaises(DatabaseError):
            Query(sql='SELECT 1', title='x', connection='explorer').execute()


@MYSQL
class PageQueriesTests(_Fixture):
    """Saved queries that feed pages go through the same layer, with per-viewer caches."""

    RAW = ("SELECT DISTINCT t.name " + PHOT_FROM + " AND pd.mag < 10")

    def test_page_query_over_photometry_is_scoped_and_cached_per_viewer(self):
        from explorer.models import Query

        from YSE_App import views as views_module

        query = Query.objects.create(title='xsc page', sql=self.RAW, created_by_user=self.owner)
        with mock.patch.object(views_module, 'connections', _ExplorerToDefault()):
            self.assertEqual(views_module.run_explorer_query_cached(query, user=self.owner), [self.mixed.name])
            self.assertEqual(views_module.run_explorer_query_cached(query, user=None), [self.mixed.name])
            self.assertEqual(views_module.run_explorer_query_cached(query, user=self.member), [])
            self.assertEqual(views_module.run_explorer_query_cached(query, user=self.staff), [])
            names, rows = views_module.run_explorer_query_rows_cached(query, user=self.member)
            self.assertEqual(rows, [])



class SavedSearchSqlTests(_search_save.SearchFixture):
    """Saved searches are compiled Django SQL: the layer must read and rewrite every one."""

    def test_every_saved_search_is_rewritten_and_runs(self):
        from django.test import RequestFactory

        from YSE_App.services.search_queries import SearchSaveError, compiled_search

        factory = RequestFactory()
        for user in (self.member, self.outsider):
            listed = set(filter_listed_transients(user, Transient.objects.all()).values_list('name', flat=True))
            for params in _search_save.CompileEquivalenceTests.PARAMS:
                with self.subTest(user=user.username, params=params):
                    request = factory.get('/search/', params)
                    request.user = user
                    try:
                        _filterset, sql = compiled_search(request.GET, request=request)
                    except SearchSaveError:
                        continue  # a search that can never match for this user
                    plan = rewrite(sql, user)
                    self.assertTrue(plan.rewritten)
                    if connection.vendor != 'mysql':
                        continue
                    with connection.cursor() as cursor:
                        cursor.execute(plan.sql.replace('%', '%%'), ())
                        names = {row[0] for row in cursor.fetchall()}
                    self.assertLessEqual(names, listed)
