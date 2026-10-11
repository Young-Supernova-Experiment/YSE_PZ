"""Before/after equivalence of photometry point ingest (#471).

``data_utils._write_photdata_points`` replaced a loop that compared every
incoming point with every existing point (an astropy ``Time`` and two lazy FK
loads per pair, one ``create()`` per new point). The functions at the bottom of
this module are frozen, verbatim copies of the pre-#471 code; each test runs the
same uploads through both on identical fixtures and compares the rows written.
"""

import copy
import datetime
import types

import numpy as np
from astropy.time import Time
from django.db import connection
from django.http import JsonResponse
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from YSE_App.common.collaboration_groups import (
    apply_collaboration_groups_to_photometry,
    collaboration_groups_from_photometry_upload,
)
from YSE_App.data_utils import (
    _add_transient_phot_rows,
    _obs_date_mjd,
    _obs_date_mjds,
    add_transient_phot_util,
)
from YSE_App.models import (
    DataQuality,
    PhotometricBand,
    TransientDiffImage,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
)
from YSE_App.services import photstat
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.view_utils import _transient_phot_cache_token

_MJD_EPOCH = datetime.datetime(1858, 11, 17)

DIFF_FIELDS = ('postage_stamp_file', 'postage_stamp_ref', 'postage_stamp_diff', 'diff_zero_point', 'valid_pixels')

STAT_FIELDS = (
    'num_obs_global', 'num_det_global', 'num_limits_global', 'last_obs_mjd',
    'first_detected_mjd', 'first_detected_mag', 'first_detected_band_id',
    'last_detected_mjd', 'last_detected_mag', 'last_detected_band_id',
    'peak_mjd', 'peak_mag', 'peak_band_id', 'deepest_limit', 'deepest_limit_band_id',
    'last_non_detection_mjd', 'last_non_detection_band_id',
)


def _iso(mjd):
    return (_MJD_EPOCH + datetime.timedelta(days=mjd)).isoformat()


def _point(mjd, band, mag=19.0, dq=0, **extra):
    p = {
        'obs_date': _iso(mjd), 'band': band, 'mag': mag, 'mag_err': 0.05,
        'flux': 10 ** (-0.4 * (mag - 27.5)), 'flux_err': 50.0, 'flux_zero_point': 27.5,
        'forced': True, 'diffim': True, 'data_quality': dq, 'discovery_point': 0,
    }
    p.update(extra)
    return p


def _diffimg(tag):
    return {'postage_stamp_file': 'stamp-%s.png' % tag, 'postage_stamp_ref': 'ref-%s.png' % tag,
            'postage_stamp_diff': 'diff-%s.png' % tag, 'diff_zero_point': 30.0, 'valid_pixels': True}


def _snapshot(transient):
    """Everything the ingest writes for ``transient``, ids and timestamps aside, in id order."""
    rows = []
    for e in TransientPhotData.objects.filter(photometry__transient=transient).select_related('photometry').order_by('id'):
        diffs = tuple(
            tuple(getattr(d, f) for f in DIFF_FIELDS)
            for d in TransientDiffImage.objects.filter(phot_data=e).order_by('id')
        )
        rows.append((
            e.photometry.instrument_id, e.photometry.obs_group_id, e.band_id, e.obs_date,
            e.flux, e.flux_err, e.flux_zero_point, e.mag, e.mag_err, e.forced, e.diffim,
            e.discovery_point, e.created_by_id, e.modified_by_id,
            tuple(sorted(e.data_quality.values_list('name', flat=True))), diffs,
        ))
    phots = sorted(
        (p.instrument_id, p.obs_group_id, tuple(sorted(p.groups.values_list('name', flat=True))))
        for p in TransientPhotometry.objects.filter(transient=transient)
    )
    stat = TransientPhotStat.objects.filter(transient=transient).first()
    stat_values = None if stat is None else tuple(getattr(stat, f) for f in STAT_FIELDS)
    return phots, rows, stat_values


def _run_util(fn, photdict, transient, user):
    """The two passes every caller makes (TransientPhotometry rows first, then points)."""
    photdict = copy.deepcopy(photdict)  # both implementations write into p['diffimg']
    _, entries = fn(photdict, transient, user, do_photdata=False)
    TransientPhotometry.objects.bulk_create(entries)
    fn(photdict, transient, user, do_photdata=True)


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('phot_ingest_equiv')
        cls.obs_group, cls.instrument, cls.band_r = create_instrument_stack(cls.user, obs_group_name='ingest-equiv')
        audit = audit_fields(cls.user)
        cls.band_g, _ = PhotometricBand.objects.get_or_create(
            name='g-ingest-equiv', instrument=cls.instrument,
            defaults={'disp_color': '#00ff00', 'disp_symbol': 'circle', **audit})
        if not PhotometricBand.objects.filter(name='Unknown').exists():
            PhotometricBand.objects.create(name='Unknown', instrument=cls.instrument,
                                           disp_color='#888888', disp_symbol='circle', **audit)
        for name in ('Bad', 'Questionable'):
            DataQuality.objects.get_or_create(name=name, defaults=audit)

    def _pair(self, tag):
        a = create_minimal_transient(self.user, name='equiv-%s-legacy' % tag, ra=10.0, dec=20.0)
        b = create_minimal_transient(self.user, name='equiv-%s-new' % tag, ra=10.0, dec=20.0)
        return a, b


class AddTransientPhotUtilEquivalenceTests(_Fixture):
    """``add_transient_phot_util`` (the /add_transient, broker and facility path)."""

    def _block(self, points):
        return {'instrument': self.instrument.name, 'obs_group': self.obs_group.name,
                'photdata': {str(i): p for i, p in enumerate(points)}}

    def _seed(self):
        r, g = self.band_r.name, self.band_g.name
        return {'clobber': False, 'mjdmatchmin': 0.001, 'seed': self._block([
            _point(60000.0, r, 19.5), _point(60001.0, r, 19.4), _point(60001.0004, r, 19.3),
            _point(60002.0, r, 19.2), _point(60010.0, r, 19.0, dq='Bad'), _point(60001.0, g, 19.6),
        ])}

    def _upload(self, clobber, tol):
        r, g = self.band_r.name, self.band_g.name
        return {'clobber': clobber, 'mjdmatchmin': tol,
                'a': self._block([
                    _point(60001.0002, r, 18.9, dq='Questionable', diffimg=_diffimg('p1')),  # two matches
                    _point(60002.0004, r, 18.8),           # matches 60002.0 (clobbered to .0004)
                    _point(60002.0013, r, 18.7),           # matches only the clobbered date
                    _point(60003.0, r, 18.6),              # new
                    _point(60003.0001, r, 18.5),           # new: same-upload points never match
                    _point(60001.0, g, 18.4),              # other band
                    _point(60020.0, g, 18.3, dq='NoSuchFlag'),  # new, flagged Bad
                    _point(60021.0, r, 18.2, diffimg=_diffimg('p8')),  # new with diff image
                    _point(60010.0, r, 18.1),              # matches the flagged seed point
                    _point(60030.0, r, 21.0, mag_err=None, flux=5.0, flux_err=100.0),  # limit
                ]),
                'b': self._block([
                    _point(60003.00005, r, 18.0),          # second block sees block a's rows
                    _point(60040.0, r, 17.9),
                ])}

    def _assert_equivalent(self, tag, clobber, tol):
        legacy_t, new_t = self._pair(tag)
        for t in (legacy_t, new_t):
            _run_util(legacy_add_transient_phot_util, self._seed(), t, self.user)
        _run_util(legacy_add_transient_phot_util, self._upload(clobber, tol), legacy_t, self.user)
        _run_util(add_transient_phot_util, self._upload(clobber, tol), new_t, self.user)
        legacy, new = _snapshot(legacy_t), _snapshot(new_t)
        self.assertEqual(legacy, new)
        self.assertIsNotNone(new[2], 'photstat row was not refreshed')
        return new

    def test_clobber(self):
        phots, rows, _ = self._assert_equivalent('clobber', True, 0.001)
        self.assertLess(len(rows), 6 + 12, 'scenario should exercise matches')

    def test_no_clobber(self):
        self._assert_equivalent('noclobber', False, 0.001)

    def test_wide_tolerance(self):
        self._assert_equivalent('wide', True, 0.5)

    def test_zero_tolerance_never_matches(self):
        phots, rows, _ = self._assert_equivalent('zero', True, 0.0)
        self.assertEqual(len(rows), 6 + 12)

    def test_inside_outer_deferred_block(self):
        legacy_t, new_t = self._pair('deferred')
        with photstat.deferred_updates():
            _run_util(legacy_add_transient_phot_util, self._seed(), legacy_t, self.user)
            _run_util(legacy_add_transient_phot_util, self._upload(True, 0.001), legacy_t, self.user)
        with photstat.deferred_updates():
            _run_util(legacy_add_transient_phot_util, self._seed(), new_t, self.user)
            _run_util(add_transient_phot_util, self._upload(True, 0.001), new_t, self.user)
        self.assertEqual(_snapshot(legacy_t), _snapshot(new_t))

    def test_plot_cache_token_changes_on_bulk_insert(self):
        _, new_t = self._pair('token')
        _run_util(add_transient_phot_util, self._seed(), new_t, self.user)
        before = _transient_phot_cache_token(new_t.id)
        _run_util(add_transient_phot_util, self._upload(False, 0.001), new_t, self.user)
        after = _transient_phot_cache_token(new_t.id)
        self.assertNotEqual(before, after)
        self.assertEqual(int(after.split(':')[0]),
                         TransientPhotData.objects.filter(photometry__transient=new_t).count())

    def test_query_count_does_not_scale_with_points(self):
        r = self.band_r.name
        _, new_t = self._pair('queries')
        seed = {'clobber': False, 'mjdmatchmin': 0.001,
                'seed': self._block([_point(59000.0 + i, r) for i in range(300)])}
        _run_util(add_transient_phot_util, seed, new_t, self.user)
        upload = {'clobber': False, 'mjdmatchmin': 0.001,
                  'a': self._block([_point(59000.0 + i + (0.0 if i % 2 else 0.5), r) for i in range(300)])}
        with CaptureQueriesContext(connection) as ctx:
            _run_util(add_transient_phot_util, upload, new_t, self.user)
        # 150 points match, 150 are new: one bulk INSERT, not 150 creates and 90,000 pair checks.
        self.assertEqual(TransientPhotData.objects.filter(photometry__transient=new_t).count(), 450)
        self.assertLess(len(ctx.captured_queries), 40, [q['sql'][:120] for q in ctx.captured_queries])


class AddTransientPhotRowsEquivalenceTests(_Fixture):
    """``_add_transient_phot_rows`` (the /add_transient_phot path)."""

    def _phot_data(self, points):
        data = {'header': {}, 'transient': {}, 'photheader': {}}
        for i, p in enumerate(points):
            data['k%d' % i] = p
        return data

    def _photometry(self, transient):
        tp, _ = TransientPhotometry.objects.get_or_create(
            transient=transient, instrument=self.instrument, obs_group=self.obs_group,
            defaults=audit_fields(self.user))
        return tp

    def _run(self, fn, transient, points, clobber, tol):
        tp = self._photometry(transient)
        hd = {'clobber': clobber, 'mjdmatchmin': tol}
        ph = {'instrument': self.instrument.name}
        with photstat.deferred_updates():
            fn(self._phot_data(copy.deepcopy(points)), hd, ph, tp,
               TransientPhotData.objects.filter(photometry=tp), self.user)

    def _check(self, tag, clobber, tol):
        r, g = self.band_r.name, self.band_g.name
        seed = [_point(60000.0, r), _point(60001.0, r), _point(60001.0004, r), _point(60001.0, g),
                _point(60005.0, r, dq='Bad')]
        upload = [_point(60001.0002, r, 18.0, dq='Questionable'), _point(60001.0, g, 18.1),
                  _point(60002.0, r, 18.2), _point(60002.0001, r, 18.3), _point(60003.0, 'no-such-band', 18.4),
                  _point(60005.0, r, 18.5), _point(60006.0, r, 18.6, dq='NoSuchFlag')]
        legacy_t, new_t = self._pair(tag)
        for t in (legacy_t, new_t):
            self._run(legacy_add_transient_phot_rows, t, seed, False, tol)
        self._run(legacy_add_transient_phot_rows, legacy_t, upload, clobber, tol)
        self._run(_add_transient_phot_rows, new_t, upload, clobber, tol)
        self.assertEqual(_snapshot(legacy_t), _snapshot(new_t))

    def test_clobber(self):
        self._check('rows-clobber', True, 0.001)

    def test_no_clobber(self):
        self._check('rows-noclobber', False, 0.001)


class ObsDateMjdTests(SimpleTestCase):
    def test_vectorised_mjds_equal_the_per_row_values(self):
        utc = datetime.timezone.utc
        rng = np.random.default_rng(471)
        base = datetime.datetime(2015, 1, 1, tzinfo=utc)
        dates = [base + datetime.timedelta(seconds=float(s)) for s in rng.uniform(0, 4e8, 500)]
        dates += [d.replace(microsecond=0) for d in dates[:50]]
        dates += [datetime.datetime(2016, 12, 31, 23, 59, 59, 999999, tzinfo=utc),
                  datetime.datetime(2017, 1, 1, tzinfo=utc)]
        rows = [types.SimpleNamespace(obs_date=d) for d in dates]
        vectorised = _obs_date_mjds(rows)
        self.assertEqual(vectorised.tolist(), [float(_obs_date_mjd(d)) for d in dates])
        self.assertEqual(_obs_date_mjds(rows[:1]).shape, (1,))
        self.assertEqual(_obs_date_mjds([]).shape, (0,))

    def test_string_dates_fall_back_per_row(self):
        rows = [types.SimpleNamespace(obs_date='2024-01-01T00:00:00')]
        self.assertEqual(_obs_date_mjds(rows).tolist(), [Time('2024-01-01T00:00:00', format='isot').mjd])


# ---------------------------------------------------------------------------
# Frozen copies of the pre-#471 code (YSE_App/data_utils.py at 007c12f8).
# Do not edit: they are the reference the tests above compare against.
# ---------------------------------------------------------------------------

def legacy_add_transient_phot_util(photdict,transient,user,do_photdata=True):

    transientphot_entries = []
    for k in photdict.keys():
        if k == 'clobber' or k == 'mjdmatchmin': continue
        photometry = photdict[k]

        from YSE_App.common.tns_photometry_map import (
            lookup_instrument_band,
            resolve_tns_photometry,
        )

        obs_group_name = photometry.get('obs_group') or None
        if obs_group_name in (None, '', 'None'):
            obs_group_name = None
        _resolved_inst = resolve_tns_photometry(photometry['instrument'], 'r')
        _obs_group, instrument, _ = lookup_instrument_band(
            user,
            photometry['instrument'],
            _resolved_inst.band,
            obs_group_name=obs_group_name or _resolved_inst.obs_group_hint,
        )
        obs_group = _obs_group

        transientphot = TransientPhotometry.objects.filter(transient=transient).filter(instrument=instrument).filter(obs_group=obs_group)
        if not len(transientphot):
            transientphot = TransientPhotometry( #.objects.create
                instrument=instrument, obs_group=obs_group, transient=transient,
                created_by_id=user.id, modified_by_id=user.id)
            transientphot_entries += [transientphot]
        else: transientphot = transientphot[0]
        existingphot = TransientPhotData.objects.filter(photometry=transientphot)
        if do_photdata:
            for k in photometry['photdata']:
                p = photometry['photdata'][k]
                pmjd = Time(p['obs_date'],format='isot').mjd

                _og, _inst, band = lookup_instrument_band(
                    user,
                    photometry['instrument'],
                    p['band'],
                    obs_group_name=obs_group_name,
                )
                if _inst.id != instrument.id:
                    instrument = _inst
                if _og.id != obs_group.id:
                    obs_group = _og
                obsExists = False

                for idx,e in enumerate(existingphot):
                    if e.photometry.id == transientphot.id:
                        if e.band == band:
                            try:
                                mjd = Time(e.obs_date.isoformat().split('+')[0],format='isot').mjd
                            except:
                                mjd = Time(e.obs_date,format='isot').mjd

                            if np.abs(mjd - pmjd) < photdict['mjdmatchmin']:
                                obsExists = True
                                if photdict['clobber']:

                                    dq = None
                                    if p['data_quality']:
                                        dq = DataQuality.objects.filter(name=p['data_quality'])
                                        if not dq:
                                            dq = DataQuality.objects.filter(name='Bad')
                                        dq = dq[0]

                                    e.obs_date = p['obs_date']
                                    e.flux = p['flux']
                                    e.flux_err = p['flux_err']
                                    e.flux_zero_point = p['flux_zero_point']
                                    e.mag = p['mag']
                                    e.mag_err = p['mag_err']
                                    e.forced = p['forced']
                                    e.diffim = p['diffim']

                                    # Because we're in CLOBBER mode, remove existing dq flags and add the one passed here.
                                    data_quality_flags = e.data_quality.all()
                                    for _dq in data_quality_flags:
                                        e.data_quality.remove(_dq)
                                    e.data_quality.add(dq)

                                    e.photometry = transientphot
                                    e.discovery_point = p['discovery_point']
                                    e.band = band
                                    e.modified_by_id = user.id
                                    e.save()

                                if 'diffimg' in p.keys():
                                    existing_diff = TransientDiffImage.objects.filter(phot_data=e)
                                    p['diffimg']['created_by_id'] = user.id
                                    p['diffimg']['modified_by_id'] = user.id
                                    p['diffimg']['phot_data_id'] = e.id
                                    if not len(existing_diff):
                                        TransientDiffImage.objects.create(**p['diffimg'])
                                    else:
                                        existing_diff.update(**p['diffimg'])


                if not obsExists:
                    dq = None
                    if p['data_quality']:
                        dq = DataQuality.objects.filter(name=p['data_quality'])
                        if not dq:
                            dq = DataQuality.objects.filter(name='Bad')
                        dq = dq[0]

                    e = TransientPhotData.objects.create(
                        obs_date=p['obs_date'],flux=p['flux'],flux_err=p['flux_err'],
                        mag=p['mag'],mag_err=p['mag_err'],forced=p['forced'],
                        diffim=p['diffim'],
                        photometry=transientphot,
                        flux_zero_point=p['flux_zero_point'],
                        discovery_point=p['discovery_point'],band=band,
                        created_by_id=user.id,modified_by_id=user.id)

                    if dq is not None:
                        # Set operator needs a list...
                        e.data_quality.set([dq])
                        e.save()
                    if 'diffimg' in p.keys():
                        p['diffimg']['created_by_id'] = user.id
                        p['diffimg']['modified_by_id'] = user.id
                        p['diffimg']['phot_data_id'] = e.id
                        TransientDiffImage.objects.create(**p['diffimg'])

        group_names = collaboration_groups_from_photometry_upload(photometry)
        if transientphot.pk:
            apply_collaboration_groups_to_photometry(transientphot, group_names)

    return_dict = {"message":"successfully added phot data"}
    return JsonResponse(return_dict),transientphot_entries


def legacy_add_transient_phot_rows(phot_data, hd, ph, transientphot, existingphot, user):
    for k in phot_data.keys():
        if k == 'header' or k == 'transient' or k == 'photheader': continue
        p = phot_data[k]
        pmjd = Time(p['obs_date'],format='isot').mjd
        band = PhotometricBand.objects.filter(name=p['band']).filter(instrument__name=ph['instrument'])
        if len(band): band = band[0]
        else: band = PhotometricBand.objects.filter(name='Unknown')[0]

        obsExists = False
        for e in existingphot:
            if e.photometry.id == transientphot.id:
                if e.band == band:
                    try:
                        mjd = Time(e.obs_date.isoformat().split('+')[0],format='isot').mjd
                    except:
                        mjd = Time(e.obs_date,format='isot').mjd
                    if np.abs(mjd - pmjd) < hd['mjdmatchmin']:
                        obsExists = True
                        if hd['clobber']:

                            dq = None
                            if p['data_quality']:
                                dq = DataQuality.objects.filter(name=p['data_quality'])
                                if not dq:
                                    dq = DataQuality.objects.filter(name='Bad')
                                dq = dq[0]

                            e.obs_date = p['obs_date']
                            e.flux = p['flux']
                            e.flux_err = p['flux_err']
                            e.flux_zero_point = p['flux_zero_point']
                            e.mag = p['mag']
                            e.mag_err = p['mag_err']
                            e.forced = p['forced']
                            e.diffim = p['diffim']

                            # Because we're in CLOBBER mode, remove existing dq flags and add the one passed here.
                            data_quality_flags = e.data_quality.all()
                            for _dq in data_quality_flags:
                                e.data_quality.remove(_dq)
                            e.data_quality.add(dq)

                            e.photometry = transientphot
                            e.discovery_point = p['discovery_point']
                            e.band = band
                            e.modified_by_id = user.id
                            e.save()

        if not obsExists:
            dq = None
            if p['data_quality']:
                dq = DataQuality.objects.filter(name=p['data_quality'])
                if not dq:
                    dq = DataQuality.objects.filter(name='Bad')
                dq = dq[0]
            tpd = TransientPhotData.objects.create(obs_date=p['obs_date'],flux=p['flux'],flux_err=p['flux_err'],
                                                   mag=p['mag'],mag_err=p['mag_err'],forced=p['forced'],
                                                   diffim=p['diffim'],
                                                   photometry=transientphot,
                                                   flux_zero_point=p['flux_zero_point'],
                                                   discovery_point=p['discovery_point'],band=band,
                                                   created_by_id=user.id,modified_by_id=user.id)
            if dq is not None:
                # Set operator needs a list...
                tpd.data_quality.set([dq])
                tpd.save()
