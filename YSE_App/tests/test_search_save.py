"""Saving a transient search as an Explorer query (#287, umbrella #284).

Pins that the SQL ``services/search_queries.py`` compiles from the shared
``TransientSearchFilterSet`` returns exactly the rows the ORM returns, in the
same order, on the ``explorer`` connection (sqlite in the default suite, MySQL
in the docker run); that it passes the dashboard guard; that relative-time
filters stay relative in the saved text; the literal quoting rules; and the
three surfaces: the service (``Query`` + optional ``UserQuery``), the search
page button / modal / confirmation, and ``POST /api/transients/save_search/``.
"""

import datetime
import json

from unittest import mock

from django.db import connections
from django.http import QueryDict
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App import views as views_module
from YSE_App.models import UserQuery
from YSE_App.services.dashboard_queries import dashboard_sql_is_supported
from YSE_App.services.search_queries import (
    SearchSaveError,
    compile_search_sql,
    compiled_search,
    default_search_title,
    inline_sql_params,
    save_search_query,
    sql_literal,
)
from YSE_App.tests.test_transient_search import PRIVATE_GROUP, SearchFixture


class _ExplorerToDefault:
    """The saved SQL runs on the 'explorer' alias; in a TestCase only 'default' sees the rows."""

    def __getitem__(self, alias):
        if alias == 'explorer':
            return connections['default']
        return connections[alias]


def run_sql(sql):
    """Names the saved statement returns, the way the dashboard runs it.

    Runs on the default connection: the test rows live in its transaction, and
    the settings point ``explorer`` at the same database (same vendor).
    """
    cursor = connections['default'].cursor()
    try:
        cursor.execute(sql.replace('%', '%%'), ())
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()


class CompileEquivalenceTests(SearchFixture):
    # compiling for the explorer alias may open that connection (MySQL asks its
    # version for REGEXP); the saved SQL itself runs on default (run_sql).
    databases = {'default', 'explorer'}


    PARAMS = (
        {},
        {'ra': '10.0', 'dec': '20.0', 'radius_arcsec': '60'},
        {'ra': '00:40:00', 'dec': '+20:00:00', 'radius_arcsec': '60', 'ordering': '-separation'},
        {'gal_b_abs_min': '10', 'ordering': '-gal_b'},
        {'gal_b_abs_max': '5'},
        {'name_contains': "26", 'ordering': 'name'},
        {'alias': 'ztf26'},
        {'has_tns_name': 'true', 'ordering': '-disc_date'},
        {'has_tns_name': 'false'},
        {'disc_date_after': (timezone.now() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')},
        {'created_before': '2099-01-01T00:00:00Z', 'modified_after': '2000-01-01'},
        {'days_since_disc_max': '30'},
        {'days_since_last_det_max': '3', 'ordering': 'last_det_mjd'},
        {'peak_mag_max': '19', 'num_det_min': '1', 'ordering': '-peak_mag'},
        {'latest_mag_min': '20', 'rise_rate_min': '0'},
        {'first_det_after': '2000-01-01', 'deepest_limit_min': '22'},
        {'spec_class': 'SN Ia'},
        {'classification': ['SN II', 'SN Ia'], 'ordering': 'name'},
        {'exclude_class': 'SN II'},
        {'has_redshift': 'true', 'redshift_min': '0.015', 'ordering': '-best_redshift'},
        {'status': ['New', 'Watch'], 'ordering': 'name'},
        {'status_in': 'New,Watch', 'obs_group': 'YSE'},
        {'internal_survey': 'YSE'},
        {'tags': ['young', 'old'], 'ordering': 'name'},
        {'tags_all': ['young', 'has-host']},
        {'tag_in': 'young,old'},
        {'has_spectrum': 'true'},
        {'has_spectrum': 'false', 'has_followup': 'false'},
        {'followup_status': 'Requested', 'has_comment': 'true'},
        {'has_host': 'true', 'host_redshift_min': '0.1'},
        {'visible_to_group': PRIVATE_GROUP},
        {'ra': '10.0', 'dec': '20.0', 'radius_arcsec': '60', 'gal_b_abs_min': '1', 'has_spectrum': 'true',
         'tags': 'young', 'status': 'Following', 'peak_mag_max': '19', 'redshift_max': '1', 'ordering': '-peak_mag'},
        {'q': '10.0 20.0 60'},
        {'q': 'PS26'},
    )

    def _request(self, params, user=None):
        request = self.factory.get('/search/', params)
        request.user = user or self.user
        return request

    def test_saved_sql_returns_what_the_search_returns(self):
        for params in self.PARAMS:
            request = self._request(params)
            filterset, sql = compiled_search(request.GET, request=request)
            expected = list(filterset.qs.values_list('name', flat=True))
            self.assertTrue(dashboard_sql_is_supported(sql), sql)
            got = run_sql(sql)
            if any(k.startswith('ordering') for k in params) or 'ra' in params or 'q' in params:
                self.assertEqual(got, expected, params)
            else:
                self.assertEqual(sorted(got), sorted(expected), params)
            self.assertNotIn('%s', sql)

    def test_saved_sql_respects_the_saving_users_group_access(self):
        request = self._request({'has_spectrum': 'true'}, user=self.outsider)
        _, sql = compiled_search(request.GET, request=request)
        self.assertEqual(run_sql(sql), ['2026sea'])
        request = self._request({'visible_to_group': PRIVATE_GROUP}, user=self.outsider)
        with self.assertRaises(SearchSaveError) as ctx:
            compiled_search(request.GET, request=request)
        self.assertIn('never match', str(ctx.exception))

    def test_relative_time_filters_stay_relative(self):
        sql = compile_search_sql(QueryDict('days_since_disc_max=30&days_since_last_det_max=5'))
        vendor = connections['default'].vendor
        if vendor == 'mysql':
            self.assertIn('UTC_TIMESTAMP() - INTERVAL 2592000 SECOND', sql)
            self.assertIn('UNIX_TIMESTAMP() / 86400.0 + 40587.0', sql)
        else:
            self.assertIn("datetime('now', '-2592000 seconds')", sql)
            self.assertIn("julianday('now') - 2400000.5", sql)
        self.assertNotIn(str(timezone.now().year) + '-', sql)
        self.assertEqual(run_sql(sql), ['2026sea'])

    def test_invalid_search_does_not_compile(self):
        with self.assertRaises(SearchSaveError) as ctx:
            compile_search_sql(QueryDict('ra=10'))
        self.assertIn('both RA and Dec', str(ctx.exception))
        with self.assertRaises(SearchSaveError) as ctx:
            compile_search_sql(QueryDict('status=Nope'))
        self.assertIn('Status', str(ctx.exception))

    def test_dict_params_are_accepted(self):
        sql_from_dict = compile_search_sql({'status': ['New', 'Watch'], 'ordering': 'name'})
        sql_from_qd = compile_search_sql(QueryDict('status=New&status=Watch&ordering=name'))
        self.assertEqual(sql_from_dict, sql_from_qd)
        self.assertEqual(run_sql(sql_from_dict), ['2026pub', '2026zzz', 'PS26abc'])

    def test_default_title_and_description(self):
        filterset, _ = compiled_search(QueryDict('status=New&has_spectrum=true'))
        title = default_search_title(filterset)
        self.assertTrue(title.startswith('Search: '))
        self.assertIn('Status New', title)
        self.assertIn('Has spectrum true', title)
        self.assertLessEqual(len(default_search_title(filterset, max_length=20)), 20)


class LiteralTests(TestCase):

    def test_sql_literal(self):
        conn = connections['default']
        self.assertEqual(sql_literal(None, conn), 'NULL')
        self.assertEqual(sql_literal(True, conn), '1')
        self.assertEqual(sql_literal(False, conn), '0')
        self.assertEqual(sql_literal(3, conn), '3')
        self.assertEqual(sql_literal(19.983333333333334, conn), '19.983333333333334')
        self.assertEqual(sql_literal("O'Brien", conn), "'O''Brien'")
        backslash = sql_literal('a\\_b', conn)
        self.assertEqual(backslash, "'a\\\\_b'" if conn.vendor == 'mysql' else "'a\\_b'")
        when = timezone.make_aware(datetime.datetime(2026, 9, 1, 12, 30, 0), datetime.timezone.utc)
        self.assertEqual(sql_literal(when, conn), "'2026-09-01 12:30:00'")
        self.assertEqual(sql_literal(datetime.date(2026, 9, 1), conn), "'2026-09-01'")
        with self.assertRaises(SearchSaveError):
            sql_literal('bad\x00byte', conn)

    def test_inline_sql_params(self):
        conn = connections['default']
        self.assertEqual(
            inline_sql_params("SELECT %s, '100%%', %s", ['a', 2], conn),
            "SELECT 'a', '100%', 2",
        )
        with self.assertRaises(SearchSaveError):
            inline_sql_params('SELECT %s', [], conn)
        with self.assertRaises(SearchSaveError):
            inline_sql_params('SELECT 1', [1], conn)


class SaveServiceTests(SearchFixture):
    # compiling for the explorer alias may open that connection (MySQL asks its
    # version for REGEXP); the saved SQL itself runs on default (run_sql).
    databases = {'default', 'explorer'}


    def test_save_creates_query_and_dashboard_link(self):
        from explorer.models import Query

        result = save_search_query(
            self.user, QueryDict('status=New&status=Watch'), 'Save test new+watch',
            add_to_dashboard=True, search_url='http://testserver/search/?status=New&status=Watch',
        )
        query = result['query']
        self.assertIsInstance(query, Query)
        self.assertEqual(query.title, 'Save test new+watch')
        self.assertEqual(query.created_by_user, self.user)
        self.assertIn('Status = New, Watch', query.description)
        self.assertIn('http://testserver/search/?status=New&status=Watch', query.description)
        self.assertIn('YSE_App_transient', query.sql)
        self.assertTrue(dashboard_sql_is_supported(query.sql))
        self.assertEqual(sorted(run_sql(query.sql)), ['2026pub', '2026zzz', 'PS26abc'])
        user_query = result['user_query']
        self.assertEqual(user_query.user, self.user)
        self.assertEqual(user_query.query, query)
        self.assertEqual(UserQuery.objects.filter(user=self.user, query=query).count(), 1)

    def test_save_without_dashboard(self):
        result = save_search_query(self.user, QueryDict('tags=young'), 'Save test young only')
        self.assertIsNone(result['user_query'])
        self.assertFalse(UserQuery.objects.filter(query=result['query']).exists())

    def test_title_rules(self):
        with self.assertRaises(SearchSaveError):
            save_search_query(self.user, QueryDict('tags=young'), '   ')
        save_search_query(self.user, QueryDict('tags=young'), 'Save test dup')
        with self.assertRaises(SearchSaveError) as ctx:
            save_search_query(self.user, QueryDict('tags=old'), 'Save test dup')
        self.assertIn('already exists', str(ctx.exception))
        with self.assertRaises(SearchSaveError):
            save_search_query(self.user, QueryDict('tags=young'), 'x' * 300)

    def test_invalid_search_or_anonymous_user_is_refused(self):
        from django.contrib.auth.models import AnonymousUser
        from explorer.models import Query

        before = Query.objects.count()
        with self.assertRaises(SearchSaveError):
            save_search_query(self.user, QueryDict('ra=10'), 'Save test bad')
        with self.assertRaises(SearchSaveError):
            save_search_query(AnonymousUser(), QueryDict('tags=young'), 'Save test anon')
        self.assertEqual(Query.objects.count(), before)

    def test_dashboard_section_renders_the_saved_search(self):
        result = save_search_query(self.user, QueryDict('has_spectrum=true'), 'Save test section', add_to_dashboard=True)
        client = Client()
        client.force_login(self.user)
        with mock.patch.object(views_module, 'connections', _ExplorerToDefault()):
            response = client.get(reverse('personaldashboard_section', args=[result['user_query'].id]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('/transient_detail/2026sea/', html)
        self.assertIn('/transient_detail/2026prv/', html)
        self.assertNotIn('/transient_detail/ps26abc/', html)


class SavePageTests(SearchFixture):
    # compiling for the explorer alias may open that connection (MySQL asks its
    # version for REGEXP); the saved SQL itself runs on default (run_sql).
    databases = {'default', 'explorer'}


    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_search_page_offers_the_save_modal_with_sql_preview(self):
        html = self.client.get('/search/?status=New&has_spectrum=false').content.decode()
        self.assertIn('id="search-save-open"', html)
        self.assertIn('id="search-save-modal"', html)
        self.assertIn('name="params" value="status=New&amp;has_spectrum=false"', html)
        self.assertIn('id="search-save-sql"', html)
        self.assertIn('YSE_App_transient', html)
        self.assertIn('value="Search: Status New; Has spectrum false"', html)
        self.assertIn('id="search-save-dashboard"', html)
        self.assertNotIn('id="search-save-blocked"', html)

    def test_invalid_search_disables_saving_with_a_reason(self):
        html = self.client.get('/search/?ra=10').content.decode()
        self.assertIn('id="search-save-blocked"', html)
        self.assertIn('id="search-save-submit" disabled', html)

    def test_post_saves_and_confirms(self):
        from explorer.models import Query

        response = self.client.post(reverse('save_search'), {
            'params': 'status=New&status=Watch&page=3', 'title': 'Page test query', 'add_to_dashboard': 'on',
        })
        self.assertEqual(response.status_code, 302)
        query = Query.objects.get(title='Page test query')
        user_query = UserQuery.objects.get(user=self.user, query=query)
        self.assertIn('saved_query=%d' % query.id, response['Location'])
        self.assertIn('saved_user_query=%d' % user_query.id, response['Location'])
        self.assertNotIn('page=3', response['Location'])
        self.assertIn('search/?status=New&status=Watch', query.description)
        html = self.client.get(response['Location']).content.decode()
        self.assertIn('id="search-saved"', html)
        self.assertIn('Page test query', html)
        self.assertIn(reverse('query_detail', args=[query.id]), html)
        self.assertIn(reverse('personaldashboard'), html)

    def test_post_without_dashboard_and_error_paths(self):
        from explorer.models import Query

        response = self.client.post(reverse('save_search'), {'params': 'tags=young', 'title': 'Page test no dash'})
        query = Query.objects.get(title='Page test no dash')
        self.assertFalse(UserQuery.objects.filter(query=query).exists())
        self.assertNotIn('saved_user_query', response['Location'])
        response = self.client.post(reverse('save_search'), {'params': 'tags=young', 'title': 'Page test no dash'})
        self.assertIn('save_error=', response['Location'])
        html = self.client.get(response['Location']).content.decode()
        self.assertIn('id="search-save-error"', html)
        self.assertIn('already exists', html)
        response = self.client.post(reverse('save_search'), {'params': 'ra=10', 'title': 'Page test bad'})
        self.assertIn('save_error=', response['Location'])
        self.assertFalse(Query.objects.filter(title='Page test bad').exists())
        self.assertEqual(self.client.get(reverse('save_search')).status_code, 405)

    def test_non_staff_can_save_but_gets_no_explorer_link(self):
        client = Client()
        client.force_login(self.outsider)
        response = client.post(reverse('save_search'), {'params': 'has_spectrum=true', 'title': 'Outsider query', 'add_to_dashboard': 'on'})
        self.assertEqual(response.status_code, 302)
        html = client.get(response['Location']).content.decode()
        self.assertIn('id="search-saved"', html)
        self.assertNotIn('open in Query Explorer', html)
        from explorer.models import Query
        self.assertEqual(run_sql(Query.objects.get(title='Outsider query').sql), ['2026sea'])

    def test_login_required(self):
        response = Client().post(reverse('save_search'), {'params': 'tags=young', 'title': 'anon'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])


class SaveApiTests(SearchFixture):
    # compiling for the explorer alias may open that connection (MySQL asks its
    # version for REGEXP); the saved SQL itself runs on default (run_sql).
    databases = {'default', 'explorer'}


    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_post_json_params(self):
        from explorer.models import Query

        response = self.client.post(
            '/api/transients/save_search/',
            data=json.dumps({'title': 'API json query', 'add_to_dashboard': True,
                             'params': {'status': ['New', 'Watch'], 'ordering': 'name'}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        query = Query.objects.get(pk=data['query_id'])
        self.assertEqual(query.title, 'API json query')
        self.assertEqual(run_sql(query.sql), ['2026pub', '2026zzz', 'PS26abc'])
        self.assertEqual(data['sql'], query.sql)
        self.assertTrue(data['explorer_url'].endswith(reverse('query_detail', args=[query.id])))
        self.assertEqual(UserQuery.objects.get(pk=data['user_query_id']).query, query)

    def test_post_form_query_string(self):
        response = self.client.post('/api/transients/save_search/', {'title': 'API form query', 'query_string': 'tags=young'})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.json()['user_query_id'])

    def test_errors(self):
        response = self.client.post('/api/transients/save_search/', {'title': '', 'query_string': 'tags=young'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/transients/save_search/', {'title': 'API bad', 'query_string': 'ra=10'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('both RA and Dec', response.json()['detail'])
        response = Client().post('/api/transients/save_search/', {'title': 'anon', 'query_string': 'tags=young'})
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.client.get('/api/transients/save_search/').status_code, 405)
