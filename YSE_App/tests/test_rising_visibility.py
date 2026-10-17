"""Rising lists, magnitude filters and search matches use visible photometry (#692, #687).

For a non-staff user outside a group, adding that group's photometry changes
no rising-list membership or order, API output, planning/field page result,
magnitude filter or search match. Group members and staff see the effect.
Synthetic data only.
"""

import base64
import datetime

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import (
    ObservationGroup,
    PhotometricBand,
    SurveyField,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientTag,
)
from YSE_App.queries.yse_python_queries import (
    annotate_recent_mag,
    recent_rising_transient_queryset,
    rising_transient_queryset,
)
from YSE_App.table_utils import RisingTransientFilter, TransientFilter
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

GROUP = "rise692-private"


def _point(phot, band, days_ago, mag, user, discovery=False):
    return TransientPhotData.objects.create(
        photometry=phot, band=band,
        obs_date=timezone.now() - datetime.timedelta(days=days_ago),
        mag=mag, mag_err=0.05, discovery_point=discovery, **audit_fields(user),
    )


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("rise692_staff")
        cls.member = create_test_user("rise692_member", is_staff=False)
        cls.outsider = create_test_user("rise692_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, _ = create_instrument_stack(cls.staff, obs_group_name="rise692")
        cls.band, _ = PhotometricBand.objects.get_or_create(
            name="r", defaults={"instrument": cls.instrument, "disp_color": "#ff0000",
                                "disp_symbol": "circle", **audit_fields(cls.staff)})
        # TNS-style names: always listed, so only the photometry rules are tested.
        cls.a = create_minimal_transient(cls.staff, name="2026risa", ra=30.5, dec=10.5)
        cls.b = create_minimal_transient(cls.staff, name="2026risb", ra=30.6, dec=10.4)
        tag = TransientTag.objects.create(name="YSE", **audit_fields(cls.staff))
        for t in (cls.a, cls.b):
            t.tags.add(tag)
            Transient.objects.filter(pk=t.pk).update(disc_date=timezone.now() - datetime.timedelta(days=3))
        # A: public points fade (not rising).  B: public points rise.
        pa = cls._phot(cls.a)
        _point(pa, cls.band, 10, 19.0, cls.staff, discovery=True)
        _point(pa, cls.band, 5, 19.1, cls.staff)
        pb = cls._phot(cls.b)
        _point(pb, cls.band, 10, 19.0, cls.staff, discovery=True)
        _point(pb, cls.band, 2, 18.0, cls.staff)

    @classmethod
    def _phot(cls, transient, *groups):
        phot = TransientPhotometry.objects.create(
            transient=transient, instrument=cls.instrument, obs_group=cls.obs_group,
            **audit_fields(cls.staff))
        if groups:
            phot.groups.add(*groups)
        return phot

    def setUp(self):
        cache.clear()

    def add_private_points(self):
        """A brightens sharply (now rising); B fades after its last public point (no longer rising)."""
        _point(self._phot(self.a, self.group), self.band, 1, 16.37, self.staff, discovery=True)
        _point(self._phot(self.b, self.group), self.band, 1, 20.0, self.staff)

    def rising(self, user, ndays=14):
        return list(rising_transient_queryset(ndays=ndays, user=user).values_list("name", flat=True))


class RisingListTests(_Fixture):
    def test_baseline_public_data(self):
        for user in (self.outsider, self.member, self.staff):
            self.assertEqual(self.rising(user), ["2026risb"])

    def test_outsider_membership_unchanged_member_and_staff_see_effect(self):
        before = self.rising(self.outsider)
        self.add_private_points()
        self.assertEqual(self.rising(self.outsider), before)
        self.assertEqual(self.rising(self.member), ["2026risa"])
        self.assertEqual(self.rising(self.staff), ["2026risa"])

    def test_every_rising_selection_is_scoped(self):
        self.add_private_points()
        self.assertEqual(
            list(recent_rising_transient_queryset(ndays=5, user=self.outsider).values_list("name", flat=True)),
            ["2026risb"])
        self.assertEqual(
            list(recent_rising_transient_queryset(ndays=5, user=self.member).values_list("name", flat=True)),
            ["2026risa"])

    def test_no_user_keeps_all_data_sql(self):
        self.add_private_points()
        self.assertEqual(
            list(rising_transient_queryset(ndays=14).values_list("name", flat=True)), ["2026risa"])


@override_settings(ONBOARDING_GATE_ENABLED=False)
class RisingApiTests(_Fixture):
    def names(self, user):
        client = Client()
        client.force_login(user)
        resp = client.get("/get_rising_transients/14/")
        self.assertEqual(resp.status_code, 200)
        return sorted(k for k in resp.json() if k != "transient")

    def test_api_membership_follows_visible_photometry(self):
        before = self.names(self.outsider)
        self.add_private_points()
        self.assertEqual(self.names(self.outsider), before)
        self.assertEqual(self.names(self.member), ["2026risa"])
        self.assertEqual(self.names(self.staff), ["2026risa"])

    def test_api_applies_the_listing_rule(self):
        hidden = create_minimal_transient(self.staff, name="risehidden692", ra=30.7, dec=10.6)
        _point(self._phot(hidden, self.group), self.band, 5, 19.0, self.staff)
        _point(self._phot(hidden, self.group), self.band, 1, 17.0, self.staff)
        self.assertIn("risehidden692", self.names(self.member))
        self.assertNotIn("risehidden692", self.names(self.outsider))

    def test_box_endpoint_applies_the_listing_rule(self):
        hidden = create_minimal_transient(self.staff, name="risehidden692", ra=30.7, dec=10.6)
        _point(self._phot(hidden, self.group), self.band, 2, 19.0, self.staff)
        _point(self._phot(hidden, self.group), self.band, 1, 17.0, self.staff)
        Transient.objects.filter(pk=hidden.pk).update(disc_date=timezone.now())
        url = "/get_rising_transients_box/30.7/10.6/2.0/2.0/"
        for user, expect in ((self.member, True), (self.outsider, False)):
            client = Client()
            client.force_login(user)
            resp = client.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual("risehidden692" in {t["transient_name"] for t in resp.json()["transients"]}, expect)

    def test_basic_auth_still_works(self):
        User.objects.create_user("rise692_basic", password="pw-for-test")
        cred = base64.b64encode(b"rise692_basic:pw-for-test").decode()
        resp = Client().get("/get_rising_transients/14/", HTTP_AUTHORIZATION="Basic " + cred)
        self.assertEqual(resp.status_code, 200)

    def test_bad_ndays_is_a_400(self):
        client = Client()
        client.force_login(self.staff)
        self.assertEqual(client.get("/get_rising_transients/abc/").status_code, 400)


class FilterAndSearchTests(_Fixture):
    def request_for(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def recent_mag_lt(self, user, limit):
        request = self.request_for(user)
        qs = annotate_recent_mag(Transient.objects.filter(pk__in=[self.a.pk, self.b.pk]), user)
        filt = RisingTransientFilter({"recent_mag_lt": limit}, queryset=qs, request=request)
        return sorted(filt.qs.values_list("name", flat=True))

    def search(self, filter_cls, user, text):
        filt = filter_cls({"ex": text}, queryset=Transient.objects.filter(pk__in=[self.a.pk, self.b.pk]),
                          request=self.request_for(user))
        return sorted(filt.qs.values_list("name", flat=True))

    def test_max_recent_mag_filter(self):
        before = self.recent_mag_lt(self.outsider, 18.5)
        self.add_private_points()
        self.assertEqual(self.recent_mag_lt(self.outsider, 18.5), before)
        self.assertEqual(self.recent_mag_lt(self.outsider, 17.5), [])
        # member's scope sees A at 16.37; B's last detection there is 20.0
        self.assertEqual(self.recent_mag_lt(self.member, 17.5), ["2026risa"])
        self.assertEqual(self.recent_mag_lt(self.staff, 17.5), ["2026risa"])

    def test_search_disc_mag_match(self):
        self.add_private_points()
        for filter_cls in (TransientFilter, RisingTransientFilter):
            self.assertEqual(self.search(filter_cls, self.outsider, "16.37"), [])
            self.assertEqual(self.search(filter_cls, self.member, "16.37"), ["2026risa"])
            self.assertEqual(self.search(filter_cls, self.staff, "16.37"), ["2026risa"])

    def test_search_recent_mag_match(self):
        self.add_private_points()
        self.assertEqual(self.search(RisingTransientFilter, self.outsider, "16.37"), [])
        self.assertEqual(self.search(RisingTransientFilter, self.member, "16.37"), ["2026risa"])

    def test_filter_without_request_is_public_only(self):
        self.add_private_points()
        filt = TransientFilter({"ex": "16.37"}, queryset=Transient.objects.filter(pk=self.a.pk))
        self.assertEqual(list(filt.qs), [])


class YseFieldsPageTests(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        yse = ObservationGroup.objects.get_or_create(name="YSE", defaults=audit_fields(cls.staff))[0]
        for fid, ra in (("500.1", 30.0), ("500.2", 33.0)):
            SurveyField.objects.create(
                obs_group=yse, field_id=fid, instrument=cls.instrument, ztf_field_id="500",
                ra_cen=ra, dec_cen=10.0, width_deg=3.3, height_deg=3.3, **audit_fields(cls.staff))

    def names(self, user, limit="18.5"):
        client = Client()
        client.force_login(user)
        with override_settings(ONBOARDING_GATE_ENABLED=False):
            resp = client.get(reverse("yse_fields", args=["1", "3", limit]))
        self.assertEqual(resp.status_code, 200)
        tables = resp.context["survey_tables"]
        return sorted(r.record.name for t in tables for r in t[0].paginated_rows)

    def test_magnitude_threshold_uses_visible_photometry(self):
        self.assertEqual(self.names(self.outsider, "17.5"), [])
        self.add_private_points()
        self.assertEqual(self.names(self.outsider, "17.5"), [])  # probe of the hidden 16.37 point
        self.assertEqual(self.names(self.member, "17.5"), ["2026risa"])
        self.assertEqual(self.names(self.staff, "17.5"), ["2026risa"])
        self.assertEqual(self.names(self.outsider, "18.5"), ["2026risb"])
