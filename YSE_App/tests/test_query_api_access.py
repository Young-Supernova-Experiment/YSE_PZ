"""/query_api/ access rules (#524).

* Saved queries: titles on ``QUERY_API_ALLOWED_QUERIES``, the caller's own
  queries, and any query for SQL Explorer users and service accounts. Anything
  else is a 404, the same as an unknown title.
* Rows are limited to transients the caller may view, except for service
  readers (the cron accounts; #725). Staff, superusers and ``api_writers``
  accounts are scoped like everyone else.
* The titles the crons and scripts request are on the built-in list.

Synthetic users, groups and transients only.
"""

import base64
import json
import os
import re
from unittest import mock
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings

from YSE_App import views as views_module
from YSE_App.perf.access import (
    MEMBER_GROUP_NAME,
    create_non_staff_user,
    seed_access_transients,
    visible_suffixes_for,
)
from YSE_App.tests.fixtures_minimal import create_test_user

_PASSWORD = 'query-api-access-pw'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
_REPO_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _basic_auth(username):
    token = base64.b64encode(('%s:%s' % (username, _PASSWORD)).encode()).decode()
    return {'HTTP_AUTHORIZATION': 'Basic %s' % token}


class _ExplorerToDefault:
    """Run the explorer-connection SQL on the test database."""

    def __getitem__(self, alias):
        from django.db import connections

        return connections['default' if alias == 'explorer' else alias]


@override_settings(
    PASSWORD_HASHERS=PASSWORD_HASHERS,
    QUERY_API_CACHE_SECONDS=0,
    QUERY_API_ALLOWED_QUERIES=['qaa published'],
    API_SERVICE_USERNAMES=['qaa_cron'],
    QUERY_API_VISIBILITY_FILTER=True,
)
class QueryApiAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.owner = create_test_user('qaa_owner')
        cls.dataset = seed_access_transients(cls.owner, prefix='qaa')
        cls.member = create_non_staff_user('qaa_member', (MEMBER_GROUP_NAME,))
        cls.cron = create_test_user('qaa_cron', is_staff=False)
        cls.writer = create_test_user('qaa_writer', is_staff=False)
        cls.staff = create_test_user('qaa_staff', is_staff=True)
        cls.superuser = create_test_user('qaa_super', is_staff=True)
        cls.superuser.is_superuser = True
        cls.superuser.save()
        cls.writer.groups.add(Group.objects.get_or_create(name=settings.API_WRITERS_GROUP)[0])
        for user in (cls.owner, cls.staff, cls.superuser, cls.member, cls.cron, cls.writer):
            user.set_password(_PASSWORD)
            user.save()
        sql = "SELECT t.name, t.ra FROM YSE_App_transient t WHERE t.name LIKE 'qaa%' ORDER BY t.name"
        cls.published = Query.objects.create(
            title='qaa published', sql=sql, snapshot=False, created_by_user=cls.owner)
        cls.unpublished = Query.objects.create(
            title='qaa unpublished', sql=sql, snapshot=False, created_by_user=cls.owner)
        cls.own = Query.objects.create(
            title='qaa member own', sql=sql, snapshot=False, created_by_user=cls.member)
        cls.by_id = Query.objects.create(
            title='qaa by id',
            sql="SELECT t.id AS transient_id, t.name AS label FROM YSE_App_transient t "
                "WHERE t.name LIKE 'qaa%' ORDER BY t.name",
            snapshot=False, created_by_user=cls.owner)
        cls.all_names = sorted(t.name for t in cls.dataset.transients)
        visible = visible_suffixes_for((MEMBER_GROUP_NAME,))
        cls.member_names = sorted(
            t.name for s, t in cls.dataset.by_suffix.items() if s in visible)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, 'connections', _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client()

    def _get(self, user, query):
        return self.client.get('/query_api/%s/' % quote(query.title), **_basic_auth(user.username))

    def _names(self, response, key='name'):
        self.assertEqual(response.status_code, 200)
        return sorted(row[key] for row in json.loads(response.content)['transients'])

    # -- allowlist ---------------------------------------------------------

    def test_allowlisted_query_runs_for_non_staff(self):
        self.assertEqual(self._names(self._get(self.member, self.published)), self.member_names)

    def test_query_not_on_allowlist_is_404_for_non_staff(self):
        self.assertEqual(self._get(self.member, self.unpublished).status_code, 404)

    def test_own_query_runs_for_non_staff(self):
        self.assertEqual(self._names(self._get(self.member, self.own)), self.member_names)

    def test_explorer_user_may_run_any_query(self):
        self.assertEqual(self._get(self.owner, self.unpublished).status_code, 200)

    def test_service_reader_may_run_any_query(self):
        self.assertEqual(self._names(self._get(self.cron, self.unpublished)), self.all_names)

    def test_api_writer_is_not_a_service_account(self):
        self.assertEqual(self._get(self.writer, self.unpublished).status_code, 404)

    def test_anonymous_is_refused(self):
        response = self.client.get('/query_api/%s/' % quote(self.published.title))
        self.assertIn(response.status_code, (302, 401, 403))

    def test_session_login_follows_the_same_rule(self):
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.get('/query_api/%s/' % quote(self.unpublished.title)).status_code, 404)

    # -- visibility --------------------------------------------------------

    def test_non_staff_rows_are_visibility_filtered(self):
        names = self._names(self._get(self.member, self.published))
        self.assertLess(len(names), len(self.all_names))
        self.assertNotIn(self.dataset.by_suffix['otherphot'].name, names)
        self.assertNotIn(self.dataset.by_suffix['nodata'].name, names)
        self.assertIn(self.dataset.by_suffix['memberphot'].name, names)

    def test_service_reader_gets_every_row(self):
        self.assertEqual(self._names(self._get(self.cron, self.published)), self.all_names)

    def test_data_service_reader_username_gets_every_row(self):
        with override_settings(API_SERVICE_USERNAMES=[], DATA_SERVICE_READER_USERNAMES=['qaa_writer']):
            self.assertEqual(self._names(self._get(self.writer, self.published)), self.all_names)

    def test_staff_superuser_and_api_writer_get_scoped_rows(self):
        for user in (self.staff, self.superuser, self.writer):
            with self.subTest(user=user.username):
                names = self._names(self._get(user, self.published))
                self.assertLess(len(names), len(self.all_names))
                self.assertNotIn(self.dataset.by_suffix['otherphot'].name, names)

    def test_transient_id_column_is_used_when_present(self):
        with override_settings(QUERY_API_ALLOWED_QUERIES=['qaa by id']):
            response = self._get(self.member, self.by_id)
        self.assertEqual(self._names(response, key='label'), self.member_names)

    def test_filter_can_be_turned_off(self):
        with override_settings(QUERY_API_VISIBILITY_FILTER=False):
            self.assertEqual(self._names(self._get(self.member, self.published)), self.all_names)

    # -- registered Python queries ----------------------------------------

    def test_python_query_rows_are_serialized_and_filtered(self):
        from YSE_App.models import Transient
        from YSE_App.queries import yse_python_queries

        def qaa_py():
            return Transient.objects.filter(name__startswith='qaa')

        with mock.patch.dict(yse_python_queries.python_query_reg.all, {'qaa_py': qaa_py}):
            member = self.client.get('/query_api/qaa_py/', **_basic_auth(self.member.username))
            cron = self.client.get('/query_api/qaa_py/', **_basic_auth(self.cron.username))
        self.assertEqual(self._names(member, key='transient_name'), self.member_names)
        self.assertEqual(self._names(cron, key='transient_name'), self.all_names)

    def test_unknown_title_is_404(self):
        response = self.client.get('/query_api/qaa_nothing/', **_basic_auth(self.member.username))
        self.assertEqual(response.status_code, 404)


class QueryApiCallerTitlesTests(SimpleTestCase):
    """Every title a cron or script requests is on the built-in allowlist."""

    CALLERS = (
        'data_ingest/QUB_data.py',
        'data_ingest/DECam_upload.py',
        'data_ingest/DECam_upload_clusters.py',
        'util/get_YSE_PAs.py',
        'yse_utils/get_YSE_PAs.py',
    )

    def test_caller_titles_are_allowlisted(self):
        titles = set()
        for rel in self.CALLERS:
            with open(os.path.join(_REPO_APP, rel)) as fh:
                src = fh.read()
            titles.update(re.findall(r"query_api/([A-Za-z0-9_]+)/", src))
            titles.update(re.findall(r"querytitle = '([^']+)'", src))
        self.assertEqual(len(titles), 4, titles)
        self.assertLessEqual(titles, set(settings.QUERY_API_DEFAULT_ALLOWED_QUERIES))
        self.assertLessEqual(titles, set(settings.QUERY_API_ALLOWED_QUERIES))
