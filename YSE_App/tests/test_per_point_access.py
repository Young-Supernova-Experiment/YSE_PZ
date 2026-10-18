"""Access is per point (#714): exact visible points everywhere, and the program link on each point.

Synthetic data only.
"""

import datetime
from io import StringIO

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.data import PhotometryService
from YSE_App.data_utils import add_transient_phot_util
from YSE_App.models import (
    Allocation,
    DataAccessRule,
    PrincipalInvestigator,
    Transient,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.serializers.phot_serializers import TransientPhotDataSerializer
from YSE_App.services import access_actions, visibility
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

R = DataAccessRule
ON = override_settings(DATA_ACCESS_GRANTS_ENABLED=True)


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("ppa714_admin", is_staff=True)
        cls.pi_user = create_test_user("ppa714_pi", is_staff=False)
        cls.member = create_test_user("ppa714_member", is_staff=False)
        cls.outsider = create_test_user("ppa714_outsider", is_staff=False)
        cls.g, _ = Group.objects.get_or_create(name="ppa714-g")
        cls.member.groups.add(cls.g)
        cls.pi_user.groups.add(cls.g)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.admin, obs_group_name="ppa714")
        cls.now = timezone.now()
        audit = audit_fields(cls.admin)
        cls.pi = PrincipalInvestigator.objects.create(
            name="ppa714 PI", phone="0", email="ppa714@example.com", institution="Synthetic", user=cls.pi_user, **audit)

        def alloc(name):
            return Allocation.objects.create(
                name=name, telescope=cls.instrument.telescope, instrument=cls.instrument,
                principal_investigator=cls.pi, start_date=cls.now - datetime.timedelta(days=60),
                end_date=cls.now + datetime.timedelta(days=60), **audit)

        cls.prog_a = alloc("ppa714 A")
        cls.prog_b = alloc("ppa714 B")
        cls.transient = create_minimal_transient(cls.admin, name="YSEppa714", ra=48.0, dec=-10.0)
        cls.header = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
        cls.header.groups.add(cls.g)
        cls.header.pis.add(cls.pi)
        cls.points = [TransientPhotData.objects.create(
            photometry=cls.header, band=cls.band, mag=18.0 + i, mag_err=0.05,
            obs_date=cls.now - datetime.timedelta(days=i + 1), **audit) for i in range(3)]

    def setUp(self):
        cache.clear()

    def fresh(self, user):
        return type(user).objects.get(pk=user.pk)


@ON
class ExactPointsTests(_Fixture):
    def test_every_read_returns_exactly_the_visible_points(self):
        hidden = self.points[1]
        R.objects.create(transient_phot_data=hidden, grantee_user=self.member, effect="deny", granted_as="pi")
        member = self.fresh(self.member)
        expected = {self.points[0].pk, self.points[2].pk}
        # the header is listed (it has visible points), and its points are filtered one by one
        self.assertIn(self.header.pk, set(TransientPhotometry.objects.filter(visibility.photometry_visible_q(member))
                                          .values_list("pk", flat=True)))
        self.assertEqual(set(PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(
            member, self.transient.pk).values_list("pk", flat=True)), expected)
        self.assertEqual(set(PhotometryService.GetAuthorizedTransientPhotData_ByPhotometry(
            member, self.header.pk).values_list("pk", flat=True)), expected)
        self.assertTrue(Transient.objects.filter(visibility.hidden_photometry_exists(member),
                                                 pk=self.transient.pk).exists())
        # REST photdata list and the photometry export
        client = Client()
        client.force_login(self.member)
        body = client.get("/api/transientphotdata/?limit=1000").json()
        items = body["results"] if isinstance(body, dict) else body
        urls = " ".join(str(i.get("url", "")) + str(i.get("id", "")) for i in items)
        self.assertNotIn("/%d/" % hidden.pk, urls)
        export = client.get(reverse("download_photometry", kwargs={"slug": self.transient.slug}))
        content = b"".join(export.streaming_content) if export.streaming else export.content
        self.assertEqual(content.count(b"\nOBS:"), 2)  # the two visible points only

    def test_a_header_with_no_visible_point_is_not_listed(self):
        for p in self.points:
            R.objects.create(transient_phot_data=p, grantee_user=self.member, effect="deny", granted_as="pi")
        member = self.fresh(self.member)
        self.assertNotIn(self.header.pk, set(TransientPhotometry.objects.filter(
            visibility.photometry_visible_q(member)).values_list("pk", flat=True)))


@ON
class PointProgramTests(_Fixture):
    def test_points_from_two_programs_follow_their_own_grants(self):
        TransientPhotData.objects.filter(pk=self.points[0].pk).update(allocation=self.prog_a)
        TransientPhotData.objects.filter(pk=self.points[1].pk).update(allocation=self.prog_b)
        grant, result = access_actions.program_action(self.pi_user, self.prog_a, effect="allow",
                                                      grantee_user=self.outsider)
        self.assertEqual(set(R.objects.filter(standing_grant=grant).values_list("transient_phot_data_id", flat=True)),
                         {self.points[0].pk})
        # a new point of program B in the same header does not get A's rule
        late_b = TransientPhotData.objects.create(photometry=self.header, band=self.band, mag=21.0, mag_err=0.1,
                                                  obs_date=self.now, allocation=self.prog_b,
                                                  **audit_fields(self.admin))
        late_a = TransientPhotData.objects.create(photometry=self.header, band=self.band, mag=21.0, mag_err=0.1,
                                                  obs_date=self.now, allocation=self.prog_a,
                                                  **audit_fields(self.admin))
        self.assertFalse(R.objects.filter(transient_phot_data=late_b).exists())
        self.assertTrue(R.objects.filter(transient_phot_data=late_a, standing_grant=grant).exists())

    def test_header_link_is_the_default_for_its_points(self):
        TransientPhotometry.objects.filter(pk=self.header.pk).update(allocation=self.prog_a)
        TransientPhotData.objects.filter(pk=self.points[2].pk).update(allocation=self.prog_b)
        grant, _ = access_actions.program_action(self.pi_user, self.prog_a, effect="allow", grantee_user=self.outsider)
        covered = set(R.objects.filter(standing_grant=grant).values_list("transient_phot_data_id", flat=True))
        self.assertEqual(covered, {self.points[0].pk, self.points[1].pk})  # point 2 is program B's

    def test_facility_results_link_the_points_not_a_claimed_header(self):
        TransientPhotometry.objects.filter(pk=self.header.pk).update(allocation=self.prog_a)
        photdict = {"clobber": False, "mjdmatchmin": 0.001, "fac": {
            "instrument": self.instrument.name, "obs_group": self.obs_group.name, "photdata": {
                "0": {"obs_date": (self.now - datetime.timedelta(hours=3)).replace(tzinfo=None).isoformat(),
                      "band": self.band.name, "mag": 22.0, "mag_err": 0.1, "flux": None, "flux_err": None,
                      "flux_zero_point": None, "forced": True, "diffim": True, "data_quality": 0,
                      "discovery_point": 0}}}}
        add_transient_phot_util(photdict, self.transient, self.admin, do_photdata=True, allocation=self.prog_b)
        self.header.refresh_from_db()
        self.assertEqual(self.header.allocation_id, self.prog_a.pk)  # the header default stays
        new = TransientPhotData.objects.get(photometry=self.header, mag=22.0)
        self.assertEqual(new.allocation_id, self.prog_b.pk)

    def test_migrate_point_programs(self):
        TransientPhotometry.objects.filter(pk=self.header.pk).update(allocation=self.prog_a)
        TransientPhotData.objects.filter(pk=self.points[2].pk).update(allocation=self.prog_b)
        out = StringIO()
        call_command("migrate_point_programs", stdout=out)
        self.assertIn("points to link: 2", out.getvalue())
        self.assertFalse(TransientPhotData.objects.filter(allocation=self.prog_a).exists())
        call_command("migrate_point_programs", "--apply", stdout=out)
        self.assertEqual(TransientPhotData.objects.filter(allocation=self.prog_a).count(), 2)
        self.assertEqual(TransientPhotData.objects.get(pk=self.points[2].pk).allocation_id, self.prog_b.pk)

    def test_api_does_not_expose_the_point_link(self):
        self.assertNotIn("allocation", TransientPhotDataSerializer().fields)
