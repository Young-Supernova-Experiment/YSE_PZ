"""manage.py yse_privacy_audit: reports and labels YSE sets; read-only without --apply."""

from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.services.visibility import YSE_COLLABORATION_GROUP_NAME, YSE_OBSERVATION_GROUP_NAME
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


class YsePrivacyAuditTests(TestCase):
    def setUp(self):
        self.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        self.yse, _ = Group.objects.get_or_create(name=YSE_COLLABORATION_GROUP_NAME)
        self.admin = create_test_user("audit_admin", is_staff=True)
        self.outsider = create_test_user("audit_public_only", is_staff=False)
        self.outsider.groups.add(self.public)
        self.transient = create_minimal_transient(self.admin, name="audit01")
        yse_stack = create_instrument_stack(self.admin, obs_group_name=YSE_OBSERVATION_GROUP_NAME)
        other_stack = create_instrument_stack(self.admin, obs_group_name="audit-ps2")
        self.yse_public = self._phot(yse_stack, self.yse, self.public)
        self.yse_bare = self._phot(yse_stack)
        self.yse_ok = self._phot(yse_stack, self.yse)
        self.other_open = self._phot(other_stack)

    def _phot(self, stack, *groups):
        obs_group, instrument, band = stack
        phot = attach_synthetic_photometry(
            self.admin, self.transient, n_points=1, obs_group=obs_group, instrument=instrument, band=band)
        phot.groups.add(*groups)
        return phot

    def _run(self, *args):
        out = StringIO()
        call_command("yse_privacy_audit", *args, stdout=out)
        return out.getvalue()

    def _names(self, phot):
        return {g.name for g in phot.groups.all()}

    def test_read_only_reports_and_changes_nothing(self):
        out = self._run("--transient", "audit01", "--user", self.outsider.username)
        self.assertIn("YSE sets not labelled YSE-only", out)
        self.assertIn(": 2", out.split("YSE sets not labelled YSE-only")[1].splitlines()[0])
        self.assertIn("Read-only", out)
        self.assertEqual(self._names(self.yse_public), {YSE_COLLABORATION_GROUP_NAME, PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertEqual(self._names(self.yse_bare), set())

    def test_apply_labels_yse_sets_only(self):
        self._run("--apply")
        for phot in (self.yse_public, self.yse_bare, self.yse_ok):
            self.assertEqual(self._names(phot), {YSE_COLLABORATION_GROUP_NAME})
        self.assertEqual(self._names(self.other_open), set())

    def test_extra_obs_group_is_labelled_when_named(self):
        self._run("--apply", "--obs-group", "audit-ps2")
        self.assertEqual(self._names(self.other_open), {YSE_COLLABORATION_GROUP_NAME})
