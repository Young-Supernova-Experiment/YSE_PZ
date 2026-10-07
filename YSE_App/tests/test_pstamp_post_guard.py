"""pstamp POSTs never silently follow a redirect (#422)."""

from unittest import mock

import requests
from django.test import SimpleTestCase

from YSE_App.data_ingest.YSE_Forced_Phot import _pstamp_post


def _response(status, location=None):
    r = requests.Response()
    r.status_code = status
    if location:
        r.headers["Location"] = location
    return r


class PstampPostGuardTests(SimpleTestCase):
    def test_redirect_raises_instead_of_turning_into_a_get(self):
        session = mock.Mock()
        session.post.return_value = _response(301, "https://pstamp.example/upload.php")
        with self.assertRaisesRegex(RuntimeError, "redirected to https://pstamp.example/upload.php"):
            _pstamp_post(session, "http://pstamp.example/upload.php", files={"filename": b"x"})
        _, kwargs = session.post.call_args
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["files"], {"filename": b"x"})

    def test_ok_and_auth_challenge_pass_through(self):
        for status in (200, 401):
            session = mock.Mock()
            session.post.return_value = _response(status)
            self.assertEqual(_pstamp_post(session, "https://pstamp.example/status.php").status_code, status)
