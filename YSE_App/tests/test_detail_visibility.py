"""Transient detail page: summary values and fragment rows come only from visible photometry (#690).

A non-staff user outside a group must see no change when that group's
photometry (and its diff images) is added; a group member and staff do.
Synthetic data only.
"""

import datetime
import os
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.utils import timezone

from YSE_App.models import TransientDiffImage, TransientPhotData, TransientPhotometry
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

GROUP = "dv690-private"
PUBLIC_MARK = "dv690-public-stamp"
HIDDEN_MARK = "dv690-hidden-stamp"


class DetailVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("dv690_staff")
        cls.member = create_test_user("dv690_member", is_staff=False)
        cls.outsider = create_test_user("dv690_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.member.groups.add(cls.group)
        cls.staff_member = create_test_user("dv690_staff_member")
        cls.staff_member.groups.add(cls.group)
        obs_group, instrument, band = create_instrument_stack(cls.staff, obs_group_name="dv690")
        cls.transient = create_minimal_transient(cls.staff, name="YSEdv690", ra=30.0, dec=5.0)
        now = timezone.now()
        audit = audit_fields(cls.staff)

        def add_set(groups, mag, days_ago, mark, discovery):
            phot = TransientPhotometry.objects.create(
                transient=cls.transient, instrument=instrument, obs_group=obs_group, **audit)
            if groups:
                phot.groups.add(*groups)
            point = TransientPhotData.objects.create(
                photometry=phot, band=band, mag=mag, mag_err=0.05,
                discovery_point=discovery,
                obs_date=now - datetime.timedelta(days=days_ago), **audit)
            TransientDiffImage.objects.create(phot_data=point, postage_stamp_file=mark, **audit)
            return phot

        # Public set: older, fainter. Restricted set: newest and earliest points, brighter.
        add_set((), 20.25, 5, PUBLIC_MARK, False)
        add_set((cls.group,), 15.5, 1, HIDDEN_MARK, False)
        add_set((cls.group,), 15.75, 30, HIDDEN_MARK + "-first", True)

    def _client(self, user):
        c = Client()
        c.force_login(user)
        return c

    def _detail(self, user, defer):
        with mock.patch.dict(os.environ, {"YSE_TRANSIENT_DETAIL_DEFER": "1" if defer else "0"}):
            response = self._client(user).get("/transient_detail/%s/" % self.transient.slug)
        self.assertEqual(response.status_code, 200)
        return response

    def test_summary_magnitudes_follow_visibility(self):
        for defer in (True, False):
            with self.subTest(defer=defer):
                out = self._detail(self.outsider, defer).context
                self.assertIn("20.25", out["recent_mag"])
                self.assertIn("20.25", out["first_mag"])
                ctx = self._detail(self.member, defer).context
                self.assertIn("15.5", ctx["recent_mag"])
                self.assertIn("15.75", ctx["first_mag"])
                # Staff in the group see it too; staff outside it get what the table gives them.
                ctx = self._detail(self.staff_member, defer).context
                self.assertIn("15.5", ctx["recent_mag"])
                ctx = self._detail(self.staff, defer).context
                self.assertIn("20.25", ctx["recent_mag"])

    def test_outsider_summary_unchanged_by_group_photometry(self):
        """Same values as if the restricted sets did not exist."""
        before = {}
        for defer in (True, False):
            ctx = self._detail(self.outsider, defer).context
            before[defer] = [ctx[k] for k in ("recent_mag", "first_mag", "recent_magdate", "first_magdate")]
        TransientPhotometry.objects.filter(groups__name=GROUP).update(transient=self._other_transient())
        for defer in (True, False):
            ctx = self._detail(self.outsider, defer).context
            after = [ctx[k] for k in ("recent_mag", "first_mag", "recent_magdate", "first_magdate")]
            self.assertEqual(before[defer], after, defer)

    def _other_transient(self):
        return create_minimal_transient(self.staff, name="YSEdv690other", ra=60.0, dec=5.0)

    def test_diff_images_fragment_rows(self):
        url = "/transient_detail/%d/photometry_fragment/" % self.transient.pk
        out = self._client(self.outsider).get(url)
        self.assertEqual(out.status_code, 200)
        self.assertEqual([d.postage_stamp_file for d in out.context["diff_images"]], [PUBLIC_MARK])
        self.assertNotContains(out, HIDDEN_MARK)
        for user in (self.member, self.staff_member):
            resp = self._client(user).get(url)
            self.assertEqual(len(resp.context["diff_images"]), 3)
            self.assertEqual(len(resp.context["allphotdata"]), 3)
        self.assertEqual(len(out.context["allphotdata"]), 1)
        # Diff rows always match the photometry table for the same viewer.
        resp = self._client(self.staff).get(url)
        self.assertEqual(len(resp.context["diff_images"]), len(resp.context["allphotdata"]))

    def test_diff_images_in_full_detail_branch(self):
        out = self._detail(self.outsider, defer=False).context
        self.assertEqual([d.postage_stamp_file for d in out["diff_images"]], [PUBLIC_MARK])
        for user in (self.member, self.staff_member):
            self.assertEqual(len(self._detail(user, defer=False).context["diff_images"]), 3)

    def test_query_budget_does_not_grow_with_photometry(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from YSE_App.views import _visible_diff_images

        def count():
            with CaptureQueriesContext(connection) as ctx:
                rows = list(_visible_diff_images(self.outsider, self.transient.pk))
                for d in rows:
                    d.phot_data.band, d.phot_data.photometry
            return len(ctx.captured_queries)

        small = count()
        for i in range(5):
            self.transient_extra_point(i)
        self.assertEqual(small, count())

    def transient_extra_point(self, i):
        obs_group, instrument, band = create_instrument_stack(self.staff, obs_group_name="dv690")
        phot = TransientPhotometry.objects.create(
            transient=self.transient, instrument=instrument, obs_group=obs_group,
            **audit_fields(self.staff))
        point = TransientPhotData.objects.create(
            photometry=phot, band=band, mag=19.0 + i, mag_err=0.1,
            obs_date=timezone.now() - datetime.timedelta(days=40 + i), **audit_fields(self.staff))
        TransientDiffImage.objects.create(phot_data=point, postage_stamp_file="x%d" % i,
                                          **audit_fields(self.staff))
