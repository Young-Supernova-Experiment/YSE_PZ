"""Photometry table: decoded quality_mask badges and the all / good / flagged filter (#85).

Synthetic data only.
"""

from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse

from YSE_App.common.quality_mask import PhotFlag, flags_from_mask
from YSE_App.models import TransientPhotData
from YSE_App.templatetags.quality_mask_extras import phot_mask_flags
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


class PhotometryTableQualityMaskTests(TestCase):
    def setUp(self):
        self.admin = create_test_user('qm_admin', is_staff=True, is_superuser=True)
        self.viewer = create_test_user('qm_viewer', is_staff=False, is_superuser=False, email='qm_viewer@example.com')
        self.transient = create_minimal_transient(self.admin, name='2026qm', obs_group_name='qm-group')
        self.obs_group, self.instrument, self.band = create_instrument_stack(self.admin, obs_group_name='qm-group')
        self.phot = attach_synthetic_photometry(
            self.admin, self.transient, obs_group=self.obs_group, instrument=self.instrument,
            band=self.band, n_points=3,
        )
        rows = list(TransientPhotData.objects.filter(photometry=self.phot).order_by('obs_date'))
        self.good, self.sat, self.rej = rows
        # NOT_PUBLISHED alone is not a legacy "flag": the row stays in "good only"
        TransientPhotData.objects.filter(pk=self.good.pk).update(quality_mask=int(PhotFlag.NOT_PUBLISHED))
        TransientPhotData.objects.filter(pk=self.sat.pk).update(quality_mask=int(PhotFlag.SATURATED))
        TransientPhotData.objects.filter(pk=self.rej.pk).update(
            quality_mask=int(PhotFlag.REJECTED | PhotFlag.MASKED))
        self.fragment = reverse('transient_detail_photometry_fragment', kwargs={'transient_id': self.transient.pk})

    def _rows(self, user, query=''):
        c = Client()
        c.force_login(user)
        response = c.get(self.fragment + query)
        self.assertEqual(response.status_code, 200)
        return response, {int(r.pk) for r in response.context['allphotdata']}

    def test_filter_returns_right_rows(self):
        _, all_rows = self._rows(self.viewer)
        self.assertEqual(all_rows, {self.good.pk, self.sat.pk, self.rej.pk})
        _, same = self._rows(self.viewer, '?dq=all')
        self.assertEqual(same, all_rows)
        _, good = self._rows(self.viewer, '?dq=good')
        self.assertEqual(good, {self.good.pk})
        _, flagged = self._rows(self.viewer, '?dq=flagged')
        self.assertEqual(flagged, {self.sat.pk, self.rej.pk})
        _, junk = self._rows(self.viewer, '?dq=bogus')
        self.assertEqual(junk, all_rows, 'unknown values mean all')

    def test_badges_use_constant_names(self):
        response, _ = self._rows(self.viewer)
        html = response.content.decode()
        for name in ('NOT_PUBLISHED', 'SATURATED', 'REJECTED', 'MASKED'):
            self.assertIn('>%s</span>' % name, html)
        self.assertEqual(phot_mask_flags(int(PhotFlag.SATURATED | PhotFlag.CR)), ['CR', 'SATURATED'])
        self.assertEqual(phot_mask_flags(None), [])
        self.assertEqual(phot_mask_flags(int(PhotFlag.REJECTED)), flags_from_mask(int(PhotFlag.REJECTED)))

    def test_filter_buttons_mark_active(self):
        html = self._rows(self.viewer, '?dq=flagged')[0].content.decode()
        self.assertIn('data-dq="flagged" class="btn btn-default btn-sm active"', html)
        self.assertNotIn('data-dq="good" class="btn btn-default btn-sm active"', html)

    def test_filter_does_not_widen_visibility(self):
        private = Group.objects.create(name='QMPRIVATE')
        self.phot.groups.add(private)  # restricted: the viewer is not in the group
        for query in ('', '?dq=good', '?dq=flagged', '?dq=all'):
            _, rows = self._rows(self.viewer, query)
            self.assertEqual(rows, set(), 'viewer must not see restricted rows with %r' % query)
        self.viewer.groups.add(private)  # a group member does see them, filtered
        _, rows = self._rows(self.viewer, '?dq=flagged')
        self.assertEqual(rows, {self.sat.pk, self.rej.pk})

    def test_deferred_tab_keeps_the_filter(self):
        """The deferred shell forwards ``?dq=`` to the photometry fragment; inline mode filters directly."""
        c = Client()
        c.force_login(self.viewer)
        url = reverse('transient_detail', kwargs={'slug': self.transient.slug}) + '?dq=good'
        with mock.patch.dict(os.environ, {'YSE_TRANSIENT_DETAIL_DEFER': '1'}):
            html = c.get(url).content.decode()
        self.assertIn("key === 'photometry' ? url + yseDqQuery() : url", html)
        self.assertIn('(good|flagged)', html)
        self.assertIn('photometry_fragment', html)
        with mock.patch.dict(os.environ, {'YSE_TRANSIENT_DETAIL_DEFER': '0'}):
            response = c.get(url)
        self.assertEqual({r.pk for r in response.context['allphotdata']}, {self.good.pk})
        self.assertEqual(response.context['dq_filter'], 'good')
