"""GET /healthz/ (#768): public, one query, 200 when the DB answers, 503 when it does not."""

from unittest import mock

from django.db import DEFAULT_DB_ALIAS, OperationalError, connections
from django.test import Client, TestCase, override_settings
from django.urls import reverse


class HealthzTests(TestCase):
    """Run the real view through the full middleware stack against the test database."""

    def setUp(self) -> None:
        """Use a cookie-less client, like the watchdog's curl."""
        self.client = Client()
        self.url = reverse("healthz")

    def test_ok_with_exactly_one_query(self) -> None:
        """An anonymous request runs SELECT 1 and nothing else (no session or user lookup)."""
        with self.assertNumQueries(1):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertTrue(response["Content-Type"].startswith("text/plain"))

    def test_uncacheable_and_sets_no_cookies(self) -> None:
        """Proxies must not cache the answer, and polling must not create sessions."""
        response = self.client.get(self.url)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(len(response.cookies), 0)

    @override_settings(LOGIN_REQUIRED_MIDDLEWARE=True)
    def test_public_with_login_required_middleware_on(self) -> None:
        """The deny-by-default login middleware lets it through without a redirect."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_head_allowed_and_post_rejected(self) -> None:
        """Monitors may use HEAD; anything that could change state is refused."""
        self.assertEqual(self.client.head(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_database_error_returns_503(self) -> None:
        """If opening a cursor fails the way an unreachable MySQL does, the answer is 503."""
        conn = connections[DEFAULT_DB_ALIAS]
        with mock.patch.object(conn, "cursor", side_effect=OperationalError(2003, "Can't connect")):
            with self.assertLogs("YSE_App.health_views", level="WARNING"):
                response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"db error")
        self.assertIn("no-store", response["Cache-Control"])
