"""Program links of datasets (#648, PR 3): derived at ingest, named by uploads, manual, backfill.

Synthetic data only. The access flag stays off: linking writes the program's
standing-grant rules (data for later), it changes no read.
"""

import datetime
import json

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from io import StringIO
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.data_utils import add_transient_phot_util
from YSE_App.models import (
    Allocation,
    AllocationDelegate,
    ClassicalObservingDate,
    ClassicalResource,
    DataAccessRule,
    FollowupStatus,
    ObservedBlock,
    PrincipalInvestigator,
    StandingAccessGrant,
    TransientFollowup,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.models.enum_models import ClassicalNightType
from YSE_App.services import access_actions, program_links
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

R = DataAccessRule


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("apl648_staff", is_staff=True)
        cls.prog_pi = create_test_user("apl648_progpi", is_staff=False)
        cls.deputy = create_test_user("apl648_deputy", is_staff=False)
        cls.data_pi = create_test_user("apl648_datapi", is_staff=False)
        cls.outsider = create_test_user("apl648_outsider", is_staff=False)
        cls.g, _ = Group.objects.get_or_create(name="apl648-g")
        cls.data_pi.groups.add(cls.g)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="apl648")
        now = timezone.now()
        cls.now = now
        audit = audit_fields(cls.staff)
        cls.prog_pi_row = PrincipalInvestigator.objects.create(
            name="apl648 program PI", phone="0", email="apl648-p@example.com", institution="Synthetic",
            user=cls.prog_pi, **audit)
        cls.data_pi_row = PrincipalInvestigator.objects.create(
            name="apl648 data PI", phone="0", email="apl648-d@example.com", institution="Synthetic",
            user=cls.data_pi, **audit)

        def allocation(name, proposal):
            return Allocation.objects.create(
                name=name, proposal_id=proposal, telescope=cls.instrument.telescope, instrument=cls.instrument,
                principal_investigator=cls.prog_pi_row, start_date=now - datetime.timedelta(days=30),
                end_date=now + datetime.timedelta(days=30), **audit)

        cls.alloc = allocation("apl648 A", "APL-A")
        cls.other_alloc = allocation("apl648 B", "APL-B")
        AllocationDelegate.objects.create(allocation=cls.alloc, user=cls.deputy, added_by=cls.staff)
        cls.resource = ClassicalResource.objects.create(
            telescope=cls.instrument.telescope, begin_date_valid=now - datetime.timedelta(days=5),
            end_date_valid=now + datetime.timedelta(days=30), allocation=cls.alloc, **audit)
        cls.status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)
        cls.transient = create_minimal_transient(cls.staff, name="YSEapl648", ra=43.0, dec=-7.0)
        cls.followup = TransientFollowup.objects.create(
            transient=cls.transient, status=cls.status, valid_start=now, valid_stop=now + datetime.timedelta(days=3),
            classical_resource=cls.resource, **audit)
        night_type, _ = ClassicalNightType.objects.get_or_create(name="Full", defaults=audit)
        cls.night = ClassicalObservingDate.objects.create(
            resource=cls.resource, night_type=night_type, obs_date=now - datetime.timedelta(hours=12), **audit)

    def phot(self, *, followup=None, groups=(), allocation=None, points=2):
        phot = TransientPhotometry.objects.create(
            transient=self.transient, instrument=self.instrument, obs_group=self.obs_group, followup=followup,
            allocation=allocation, **audit_fields(self.staff))
        if groups:
            phot.groups.add(*groups)
        phot.pis.add(self.data_pi_row)
        for i in range(points):
            TransientPhotData.objects.create(photometry=phot, band=self.band, mag=19.0, mag_err=0.1,
                                             obs_date=self.now - datetime.timedelta(days=i + 1),
                                             **audit_fields(self.staff))
        return phot

    def spec(self, *, followup=None, obs_date=None, groups=()):
        spec = TransientSpectrum.objects.create(
            transient=self.transient, instrument=self.instrument, obs_group=self.obs_group, followup=followup,
            ra=1.0, dec=1.0, obs_date=obs_date or self.now - datetime.timedelta(days=1), **audit_fields(self.staff))
        if groups:
            spec.groups.add(*groups)
        return spec


class DeriveAtIngestTests(_Fixture):
    def test_followup_resource_allocation(self):
        phot = self.phot(followup=self.followup)
        phot.refresh_from_db()
        self.assertEqual(phot.allocation_id, self.alloc.pk)
        spec = self.spec(followup=self.followup)
        spec.refresh_from_db()
        self.assertEqual(spec.allocation_id, self.alloc.pk)

    def test_no_followup_no_link_and_no_query(self):
        with CaptureQueriesContext(connection) as ctx:
            TransientPhotometry.objects.create(transient=self.transient, instrument=self.instrument,
                                               obs_group=self.obs_group, **audit_fields(self.staff))
        sql = " ".join(q["sql"].lower() for q in ctx.captured_queries)
        self.assertNotIn("yse_app_allocation", sql)
        self.assertNotIn("observedblock", sql)

    def test_ambiguous_followup_is_left_unlinked(self):
        from YSE_App.models import FacilityRequest

        FacilityRequest.objects.create(transient=self.transient, followup=self.followup, allocation=self.other_alloc,
                                       submitted_by=self.staff, **audit_fields(self.staff))
        phot = self.phot(followup=self.followup)
        phot.refresh_from_db()
        self.assertIsNone(phot.allocation_id)
        self.assertEqual(program_links.derived_allocation_ids(phot), {self.alloc.pk, self.other_alloc.pk})

    @override_settings(ASTROSCHEDULER_ENABLED=True)
    def test_observed_block_night(self):
        ObservedBlock.objects.create(
            night=self.night, transient=self.transient, kind="science", target_name=self.transient.name,
            outcome="observed", recorded_by=self.staff, source="admin", **audit_fields(self.staff))
        spec = self.spec(obs_date=self.night.obs_date + datetime.timedelta(hours=3))
        spec.refresh_from_db()
        self.assertEqual(spec.allocation_id, self.alloc.pk)

    def test_observed_block_ignored_with_the_planner_off(self):
        ObservedBlock.objects.create(
            night=self.night, transient=self.transient, kind="science", target_name=self.transient.name,
            outcome="observed", recorded_by=self.staff, source="admin", **audit_fields(self.staff))
        spec = self.spec(obs_date=self.night.obs_date + datetime.timedelta(hours=3))
        spec.refresh_from_db()
        self.assertIsNone(spec.allocation_id)

    def test_standing_grant_applies_to_a_dataset_linked_at_ingest(self):
        grant, _ = access_actions.program_action(self.prog_pi, self.alloc, effect="allow", grantee_user=self.outsider)
        phot = self.phot(followup=self.followup, groups=(self.g,))
        self.assertEqual(R.objects.filter(photometry=phot, grantee_user=self.outsider, standing_grant=grant).count(), 2)


class LinkTests(_Fixture):
    def test_who_may_link_an_existing_dataset(self):
        phot = self.phot(groups=(self.g,))
        for user in (self.outsider, self.prog_pi, self.deputy):  # the program side alone may not claim it
            with self.subTest(user=user.username):
                with self.assertRaises(PermissionDenied):
                    program_links.link_dataset(user, phot, self.alloc)
        result = program_links.link_dataset(self.data_pi, phot, self.alloc)
        self.assertTrue(result.changed)
        phot.refresh_from_db()
        self.assertEqual(phot.allocation_id, self.alloc.pk)
        # once linked, the program's PI and deputies may change it (dataset_role)
        program_links.link_dataset(self.deputy, phot, None)
        phot.refresh_from_db()
        self.assertIsNone(phot.allocation_id)
        program_links.link_dataset(self.staff, phot, self.other_alloc)
        phot.refresh_from_db()
        self.assertEqual(phot.allocation_id, self.other_alloc.pk)

    def test_new_dataset_may_be_linked_by_the_program(self):
        phot = self.phot(groups=(self.g,))
        program_links.link_dataset(self.prog_pi, phot, self.alloc, new_dataset=True)
        phot.refresh_from_db()
        self.assertEqual(phot.allocation_id, self.alloc.pk)

    def test_linking_later_applies_standing_grants_and_shared_stays_shared(self):
        phot = self.phot(groups=(self.g,))
        access_actions.program_action(self.prog_pi, self.alloc, effect="allow", grantee_user=self.outsider)
        access_actions.program_action(self.prog_pi, self.alloc, effect="deny", grantee_group=self.g)
        result = program_links.link_dataset(self.data_pi, phot, self.alloc)
        self.assertEqual(R.objects.filter(photometry=phot, grantee_user=self.outsider, effect="allow").count(), 2)
        # the group is on the dataset: its points were shared with it, so no restriction is written
        self.assertFalse(R.objects.filter(photometry=phot, grantee_group=self.g).exists())
        self.assertEqual(result.grants["kept_shared"], 2)
        # unlinking removes no rule
        program_links.link_dataset(self.data_pi, phot, None)
        self.assertEqual(R.objects.filter(photometry=phot).count(), 2)

    def test_a_stopped_grant_still_covers_data_taken_before_the_stop(self):
        program, _ = access_actions.program_action(self.prog_pi, self.alloc, effect="allow",
                                                   grantee_user=self.outsider)
        start = self.now - datetime.timedelta(days=3)
        night, _ = access_actions.program_action(self.prog_pi, self.alloc, effect="allow", grantee_user=self.deputy,
                                                 night_start=start, night_end=start + datetime.timedelta(days=4))
        stop = self.now - datetime.timedelta(hours=36)  # between the two points of a set (taken 1 and 2 days ago)
        StandingAccessGrant.objects.filter(pk__in=[program.pk, night.pk]).update(stopped_at=stop)
        phot = self.phot(groups=(self.g,))
        after_stop = TransientPhotData.objects.filter(photometry=phot, obs_date__gte=stop).get()
        before_stop = TransientPhotData.objects.filter(photometry=phot, obs_date__lt=stop).get()
        program_links.link_dataset(self.data_pi, phot, self.alloc)
        for grantee in (self.outsider, self.deputy):
            covered = set(R.objects.filter(photometry=phot, grantee_user=grantee).values_list(
                "transient_phot_data_id", flat=True))
            self.assertEqual(covered, {before_stop.pk}, grantee.username)
        self.assertNotIn(after_stop.pk, set(R.objects.values_list("transient_phot_data_id", flat=True)))
        spec = self.spec(groups=(self.g,), obs_date=stop - datetime.timedelta(hours=1))
        late = self.spec(groups=(self.g,), obs_date=stop + datetime.timedelta(hours=1))
        program_links.link_dataset(self.data_pi, spec, self.alloc)
        program_links.link_dataset(self.data_pi, late, self.alloc)
        self.assertTrue(R.objects.filter(transient_spectrum=spec, grantee_user=self.outsider).exists())
        self.assertFalse(R.objects.filter(transient_spectrum=late).exists())
        self.assertTrue(program.__class__.objects.get(pk=program.pk).covers_obs_date(stop - datetime.timedelta(1)))
        self.assertFalse(program.__class__.objects.get(pk=program.pk).covers_obs_date(stop))

    def test_resolve_allocation(self):
        self.assertEqual(program_links.resolve_allocation(self.alloc.pk), self.alloc)
        self.assertEqual(program_links.resolve_allocation(str(self.alloc.pk)), self.alloc)
        self.assertEqual(program_links.resolve_allocation("APL-B"), self.other_alloc)
        self.assertEqual(program_links.resolve_allocation("apl648 A"), self.alloc)
        self.assertIsNone(program_links.resolve_allocation(""))
        with self.assertRaises(ValidationError):
            program_links.resolve_allocation("no such program")
        Allocation.objects.filter(pk=self.other_alloc.pk).update(proposal_id="APL-A")
        with self.assertRaises(ValidationError):
            program_links.resolve_allocation("APL-A")


class UploadTests(_Fixture):
    def upload(self, user, allocation=None, trusted=None):
        dataset = {"instrument": self.instrument.name, "obs_group": self.obs_group.name, "photdata": {
            "0": {"obs_date": (self.now - datetime.timedelta(days=1)).replace(tzinfo=None).isoformat(),
                  "band": self.band.name, "mag": 19.0, "mag_err": 0.1, "flux": None, "flux_err": None,
                  "flux_zero_point": None, "forced": False, "diffim": False, "data_quality": 0,
                  "discovery_point": 0}}}
        if allocation is not None:
            dataset["allocation"] = allocation
        photdict = {"clobber": False, "mjdmatchmin": 0.001, "ds": dataset}
        _r, new = add_transient_phot_util(photdict, self.transient, user, do_photdata=False, allocation=trusted)
        if new:
            TransientPhotometry.objects.bulk_create(new)
        response, _ = add_transient_phot_util(photdict, self.transient, user, do_photdata=True, allocation=trusted)
        return json.loads(response.content), TransientPhotometry.objects.get(
            transient=self.transient, instrument=self.instrument, obs_group=self.obs_group)

    def test_program_pi_names_the_allocation_of_new_data(self):
        body, phot = self.upload(self.prog_pi, allocation="APL-A")
        self.assertEqual(phot.allocation_id, self.alloc.pk)
        self.assertNotIn("program_link_skipped", body)

    def test_others_are_skipped_and_told(self):
        body, phot = self.upload(self.outsider, allocation=self.alloc.pk)
        self.assertIsNone(phot.allocation_id)
        self.assertTrue(body["program_link_skipped"])

    def test_unknown_allocation_is_reported(self):
        body, phot = self.upload(self.prog_pi, allocation="nope")
        self.assertIsNone(phot.allocation_id)
        self.assertTrue(body["program_link_skipped"])

    def test_facility_results_link_trusted(self):
        _body, phot = self.upload(self.outsider, trusted=self.alloc)
        self.assertEqual(phot.allocation_id, self.alloc.pk)

    def test_no_allocation_key_changes_nothing(self):
        body, phot = self.upload(self.outsider)
        self.assertIsNone(phot.allocation_id)
        self.assertNotIn("program_link_skipped", body)


class EndpointTests(_Fixture):
    def url(self, kind, pk):
        return reverse("api-dataset-program", kwargs={"kind": kind, "dataset_id": pk})

    def test_get_and_post(self):
        phot = self.phot(followup=None, groups=(self.g,))
        client = Client()
        client.force_login(self.data_pi)
        resp = client.get(self.url("photometry", phot.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["allocation"])
        resp = client.post(self.url("photometry", phot.pk), {"allocation": "APL-A"}, content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["allocation"], self.alloc.pk)
        resp = client.post(self.url("photometry", phot.pk), {"allocation": None}, content_type="application/json")
        self.assertIsNone(resp.json()["allocation"])
        resp = client.post(self.url("photometry", phot.pk), {"allocation": "nope"}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_refusals(self):
        phot = self.phot(groups=(self.g,))
        client = Client()
        client.force_login(self.outsider)
        self.assertEqual(client.get(self.url("photometry", phot.pk)).status_code, 404)  # cannot see it
        self.g.user_set.add(self.outsider)
        self.assertEqual(client.get(self.url("photometry", phot.pk)).status_code, 403)
        resp = client.post(self.url("photometry", phot.pk), {"allocation": self.alloc.pk},
                           content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(client.get(self.url("spectrum", 10 ** 9)).status_code, 404)
        self.assertIn(Client().get(self.url("photometry", phot.pk)).status_code, (302, 401, 403))


class BackfillTests(_Fixture):
    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_data_programs", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_then_apply(self):
        linked = self.phot(groups=(self.g,))
        TransientPhotometry.objects.filter(pk=linked.pk).update(followup=self.followup)  # as if from old data
        dated = self.spec(groups=(self.g,))
        public = self.phot()
        TransientPhotometry.objects.filter(pk=public.pk).update(followup=self.followup)
        Allocation.objects.filter(pk=self.other_alloc.pk).update(  # does not cover the spectrum's date
            start_date=self.now - datetime.timedelta(days=100), end_date=self.now - datetime.timedelta(days=50))
        out = self.run_command()
        self.assertIn("datasets to link: 2 (followup 1, observed 0, date 1)", out)
        self.assertIn("dry run", out)
        self.assertFalse(TransientPhotometry.objects.filter(allocation__isnull=False).exists())
        grant, _ = access_actions.program_action(self.prog_pi, self.alloc, effect="allow", grantee_user=self.outsider)
        out = self.run_command("--apply")
        self.assertIn("written: 2", out)
        self.assertEqual(TransientPhotometry.objects.get(pk=linked.pk).allocation_id, self.alloc.pk)
        self.assertEqual(TransientSpectrum.objects.get(pk=dated.pk).allocation_id, self.alloc.pk)
        self.assertIsNone(TransientPhotometry.objects.get(pk=public.pk).allocation_id)
        self.assertEqual(R.objects.filter(photometry=linked, standing_grant=grant).count(), 2)
        self.assertTrue(StandingAccessGrant.objects.filter(pk=grant.pk).exists())

    def test_ambiguous_dates_are_skipped(self):
        self.spec(groups=(self.g,))  # both allocations cover it on the same instrument
        out = self.run_command()
        self.assertIn("ambiguous 1", out)
