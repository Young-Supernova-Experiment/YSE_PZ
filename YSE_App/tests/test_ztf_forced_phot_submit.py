"""Legacy ZTF forced-phot submission reports why wget failed (#411).

wget runs through subprocess and is mocked; nothing contacts ZTF.
"""

import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase, override_settings

from YSE_App.data_ingest import ZTF_Forced_Phot as fp


def _client():
    return fp.ZTF_Forced_Phot(
        ztf_email_address="a@example.com", ztf_email_password="x", ztf_email_imapserver="imap.example.com",
        ztf_user_address="a@example.com", ztf_user_password="SECRETPASS")


def _popen(returncode, stderr=b"", create_file=False, content="ok"):
    """A Popen stand-in; with create_file it writes the -O target like wget would."""
    def factory(command, **kwargs):
        if create_file:
            target = command.split(" -O ")[1].split(" ")[0]
            with open(target, "w") as fh:
                fh.write(content)
        proc = mock.MagicMock()
        proc.communicate.return_value = (b"", stderr)
        proc.returncode = returncode
        return proc
    return factory


class ZTFForcedPhotSubmitTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        override = override_settings(ZTFTMPDIR=self.tmpdir)
        override.enable()
        self.addCleanup(override.disable)

    def test_missing_output_directory_is_created(self):
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "forced_phot_out")))
        with mock.patch.object(fp.subprocess, "Popen", side_effect=_popen(0, create_file=True)):
            log_file = _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        self.assertTrue(os.path.exists(log_file))
        self.assertEqual(os.path.dirname(log_file), os.path.join(self.tmpdir, "forced_phot_out"))

    def test_wget_failure_raises_with_its_message_and_no_credentials(self):
        stderr = (b"--2026-10-06 18:00:00--  https://ztfweb.ipac.caltech.edu/cgi-bin/requestForcedPhotometry.cgi"
                  b"?ra=10&email=a@example.com&userpass=SECRETPASS\n"
                  b"Connecting to ztfweb.ipac.caltech.edu... connected.\n"
                  b"/data/x/forced_phot_out/ztffp_ABC.txt: Permission denied\n")
        with mock.patch.object(fp.subprocess, "Popen", side_effect=_popen(3, stderr=stderr)):
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        message = str(ctx.exception)
        self.assertIn("status 3", message)
        self.assertIn("Permission denied", message)
        self.assertNotIn("SECRETPASS", message)
        self.assertNotIn("a@example.com", message)

    def test_wget_missing_from_path(self):
        with mock.patch.object(fp.subprocess, "Popen", side_effect=_popen(127, stderr=b"/bin/sh: 1: wget: not found\n")):
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        self.assertIn("wget: not found", str(ctx.exception))

    def test_error_summary_redacts_password_fields(self):
        summary = fp._wget_error_summary(b"ERROR: bad request userpass=SECRETPASS\n")
        self.assertIn("userpass=***", summary)
        self.assertNotIn("SECRETPASS", summary)

    def test_ztf_rejection_reason_is_reported(self):
        """ZTF's 400 page says why (#411); --content-on-error keeps it for the message."""
        page = ("<html><body><h2>Forced-Photometry Service</h2>"
                "<p>Error: e-mail address is unknown.</p></body></html>")
        stderr = b"2026-10-07 23:26:35 ERROR 400: Bad Request.\n"
        with mock.patch.object(fp.subprocess, "Popen",
                               side_effect=_popen(8, stderr=stderr, create_file=True, content=page)) as popen:
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                _client().ztf_forced_photometry(ra=10.0, decl=-5.0, verbose=False)
        self.assertIn("--content-on-error", popen.call_args.args[0])
        message = str(ctx.exception)
        self.assertIn("ERROR 400: Bad Request.", message)
        self.assertIn("ZTF says: e-mail address is unknown.", message)
        self.assertEqual(os.listdir(os.path.join(self.tmpdir, "forced_phot_out")), [])
