"""Credential handling and shell-free helpers (#528, #531, #532, #533). All external calls are mocked."""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from YSE_App.data_ingest import ZTF_Forced_Phot as fp
from YSE_App.facilities import gemini as gemini_mod
from YSE_App.yse_utils import utility_functions as uf


class GeminiVerifyTests(SimpleTestCase):
    def _submit(self):
        fac = gemini_mod.GeminiFacility()
        fake = mock.MagicMock(status_code=200, text="GN-2026A-Q-1-1")
        request = mock.MagicMock(payload={"site": "north"})
        with mock.patch.object(fac, "build_payload", return_value={"prog": "p"}), \
                mock.patch.object(gemini_mod, "http_request", return_value=fake) as http:
            fac.submit(request)
        return http.call_args.kwargs

    @override_settings(GEMINI_CA_BUNDLE="")
    def test_certificate_is_verified_by_default(self):
        self.assertIs(self._submit()["verify"], True)

    @override_settings(GEMINI_CA_BUNDLE="/etc/ssl/private-ca.pem")
    def test_ca_bundle_setting_is_used(self):
        self.assertEqual(self._submit()["verify"], "/etc/ssl/private-ca.pem")


class ZTFPasswordRequiredTests(SimpleTestCase):
    def _make(self, password):
        return fp.ZTF_Forced_Phot(
            ztf_email_address="a@example.com", ztf_email_password="x", ztf_email_imapserver="imap.example.com",
            ztf_user_address="a@example.com", ztf_user_password=password)

    def test_placeholder_or_empty_password_fails_clearly_when_used(self):
        for value in ("", "<ZTF forced-photometry account pwd>"):
            with self.assertRaises(fp.ZTFForcedPhotSubmitError) as ctx:
                self._make(value)
            self.assertIn("not configured", str(ctx.exception))

    def test_real_value_is_accepted(self):
        self.assertIsNotNone(self._make("x-test-value"))


class HealpixInsertTests(SimpleTestCase):
    def test_rows_are_inserted_in_parameterized_batches(self):
        cursor = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        rows = [(i, 1.0, 2.0, 0.5, 7) for i in range(7)]
        with mock.patch("django.db.connection", conn), \
                mock.patch("django.db.transaction.atomic", return_value=mock.MagicMock()):
            total = uf.insert_healpix_cells(rows, batch_size=3)
        self.assertEqual(total, 7)
        self.assertEqual([len(c.args[1]) for c in cursor.executemany.call_args_list], [3, 3, 1])
        sql = cursor.executemany.call_args.args[0]
        self.assertIn("VALUES (%s, %s, %s, %s, %s)", sql)
        self.assertNotIn("INFILE", sql.upper())


class NoShellTests(SimpleTestCase):
    def test_helpers_do_not_use_a_shell(self):
        import os
        root = os.path.dirname(os.path.dirname(__file__))
        for rel in ("util/get_YSE_PAs.py", "yse_utils/get_YSE_PAs.py", "yse_utils/yse_pa.py",
                    "util/lcogt.py", "data_ingest/ZTF_Forced_Phot.py"):
            with open(os.path.join(root, rel)) as fh:
                source = fh.read()
            self.assertNotIn("shell=True", source, rel)
            self.assertNotIn("os.system", source, rel)
