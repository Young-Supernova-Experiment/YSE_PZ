"""Legacy ZTF forced-phot submission and downloads use requests, not a shell (#411, #530).

HTTP is mocked; nothing contacts ZTF.
"""

import os
import tempfile
from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from YSE_App.data_ingest import ZTF_Forced_Phot as fp

# synthetic download login; the real defaults live in settings (#758)
WEBDAV = dict(ZTF_WEBDAV_USER="fp-download-user", ZTF_WEBDAV_PASS="fp-download-pw")
WEBDAV_AUTH = (WEBDAV["ZTF_WEBDAV_USER"], WEBDAV["ZTF_WEBDAV_PASS"])


def _client():
    return fp.ZTF_Forced_Phot(
        ztf_email_address="a@example.com", ztf_email_password="x", ztf_email_imapserver="imap.example.com",
        ztf_user_address="a@example.com", ztf_user_password="SECRETPASS")


def _response(status=200, text="ok"):
    resp = mock.MagicMock()
    resp.status_code = status
    resp.text = text
    resp.content = text.encode()
    return resp


class ZTFForcedPhotSubmitTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        override = override_settings(ZTFTMPDIR=self.tmpdir, **WEBDAV)
        override.enable()
        self.addCleanup(override.disable)

    def test_missing_output_directory_is_created_and_request_is_authenticated(self):
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "forced_phot_out")))
        with mock.patch.object(fp.requests, "get", return_value=_response()) as get:
            log_file = _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        self.assertTrue(os.path.exists(log_file))
        self.assertEqual(os.path.dirname(log_file), os.path.join(self.tmpdir, "forced_phot_out"))
        self.assertEqual(get.call_args.args[0], fp.ZTF_REQUEST_URL)
        self.assertEqual(get.call_args.kwargs["auth"], WEBDAV_AUTH)
        self.assertTrue(get.call_args.kwargs["timeout"])

    def test_verbose_output_has_no_credentials(self):
        with mock.patch.object(fp.requests, "get", return_value=_response()), \
                mock.patch("builtins.print") as printed:
            _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=True)
        shown = " ".join(str(c) for c in printed.call_args_list)
        self.assertNotIn("SECRETPASS", shown)
        self.assertNotIn(WEBDAV["ZTF_WEBDAV_PASS"], shown)

    def test_connection_error_hides_credentials(self):
        err = requests.ConnectionError("Max retries with url: /x?userpass=SECRETPASS")
        with mock.patch.object(fp.requests, "get", side_effect=err):
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        self.assertNotIn("SECRETPASS", str(ctx.exception))
        self.assertIn("ConnectionError", str(ctx.exception))

    def test_ztf_rejection_reason_is_reported(self):
        """ZTF's 400 page says why (#411)."""
        page = ("<html><body><h2>Forced-Photometry Service</h2>"
                "<p>Error: e-mail address is unknown.</p></body></html>")
        with mock.patch.object(fp.requests, "get", return_value=_response(400, page)):
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        message = str(ctx.exception)
        self.assertIn("HTTP status 400", message)
        self.assertIn("ZTF says: e-mail address is unknown.", message)
        self.assertNotIn("SECRETPASS", message)
        self.assertNotIn("a@example.com", message)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "forced_phot_out", "x")))


class ZTFDownloadTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.tmpdir = tmp.name
        override = override_settings(**WEBDAV)
        override.enable()
        self.addCleanup(override.disable)

    def test_download_saves_file_with_auth(self):
        url = "https://%s/data/FPwork/forcedphotometry_req00001_lc.txt" % fp.ZTF_HOST
        with mock.patch.object(fp.requests, "get", return_value=_response(200, "data")) as get:
            name = fp.download_ztf_url(url, verbose=False)
        self.assertEqual(name, "forcedphotometry_req00001_lc.txt")
        self.assertEqual(open(os.path.join(self.tmpdir, name)).read(), "data")
        self.assertEqual(get.call_args.kwargs["auth"], WEBDAV_AUTH)
        self.assertTrue(get.call_args.kwargs["timeout"])

    def test_other_hosts_and_schemes_are_refused(self):
        for url in ("https://evil.example.com/x_lc.txt", "http://%s/x_lc.txt" % fp.ZTF_HOST,
                    "https://%s.evil.example.com/x_lc.txt" % fp.ZTF_HOST,
                    "https://user@evil.example.com/%s/x_lc.txt" % fp.ZTF_HOST):
            with mock.patch.object(fp.requests, "get") as get:
                with self.assertRaises(fp.ZTFForcedPhotSubmitError):
                    fp.download_ztf_url(url, verbose=False)
            get.assert_not_called()

    def test_file_name_cannot_escape_directory(self):
        url = "https://%s/a/..%%2F..%%2Fx_lc.txt" % fp.ZTF_HOST
        with mock.patch.object(fp.requests, "get", return_value=_response(200, "d")):
            name = fp.download_ztf_url(url, verbose=False)
        self.assertNotIn("/", name)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, name)))

    def test_http_error_is_reported_without_url(self):
        url = "https://%s/x_lc.txt" % fp.ZTF_HOST
        with mock.patch.object(fp.requests, "get", return_value=_response(404)):
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                fp.download_ztf_url(url, verbose=False)
        self.assertIn("404", str(ctx.exception))


class ZTFAccountAddressTests(SimpleTestCase):
    """#434: same rule as settings.DEFAULT_FROM_EMAIL."""

    def test_login_name_gets_gmail(self):
        self.assertEqual(fp.ztf_account_address("ysepz"), "ysepz@gmail.com")

    def test_full_address_is_used_as_is(self):
        self.assertEqual(fp.ztf_account_address(" ysepz@ucsc.edu "), "ysepz@ucsc.edu")

    @override_settings(SMTP_LOGIN="someone@example.org")
    def test_defaults_to_settings(self):
        self.assertEqual(fp.ztf_account_address(), "someone@example.org")


class ZTFWebdavLoginSettingsTests(SimpleTestCase):
    """The download login comes from settings, with ZTF's published login as default (#758)."""

    def test_default_login_is_configured(self):
        from django.conf import settings
        user, password = fp.ztf_webdav_auth()
        self.assertTrue(user)
        self.assertTrue(password)
        self.assertEqual((user, password), (settings.ZTF_WEBDAV_USER, settings.ZTF_WEBDAV_PASS))

    @override_settings(**WEBDAV)
    def test_login_follows_settings(self):
        self.assertEqual(fp.ztf_webdav_auth(), WEBDAV_AUTH)

    def test_module_has_no_inline_login(self):
        self.assertFalse(hasattr(fp, "_ztfuser"))
        self.assertFalse(hasattr(fp, "_ztfinfo"))
