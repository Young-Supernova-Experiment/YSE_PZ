"""Feedback button (#443): storing, throttling, CSRF, the flag, and the
staff-only "Create GitHub issue" action (GitHub mocked; nothing is posted
automatically)."""

from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.models import EncryptedCredential, Feedback
from YSE_App.services import feedback as svc

XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
TEST_TOKEN = "test-token-not-real"
TEST_CREDENTIALS_KEY = Fernet.generate_key().decode()


def _payload(**extra):
    data = {"kind": "bug", "title": "Plot legend overlaps", "text": "The legend covers the points.",
            "include_page": "1", "page_url": "/transient_detail/2026abc/?tab=phot", "next": "/dashboard/"}
    data.update(extra)
    return data


def _github_reply(status=201, url="https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/9999"):
    response = mock.Mock(status_code=status)
    response.json.return_value = {"html_url": url, "number": 9999}
    return response


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    FEEDBACK_ENABLED=True,
    FEEDBACK_MAX_PER_HOUR=3,
)
class FeedbackSubmitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("feedback_member", email="member@example.org")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_submit_stores_feedback(self):
        response = self.client.post(reverse("feedback_submit"), _payload(), **XHR)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        item = Feedback.objects.get()
        self.assertEqual(item.user, self.user)
        self.assertEqual(item.kind, "bug")
        self.assertEqual(item.title, "Plot legend overlaps")
        self.assertEqual(item.text, "The legend covers the points.")
        self.assertEqual(item.page_url, "/transient_detail/2026abc/?tab=phot")
        self.assertEqual(item.status, Feedback.STATUS_NEW)
        self.assertEqual(item.github_issue_url, "")

    def test_submit_never_calls_github(self):
        with mock.patch("YSE_App.services.feedback.requests.post") as post:
            self.client.post(reverse("feedback_submit"), _payload(), **XHR)
        post.assert_not_called()
        self.assertEqual(Feedback.objects.get().status, Feedback.STATUS_NEW)

    def test_page_link_optional_and_cleaned(self):
        self.client.post(reverse("feedback_submit"), _payload(include_page=""), **XHR)
        self.assertEqual(Feedback.objects.latest("id").page_url, "")
        self.client.post(reverse("feedback_submit"), _payload(
            page_url="https://elsewhere.example/x/?api_token=abc&tab=1#frag"), **XHR)
        self.assertEqual(Feedback.objects.latest("id").page_url, "/x/?tab=1")

    def test_clean_page_url_is_always_a_site_path(self):
        self.assertEqual(svc.clean_page_url("//evil.example/a"), "/a")
        self.assertEqual(svc.clean_page_url("/\\evil.example/a"), "/evil.example/a")
        self.assertEqual(svc.clean_page_url("/p/?session=1&q=sn"), "/p/?q=sn")

    def test_empty_text_refused(self):
        response = self.client.post(reverse("feedback_submit"), _payload(text="  "), **XHR)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Feedback.objects.exists())

    def test_throttled_per_user(self):
        for _ in range(3):
            self.assertEqual(self.client.post(reverse("feedback_submit"), _payload(), **XHR).status_code, 200)
        response = self.client.post(reverse("feedback_submit"), _payload(), **XHR)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(Feedback.objects.count(), 3)
        other = User.objects.create_user("feedback_other")
        client = Client()
        client.force_login(other)
        self.assertEqual(client.post(reverse("feedback_submit"), _payload(), **XHR).status_code, 200)

    def test_get_refused(self):
        self.assertEqual(self.client.get(reverse("feedback_submit")).status_code, 405)

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse("feedback_submit"), _payload(), **XHR).status_code, 403)
        self.assertFalse(Feedback.objects.exists())

    def test_anonymous_redirected(self):
        response = Client().post(reverse("feedback_submit"), _payload())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Feedback.objects.exists())

    def test_form_post_redirects_to_safe_next(self):
        response = self.client.post(reverse("feedback_submit"), _payload(next="https://evil.example/"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("dashboard"))

    def test_button_shown_when_enabled(self):
        html = self.client.get(reverse("notification_list")).content.decode()
        self.assertIn('id="yse-feedback-modal"', html)
        self.assertIn('data-target="#yse-feedback-modal"', html)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], FEEDBACK_ENABLED=False)
class FeedbackFlagOffTests(TestCase):
    def test_off_by_default_in_settings(self):
        from YSE_App.tests.test_settings_hardening import _load_settings

        self.assertIs(_load_settings(debug="True").FEEDBACK_ENABLED, False)

    def test_endpoint_and_button_hidden(self):
        user = User.objects.create_user("feedback_flag_off")
        client = Client()
        client.force_login(user)
        self.assertEqual(client.post(reverse("feedback_submit"), _payload(), **XHR).status_code, 404)
        self.assertFalse(Feedback.objects.exists())
        self.assertNotIn("yse-feedback-modal", client.get(reverse("notification_list")).content.decode())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], FEEDBACK_ENABLED=True,
                   FEEDBACK_GITHUB_REPO="Young-Supernova-Experiment/YSE_PZ", CREDENTIALS_KEY=TEST_CREDENTIALS_KEY)
class FeedbackReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("feedback_staff", is_staff=True)
        cls.member = User.objects.create_user("feedback_sender", email="sender@example.org")
        cls.item = Feedback.objects.create(user=cls.member, kind="idea", title="Add a filter",
                                           text="A filter for host redshift.", page_url="/dashboard/")

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _store_token(self):
        cred = EncryptedCredential(name="GitHub feedback", service="github_feedback",
                                   created_by=self.staff, modified_by=self.staff)
        cred.set_secret({"token": TEST_TOKEN})
        cred.save()

    def _post_issue(self, client, **extra):
        data = {"action": "create_issue", "issue_title": "[Idea] Add a filter", "issue_body": "Edited body."}
        data.update(extra)
        return client.post(reverse("feedback_review_detail", args=[self.item.pk]), data)

    def test_review_pages_staff_only(self):
        member = self._client(self.member)
        self.assertEqual(member.get(reverse("feedback_review_list")).status_code, 403)
        self.assertEqual(member.get(reverse("feedback_review_detail", args=[self.item.pk])).status_code, 403)
        staff = self._client(self.staff)
        self.assertEqual(staff.get(reverse("feedback_review_list")).status_code, 200)
        response = staff.get(reverse("feedback_review_detail", args=[self.item.pk]))
        self.assertContains(response, "A filter for host redshift.")
        self.assertContains(response, "Create GitHub issue")

    def test_draft_has_no_sender_identity(self):
        body = svc.default_issue_body(self.item)
        self.assertIn("A filter for host redshift.", body)
        self.assertIn("/dashboard/", body)
        self.assertNotIn(self.member.username, body)
        self.assertNotIn(self.member.email, body)

    def test_non_staff_cannot_post(self):
        self._store_token()
        with mock.patch("YSE_App.services.feedback.requests.post") as post:
            response = self._post_issue(self._client(self.member))
        self.assertEqual(response.status_code, 403)
        post.assert_not_called()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, Feedback.STATUS_NEW)

    def test_staff_post_creates_issue_with_edited_text(self):
        self._store_token()
        with mock.patch("YSE_App.services.feedback.requests.post", return_value=_github_reply()) as post:
            response = self._post_issue(self._client(self.staff))
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.github.com/repos/Young-Supernova-Experiment/YSE_PZ/issues")
        self.assertEqual(kwargs["json"], {"title": "[Idea] Add a filter", "body": "Edited body.", "labels": ["feedback"]})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TEST_TOKEN)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, Feedback.STATUS_POSTED)
        self.assertEqual(self.item.github_issue_url, "https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/9999")
        self.assertEqual(self.item.reviewed_by, self.staff)

    def test_second_post_refused(self):
        self._store_token()
        client = self._client(self.staff)
        with mock.patch("YSE_App.services.feedback.requests.post", return_value=_github_reply()) as post:
            self._post_issue(client)
            self._post_issue(client)
        self.assertEqual(post.call_count, 1)

    def test_no_token_configured(self):
        with mock.patch("YSE_App.services.feedback.requests.post") as post:
            response = self._post_issue(self._client(self.staff))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No GitHub token is configured")
        post.assert_not_called()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, Feedback.STATUS_NEW)
        self.assertEqual(self.item.github_issue_url, "")

    def test_github_error_keeps_feedback_unposted(self):
        self._store_token()
        with mock.patch("YSE_App.services.feedback.requests.post", return_value=_github_reply(status=403)):
            response = self._post_issue(self._client(self.staff))
        self.assertContains(response, "GitHub returned HTTP 403")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, Feedback.STATUS_NEW)

    def test_staff_mark_reviewed_and_dismissed(self):
        client = self._client(self.staff)
        with mock.patch("YSE_App.services.feedback.requests.post") as post:
            client.post(reverse("feedback_review_detail", args=[self.item.pk]), {"action": "reviewed"})
            self.item.refresh_from_db()
            self.assertEqual(self.item.status, Feedback.STATUS_REVIEWED)
            client.post(reverse("feedback_review_detail", args=[self.item.pk]), {"action": "dismissed"})
        post.assert_not_called()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, Feedback.STATUS_DISMISSED)

    def test_admin_lists_feedback_for_staff_superuser(self):
        admin = User.objects.create_superuser("feedback_admin", "admin@example.org", "pw")
        response = self._client(admin).get(reverse("admin:YSE_App_feedback_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add a filter")

    def test_feedback_hidden_from_explorer_schema(self):
        from django.conf import settings

        self.assertIn("YSE_App_feedback", settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)
