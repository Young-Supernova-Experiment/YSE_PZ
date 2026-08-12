"""Observing-request parent/child schema (issues #135–#138)."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App.forms import TransientFollowupForm
from YSE_App.models import (
    FollowupStatus,
    TransientFollowup,
    TransientFollowupRequest,
)
from YSE_App.services.followup_requests import (
    DEFAULT_PRIORITY,
    create_or_attach_request,
    effective_priority,
    format_comments,
    format_requestors,
)
from YSE_App.services.visibility import (
    filter_transient_followups_for_user,
    followup_visible_to_user,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    create_minimal_transient,
    create_test_user,
)


class FollowupRequestTests(TestCase):
    def setUp(self):
        self.user = create_test_user("fu_req_user")
        self.user_b = create_test_user("fu_req_user_b", is_staff=False)
        self.transient = create_minimal_transient(self.user, name="fureq01")
        self.requested, _ = FollowupStatus.objects.get_or_create(
            name="Requested",
            defaults={"created_by": self.user, "modified_by": self.user},
        )
        self.successful, _ = FollowupStatus.objects.get_or_create(
            name="Successful",
            defaults={"created_by": self.user, "modified_by": self.user},
        )
        self.failed, _ = FollowupStatus.objects.get_or_create(
            name="Failed",
            defaults={"created_by": self.user, "modified_by": self.user},
        )
        self.now = timezone.now()
        self.stop = self.now + timedelta(days=7)

    def _create(self, user, *, status=None, priority=DEFAULT_PRIORITY, comment="", **kwargs):
        return create_or_attach_request(
            user,
            self.transient,
            status=status or self.requested,
            valid_start=self.now,
            valid_stop=self.stop,
            priority=priority,
            comment=comment,
            **kwargs,
        )

    def test_first_submit_creates_parent_and_child(self):
        parent, child, created = self._create(self.user, comment="first look")
        self.assertTrue(created)
        self.assertEqual(TransientFollowup.objects.count(), 1)
        self.assertEqual(TransientFollowupRequest.objects.count(), 1)
        self.assertEqual(child.requestor, self.user)
        self.assertEqual(child.priority, DEFAULT_PRIORITY)
        self.assertEqual(child.comment, "first look")
        self.assertEqual(parent.requested_by, self.user)
        self.assertEqual(parent.status, self.requested)
        self.assertEqual(parent.priority, DEFAULT_PRIORITY)

    def test_second_submit_same_resource_attaches(self):
        parent1, _, created1 = self._create(self.user, comment="one")
        parent2, child2, created2 = self._create(self.user_b, priority=2.0, comment="two")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(parent1.id, parent2.id)
        self.assertEqual(TransientFollowup.objects.count(), 1)
        self.assertEqual(parent2.requests.count(), 2)
        self.assertEqual(child2.requestor, self.user_b)

    def test_attach_does_not_change_parent_status(self):
        parent, _, _ = self._create(self.user)
        in_process, _ = FollowupStatus.objects.get_or_create(
            name="InProcess",
            defaults={"created_by": self.user, "modified_by": self.user},
        )
        parent.status = in_process
        parent.save(update_fields=["status"])
        _, _, created = self._create(self.user_b, status=self.requested)
        parent.refresh_from_db()
        self.assertFalse(created)
        self.assertEqual(parent.status, in_process)

    def test_terminal_parent_gets_new_parent(self):
        parent, _, _ = self._create(self.user)
        parent.status = self.successful
        parent.save(update_fields=["status"])
        new_parent, _, created = self._create(self.user, comment="again")
        self.assertTrue(created)
        self.assertNotEqual(parent.id, new_parent.id)
        self.assertEqual(TransientFollowup.objects.count(), 2)

    def test_same_user_new_child_uses_latest_priority(self):
        parent, _, _ = self._create(self.user, priority=2.0)
        _, _, created = self._create(self.user, priority=4.0)
        self.assertFalse(created)
        parent.refresh_from_db()
        self.assertEqual(parent.requests.count(), 2)
        self.assertEqual(effective_priority(parent), 4.0)
        self.assertEqual(parent.priority, 4.0)

    def test_effective_priority_is_min_of_each_users_latest(self):
        parent, _, _ = self._create(self.user, priority=2.0)
        self._create(self.user, priority=4.0)
        self._create(self.user_b, priority=3.0)
        parent.refresh_from_db()
        self.assertEqual(effective_priority(parent), 3.0)
        self.assertEqual(parent.priority, 3.0)

    def test_comments_accumulate_with_requestor_labels(self):
        parent, _, _ = self._create(self.user, comment="need spectrum")
        self._create(self.user_b, comment="ToO tonight")
        parent.refresh_from_db()
        text = format_comments(parent)
        self.assertIn("fu_req_user: need spectrum", text)
        self.assertIn("fu_req_user_b: ToO tonight", text)
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")
        requestors = format_requestors(parent)
        self.assertIn("fu_req_user", requestors)
        self.assertIn("fu_req_user_b", requestors)

    def test_priority_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            self._create(self.user, priority=0.5)
        with self.assertRaises(ValidationError):
            self._create(self.user, priority=5.1)

    def test_form_default_priority_is_4(self):
        form = TransientFollowupForm()
        self.assertEqual(form.fields["priority"].initial, 4.0)

    def test_form_rejects_priority_outside_range(self):
        data = {
            "status": self.requested.id,
            "valid_start": self.now,
            "valid_stop": self.stop,
            "priority": 0.0,
            "transient": self.transient.id,
        }
        form = TransientFollowupForm(data)
        self.assertFalse(form.is_valid())
        self.assertIn("priority", form.errors)

        data["priority"] = 6.0
        form = TransientFollowupForm(data)
        self.assertFalse(form.is_valid())
        self.assertIn("priority", form.errors)

    def test_non_ajax_post_creates_request_and_redirects(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("add_transient_followup"),
            {
                "status": self.requested.id,
                "valid_start": self.now.strftime("%Y-%m-%d %H:%M:%S"),
                "valid_stop": self.stop.strftime("%Y-%m-%d %H:%M:%S"),
                "priority": 4.0,
                "comment": "native post",
                "transient": self.transient.id,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.transient.slug, response.url)
        self.assertEqual(TransientFollowup.objects.count(), 1)
        self.assertEqual(TransientFollowupRequest.objects.count(), 1)

    def test_ajax_form_attaches_second_request(self):
        client = Client()
        client.force_login(self.user)
        payload = {
            "status": self.requested.id,
            "valid_start": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "valid_stop": self.stop.strftime("%Y-%m-%d %H:%M:%S"),
            "priority": 4.0,
            "comment": "ajax one",
            "transient": self.transient.id,
        }
        response = client.post(
            reverse("add_transient_followup"),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        first_id = response.json()["data"]["id"]
        self.assertFalse(response.json()["data"]["attached"])

        client_b = Client()
        client_b.force_login(self.user_b)
        payload["priority"] = 2.0
        payload["comment"] = "ajax two"
        response = client_b.post(
            reverse("add_transient_followup"),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["attached"])
        self.assertEqual(data["id"], first_id)
        self.assertEqual(data["priority"], 2.0)
        self.assertIn("fu_req_user", data["requestors"])
        self.assertIn("fu_req_user_b", data["requestors"])
        self.assertIn("ajax one", data["comment"])
        self.assertIn("ajax two", data["comment"])
        self.assertEqual(TransientFollowup.objects.count(), 1)
        self.assertEqual(TransientFollowupRequest.objects.count(), 2)

    def test_child_requestor_sees_private_parent(self):
        attach_synthetic_photometry(self.user, self.transient, n_points=2)
        parent, _, _ = self._create(self.user, comment="private")
        parent.is_public = False
        parent.save(update_fields=["is_public"])
        self._create(self.user_b, priority=3.0, comment="joining")
        parent.refresh_from_db()
        self.assertTrue(followup_visible_to_user(self.user_b, parent))
        qs = filter_transient_followups_for_user(
            TransientFollowup.objects.filter(transient=self.transient),
            self.user_b,
        )
        self.assertEqual(qs.count(), 1)

    def test_automated_spectrum_style_default_priority_creates_child(self):
        parent, child, created = create_or_attach_request(
            self.user,
            self.transient,
            status=self.requested,
            valid_start=self.now,
            valid_stop=self.stop,
            priority=DEFAULT_PRIORITY,
        )
        self.assertTrue(created)
        self.assertEqual(child.priority, 4.0)
        self.assertEqual(parent.priority, 4.0)
