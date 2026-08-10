"""Phase 5: DRF scoping, export header redaction, notification audience checks."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from YSE_App.models import FollowupStatus, Log, TransientFollowup
from YSE_App.services.notifications import collect_mention_emails
from YSE_App.services.visibility import (
    EXPORT_TRANSIENT_HEADER_ALLOWLIST,
    redact_transient_export_header_fields,
)
from YSE_App.tests.fixtures_minimal import create_minimal_transient
from YSE_App.tests.fixtures_security_matrix import (
    TRANSIENT_NAME,
    seed_security_test_matrix,
)


class Phase5DRFHardeningTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.transient, cls.users = seed_security_test_matrix()
        cls.user_b = cls.users["sec_user_b"]
        cls.staff = User.objects.create_user(
            username="sec_staff", password="sec-test-pass", is_staff=True
        )

    def test_user_viewset_non_staff_sees_self_only(self):
        client = APIClient()
        client.force_authenticate(user=self.user_b)
        response = client.get("/api/users/")
        self.assertEqual(response.status_code, 200)
        results = response.data["results"] if "results" in response.data else response.data
        usernames = {row["username"] for row in results}
        self.assertEqual(usernames, {"sec_user_b"})

    def test_group_viewset_non_staff_sees_own_groups_only(self):
        client = APIClient()
        client.force_authenticate(user=self.user_b)
        response = client.get("/api/groups/")
        self.assertEqual(response.status_code, 200)
        results = response.data["results"] if "results" in response.data else response.data
        names = {row["name"] for row in results}
        self.assertTrue(
            names.issubset(set(self.user_b.groups.values_list("name", flat=True)))
        )
        self.assertNotIn("sec-group-c", names)
        self.assertNotIn("sec-group-d", names)

    def test_transient_viewset_respects_access_filter(self):
        client = APIClient()
        client.force_authenticate(user=self.user_b)
        response = client.get("/api/transients/")
        self.assertEqual(response.status_code, 200)


class Phase5ExportRedactionTests(TestCase):
    def test_redact_drops_non_allowlisted_keys(self):
        fields = {
            "name": "2026abc",
            "ra": 1.0,
            "dec": 2.0,
            "status": "New",
            "created_by": "admin",
            "some_private_note": "secret",
        }
        redacted = redact_transient_export_header_fields(fields)
        self.assertEqual(set(redacted.keys()), {"name", "ra", "dec", "status"})
        self.assertNotIn("created_by", redacted)
        self.assertNotIn("some_private_note", redacted)
        self.assertTrue(set(redacted.keys()).issubset(EXPORT_TRANSIENT_HEADER_ALLOWLIST))


class Phase5NotificationAudienceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="notif_admin", password="x")
        cls.group_a = Group.objects.create(name="notif-group-a")
        cls.group_b = Group.objects.create(name="notif-group-b")
        cls.author = User.objects.create_user(
            username="notif_author", password="x", email="author@example.com"
        )
        cls.author.groups.add(cls.group_a)
        cls.insider = User.objects.create_user(
            username="notif_insider", password="x", email="insider@example.com"
        )
        cls.insider.groups.add(cls.group_a)
        cls.outsider = User.objects.create_user(
            username="notif_outsider", password="x", email="outsider@example.com"
        )
        cls.outsider.groups.add(cls.group_b)
        cls.transient = create_minimal_transient(cls.admin, name="notif-transient")

    def _private_log(self, comment: str) -> Log:
        log = Log.objects.create(
            transient=self.transient,
            comment=comment,
            is_public=False,
            created_by=self.author,
            modified_by=self.author,
        )
        log.groups.set([self.group_a])
        return log

    def test_channel_mention_skips_users_who_cannot_view_log(self):
        log = self._private_log("@channel please look")
        emails = collect_mention_emails(log.comment, log=log)
        self.assertIn("insider@example.com", emails)
        self.assertIn("author@example.com", emails)
        self.assertNotIn("outsider@example.com", emails)

    def test_user_mention_skips_invisible_recipient(self):
        log = self._private_log("@notif_outsider secret")
        emails = collect_mention_emails(log.comment, log=log)
        self.assertEqual(list(emails), [])

    def test_user_mention_allows_visible_recipient(self):
        log = self._private_log("@notif_insider hello")
        emails = collect_mention_emails(log.comment, log=log)
        self.assertEqual(list(emails), ["insider@example.com"])


class Phase5CreatorOnlyResourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.transient, cls.users = seed_security_test_matrix()
        cls.user_ab = cls.users["sec_user_ab"]
        cls.status, _ = FollowupStatus.objects.get_or_create(
            name="sec-phase5-requested",
            defaults={
                "created_by": cls.user_ab,
                "modified_by": cls.user_ab,
            },
        )

    def test_seed_creates_creator_only_classical_resource(self):
        from YSE_App.models import ClassicalResource
        from YSE_App.services.audience import resource_is_creator_only

        resource = ClassicalResource.objects.get(
            telescope__name="SecVis-Cls-mag99-creatorOnly"
        )
        self.assertTrue(resource.creator_only)
        self.assertTrue(resource_is_creator_only(resource))

    def test_empty_audience_allowed_on_creator_only_resource(self):
        from YSE_App.models import ClassicalResource

        resource = ClassicalResource.objects.get(
            telescope__name="SecVis-Cls-mag99-creatorOnly"
        )
        client = Client()
        client.force_login(self.user_ab)
        now = timezone.now()
        response = client.post(
            reverse("add_transient_followup"),
            {
                "status": self.status.pk,
                "transient": self.transient.pk,
                "valid_start": now.isoformat(),
                "valid_stop": (now + timedelta(days=2)).isoformat(),
                "classical_resource": resource.pk,
                "audience_groups": [],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        followup = TransientFollowup.objects.filter(
            transient=self.transient,
            classical_resource=resource,
            requested_by=self.user_ab,
        ).latest("id")
        self.assertFalse(followup.groups.exists())
