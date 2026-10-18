"""Staff see only the data they have access to (#706).

A staff member (and a superuser) outside a collaboration group sees exactly
what a non-staff outsider sees: central rule, listing, PhotStat, plots cache
token, REST API, exports. Only the configured service readers (cron and script
accounts) see everything. Administrative rights are unchanged. Synthetic data.
"""

import datetime

from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.data import PhotometryService, SpectraService
from YSE_App.models import Transient, TransientPhotData, TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing, photstat, photstat_visibility, visibility
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

SERVICE = override_settings(DATA_SERVICE_READER_USERNAMES=["stv706_service"])


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("stv706_admin", is_staff=True)
        cls.staff = create_test_user("stv706_staff", is_staff=True)
        cls.superuser = create_test_user("stv706_super", is_staff=True, is_superuser=True)
        cls.outsider = create_test_user("stv706_outsider", is_staff=False)
        cls.member = create_test_user("stv706_member", is_staff=False)
        cls.service = create_test_user("stv706_service", is_staff=False)
        cls.g, _ = Group.objects.get_or_create(name="stv706-private")
        cls.member.groups.add(cls.g)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.admin, obs_group_name="stv706")
        now = timezone.now()
        cls.private_t = create_minimal_transient(cls.admin, name="YSEstv706priv", ra=46.0, dec=-9.0)
        cls.mixed_t = create_minimal_transient(cls.admin, name="YSEstv706mix", ra=47.0, dec=-9.0)
        cls.sets = {}
        for key, transient, groups in (("private", cls.private_t, (cls.g,)), ("mixed_public", cls.mixed_t, ()),
                                       ("mixed_private", cls.mixed_t, (cls.g,))):
            phot = TransientPhotometry.objects.create(transient=transient, instrument=cls.instrument,
                                                      obs_group=cls.obs_group, **audit_fields(cls.admin))
            spec = TransientSpectrum.objects.create(
                transient=transient, instrument=cls.instrument, obs_group=cls.obs_group, ra=1.0, dec=1.0,
                obs_date=now - datetime.timedelta(days=1), **audit_fields(cls.admin))
            if groups:
                phot.groups.add(*groups)
                spec.groups.add(*groups)
            for days, mag in ((1, 18.0), (3, 19.0)):
                TransientPhotData.objects.create(photometry=phot, band=cls.band, mag=mag + (0 if groups else 1),
                                                 mag_err=0.05, obs_date=now - datetime.timedelta(days=days),
                                                 **audit_fields(cls.admin))
            cls.sets[key] = (phot, spec)
        photstat.recompute_many([cls.private_t.pk, cls.mixed_t.pk])

    def snapshot(self, user):
        tids = [self.private_t.pk, self.mixed_t.pk]
        points_q = visibility.visible_photometry_ids_q(user)
        stats = photstat_visibility.visible_stats(user, tids)
        return {
            "sets": set(TransientPhotometry.objects.filter(visibility.photometry_visible_q(user))
                        .values_list("pk", flat=True)),
            "spectra": set(TransientSpectrum.objects.filter(visibility.spectrum_visible_q(user))
                           .values_list("pk", flat=True)),
            "points": set((TransientPhotData.objects.filter(points_q) if points_q is not None
                           else TransientPhotData.objects.all()).values_list("pk", flat=True)),
            "service_sets": set(PhotometryService.GetAuthorizedTransientPhotometry_ByUser(user)
                                .values_list("pk", flat=True)),
            "service_spectra": set(SpectraService.GetAuthorizedTransientSpectrum_ByUser(user)
                                   .values_list("pk", flat=True)),
            "access": set(visibility.filter_transients_by_user_access(user, Transient.objects.filter(pk__in=tids))
                          .values_list("pk", flat=True)),
            "listed": set(visibility.filter_listed_transients(user, Transient.objects.filter(pk__in=tids))
                          .values_list("pk", flat=True)),
            "hidden": visibility.transients_with_hidden_photometry(user, tids),
            "peak": {tid: getattr(row, "peak_mag", None) for tid, row in stats.items()},
            "can_view": {tid: visibility.user_can_view_transient(user, tid) for tid in tids},
            "objects": {p.pk: visibility.object_visible_to_user(user, p) for p, _s in self.sets.values()},
        }


class StaffLikeOutsiderTests(_Fixture):
    def test_central_functions_listing_and_photstat(self):
        outsider = self.snapshot(self.outsider)
        for user in (self.staff, self.superuser):
            with self.subTest(user=user.username):
                self.assertEqual(self.snapshot(user), outsider)
        # sanity: the outsider sees the public half only, with public-only derived values
        self.assertEqual(outsider["sets"], {self.sets["mixed_public"][0].pk})
        self.assertEqual(outsider["hidden"], {self.private_t.pk, self.mixed_t.pk})
        self.assertEqual(outsider["peak"][self.mixed_t.pk], 19.0)
        self.assertFalse(visibility.user_sees_everything(self.superuser))

    def test_plot_cache_tokens_follow_groups(self):
        self.assertEqual(visibility.group_access_plot_cache_token(self.staff),
                         visibility.group_access_plot_cache_token(self.outsider))

    def test_rest_api(self):
        def rows(user, name):
            client = Client()
            client.force_login(user)
            resp = client.get("/api/%s/?limit=1000" % name)
            self.assertEqual(resp.status_code, 200, resp.content[:200])
            body = resp.json()
            items = body["results"] if isinstance(body, dict) else body
            return sorted(item.get("id") or item.get("url") for item in items)

        for name in ("transientphotometry", "transientspectra", "transientphotdata"):
            with self.subTest(endpoint=name):
                self.assertEqual(rows(self.staff, name), rows(self.outsider, name))

    def test_photometry_export(self):
        def export(user):
            client = Client()
            client.force_login(user)
            resp = client.get(reverse("download_photometry", kwargs={"slug": self.mixed_t.slug}))
            self.assertEqual(resp.status_code, 200)
            return b"".join(resp.streaming_content) if resp.streaming else resp.content

        staff_export = export(self.staff)
        self.assertEqual(staff_export, export(self.outsider).replace(b"stv706_outsider", b"stv706_staff"))

    def test_staff_keep_administrative_rights(self):
        phot = self.sets["private"][0]
        self.assertEqual(data_sharing.share_role(self.staff, phot), data_sharing.ROLE_STAFF)
        client = Client()
        client.force_login(self.staff)
        self.assertEqual(client.get(reverse("admin:YSE_App_transientphotometry_changelist")).status_code, 200)


@SERVICE
class ServiceReaderTests(_Fixture):
    def test_service_reader_sees_everything(self):
        self.assertTrue(visibility.user_sees_everything(self.service))
        snap = self.snapshot(self.service)
        self.assertEqual(snap["access"], {self.private_t.pk, self.mixed_t.pk})
        self.assertEqual(snap["hidden"], set())
        self.assertEqual(snap["peak"][self.mixed_t.pk], 18.0)
        self.assertEqual(visibility.group_access_plot_cache_token(self.service), "service")

    def test_inactive_or_unlisted_accounts_are_not_service_readers(self):
        self.assertFalse(visibility.user_sees_everything(self.staff))
        self.service.is_active = False
        self.assertFalse(visibility.user_sees_everything(self.service))
