"""Comments may go to any of the user's groups, not only those on the transient's data (2026-10-22)."""

from django.contrib.auth.models import Group
from django.test import TestCase

from YSE_App.forms import TransientCommentForm
from YSE_App.models import UserPreference
from YSE_App.services import groups as group_svc
from YSE_App.services.audience import resolve_comment_audience
from YSE_App.tests.fixtures_minimal import attach_synthetic_photometry, create_minimal_transient, create_test_user


class CommentAudienceAnyGroupTests(TestCase):
    def setUp(self):
        self.user = create_test_user("comment_any_group_user")
        self.on_data = Group.objects.create(name="cag-on-data")
        self.other = Group.objects.create(name="cag-other")
        self.hidden = Group.objects.create(name="cag-hidden")
        self.user.groups.add(self.on_data, self.other, self.hidden)
        self.transient = create_minimal_transient(self.user, name="2026cag")
        phot = attach_synthetic_photometry(self.user, self.transient, n_points=2)
        phot.groups.add(self.on_data)
        pref, _ = UserPreference.objects.get_or_create(user=self.user)
        pref.hidden_groups.add(self.hidden)
        group_svc.forget_preferences(self.user)

    def _offered(self):
        form = TransientCommentForm(user=self.user, transient_id=self.transient.id)
        return {label for _value, label in form.fields["audience_groups"].choices}

    def test_picker_offers_groups_without_data_on_the_transient(self):
        offered = self._offered()
        self.assertIn("cag-on-data", offered)
        self.assertIn("cag-other", offered)
        self.assertNotIn("cag-hidden", offered)

    def test_only_the_default_audience_starts_checked(self):
        form = TransientCommentForm(user=self.user, transient_id=self.transient.id)
        self.assertEqual(list(form.fields["audience_groups"].initial), [self.on_data.pk])

    def test_server_accepts_a_group_without_data_on_the_transient(self):
        is_public, groups = resolve_comment_audience(
            self.user, self.transient.id, is_public=False, audience_group_ids=[self.other.pk])
        self.assertFalse(is_public)
        self.assertEqual(groups, [self.other])
