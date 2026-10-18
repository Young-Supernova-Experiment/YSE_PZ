"""Access-rule pages and API (#648, PR 5): program, object-night and dataset actions, deputies, staff removal.

Synthetic data only. Everything is 404 with ``DATA_ACCESS_GRANTS_ENABLED`` off.
"""

import datetime

from django.contrib.auth.models import Group
from django.template import Context, Template
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import (
    Allocation,
    AllocationDelegate,
    DataAccessRule,
    PrincipalInvestigator,
    StandingAccessGrant,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.services import access_pages
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
        cls.staff = create_test_user("aui648_staff", is_staff=True)
        cls.pi_user = create_test_user("aui648_pi", is_staff=False)
        cls.deputy = create_test_user("aui648_deputy", is_staff=False)
        cls.outsider = create_test_user("aui648_outsider", is_staff=False)
        cls.g, _ = Group.objects.get_or_create(name="aui648-g")
        cls.o, _ = Group.objects.get_or_create(name="aui648-o")
        cls.pi_user.groups.add(cls.g)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="aui648")
        now = timezone.now()
        cls.now = now
        audit = audit_fields(cls.staff)
        cls.pi = PrincipalInvestigator.objects.create(
            name="aui648 PI", phone="0", email="aui648@example.com", institution="Synthetic", user=cls.pi_user, **audit)
        cls.alloc = Allocation.objects.create(
            name="aui648 A", proposal_id="AUI-A", telescope=cls.instrument.telescope, instrument=cls.instrument,
            principal_investigator=cls.pi, start_date=now - datetime.timedelta(days=30),
            end_date=now + datetime.timedelta(days=30), **audit)
        AllocationDelegate.objects.create(allocation=cls.alloc, user=cls.deputy, added_by=cls.staff)
        cls.transient = create_minimal_transient(cls.staff, name="YSEaui648", ra=44.0, dec=-8.0)
        cls.sets = []
        for band_offset in range(2):  # two sets (filters / instruments alike): one night action covers both
            phot = TransientPhotometry.objects.create(
                transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, allocation=cls.alloc,
                **audit)
            phot.groups.add(cls.g)
            phot.pis.add(cls.pi)
            for days in (1, 5):
                TransientPhotData.objects.create(photometry=phot, band=cls.band, mag=19.0 + band_offset, mag_err=0.1,
                                                 obs_date=now - datetime.timedelta(days=days), **audit)
            cls.sets.append(phot)
        cls.spec = TransientSpectrum.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, ra=1.0, dec=1.0,
            obs_date=now - datetime.timedelta(days=1), allocation=cls.alloc, **audit)
        cls.spec.groups.add(cls.g)
        cls.spec.pis.add(cls.pi)

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client


class FlagOffTests(_Fixture):
    def test_everything_is_hidden(self):
        client = self.client_for(self.staff)
        for url in (reverse("access_programs"), reverse("access_program", args=[self.alloc.pk]),
                    reverse("access_transient_rules", args=[self.transient.pk]),
                    reverse("api-access-program-grants", args=[self.alloc.pk]),
                    reverse("api-access-transient-nights", args=[self.transient.pk]),
                    reverse("api-access-dataset-rules", args=["photometry", self.sets[0].pk])):
            self.assertEqual(client.get(url).status_code, 404, url)
        html = Template("{% load access_rules_tags %}{% access_rules_enabled as on %}{{ on }}").render(Context())
        self.assertEqual(html, "False")


@ON
class ProgramPageTests(_Fixture):
    def test_list(self):
        self.assertContains(self.client_for(self.pi_user).get(reverse("access_programs")), "aui648 A")
        self.assertContains(self.client_for(self.deputy).get(reverse("access_programs")), "aui648 A")
        self.assertNotContains(self.client_for(self.outsider).get(reverse("access_programs")), "aui648 A")

    def test_access_and_actions(self):
        url = reverse("access_program", args=[self.alloc.pk])
        self.assertEqual(self.client_for(self.outsider).get(url).status_code, 403)
        client = self.client_for(self.deputy)
        self.assertEqual(client.get(url).status_code, 200)
        resp = client.post(url, {"action": "grant", "effect": "allow", "grantee_kind": "group", "group": self.o.pk,
                                 "scope": "program"}, follow=True)
        self.assertContains(resp, "may now see 5 new items")  # 4 points and 1 spectrum
        grant = StandingAccessGrant.objects.get(allocation=self.alloc)
        self.assertEqual(grant.created_as, "program_deputy")
        # a restriction for the group already on the data is written (a PI may restrict shared data)
        resp = client.post(url, {"action": "grant", "effect": "deny", "grantee_kind": "group", "group": self.g.pk,
                                 "scope": "program"}, follow=True)
        self.assertContains(resp, "may not see 5 new items")
        resp = client.post(url, {"action": "stop", "grant_id": grant.pk}, follow=True)
        self.assertContains(resp, "going forward")
        grant.refresh_from_db()
        self.assertIsNotNone(grant.stopped_at)
        self.assertTrue(R.objects.filter(standing_grant=grant).exists())  # forward: earlier data keep it
        # deputies cannot manage deputies; the PI can
        resp = client.post(url, {"action": "add_deputy", "username": self.outsider.username}, follow=True)
        self.assertContains(resp, "Not changed")
        pi_client = self.client_for(self.pi_user)
        pi_client.post(url, {"action": "add_deputy", "username": self.outsider.username})
        row = AllocationDelegate.objects.get(allocation=self.alloc, user=self.outsider)
        pi_client.post(url, {"action": "remove_deputy", "deputy_id": row.pk})
        self.assertFalse(AllocationDelegate.objects.filter(pk=row.pk).exists())

    def test_night_scope(self):
        url = reverse("access_program", args=[self.alloc.pk])
        night = (self.now - datetime.timedelta(days=1)).date()
        self.client_for(self.pi_user).post(url, {
            "action": "grant", "effect": "allow", "grantee_kind": "user", "username": self.outsider.username,
            "scope": "night", "night": (night - datetime.timedelta(days=1)).isoformat(), "utc_offset": "0"})
        grant = StandingAccessGrant.objects.get(allocation=self.alloc)
        self.assertIsNotNone(grant.night_start)
        self.assertEqual(grant.night_end - grant.night_start, datetime.timedelta(days=1))


@ON
class TransientPageTests(_Fixture):
    def setUp(self):
        self.url = reverse("access_transient_rules", args=[self.transient.pk])

    def test_who_sees_what(self):
        resp = self.client_for(self.outsider).get(self.url)
        self.assertContains(resp, "You may not set access")
        resp = self.client_for(self.pi_user).get(self.url)
        self.assertContains(resp, "yse-access-nights")

    def test_night_action_covers_every_set_of_that_night(self):
        nights = access_pages.object_nights(self.transient, self.sets, [self.spec])
        last = next(n for n in nights if n["spectra"] == 1)  # yesterday: one point per set and the spectrum
        resp = self.client_for(self.pi_user).post(self.url, {
            "action": "night", "night_start": last["start"].isoformat(), "night_end": last["end"].isoformat(),
            "effect": "allow", "grantee_kind": "user", "username": self.outsider.username}, follow=True)
        self.assertContains(resp, "may now see 3 new items")
        covered = set(R.objects.filter(grantee_user=self.outsider).values_list("photometry_id", flat=True))
        self.assertEqual(covered, {self.sets[0].pk, self.sets[1].pk, None})  # both sets and the spectrum

    def test_dataset_action_and_turning_it_off(self):
        pi = self.client_for(self.pi_user)
        pi.post(self.url, {"action": "dataset", "kind": "photometry", "dataset_id": self.sets[0].pk,
                           "effect": "allow", "grantee_kind": "group", "group": self.o.pk})
        self.assertEqual(R.objects.filter(photometry=self.sets[0], grantee_group=self.o).count(), 2)
        off = {"action": "remove_allow", "kind": "photometry", "dataset_id": self.sets[0].pk,
               "grantee_group_id": self.o.pk}
        resp = pi.post(self.url, off, follow=True)  # forward by default: earlier data stay visible
        self.assertContains(resp, "going forward")
        self.assertEqual(R.objects.filter(photometry=self.sets[0], grantee_group=self.o).count(), 2)
        resp = self.client_for(self.outsider).post(self.url, dict(off, direction="backward"), follow=True)
        self.assertContains(resp, "Not changed")
        resp = pi.post(self.url, dict(off, direction="backward"), follow=True)
        self.assertContains(resp, "removed backwards from 2 items")
        self.assertFalse(R.objects.filter(photometry=self.sets[0]).exists())
        pi.post(self.url, {"action": "dataset", "kind": "spectrum", "dataset_id": self.spec.pk,
                           "effect": "deny", "grantee_kind": "user", "username": self.outsider.username})
        resp = pi.post(self.url, {"action": "lift", "kind": "spectrum", "dataset_id": self.spec.pk,
                                  "grantee_username": self.outsider.username}, follow=True)
        self.assertContains(resp, "Restriction lifted")
        self.assertFalse(R.objects.filter(transient_spectrum=self.spec).exists())

    def test_link_on_the_photometry_tab(self):
        html = Template("{% load access_rules_tags %}{% access_rules_enabled as on %}{{ on }}").render(Context())
        self.assertEqual(html, "True")


@ON
class ApiTests(_Fixture):
    def test_program_grants_and_stop(self):
        url = reverse("api-access-program-grants", args=[self.alloc.pk])
        self.assertEqual(self.client_for(self.outsider).get(url).status_code, 403)
        client = self.client_for(self.pi_user)
        resp = client.post(url, {"effect": "allow", "user": self.outsider.username}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["written"]["created"], 5)
        grant_id = body["grant"]["id"]
        self.assertEqual(len(client.get(url).json()["grants"]), 1)
        resp = client.post(reverse("api-access-grant-stop", args=[grant_id]))
        self.assertIsNotNone(resp.json()["stopped_at"])
        resp = client.post(url, {"effect": "maybe", "user": self.outsider.username}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_deputies(self):
        url = reverse("api-access-program-deputies", args=[self.alloc.pk])
        resp = self.client_for(self.deputy).post(url, {"user": self.outsider.username}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        resp = self.client_for(self.pi_user).post(url, {"user": self.outsider.username},
                                                  content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        deputy_id = resp.json()["id"]
        self.assertEqual(len(self.client_for(self.pi_user).get(url).json()["deputies"]), 2)
        resp = self.client_for(self.pi_user).post(
            reverse("api-access-program-deputy-remove", args=[self.alloc.pk, deputy_id]))
        self.assertEqual(resp.status_code, 200)

    def test_dataset_rules(self):
        url = reverse("api-access-dataset-rules", args=["spectrum", self.spec.pk])
        self.assertEqual(self.client_for(self.outsider).get(url).status_code, 404)  # cannot see it
        client = self.client_for(self.pi_user)
        resp = client.post(url, {"effect": "deny", "group": self.g.name}, content_type="application/json")
        self.assertEqual(resp.json()["written"]["created"], 1)  # shared data may be restricted
        resp = client.post(url, {"effect": "allow", "user": self.outsider.username}, content_type="application/json")
        self.assertEqual(resp.json()["written"]["created"], 1)
        remove = reverse("api-access-dataset-rules-remove", args=["spectrum", self.spec.pk])
        # the outsider now sees the spectrum (the allow) but may not change its access
        self.assertEqual(self.client_for(self.outsider).post(remove, {"user": self.outsider.username},
                                                             content_type="application/json").status_code, 403)
        resp = client.post(remove, {"user": self.outsider.username}, content_type="application/json")
        self.assertEqual(resp.json()["removed"], 0)  # forward: earlier data stay visible
        resp = client.post(remove, {"user": self.outsider.username, "backwards": True},
                           content_type="application/json")
        self.assertEqual(resp.json()["removed"], 1)
        resp = client.post(remove, {"group": self.g.name, "effect": "deny"}, content_type="application/json")
        self.assertEqual(resp.json()["removed"], 1)

    def test_nights(self):
        url = reverse("api-access-transient-nights", args=[self.transient.pk])
        client = self.client_for(self.pi_user)
        nights = client.get(url).json()["nights"]
        self.assertEqual(sum(n["points"] for n in nights), 4)
        target = next(n for n in nights if n["spectra"] == 1)
        resp = client.post(url, {"night_start": target["night_start"], "night_end": target["night_end"],
                                 "effect": "allow", "group": self.o.pk}, content_type="application/json")
        self.assertEqual(resp.json()["written"]["created"], 3)
        self.assertEqual(self.client_for(self.outsider).get(url).json()["nights"], [])


class NightHelperTests(TestCase):
    def test_windows(self):
        day = datetime.date(2026, 10, 1)
        start, end = access_pages.night_window(day, -10)  # Hawaii: local noon is 22:00 UT
        self.assertEqual(start, datetime.datetime(2026, 10, 1, 22, tzinfo=datetime.timezone.utc))
        self.assertEqual(end - start, datetime.timedelta(days=1))
        obs = datetime.datetime(2026, 10, 2, 8, tzinfo=datetime.timezone.utc)  # 22:00 local on Oct 1
        self.assertEqual(access_pages.night_of(obs, -10), day)
        self.assertTrue(start <= obs < end)
