"""Saved Explorer queries that feed app pages are viewer-scoped (#715, #723).

For a viewer who does not see all data, a page-feeding query is rewritten to
that viewer's rows (#723), so it may read photometry; with the rollback switch
``EXPLORER_QUERY_SCOPE: off`` only transient metadata tables are allowed
(#715). Service readers and internal callers (``user=None``) are unchanged.
Derived-value tables may be added by setting; their columns select rows but
are not returned to a scoped viewer.

Synthetic users, groups and transients only.
"""

import base64
import json
from unittest import mock
from urllib.parse import quote

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings

from YSE_App import views as views_module
from YSE_App.perf.access import MEMBER_GROUP_NAME, create_non_staff_user, seed_access_transients
from YSE_App.services import page_query_scope as pqs
from YSE_App.services.dashboard_queries import dashboard_sql_rejection_reason
from YSE_App.tests.fixtures_minimal import create_test_user

_PASSWORD = 'page-query-scope-pw'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

META_SQL = "SELECT t.name, t.ra FROM YSE_App_transient t WHERE t.name LIKE 'pqs%' ORDER BY t.name"
RAW_PHOT_SQL = (
    "SELECT t.name FROM YSE_App_transient t WHERE t.name LIKE 'pqs%' AND EXISTS "
    "(SELECT 1 FROM YSE_App_transientphotdata pd JOIN YSE_App_transientphotometry p "
    "ON pd.photometry_id = p.id WHERE p.transient_id = t.id AND pd.mag < 30)"
)


class _ExplorerToDefault:
    def __getitem__(self, alias):
        from django.db import connections

        return connections['default' if alias == 'explorer' else alias]


def _basic_auth(username):
    token = base64.b64encode(('%s:%s' % (username, _PASSWORD)).encode()).decode()
    return {'HTTP_AUTHORIZATION': 'Basic %s' % token}


class TableRuleTests(SimpleTestCase):
    def test_metadata_tables_are_allowed(self):
        self.assertEqual(pqs.disallowed_tables(META_SQL), [])
        sql = ("SELECT t.name FROM YSE_App_transient t JOIN YSE_App_transient_tags tt ON tt.transient_id = t.id "
               "JOIN YSE_App_transienttag tg ON tg.id = tt.transienttag_id WHERE tg.name = 'YSE'")
        self.assertEqual(pqs.disallowed_tables(sql), [])

    def test_raw_data_tables_are_refused_however_written(self):
        for sql in (
            RAW_PHOT_SQL,
            "SELECT t.name FROM YSE_App_transient t, `YSE_App_transientphotdata` pd WHERE pd.id=1",
            "select name from yse_app_transient where id in (select transient_id from db.YSE_APP_TRANSIENTSPECTRUM)",
            "SELECT t.name FROM YSE_App_transient t JOIN YSE_App_transientphotstat s ON s.transient_id=t.id",
            "SELECT t.name FROM YSE_App_transient t JOIN YSE_App_transientphotstatscope s ON s.transient_id=t.id",
            "SELECT name FROM YSE_App_transient /*! UNION SELECT username FROM auth_user */",
            "SELECT name FROM YSE_App_transient UNION SELECT table_name FROM information_schema.tables",
        ):
            with self.subTest(sql=sql):
                self.assertTrue(pqs.disallowed_tables(sql))

    def test_unknown_application_table_is_refused(self):
        self.assertEqual(
            pqs.disallowed_tables("SELECT name FROM YSE_App_transient JOIN YSE_App_brandnewtable x ON 1"),
            ['yse_app_brandnewtable'])

    def test_extra_allowed_tables_setting(self):
        sql = "SELECT t.name FROM YSE_App_transient t JOIN yse_v_derived d ON d.transient_id = t.id"
        self.assertEqual(pqs.disallowed_tables(sql), [])  # not application-prefixed
        with override_settings(PAGE_QUERY_EXTRA_ALLOWED_TABLES='yse_app_v_derived, other'):
            sql = "SELECT t.name FROM YSE_App_transient t JOIN YSE_App_v_derived d ON d.transient_id = t.id"
            self.assertEqual(pqs.disallowed_tables(sql), [])
        self.assertEqual(pqs.disallowed_tables(sql), ['yse_app_v_derived'])


class ViewerRuleTests(TestCase):
    def test_who_is_scoped(self):
        staff = create_test_user('pqs_staff', is_staff=True)
        member = create_test_user('pqs_plain', is_staff=False)
        self.assertFalse(pqs.viewer_is_scoped(None))
        self.assertTrue(pqs.viewer_is_scoped(staff))  # staff see only what they have access to (#706)
        with override_settings(DATA_SERVICE_READER_USERNAMES=['pqs_staff']):
            self.assertFalse(pqs.viewer_is_scoped(staff))
        self.assertTrue(pqs.viewer_is_scoped(member))
        self.assertTrue(pqs.viewer_is_scoped(AnonymousUser()))
        with override_settings(PAGE_QUERY_TABLE_POLICY='off'):
            self.assertFalse(pqs.viewer_is_scoped(member))

    def test_rejection_reason_for_scoped_viewers(self):
        staff = create_test_user('pqs_staff2', is_staff=True)
        member = create_test_user('pqs_plain2', is_staff=False)
        self.assertIsNone(dashboard_sql_rejection_reason(RAW_PHOT_SQL))
        # #723: queries over photometry are rewritten to the viewer's rows, not refused
        self.assertIsNone(dashboard_sql_rejection_reason(RAW_PHOT_SQL, staff))
        self.assertIsNone(dashboard_sql_rejection_reason(RAW_PHOT_SQL, member))
        self.assertIsNone(dashboard_sql_rejection_reason(META_SQL, member))
        refused = "SELECT t.name FROM YSE_App_transient t WHERE t.id IN (SELECT 1 FROM YSE_App_researcherprofile)"
        self.assertIn('cannot be limited', dashboard_sql_rejection_reason(refused, member))
        self.assertIn('cannot be limited', dashboard_sql_rejection_reason(refused, staff))
        with override_settings(DATA_SERVICE_READER_USERNAMES=['pqs_staff2']):
            self.assertIsNone(dashboard_sql_rejection_reason(refused, staff))
        # the rollback switch restores the #715 rule
        with override_settings(EXPLORER_QUERY_SCOPE='off'):
            self.assertIn('yse_app_transientphotdata', dashboard_sql_rejection_reason(RAW_PHOT_SQL, member))

    def test_runner_never_serves_the_shared_cache_to_a_scoped_viewer(self):
        from explorer.models import Query

        owner = create_test_user('pqs_owner', is_staff=True)
        member = create_test_user('pqs_plain3', is_staff=False)
        query = Query.objects.create(title='pqs raw', sql=RAW_PHOT_SQL, snapshot=False, created_by_user=owner)
        cache.set(views_module.explorer_query_cache_key(query.id), ['staff-only-result'])
        self.addCleanup(cache.clear)
        with override_settings(DATA_SERVICE_READER_USERNAMES=['pqs_owner']):  # a service reader (#706)
            self.assertEqual(views_module.run_explorer_query_cached(query, user=owner), ['staff-only-result'])
        with mock.patch.object(views_module, 'connections', _ExplorerToDefault()):
            self.assertEqual(views_module.run_explorer_query_cached(query, user=member), [])
        with override_settings(EXPLORER_QUERY_SCOPE='off'), self.assertRaises(pqs.PageQueryRefused):
            views_module.run_explorer_query_cached(query, user=member)

    def test_selection_only_result_drops_derived_columns_for_scoped_viewers(self):
        member = create_test_user('pqs_plain4', is_staff=False)
        staff = create_test_user('pqs_staff4', is_staff=True)
        names, rows = ['name', 'salt_x1'], [('a', 1.5), ('b', 2.5)]
        sql = "SELECT t.name, d.salt_x1 FROM YSE_App_transient t JOIN YSE_App_v_derived d ON d.id = t.id"
        with override_settings(PAGE_QUERY_EXTRA_ALLOWED_TABLES='YSE_App_v_derived'):
            self.assertEqual(pqs.selection_only_result(sql, member, names, rows), (['name'], [('a',), ('b',)]))
            self.assertEqual(pqs.selection_only_result(sql, staff, names, rows), (['name'], [('a',), ('b',)]))
            with override_settings(DATA_SERVICE_READER_USERNAMES=['pqs_staff4']):
                self.assertEqual(pqs.selection_only_result(sql, staff, names, rows), (names, rows))
            self.assertEqual(pqs.selection_only_result(sql, None, names, rows), (names, rows))
        # no extra table read: unchanged
        self.assertEqual(pqs.selection_only_result(META_SQL, member, names, rows), (names, rows))


@override_settings(
    PASSWORD_HASHERS=PASSWORD_HASHERS,
    QUERY_API_CACHE_SECONDS=0,
    QUERY_API_ALLOWED_QUERIES=['pqs meta', 'pqs raw'],
    API_SERVICE_USERNAMES=['pqs_cron'],
    DATA_SERVICE_READER_USERNAMES=['pqs_api_owner'],  # the staff owner reads all data as a service reader (#706)
    QUERY_API_VISIBILITY_FILTER=True,
)
class QueryApiScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.owner = create_test_user('pqs_api_owner', is_staff=True)
        cls.dataset = seed_access_transients(cls.owner, prefix='pqs')
        cls.member = create_non_staff_user('pqs_member', (MEMBER_GROUP_NAME,))
        cls.cron = create_test_user('pqs_cron', is_staff=False)
        for user in (cls.owner, cls.member, cls.cron):
            user.set_password(_PASSWORD)
            user.save()
        cls.meta = Query.objects.create(title='pqs meta', sql=META_SQL, snapshot=False, created_by_user=cls.owner)
        cls.raw = Query.objects.create(title='pqs raw', sql=RAW_PHOT_SQL, snapshot=False, created_by_user=cls.owner)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, 'connections', _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client()

    def _get(self, user, query):
        return self.client.get('/query_api/%s/' % quote(query.title), **_basic_auth(user.username))

    def test_non_staff_may_run_a_metadata_query(self):
        self.assertEqual(self._get(self.member, self.meta).status_code, 200)

    def test_non_staff_get_a_query_over_raw_photometry_scoped_to_their_data(self):
        response = self._get(self.member, self.raw)
        self.assertEqual(response.status_code, 200)
        names = {r['name'] for r in json.loads(response.content)['transients']}
        from YSE_App.models import TransientPhotData
        from YSE_App.services.visibility import visible_photometry_ids_q

        member = type(self.member).objects.get(pk=self.member.pk)
        points = TransientPhotData.objects.filter(
            visible_photometry_ids_q(member), photometry__transient__name__startswith='pqs', mag__lt=30)
        expected = set(points.values_list('photometry__transient__name', flat=True))
        self.assertTrue(expected)
        self.assertEqual(names, expected)
        with override_settings(EXPLORER_QUERY_SCOPE='off'):
            self.assertEqual(self._get(self.member, self.raw).status_code, 404)

    def test_staff_and_service_accounts_may_run_it(self):
        for user in (self.owner, self.cron):
            with self.subTest(user=user.username):
                self.assertEqual(self._get(user, self.raw).status_code, 200)

    def test_policy_off_restores_the_old_behaviour(self):
        with override_settings(PAGE_QUERY_TABLE_POLICY='off'):
            self.assertEqual(self._get(self.member, self.raw).status_code, 200)

    def test_derived_columns_select_but_are_not_returned(self):
        from explorer.models import Query

        # Stand-in for a derived-value view: the query "reads an extra table" and
        # carries the derived value in a non-identifying column.
        sql = ("SELECT t.name, t.ra AS fit_param FROM YSE_App_transient t "
               "WHERE t.name LIKE 'pqs%' AND t.ra >= 0 ORDER BY t.name")
        derived = Query.objects.create(title='pqs derived', sql=sql, snapshot=False, created_by_user=self.owner)
        with override_settings(QUERY_API_ALLOWED_QUERIES=['pqs derived']), \
                mock.patch.object(pqs, 'reads_extra_tables', return_value=True):
            rows = json.loads(self._get(self.member, derived).content)['transients']
        self.assertTrue(rows)
        self.assertTrue(all(set(r) == {'name'} for r in rows))
