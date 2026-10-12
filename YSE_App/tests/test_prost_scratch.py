"""Prost scratch isolation (#617): each run uses its own directory and removes only it."""

from __future__ import annotations

import os
import tempfile
import time
from unittest import mock

import astropy.units as u
from astropy.coordinates import SkyCoord
from django.test import SimpleTestCase

from YSE_App.data_ingest import TNS_uploads
from YSE_App.data_ingest.TNS_uploads import make_prost_run_dir, processTNS


def _proc(base, prost_path=''):
    p = processTNS()
    p.ghost_path = base
    p.prost_path = prost_path
    return p


def _call(p):
    return p.getProstData(['2026abc'], [SkyCoord(10 * u.deg, 5 * u.deg)])


class ProstScratchTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = os.path.join(self._tmp.name, 'scratch')
        os.makedirs(self.base)
        patcher = mock.patch.object(TNS_uploads, 'HAS_ASTRO_PROST', True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_interleaved_runs_do_not_delete_each_other(self):
        other_dirs = []

        def fake_associate(*args, save_path=None, **kw):
            self.assertTrue(os.path.isdir(save_path))
            self.assertEqual(os.path.dirname(save_path), self.base)
            self.assertTrue(os.path.basename(save_path).startswith('prost-'))
            open(os.path.join(save_path, 'out.txt'), 'w').close()
            if not other_dirs:
                # A second run starts and finishes while the first is in progress.
                other_dirs.append(save_path)
                _call(_proc(self.base))
                self.assertTrue(os.path.exists(os.path.join(save_path, 'out.txt')))
            return 'hosts'

        with mock.patch.object(TNS_uploads, 'associate_sample', side_effect=fake_associate):
            self.assertEqual(_call(_proc(self.base)), 'hosts')
        self.assertTrue(os.path.isdir(self.base))
        self.assertEqual(os.listdir(self.base), [])

    def test_base_survives_and_other_content_kept(self):
        keep = os.path.join(self.base, 'unrelated')
        os.makedirs(keep)
        with mock.patch.object(TNS_uploads, 'associate_sample', return_value='h'):
            _call(_proc(self.base))
        self.assertTrue(os.path.isdir(keep))
        self.assertEqual(os.listdir(self.base), ['unrelated'])

    def test_missing_base_is_created_and_kept(self):
        base = os.path.join(self._tmp.name, 'new', 'scratch')
        with mock.patch.object(TNS_uploads, 'associate_sample', return_value='h'):
            _call(_proc(base))
        self.assertTrue(os.path.isdir(base))

    def test_cleanup_on_exception(self):
        seen = []

        def boom(*args, save_path=None, **kw):
            seen.append(save_path)
            raise RuntimeError('prost failed')

        with mock.patch.object(TNS_uploads, 'associate_sample', side_effect=boom):
            with self.assertRaises(RuntimeError):
                _call(_proc(self.base))
        self.assertFalse(os.path.exists(seen[0]))
        self.assertTrue(os.path.isdir(self.base))

    def test_prost_path_preferred_over_ghost_path(self):
        new_base = os.path.join(self._tmp.name, 'prost_base')
        seen = []

        def fake(*args, save_path=None, **kw):
            seen.append(save_path)
            return 'h'

        with mock.patch.object(TNS_uploads, 'associate_sample', side_effect=fake):
            _call(_proc(self.base, prost_path=new_base))
        self.assertEqual(os.path.dirname(seen[0]), new_base)

    def test_only_stale_prost_dirs_are_removed(self):
        old = os.path.join(self.base, 'prost-old')
        new = os.path.join(self.base, 'prost-new')
        other = os.path.join(self.base, 'other-old')
        for d in (old, new, other):
            os.makedirs(d)
        past = time.time() - 3 * 24 * 3600
        for d in (old, other):
            os.utime(d, (past, past))
        run = make_prost_run_dir(self.base)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))
        self.assertTrue(os.path.isdir(other))
        self.assertTrue(os.path.isdir(run))
