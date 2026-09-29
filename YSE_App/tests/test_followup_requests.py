"""Observing-request parent/child schema (issues #135–#138)."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App.forms import TransientFollowupForm
from YSE_App.models import (
    ClassicalResource,
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
    audit_fields,
    create_instrument_stack,
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
        # The HTTP form requires a linked observing resource. An ungrouped
        # resource is visible to every user; creator_only permits an empty
        # audience, so the form posts below need no audience_groups.
        _obs_group, instrument, _band = create_instrument_stack(
            self.user, obs_group_name="fureq-resource"
        )
        self.resource = ClassicalResource.objects.create(
            telescope=instrument.telescope,
            begin_date_valid=self.now - timedelta(days=1),
            end_date_valid=self.stop,
            creator_only=True,
            **audit_fields(self.user),
        )

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
                "classical_resource": self.resource.id,
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
            "classical_resource": self.resource.id,
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


class SharedClassicalRequestTests(TestCase):
    """Several people request the same object for the same classical night.

    Each keeps their own comment and priority under one parent follow-up;
    deleting a request never touches someone else's.
    """

    def setUp(self):
        from YSE_App.models import ClassicalObservingDate
        from YSE_App.tests.deploy_checklist_helpers import (
            create_telescope,
            ensure_classical_night_type,
        )

        self.user_a = create_test_user("shared_req_a", is_staff=False)
        self.user_b = create_test_user("shared_req_b", is_staff=False)
        self.staff = create_test_user("shared_req_staff", is_staff=True)
        self.transient = create_minimal_transient(self.user_a, name="sharedreq01")
        # Non-staff users only see follow-ups of transients they have data for.
        attach_synthetic_photometry(self.user_a, self.transient, n_points=2)
        self.requested, _ = FollowupStatus.objects.get_or_create(
            name="Requested",
            defaults={"created_by": self.user_a, "modified_by": self.user_a},
        )
        now = timezone.now()
        self.telescope = create_telescope(self.user_a, "SharedReqTel")
        self.resource = ClassicalResource.objects.create(
            telescope=self.telescope,
            begin_date_valid=now - timedelta(days=1),
            end_date_valid=now + timedelta(days=6),
            creator_only=True,
            **audit_fields(self.user_a),
        )
        self.night = ClassicalObservingDate.objects.create(
            resource=self.resource,
            night_type=ensure_classical_night_type(self.user_a),
            obs_date=(now + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0),
            **audit_fields(self.user_a),
        )

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _post_request(self, user, *, priority, comment):
        response = self._client(user).post(
            reverse("add_transient_followup"),
            {
                "status": self.requested.id,
                "classical_resource": self.resource.id,
                "priority": priority,
                "comment": comment,
                "transient": self.transient.id,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def _service_request(self, user, *, priority, comment):
        parent, child, _ = create_or_attach_request(
            user,
            self.transient,
            status=self.requested,
            valid_start=self.resource.begin_date_valid,
            valid_stop=self.resource.end_date_valid,
            priority=priority,
            comment=comment,
            classical_resource=self.resource,
        )
        return parent, child

    def _night_url(self):
        return reverse(
            "observing_night",
            kwargs={
                "telescope": self.telescope.name.replace(" ", "_"),
                "obs_date": self.night.obs_date.strftime("%Y-%m-%d"),
                "pi_name": "None",
            },
        )

    def test_two_users_same_night_keep_their_own_comments(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        first = self._post_request(self.user_a, priority=3.0, comment="a")
        second = self._post_request(self.user_b, priority=2.0, comment="b")
        self.assertFalse(first["attached"])
        self.assertTrue(second["attached"])
        self.assertEqual(first["id"], second["id"])

        self.assertEqual(TransientFollowup.objects.filter(transient=self.transient).count(), 1)
        parent = TransientFollowup.objects.get(transient=self.transient)
        children = {r.requestor.username: r for r in parent.requests.all()}
        self.assertEqual(set(children), {"shared_req_a", "shared_req_b"})
        self.assertEqual(children["shared_req_a"].comment, "a")
        self.assertEqual(children["shared_req_b"].comment, "b")
        self.assertEqual(children["shared_req_a"].priority, 3.0)
        self.assertEqual(children["shared_req_b"].priority, 2.0)
        self.assertEqual(parent.priority, 2.0)

        # Follow-up tab (as B): both requests listed, delete only on B's own.
        fragment = self._client(self.user_b).get(
            reverse("transient_detail_followup_fragment", kwargs={"transient_id": self.transient.id})
        )
        self.assertEqual(fragment.status_code, 200)
        html = fragment.content.decode()
        self.assertIn("shared_req_a", html)
        self.assertIn("shared_req_b", html)
        self.assertIn(">a<", html)
        self.assertIn(">b<", html)
        self.assertIn(
            reverse("delete_followup_request", kwargs={"request_id": children["shared_req_b"].id}), html
        )
        self.assertNotIn(
            reverse("delete_followup_request", kwargs={"request_id": children["shared_req_a"].id}), html
        )
        self.assertNotIn(reverse("delete_followup", kwargs={"followup_id": parent.id}), html)

        # Observing night page and target list show every requester and comment.
        with iers_offline():
            night_page = self._client(self.user_a).get(self._night_url())
        self.assertEqual(night_page.status_code, 200)
        self.assertContains(night_page, self.transient.name)
        self.assertContains(night_page, "shared_req_a")
        self.assertContains(night_page, "shared_req_b")
        self.assertContains(night_page, "shared_req_a: a")
        self.assertContains(night_page, "shared_req_b: b")

        with iers_offline():
            target_list = self._client(self.user_a).get(
                reverse(
                    "download_target_list",
                    kwargs={
                        "telescope": self.telescope.name.replace(" ", "_"),
                        "obs_date": self.night.obs_date.strftime("%Y-%m-%d"),
                    },
                )
            )
        self.assertEqual(target_list.status_code, 200)
        text = target_list.content.decode()
        self.assertIn("shared_req_a: a", text)
        self.assertIn("shared_req_b: b", text)

    def test_delete_own_request_leaves_others(self):
        parent, child_a = self._service_request(self.user_a, priority=3.0, comment="a")
        _, child_b = self._service_request(self.user_b, priority=2.0, comment="b")
        client_b = self._client(self.user_b)

        response = client_b.get(reverse("delete_followup_request", kwargs={"request_id": child_a.id}))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(parent.requests.count(), 2)

        response = client_b.get(reverse("delete_followup_request", kwargs={"request_id": child_b.id}))
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.transient.slug, response.url)
        self.assertTrue(TransientFollowup.objects.filter(pk=parent.id).exists())
        self.assertEqual(list(parent.requests.values_list("id", flat=True)), [child_a.id])
        parent.refresh_from_db()
        self.assertEqual(parent.priority, 3.0)
        self.assertEqual(TransientFollowupRequest.objects.get(pk=child_a.id).comment, "a")

        # Last request gone: parent goes with it.
        response = self._client(self.user_a).get(
            reverse("delete_followup_request", kwargs={"request_id": child_a.id})
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TransientFollowup.objects.filter(pk=parent.id).exists())

    def test_parent_delete_by_non_staff_only_withdraws_own_requests(self):
        parent, child_a = self._service_request(self.user_a, priority=3.0, comment="a")
        _, child_b = self._service_request(self.user_b, priority=2.0, comment="b")

        response = self._client(self.user_b).get(
            reverse("delete_followup", kwargs={"followup_id": parent.id})
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TransientFollowup.objects.filter(pk=parent.id).exists())
        self.assertEqual(list(parent.requests.values_list("id", flat=True)), [child_a.id])
        parent.refresh_from_db()
        self.assertEqual(parent.priority, 3.0)

        # Staff may still remove the whole follow-up.
        response = self._client(self.staff).get(
            reverse("delete_followup", kwargs={"followup_id": parent.id})
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TransientFollowup.objects.filter(pk=parent.id).exists())
