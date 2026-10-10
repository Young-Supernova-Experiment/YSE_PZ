"""Ingest / cron performance fixes (#472 cron lock, #473 HTTP timeouts, #474 query_api cache,
#475 get_host prefilter, #476 box_search). No test touches the network."""

import base64
import datetime
import json
import os
import re
import tempfile
from unittest import mock
from urllib.parse import quote

import numpy as np
import requests
from astropy import units as u
from astropy.coordinates import SkyCoord
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from YSE_App.common import ingest_http
from YSE_App.models import Host, Transient, TransientTag
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user

_PASSWORD = 'ingest-cron-test-pw'
_INGEST_MODULES = ('QUB_data.py', 'TNS_uploads.py', 'DECam_upload.py', 'DECam_upload_clusters.py',
                   'YSE_Forced_Phot.py')


def _basic_auth(username):
    token = base64.b64encode(('%s:%s' % (username, _PASSWORD)).encode()).decode()
    return {'HTTP_AUTHORIZATION': 'Basic %s' % token}


def _api_user(username):
    user = create_test_user(username)
    user.set_password(_PASSWORD)
    user.save()
    return user


# ------------------------------------------------------------------ #472


class _FakeCron:
    code = 'ingest_cron_perf.fake'


class CronFileLockTests(SimpleTestCase):
    def test_settings_select_the_file_lock(self):
        self.assertEqual(settings.DJANGO_CRON_LOCK_BACKEND, 'django_cron.backends.lock.file.FileLock')
        self.assertTrue(settings.DJANGO_CRON_LOCKFILE_PATH)

    def test_second_holder_is_refused_until_release(self):
        from django_cron.backends.lock.file import FileLock

        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = os.path.join(tmp, 'locks')
            with override_settings(DJANGO_CRON_LOCKFILE_PATH=lock_dir):
                first = FileLock(_FakeCron, True)
                self.assertTrue(first.lock())
                second = FileLock(_FakeCron, True)
                self.assertFalse(second.lock(), 'a second runcrons process must not run the same job')
                first.release()
                third = FileLock(_FakeCron, True)
                self.assertTrue(third.lock())
                third.release()


# ------------------------------------------------------------------ #473


class IngestHttpSessionTests(SimpleTestCase):
    def _ok(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        return response

    def test_default_timeout_applied_and_explicit_timeout_wins(self):
        session = ingest_http.ingest_session(timeout=(3, 7))
        with mock.patch('requests.adapters.HTTPAdapter.send', return_value=self._ok()) as send:
            session.get('https://example.invalid/a')
            self.assertEqual(send.call_args.kwargs['timeout'], (3, 7))
            session.post('https://example.invalid/b', data={'x': 1}, timeout=42)
            self.assertEqual(send.call_args.kwargs['timeout'], 42)

    @override_settings(INGEST_HTTP_CONNECT_TIMEOUT_SECONDS=4, INGEST_HTTP_READ_TIMEOUT_SECONDS=9,
                       INGEST_HTTP_UPLOAD_TIMEOUT_SECONDS=99)
    def test_timeouts_come_from_settings(self):
        self.assertEqual(ingest_http.default_timeout(), (4.0, 9.0))
        self.assertEqual(ingest_http.upload_timeout(), (4.0, 99.0))
        self.assertEqual(ingest_http.ingest_session().default_timeout, (4.0, 9.0))

    def test_retries_are_bounded_and_never_resend_a_post(self):
        session = ingest_http.ingest_session(retries=2)
        retry = session.get_adapter('https://example.invalid').max_retries
        self.assertEqual(retry.total, 2)
        self.assertTrue(retry.is_retry('GET', 503))
        self.assertTrue(retry.is_retry('GET', 502))
        self.assertFalse(retry.is_retry('GET', 404))
        self.assertFalse(retry.is_retry('POST', 503))
        self.assertFalse(retry.raise_on_status)

    def test_module_facade_uses_one_lazy_session(self):
        facade = ingest_http._LazySession()
        with mock.patch('requests.adapters.HTTPAdapter.send', return_value=self._ok()) as send:
            facade.get('https://example.invalid/a')
            facade.post(url='https://example.invalid/b', data='{}')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.kwargs['timeout'], ingest_http.default_timeout())

    def test_ingest_modules_have_no_bare_requests_calls(self):
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data_ingest')
        bare = re.compile(r'^(?!\s*#).*\brequests\.(get|post|Session)\(', re.M)
        for name in _INGEST_MODULES:
            with open(os.path.join(root, name)) as fh:
                source = fh.read()
            self.assertEqual(bare.findall(source), [], '%s still calls requests without a timeout' % name)
            self.assertIn('ingest_http', source)


# ------------------------------------------------------------------ #474


class _ExplorerToDefault:
    """Route 'explorer' to 'default' (CI has no explorer test DB; see test_speed_regressions)."""

    def __getitem__(self, alias):
        from django.db import connections

        return connections['default' if alias == 'explorer' else alias]


@override_settings(QUERY_API_CACHE_SECONDS=300)
class QueryApiCacheTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.user = _api_user('query_api_cache_user')
        cls.transient = create_minimal_transient(cls.user, name='2026qapi', ra=12.5, dec=-3.25)
        cls.query = Query.objects.create(
            title='query api cache test',
            sql="SELECT t.name, t.ra FROM YSE_App_transient t WHERE t.name = '2026qapi'",
            snapshot=False, created_by_user=cls.user,
        )

    def setUp(self):
        from YSE_App import views as views_module

        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, 'connections', _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client()

    def _call(self):
        """(response, times the saved SQL itself ran)."""
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get('/query_api/%s/' % quote(self.query.title), **_basic_auth(self.user.username))
        saved_sql = self.query.sql.lower()
        runs = [q for q in ctx.captured_queries if q['sql'].lower().strip() == saved_sql]
        return response, len(runs)

    def test_rows_are_cached_and_returned_as_before(self):
        first, cold = self._call()
        second, warm = self._call()
        self.assertEqual((cold, warm), (1, 0))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(json.loads(first.content), {'transients': [{'name': '2026qapi', 'ra': 12.5}]})
        self.assertEqual(json.loads(second.content), json.loads(first.content))

    def test_edited_sql_is_not_served_from_the_old_entry(self):
        self._call()
        self.query.sql = "SELECT t.name FROM YSE_App_transient t WHERE t.name = '2026qapi'"
        self.query.save()
        response, runs = self._call()
        self.assertEqual(runs, 1)
        self.assertEqual(json.loads(response.content), {'transients': [{'name': '2026qapi'}]})

    @override_settings(QUERY_API_CACHE_SECONDS=0)
    def test_zero_ttl_runs_every_time(self):
        self.assertEqual([self._call()[1] for _ in range(2)], [1, 1])

    def test_capped_query_answers_504(self):
        from YSE_App.common.db_time_cap import QueryTimeout

        with mock.patch('YSE_App.views.run_explorer_query_rows_cached',
                        side_effect=QueryTimeout('query exceeded the 1000 ms execution time cap')):
            response, _ = self._call()
        self.assertEqual(response.status_code, 504)
        self.assertIn('execution time cap', json.loads(response.content)['message'])


# ------------------------------------------------------------------ #475


class GetHostPrefilterTests(TestCase):
    POSITIONS = [
        # (ra, dec): near the default query, across RA 0/360, near a pole, far away
        (150.0, 2.0), (150.004, 2.003), (150.02, 2.0), (149.9, 2.0), (151.0, 2.0),
        (359.999, 0.0), (0.001, 0.001), (0.03, 0.0),
        (10.0, 89.995), (190.0, 89.995), (100.0, 89.9),
        (150.0, 60.0), (150.012, 60.0), (150.03, 60.0),
        (300.0, -45.0),
    ]

    @classmethod
    def setUpTestData(cls):
        cls.user = _api_user('get_host_prefilter_user')
        audit = audit_fields(cls.user)
        for i, (ra, dec) in enumerate(cls.POSITIONS):
            Host.objects.create(name='prefilter-host-%d' % i, ra=ra, dec=dec, **audit)

    def _expected(self, ra, dec, sep):
        """The old algorithm: every host in one SkyCoord, cut on separation <= sep arcmin."""
        hosts = list(Host.objects.all().order_by('pk'))
        coords = SkyCoord([h.ra for h in hosts], [h.dec for h in hosts], unit=(u.deg, u.deg))
        seps = coords.separation(SkyCoord(ra, dec, unit=(u.deg, u.deg))).arcminute
        return [(hosts[i].id, float(seps[i])) for i in np.where(seps <= float(sep))[0]]

    def _actual(self, ra, dec, sep):
        response = self.client.get('/get_host/%s/%s/%s/' % (ra, dec, sep), **_basic_auth(self.user.username))
        self.assertEqual(response.status_code, 200)
        return [(h['host_id'], h['host_sep']) for h in json.loads(response.content)['host candidates']]

    def test_same_matches_as_the_full_table_scan(self):
        for ra, dec, sep in [('150.0', '2.0', '1.0'), ('150.0', '2.0', '0.1'), ('0.0005', '0.0', '1.0'),
                             ('359.9995', '0.0', '2.5'), ('45.0', '89.99', '1.0'), ('150.0', '60.0', '1.0'),
                             ('150.0', '60.0', '2.0'), ('300.0', '-45.0', '0.5'), ('20.0', '20.0', '5.0')]:
            with self.subTest(ra=ra, dec=dec, sep=sep):
                self.assertEqual(self._actual(ra, dec, sep), self._expected(ra, dec, sep))

    def test_reads_only_the_box(self):
        from YSE_App.data_utils import _host_cone_candidates

        names = set(_host_cone_candidates('150.0', '2.0', '1.0').values_list('name', flat=True))
        self.assertIn('prefilter-host-0', names)
        self.assertNotIn('prefilter-host-14', names)
        with CaptureQueriesContext(connection) as ctx:
            self._actual('150.0', '2.0', '30.0')
        host_reads = [q for q in ctx.captured_queries if 'yse_app_host' in q['sql'].lower()]
        self.assertEqual(len(host_reads), 1, 'one read of the candidates, not one query per match')


# ------------------------------------------------------------------ #476


class BoxSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = _api_user('box_search_user')
        cls.yse, _ = TransientTag.objects.get_or_create(name='YSE', defaults=audit_fields(cls.user))
        specs = {
            'centre': (150.0, 20.0, True),
            'old-box': (151.0, 21.0, True),          # inside the old +-1.65 box too
            'high-dec-in-field': (153.0, 60.0, True),  # 1.5 deg on sky; outside the old RA box
            'wrap': (359.8, 0.2, True),             # field at RA 0.5: across RA 0
            'too-far-dec': (150.0, 22.0, True),
            'no-tag': (150.1, 20.1, False),
            'old': (150.2, 20.2, True),
        }
        cls.names = {}
        for key, (ra, dec, tagged) in specs.items():
            t = create_minimal_transient(cls.user, name='2026box-%s' % key, ra=ra, dec=dec)
            if tagged:
                t.tags.add(cls.yse)
            cls.names[key] = t.name
        Transient.objects.filter(name=cls.names['old']).update(
            created_date=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=120))

    def _names(self, ra, dec, radius):
        response = self.client.get('/box_search/%s/%s/%s/' % (ra, dec, radius), **_basic_auth(self.user.username))
        self.assertEqual(response.status_code, 200)
        return {t['transient_name'] for t in json.loads(response.content)['transients']}

    def test_field_search(self):
        names = self._names('150.0', '20.0', '1.65')
        self.assertEqual(names, {self.names['centre'], self.names['old-box']})

    def test_ra_width_is_corrected_for_dec(self):
        self.assertIn(self.names['high-dec-in-field'], self._names('150.5', '60.0', '1.65'))

    def test_ra_wrap(self):
        self.assertIn(self.names['wrap'], self._names('0.5', '0.0', '1.65'))
