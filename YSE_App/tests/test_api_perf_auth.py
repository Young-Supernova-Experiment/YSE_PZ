"""API list query counts (#456), token auth (#457) and Basic auth user (#458)."""

import base64
import json
from unittest import mock

from django.contrib.auth.models import AnonymousUser, Group, User
from django.db import connection
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.authtoken.models import Token

from YSE_App.basicauth import login_or_basic_auth_required
from YSE_App.models import (
    DataQuality,
    Host,
    TransientPhotData,
    TransientTag,
)
from YSE_App.serializers.related_fields import IdHyperlinkedRelatedField
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

LIST_URLS = (
    "/api/transients/",
    "/api/transientphotometry/",
    "/api/transientphotdata/",
    "/api/transientspectra/",
)


def _results(response):
    data = response.json()
    return data["results"] if isinstance(data, dict) and "results" in data else data


class ApiListFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("api_perf_user")
        audit = audit_fields(cls.user)
        cls.tag = TransientTag.objects.create(name="api-perf-tag", **audit)
        cls.group = Group.objects.create(name="api-perf-group")
        cls.user.groups.add(cls.group)
        cls.dq = DataQuality.objects.create(name="api-perf-dq", **audit)
        cls.transients = []
        for i in range(6):
            t = create_minimal_transient(cls.user, name="api-perf-sn-%d" % i)
            t.tags.add(cls.tag)
            attach_synthetic_host(cls.user, t)
            phot = attach_synthetic_photometry(cls.user, t, n_points=2)
            phot.groups.add(cls.group)
            for pd in TransientPhotData.objects.filter(photometry=phot):
                pd.data_quality.add(cls.dq)
            spec = attach_synthetic_spectrum(cls.user, t)
            spec.groups.add(cls.group)
            spec.data_quality.add(cls.dq)
            cls.transients.append(t)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _count(self, url, limit):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url, {"limit": limit})
        self.assertEqual(response.status_code, 200, url)
        self.assertEqual(len(_results(response)), limit, url)
        return len(ctx.captured_queries)


class QueryCountTests(ApiListFixture):
    def test_list_query_count_does_not_grow_with_page_size(self):
        for url in LIST_URLS:
            with self.subTest(url=url):
                small = self._count(url, 2)
                large = self._count(url, 6)
                self.assertEqual(small, large, "%s: %d queries for 2 rows, %d for 6" % (url, small, large))


# The fixture account reads all data as a service reader; staff alone no longer do (#706).
@override_settings(DATA_SERVICE_READER_USERNAMES=['api_perf_user'])
class OutputUnchangedTests(ApiListFixture):
    def test_json_identical_without_pk_only_optimization(self):
        """The pk-only field must render byte-identical JSON to the old per-row load."""
        for url in LIST_URLS:
            with self.subTest(url=url):
                new = self.client.get(url, {"limit": 6})
                with mock.patch.object(
                    IdHyperlinkedRelatedField, "use_pk_only_optimization", return_value=False
                ):
                    old = self.client.get(url, {"limit": 6})
                self.assertEqual(new.status_code, 200)
                self.assertEqual(new.content, old.content)
                rows = _results(new)
                self.assertTrue(any("/api/" in json.dumps(r) for r in rows))

    def test_null_host_renders_null(self):
        t = create_minimal_transient(self.user, name="api-perf-nohost")
        t.tags.add(self.tag)
        response = self.client.get("/api/transients/", {"name": t.name})
        rows = _results(response)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["host"])
        self.assertIsNone(rows[0]["best_spectrum"])
        self.assertTrue(Host.objects.exists())


class TokenAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("api_token_user")
        cls.token = Token.objects.create(user=cls.user)

    def test_token_header_authenticates_api(self):
        response = Client().get("/api/transients/", {"limit": 1}, HTTP_AUTHORIZATION="Token %s" % self.token.key)
        self.assertEqual(response.status_code, 200)

    def test_bad_token_rejected(self):
        response = Client().get("/api/transients/", {"limit": 1}, HTTP_AUTHORIZATION="Token not-a-real-token")
        self.assertEqual(response.status_code, 401)

    def test_basic_auth_still_works(self):
        user = User.objects.create_user("api_basic_user", password="pw-for-test")
        cred = base64.b64encode(b"api_basic_user:pw-for-test").decode()
        response = Client().get("/api/transients/", {"limit": 1}, HTTP_AUTHORIZATION="Basic " + cred)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(user.is_active)


class BasicAuthSetsUserTests(TestCase):
    """#458: the decorator must expose the authenticated user as request.user."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("api_basic_deco", password="pw-for-test")
        cls.header = "Basic " + base64.b64encode(b"api_basic_deco:pw-for-test").decode()

    def test_function_view_sees_authenticated_user(self):
        @login_or_basic_auth_required
        def view(request):
            return HttpResponse(request.user.username)

        request = RequestFactory().get("/x", HTTP_AUTHORIZATION=self.header)
        request.user = AnonymousUser()
        response = view(request)
        self.assertEqual(response.content.decode(), "api_basic_deco")
        self.assertEqual(request.user, self.user)

    def test_class_view_request_sees_authenticated_user(self):
        class FakeView:
            pass

        @login_or_basic_auth_required
        def view(self_):
            return HttpResponse(self_.request.user.username)

        fake = FakeView()
        fake.request = RequestFactory().get("/x", HTTP_AUTHORIZATION=self.header)
        fake.request.user = AnonymousUser()
        response = view(fake)
        self.assertEqual(response.content.decode(), "api_basic_deco")

    def test_wrong_password_forbidden_and_user_untouched(self):
        @login_or_basic_auth_required
        def view(request):
            return HttpResponse("ok")

        bad = "Basic " + base64.b64encode(b"api_basic_deco:wrong").decode()
        request = RequestFactory().get("/x", HTTP_AUTHORIZATION=bad)
        request.user = AnonymousUser()
        self.assertEqual(view(request).status_code, 403)
        self.assertFalse(request.user.is_authenticated)
