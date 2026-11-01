"""CasJobs login comes from settings / env, never from source (#626). No network."""
import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from YSE_App.common import casjobs_credentials as cj

_KEYS = ('CASJOBS_WSID', 'CASJOBS_PW')


def _clean_env():
    return mock.patch.dict(os.environ, {}, clear=False)


class CasJobsCredentialsTests(SimpleTestCase):
    def setUp(self):
        patcher = _clean_env()
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in _KEYS:
            os.environ.pop(k, None)

    @override_settings(CASJOBS_WSID='wsid-from-settings', CASJOBS_PW='pw-from-settings')
    def test_reads_from_settings(self):
        self.assertEqual(cj.get_casjobs_credentials(), ('wsid-from-settings', 'pw-from-settings'))

    @override_settings(CASJOBS_WSID='from-settings', CASJOBS_PW='from-settings')
    def test_env_overrides_settings(self):
        os.environ['CASJOBS_WSID'] = 'env-wsid'
        os.environ['CASJOBS_PW'] = 'env-pw'
        self.assertEqual(cj.get_casjobs_credentials(), ('env-wsid', 'env-pw'))

    @override_settings(CASJOBS_WSID='', CASJOBS_PW='')
    def test_missing_gives_clear_error(self):
        with self.assertRaises(cj.CasJobsCredentialsError) as ctx:
            cj.get_casjobs_credentials()
        msg = str(ctx.exception)
        self.assertIn('[casjobs]', msg)
        self.assertIn('wsid', msg)
        self.assertIn('password', msg)

    @override_settings(CASJOBS_WSID='<placeholder>', CASJOBS_PW='only-pw')
    def test_placeholder_counts_as_missing(self):
        with self.assertRaises(cj.CasJobsCredentialsError) as ctx:
            cj.get_casjobs_credentials()
        self.assertIn('missing: wsid', str(ctx.exception))

    @override_settings(CASJOBS_WSID='w', CASJOBS_PW='p')
    def test_configure_exports_environment(self):
        cj.configure_casjobs_env()
        self.assertEqual(os.environ['CASJOBS_WSID'], 'w')
        self.assertEqual(os.environ['CASJOBS_PW'], 'p')

    @override_settings(CASJOBS_WSID='', CASJOBS_PW='')
    def test_configure_missing_raises_and_sets_nothing(self):
        with self.assertRaises(cj.CasJobsCredentialsError):
            cj.configure_casjobs_env()
        self.assertNotIn('CASJOBS_PW', os.environ)


class IngestJobUsesSettingsTests(SimpleTestCase):
    """get_ps_score reads the settings-backed login and stops cleanly when it is missing."""

    def setUp(self):
        patcher = _clean_env()
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in _KEYS:
            os.environ.pop(k, None)

    def _module(self):
        import importlib
        try:
            return importlib.import_module('YSE_App.data_ingest.DECam_upload_clusters')
        except Exception as exc:  # optional ingest deps (dustmaps data) may be absent in CI
            self.skipTest('ingest module not importable here: %s' % exc)

    @override_settings(CASJOBS_WSID='', CASJOBS_PW='')
    def test_missing_setting_fails_only_the_job(self):
        mod = self._module()
        with mock.patch.object(mod.mastrequests, 'MastCasJobs') as casjobs:
            with self.assertRaises(cj.CasJobsCredentialsError):
                mod.get_ps_score(10.0, 20.0)
        casjobs.assert_not_called()

    @override_settings(CASJOBS_WSID='w', CASJOBS_PW='p')
    def test_configured_job_uses_settings(self):
        mod = self._module()
        with mock.patch.object(mod.mastrequests, 'MastCasJobs') as casjobs:
            casjobs.return_value.quick.return_value = 'ps_score\n0.5'
            self.assertEqual(mod.get_ps_score(10.0, 20.0), 0.5)
        self.assertEqual(os.environ['CASJOBS_PW'], 'p')

    def test_no_inline_credentials_in_sources(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pat = re.compile(r"""environ\[\s*['"]CASJOBS_(PW|WSID)['"]\s*\]\s*=\s*['"][^'"]""")
        for rel in ('data_ingest/DECam_upload_clusters.py', 'data_ingest/DECam_upload.py',
                    'data_ingest/TNS_uploads.py', 'data_ingest/QUB_data.py'):
            with open(os.path.join(root, rel)) as fh:
                self.assertIsNone(pat.search(fh.read()), rel)
