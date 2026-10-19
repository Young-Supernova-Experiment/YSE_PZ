"""Updated-request and status-change notices on follow-ups (#69): right users, once, never the actor."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone

from YSE_App.models import (
    ClassicalResource,
    FollowupStatus,
    Notification,
    TransientFollowup,
    TransientFollowupRequest,
    UserTelescopeToFollow,
)
from YSE_App.models.profile_models import Profile
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


def _follow(user, telescope):
    profile, _ = Profile.objects.get_or_create(user=user)
    return UserTelescopeToFollow.objects.create(profile=profile, telescope=telescope, **audit_fields(user))


class _Base(TestCase):
    def setUp(self):
        self.admin = create_test_user("fnu_admin")  # staff: sees every follow-up
        self.public = Group.objects.get_or_create(name="Public")[0]
        self.alice = self._member("fnu_alice")  # first requester
        self.bob = self._member("fnu_bob")  # second requester
        self.carol = self._member("fnu_carol")  # follows the telescope only
        self.transient = create_minimal_transient(self.admin, name="2026fnu")
        attach_synthetic_photometry(self.admin, self.transient, n_points=3)  # ungrouped: everyone may see the transient
        _obs, instrument, _band = create_instrument_stack(self.admin, obs_group_name="fnu-stack")
        self.telescope = instrument.telescope
        now = timezone.now()
        self.now, self.stop = now, now + datetime.timedelta(days=5)
        self.resource = ClassicalResource.objects.create(
            telescope=self.telescope, begin_date_valid=now - datetime.timedelta(days=1),
            end_date_valid=self.stop, **audit_fields(self.admin))
        self.requested = self._status("Requested")
        self.in_process = self._status("InProcess")
        for user in (self.carol, self.alice):
            _follow(user, self.telescope)

    def _member(self, name):
        user = create_test_user(name, is_staff=False)
        user.groups.add(self.public)
        return user

    def _status(self, name):
        return FollowupStatus.objects.get_or_create(name=name, defaults=audit_fields(self.admin))[0]

    def _request(self, user, priority=4.0):
        return create_or_attach_request(
            user, self.transient, status=self.requested, valid_start=self.now, valid_stop=self.stop,
            priority=priority, classical_resource=self.resource)

    def _rows(self, kind, **kw):
        return Notification.objects.filter(kind=kind, **kw)

    def _recipients(self, kind):
        return sorted(self._rows(kind).values_list("recipient__username", flat=True))


class UpdatedRequestNoticeTests(_Base):
    def _seed(self):
        """Alice creates the follow-up; that creation notice goes to Carol only."""
        parent, _child, created = self._request(self.alice)
        self.assertTrue(created)
        self.assertEqual(self._recipients("followup_request"), ["fnu_carol"])
        Notification.objects.all().delete()
        return parent

    def test_first_request_is_not_an_update(self):
        self._request(self.alice)
        rows = list(self._rows("followup_request"))
        self.assertEqual([r.recipient.username for r in rows], ["fnu_carol"])
        self.assertNotIn("event", rows[0].payload)  # the "new request" notice, not an update

    def test_new_request_notifies_followers_and_earlier_requesters_not_actor(self):
        parent = self._seed()
        _follow(self.bob, self.telescope)  # the actor follows the telescope too
        self._request(self.bob, priority=2.0)
        self.assertEqual(self._recipients("followup_request"), ["fnu_alice", "fnu_carol"])
        for row in self._rows("followup_request"):
            self.assertEqual(row.payload["event"], "updated")
            self.assertEqual(row.payload["followup_id"], parent.pk)
            self.assertIn("updated", row.subject)
            self.assertNotIn("2.0", row.text)  # no priorities or comments in the notice

    def test_repeats_collapse_within_window_and_resume_after(self):
        self._seed()
        self._request(self.bob)
        self._request(self.bob, priority=1.0)
        self.assertEqual(self._rows("followup_request", recipient=self.carol).count(), 1)
        self.assertEqual(self._rows("followup_request", recipient=self.alice).count(), 1)
        self._rows("followup_request").update(created=timezone.now() - datetime.timedelta(hours=1))
        self._request(self.bob, priority=3.0)
        self.assertEqual(self._rows("followup_request", recipient=self.carol).count(), 2)

    @override_settings(FOLLOWUP_NOTICE_COLLAPSE_SECONDS=0)
    def test_window_zero_never_collapses(self):
        self._seed()
        self._request(self.bob)
        self._request(self.bob, priority=1.0)
        self.assertEqual(self._rows("followup_request", recipient=self.carol).count(), 2)

    def test_creator_adding_priority_change_notifies_others_only(self):
        self._seed()
        self._request(self.alice, priority=1.0)
        self.assertEqual(self._recipients("followup_request"), ["fnu_carol"])

    def test_does_not_reach_people_who_cannot_see_the_followup(self):
        parent = self._seed()
        private_group = Group.objects.create(name="fnu-private")
        self.alice.groups.add(private_group)
        self.bob.groups.add(private_group)
        parent.groups.set([private_group])
        _follow(self.bob, self.telescope)
        outsider = create_test_user("fnu_outsider", is_staff=False)  # no groups at all
        _follow(outsider, self.telescope)
        self._request(self.bob)
        # Carol is in Public only, the outsider in nothing: neither sees the private follow-up
        self.assertEqual(self._recipients("followup_request"), ["fnu_alice"])

    def test_notice_failure_does_not_break_the_request(self):
        from unittest import mock

        self._seed()
        with mock.patch("YSE_App.services.followup_notices.notify_request_added", side_effect=RuntimeError("x")):
            _parent, child, _ = self._request(self.bob)
        self.assertIsNotNone(child.pk)
        self.assertEqual(TransientFollowupRequest.objects.count(), 2)


class StatusNoticeTests(_Base):
    def _two_requesters(self):
        parent, _c, _ = self._request(self.alice)
        self._request(self.bob)
        Notification.objects.all().delete()
        return TransientFollowup.objects.get(pk=parent.pk)

    def _change(self, followup, status, actor):
        followup.status = status
        followup.modified_by = actor
        followup.save()

    def test_status_change_notifies_requested_by_and_all_requesters_not_actor(self):
        followup = self._two_requesters()
        self._change(followup, self.in_process, self.bob)
        rows = self._rows("followup_status")
        self.assertEqual(sorted(rows.values_list("recipient__username", flat=True)), ["fnu_alice"])
        self.assertIn("Requested -> InProcess", rows[0].text)
        self.assertEqual(rows[0].payload["followup_id"], followup.pk)

    def test_system_change_notifies_every_requester_once(self):
        followup = self._two_requesters()
        self._change(followup, self.in_process, self.admin)  # actor is an unrelated user
        self.assertEqual(self._recipients("followup_status"), ["fnu_alice", "fnu_bob"])
        followup.refresh_from_db()
        followup.save()  # unchanged status: nothing more
        self.assertEqual(self._rows("followup_status").count(), 2)

    def test_requested_by_without_a_request_row_is_notified(self):
        followup = self._two_requesters()
        carol_owned = TransientFollowup.objects.create(
            transient=self.transient, status=self.requested, valid_start=self.now, valid_stop=self.stop,
            requested_by=self.carol, classical_resource=self.resource, **audit_fields(self.carol))
        Notification.objects.all().delete()
        self._change(carol_owned, self.in_process, self.admin)
        self.assertEqual(self._recipients("followup_status"), ["fnu_carol"])
        self.assertIsNotNone(followup)

    def test_creation_and_non_status_edits_send_nothing(self):
        followup = self._two_requesters()
        followup.priority = 1.0
        followup.save()
        self.assertEqual(self._rows("followup_status").count(), 0)

    def test_hidden_followup_is_not_announced(self):
        followup = self._two_requesters()
        team = Group.objects.create(name="fnu-team")
        self.alice.groups.add(team)
        followup.groups.set([team])
        # Dave is requested_by on the follow-up but not in its audience and has no request row
        dave = self._member("fnu_dave")
        followup.requested_by = dave
        followup.save()
        Notification.objects.all().delete()
        self._change(followup, self.in_process, self.admin)
        # Bob and Alice have request rows (the listing rule shows them the follow-up); Dave cannot see it
        self.assertEqual(self._recipients("followup_status"), ["fnu_alice", "fnu_bob"])

    def test_observation_outcome_status_change_notifies_once(self):
        """AstroScheduler outcomes (#670/#672) change status through a normal save: one notice each."""
        from YSE_App.services import observations

        followup = self._two_requesters()
        changed = observations._set_status(followup, "Successful", self.admin)
        self.assertTrue(changed)
        self.assertEqual(self._recipients("followup_status"), ["fnu_alice", "fnu_bob"])
        self.assertFalse(observations._set_status(followup, "Successful", self.admin))
        self.assertEqual(self._rows("followup_status").count(), 2)
