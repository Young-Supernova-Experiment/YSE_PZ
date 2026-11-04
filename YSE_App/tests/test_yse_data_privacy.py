"""YSE data stays with YSE: ``Public`` next to a team group, and unlabelled YSE data."""

from django.contrib.auth.models import Group
from django.test import TestCase

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.data import PhotometryService, SpectraService
from YSE_App.services.visibility import (
    YSE_COLLABORATION_GROUP_NAME,
    YSE_OBSERVATION_GROUP_NAME,
    user_can_view_transient,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


class YseDataPrivacyTests(TestCase):
    def setUp(self):
        self.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        self.yse, _ = Group.objects.get_or_create(name=YSE_COLLABORATION_GROUP_NAME)
        self.other_team, _ = Group.objects.get_or_create(name="privacy-other-team")

        self.member = create_test_user("privacy_yse_member", is_staff=False)
        self.member.groups.add(self.yse, self.public)
        self.outsider = create_test_user("privacy_public_only", is_staff=False)
        self.outsider.groups.add(self.public)
        self.other = create_test_user("privacy_other_team", is_staff=False)
        self.other.groups.add(self.other_team, self.public)

        self.admin = create_test_user("privacy_admin", is_staff=True)
        self.transient = create_minimal_transient(self.admin, name="privacy01")
        self.yse_stack = create_instrument_stack(self.admin, obs_group_name=YSE_OBSERVATION_GROUP_NAME)
        self.other_stack = create_instrument_stack(self.admin, obs_group_name="privacy-other-survey")

    def _phot(self, stack, *groups):
        obs_group, instrument, band = stack
        phot = attach_synthetic_photometry(
            self.admin, self.transient, n_points=2, obs_group=obs_group, instrument=instrument, band=band)
        phot.groups.add(*groups)
        return phot

    def _spec(self, stack, *groups):
        obs_group, instrument, _band = stack
        spec = attach_synthetic_spectrum(self.admin, self.transient, obs_group=obs_group, instrument=instrument)
        spec.groups.add(*groups)
        return spec

    def _sees_phot(self, user, phot):
        sets = PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, self.transient.id)
        points = PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(user, self.transient.id)
        visible = sets.filter(pk=phot.pk).exists()
        self.assertEqual(visible, points.filter(photometry=phot).exists())
        self.assertEqual(visible, PhotometryService.GetAuthorizedTransientPhotometry_ByUser(user).filter(
            pk=phot.pk).exists())
        return visible

    def _sees_spec(self, user, spec):
        visible = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(
            user, self.transient.id).filter(pk=spec.pk).exists()
        self.assertEqual(visible, SpectraService.GetAuthorizedTransientSpectrum_ByUser(user).filter(
            pk=spec.pk).exists())
        return visible

    def test_public_next_to_yse_does_not_open_the_set(self):
        phot = self._phot(self.yse_stack, self.yse, self.public)
        spec = self._spec(self.other_stack, self.yse, self.public)
        self.assertTrue(self._sees_phot(self.member, phot))
        self.assertTrue(self._sees_spec(self.member, spec))
        for user in (self.outsider, self.other):
            self.assertFalse(self._sees_phot(user, phot))
            self.assertFalse(self._sees_spec(user, spec))
        self.assertFalse(user_can_view_transient(self.outsider, self.transient.id))

    def test_unlabelled_yse_data_is_yse_only(self):
        phot = self._phot(self.yse_stack)
        spec = self._spec(self.yse_stack)
        self.assertTrue(self._sees_phot(self.member, phot))
        self.assertTrue(self._sees_spec(self.member, spec))
        self.assertFalse(self._sees_phot(self.outsider, phot))
        self.assertFalse(self._sees_spec(self.outsider, spec))

    def test_public_only_yse_obs_group_is_yse_only(self):
        phot = self._phot(self.yse_stack, self.public)
        self.assertTrue(self._sees_phot(self.member, phot))
        self.assertFalse(self._sees_phot(self.outsider, phot))

    def test_open_data_unchanged(self):
        ungrouped = self._phot(self.other_stack)
        public_only = self._phot(self.other_stack, self.public)
        for user in (self.member, self.outsider, self.other):
            self.assertTrue(self._sees_phot(user, ungrouped))
            self.assertTrue(self._sees_phot(user, public_only))

    def test_other_team_keeps_its_data(self):
        phot = self._phot(self.other_stack, self.other_team, self.public)
        self.assertTrue(self._sees_phot(self.other, phot))
        self.assertFalse(self._sees_phot(self.member, phot))
        self.assertFalse(self._sees_phot(self.outsider, phot))

    def test_yse_obs_group_set_shared_with_a_team(self):
        phot = self._phot(self.yse_stack, self.yse, self.other_team)
        self.assertTrue(self._sees_phot(self.other, phot))
        self.assertFalse(self._sees_phot(self.outsider, phot))


class HiddenPhotometryTests(YseDataPrivacyTests):
    def test_hidden_when_a_yse_set_is_hidden(self):
        from YSE_App.services.visibility import transients_with_hidden_photometry

        self._phot(self.other_stack)
        self.assertEqual(transients_with_hidden_photometry(self.outsider, [self.transient.id]), set())
        self._phot(self.yse_stack, self.yse, self.public)
        self.assertEqual(transients_with_hidden_photometry(self.outsider, [self.transient.id]), {self.transient.id})
        self.assertEqual(transients_with_hidden_photometry(self.member, [self.transient.id]), set())

    def test_unlabelled_yse_set_is_hidden_from_outsiders(self):
        from YSE_App.services.visibility import transients_with_hidden_photometry

        self._phot(self.yse_stack)
        self.assertEqual(transients_with_hidden_photometry(self.outsider, [self.transient.id]), {self.transient.id})
        self.assertEqual(transients_with_hidden_photometry(self.member, [self.transient.id]), set())
