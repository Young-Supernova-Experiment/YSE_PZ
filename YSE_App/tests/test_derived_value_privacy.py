"""Values derived from all of a transient's photometry never reach a viewer who cannot see all of it.

A synthetic transient has an open photometry set (older, fainter point) and a
YSE-only set (newer, brighter point). A ``Public``-only user must get the open
values or blanks, never the YSE ones, from the table cells and sorts, the
photometry-statistics and image APIs, the stored summary and the target list;
a YSE member gets the YSE values.
"""

import datetime

from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import (
    ClassicalNightType,
    ClassicalObservingDate,
    ClassicalResource,
    FollowupStatus,
    Transient,
    TransientImage,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.services import photstat
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.services.visibility import YSE_COLLABORATION_GROUP_NAME, YSE_OBSERVATION_GROUP_NAME
from YSE_App.table_utils import TransientTable
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

OPEN_MAG = 19.11
YSE_MAG = 16.37
OTHER_MAG = 17.50
SUMMARY = "Synthetic summary written from every photometry set."


def _rows(data):
    return data["results"] if isinstance(data, dict) and "results" in data else data


class DerivedValuePrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        yse, _ = Group.objects.get_or_create(name=YSE_COLLABORATION_GROUP_NAME)
        cls.member = create_test_user("derived_yse_member", is_staff=False)
        cls.member.groups.add(yse, public)
        cls.outsider = create_test_user("derived_public_only", is_staff=False)
        cls.outsider.groups.add(public)
        cls.admin = create_test_user("derived_admin", is_staff=True)
        cls.audit = audit_fields(cls.admin)

        cls.open_stack = create_instrument_stack(cls.admin, obs_group_name="derived-open-survey")
        cls.yse_stack = create_instrument_stack(cls.admin, obs_group_name=YSE_OBSERVATION_GROUP_NAME)
        now = timezone.now()

        cls.transient = create_minimal_transient(cls.admin, name="2026drv", ra=30.0, dec=5.0)
        open_phot = cls._phot(cls.transient, cls.open_stack)
        cls.open_point = cls._point(open_phot, cls.open_stack, now - datetime.timedelta(days=10), OPEN_MAG)
        yse_phot = cls._phot(cls.transient, cls.yse_stack)
        yse_phot.groups.add(yse)
        cls.yse_point = cls._point(yse_phot, cls.yse_stack, now - datetime.timedelta(days=1), YSE_MAG)
        Transient.objects.filter(pk=cls.transient.pk).update(summary=SUMMARY, summary_modified=now)

        cls.other = create_minimal_transient(cls.admin, name="2026drw", ra=31.0, dec=5.0)
        cls._point(cls._phot(cls.other, cls.open_stack), cls.open_stack, now - datetime.timedelta(days=2), OTHER_MAG)

        for t in (cls.transient, cls.other):
            photstat.recompute(t.pk)

    @classmethod
    def _phot(cls, transient, stack):
        obs_group, instrument, _band = stack
        return TransientPhotometry.objects.create(
            transient=transient, instrument=instrument, obs_group=obs_group, **cls.audit)

    @classmethod
    def _point(cls, phot, stack, obs_date, mag):
        return TransientPhotData.objects.create(
            photometry=phot, band=stack[2], obs_date=obs_date, mag=mag, mag_err=0.05, **cls.audit)

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    # --- tables -------------------------------------------------------------
    def _table(self, user, **kwargs):
        qs = Transient.objects.filter(pk__in=[self.transient.pk, self.other.pk])
        return TransientTable(qs, viewer=user, **kwargs)

    def test_table_cells_and_sort(self):
        cells = {row.record.pk: row for row in self._table(self.outsider).rows}[self.transient.pk]
        self.assertEqual(cells.get_cell("recent_mag"), "%.2f" % OPEN_MAG)
        self.assertNotIn("%.2f" % YSE_MAG, cells.get_cell("peak_mag"))
        self.assertNotIn(SUMMARY, cells.get_cell("name_string"))
        order = [row.record.pk for row in self._table(self.outsider, order_by="recent_mag").rows]
        self.assertEqual(order, [self.other.pk, self.transient.pk])

        cells = {row.record.pk: row for row in self._table(self.member).rows}[self.transient.pk]
        self.assertEqual(cells.get_cell("recent_mag"), "%.2f" % YSE_MAG)
        self.assertEqual(cells.get_cell("peak_mag"), "%.2f" % YSE_MAG)
        self.assertIn(SUMMARY, cells.get_cell("name_string"))
        order = [row.record.pk for row in self._table(self.member, order_by="recent_mag").rows]
        self.assertEqual(order, [self.transient.pk, self.other.pk])

    # --- APIs ---------------------------------------------------------------
    def test_photstats_api(self):
        def rows(user, query=""):
            response = self._client(user).get("/api/transientphotstats/" + query)
            self.assertEqual(response.status_code, 200)
            return _rows(response.json())

        self.assertNotIn("%.2f" % YSE_MAG, self._client(self.outsider).get("/api/transientphotstats/").content.decode())
        self.assertEqual(len(rows(self.outsider)), 1)  # the open-only transient
        self.assertEqual(len(rows(self.outsider, "?peak_mag_lte=17")), 0)
        self.assertEqual(len(rows(self.member)), 2)
        self.assertEqual(len(rows(self.member, "?peak_mag_lte=17")), 1)

    def test_transients_api_filter_and_summary(self):
        def names(user, query):
            response = self._client(user).get("/api/transients/?" + query)
            self.assertEqual(response.status_code, 200)
            return [row["name"] for row in _rows(response.json())]

        self.assertNotIn(self.transient.name, names(self.outsider, "peak_mag_lte=17"))
        self.assertIn(self.transient.name, names(self.member, "peak_mag_lte=17"))

        url = "/api/transients/%d/" % self.transient.pk
        self.assertFalse(self._client(self.outsider).get(url).json().get("summary"))
        self.assertEqual(self._client(self.member).get(url).json().get("summary"), SUMMARY)
        listed = self._client(self.outsider).get("/api/transients/?name=2026drv").content.decode()
        self.assertNotIn(SUMMARY, listed)

    def test_transientimages_api(self):
        TransientImage.objects.create(phot_data=self.open_point, img_file="derived_open_img.fits", **self.audit)
        TransientImage.objects.create(phot_data=self.yse_point, img_file="derived_yse_img.fits", **self.audit)
        outsider = self._client(self.outsider).get("/api/transientimages/").content.decode()
        self.assertIn("derived_open_img.fits", outsider)
        self.assertNotIn("derived_yse_img.fits", outsider)
        member = self._client(self.member).get("/api/transientimages/").content.decode()
        self.assertIn("derived_yse_img.fits", member)

    # --- target list ----------------------------------------------------------
    def test_target_list_mags(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        obs_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        _obs_group, instrument, _band = self.open_stack
        observatory = instrument.telescope.observatory
        observatory.utc_offset = -10
        observatory.save()
        night_type, _ = ClassicalNightType.objects.get_or_create(name="Full", defaults=self.audit)
        resource = ClassicalResource.objects.create(
            telescope=instrument.telescope,
            begin_date_valid=obs_date - datetime.timedelta(days=3),
            end_date_valid=obs_date + datetime.timedelta(days=3),
            **self.audit,
        )
        ClassicalObservingDate.objects.create(resource=resource, night_type=night_type, obs_date=obs_date, **self.audit)
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=self.audit)
        for user in (self.outsider, self.member):
            create_or_attach_request(
                user, self.transient, status=status,
                valid_start=obs_date - datetime.timedelta(days=4),
                valid_stop=obs_date + datetime.timedelta(days=4),
                comment="derived", classical_resource=resource,
            )
        url = reverse("download_target_list", kwargs={
            "telescope": instrument.telescope.name.replace(" ", "_"),
            "obs_date": obs_date.strftime("%Y-%m-%d"),
        })

        def line(user):
            with iers_offline():
                response = self._client(user).get(url)
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            return next(row for row in body.splitlines() if row.startswith(self.transient.name))

        outsider = line(self.outsider)
        self.assertIn("mag = %.2f " % OPEN_MAG, outsider)
        self.assertNotIn("%.2f" % YSE_MAG, outsider)
        self.assertIn("mag = %.2f " % YSE_MAG, line(self.member))
