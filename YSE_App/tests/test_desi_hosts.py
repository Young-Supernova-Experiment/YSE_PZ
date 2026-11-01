"""DESI host matching for host-less transients and the DESI button (#738).

Fake clients only: no network. Synthetic names and positions.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App.models import Host
from YSE_App.models.change_log_models import ValueChange
from YSE_App.models.job_models import Job
from YSE_App.services import desi, desi_hosts, enrichment
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

SERVICE_USERNAME = "desi_hostless_cron"


def _m(targetid, sep, z, ra=10.001, dec=1.0, err=0.0001):
    return desi.DESIMatch(str(targetid), ra, dec, z, err, 120.0, sep)


class FakeDESI:
    def __init__(self, matches=(), error=None):
        self.matches, self.error, self.calls = list(matches), error, []

    def redshifts_near(self, ra, dec, radius_arcsec):
        self.calls.append((ra, dec, radius_arcsec))
        if self.error:
            raise desi.DESIError(self.error)
        return sorted(self.matches, key=lambda m: m.sep_arcsec)


class SelectHostTests(SimpleTestCase):
    def test_smallest_separation_times_z_wins(self):
        near_high_z = _m(1, 2.0, 0.5)   # 1.0
        farther_low_z = _m(2, 5.0, 0.03)  # 0.15
        self.assertEqual(desi_hosts.select_host([near_high_z, farther_low_z]).targetid, "2")

    def test_beyond_100_kpc_is_no_host(self):
        self.assertIsNone(desi_hosts.select_host([_m(3, 600.0, 0.01)]))

    def test_empty(self):
        self.assertIsNone(desi_hosts.select_host([]))

    def test_same_rule_as_ned(self):
        from YSE_App.services import ned

        matches = [_m(1, 2.0, 0.5), _m(2, 5.0, 0.03), _m(3, 600.0, 0.01)]
        chosen = ned.select_host(desi_hosts._as_ned_objects(matches))
        self.assertEqual(chosen.name, "DESI %s" % desi_hosts.select_host(matches).targetid)


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], REDSHIFT_CATALOG_CRON_ENABLED=False)
class MatchTransientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)

    def setUp(self):
        self.t = create_minimal_transient(self.user, name="2026dhl", ra=10.0, dec=1.0)

    def test_links_host_and_sets_host_redshift_only(self):
        client = FakeDESI([_m(7, 5.0, 0.03), _m(8, 2.0, 0.5)])
        outcome = desi_hosts.match_transient(self.t, user=self.user, client=client)
        self.assertEqual(outcome.action, enrichment.UPDATED)
        self.t.refresh_from_db()
        host = self.t.host
        self.assertEqual((host.name, host.redshift, host.redshift_source), ("DESI 7", 0.03, desi.source_label()))
        self.assertIsNone(self.t.redshift)  # never from a nearby galaxy
        self.assertAlmostEqual(client.calls[0][2], 300.0)
        kinds = set(ValueChange.objects.filter(transient=self.t).values_list("field", flat=True))
        self.assertIn("host", kinds)
        self.assertTrue(ValueChange.objects.filter(host=host, field="redshift").exists())

    def test_dry_run_changes_nothing(self):
        outcome = desi_hosts.match_transient(self.t, dry_run=True, client=FakeDESI([_m(7, 5.0, 0.03)]))
        self.assertEqual(outcome.action, enrichment.UPDATED)
        self.t.refresh_from_db()
        self.assertIsNone(self.t.host_id)
        self.assertFalse(Host.objects.filter(name="DESI 7").exists())

    def test_existing_higher_ranked_redshift_is_kept(self):
        Host.objects.create(name="DESI 7", ra=10.001, dec=1.0, redshift=0.0301, redshift_source="NED",
                            created_by=self.user, modified_by=self.user)
        desi_hosts.match_transient(self.t, user=self.user, client=FakeDESI([_m(7, 5.0, 0.03)]))
        self.t.refresh_from_db()
        self.assertEqual((self.t.host.redshift, self.t.host.redshift_source), (0.0301, "NED"))

    def test_no_match_error_and_skips(self):
        self.assertEqual(desi_hosts.match_transient(self.t, client=FakeDESI([_m(3, 600.0, 0.01)])).action,
                         enrichment.NO_MATCH)
        self.assertEqual(desi_hosts.match_transient(self.t, client=FakeDESI(error="down")).action,
                         enrichment.ERROR)
        self.t.host = Host.objects.create(name="h", ra=1, dec=1, created_by=self.user, modified_by=self.user)
        self.t.save()
        fake = FakeDESI([_m(7, 5.0, 0.03)])
        self.assertEqual(desi_hosts.match_transient(self.t, client=fake).action, enrichment.SKIPPED)
        self.assertEqual(fake.calls, [])

    def test_registered_and_needs_selection(self):
        service = enrichment.get("desi-hostless")
        self.assertTrue(service.needs_selection)
        self.assertIn(self.t.pk, [t.pk for t in service.candidates(True)])
        summary = enrichment.run(service, [self.t], dry_run=False, user=self.user,
                                 ctx={"desi_client": FakeDESI([_m(7, 5.0, 0.03)]), "user": self.user})
        self.assertEqual(summary.counts, {enrichment.UPDATED: 1})
        self.t.refresh_from_db()
        self.assertEqual(self.t.host.name, "DESI 7")


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], REDSHIFT_CATALOG_CRON_ENABLED=False)
class DesiButtonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)

    def setUp(self):
        cache.clear()
        self.t = create_minimal_transient(self.user, name="2026dbt", ra=10.0, dec=1.0)
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("desi_lookup", kwargs={"slug": self.t.slug})

    def test_login_required(self):
        response = Client().post(self.url)
        self.assertIn(response.status_code, (302, 401, 403))
        self.assertFalse(Job.objects.filter(kind="enrich.objects").exists())

    def test_hostless_queues_host_match(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        job = Job.objects.get(kind="enrich.objects")
        self.assertEqual(job.payload, {"service": "desi-hostless", "ids": [self.t.pk]})
        self.assertEqual(job.transient_id, self.t.pk)

    def test_with_host_queues_host_redshift_check(self):
        self.t.host = Host.objects.create(name="h", ra=10.0, dec=1.0, created_by=self.user, modified_by=self.user)
        self.t.save()
        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(Job.objects.get(kind="enrich.objects").payload,
                         {"service": "desi", "ids": [self.t.host_id]})

    def test_rate_limited_per_object_and_per_user(self):
        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 429)
        self.assertEqual(Job.objects.filter(kind="enrich.objects").count(), 1)
        cache.clear()
        with override_settings(DESI_BUTTON_MAX_PER_HOUR=1):
            self.assertEqual(self.client.post(self.url).status_code, 200)
            other = create_minimal_transient(self.user, name="2026dbu", ra=11.0, dec=1.0)
            r = self.client.post(reverse("desi_lookup", kwargs={"slug": other.slug}))
            self.assertEqual(r.status_code, 429)

    def test_status_shows_result_after_the_job_ran(self):
        self.client.post(self.url)
        enrichment.run(enrichment.get("desi-hostless"), [self.t], dry_run=False, user=self.user,
                       ctx={"desi_client": FakeDESI([_m(7, 5.0, 0.03)]), "user": self.user})
        body = self.client.get(self.url).json()
        self.assertIn("linked host DESI 7", body["result"])
