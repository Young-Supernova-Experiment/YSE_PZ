"""YSE_PZ/static/latest_YSE*.py read settings.ini and write next to themselves (#425).

HTTP is mocked; nothing contacts TNS or the API.
"""

import importlib.util
import json
import os
import tempfile
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

SETTINGS_INI = """[main]
dburl=https://stack.example/yse_test/api/
dblogin=apiuser
dbpassword=apipass
tnsapikey=KEY123
tns_bot_id=42
tns_bot_name=ysebot
tns_marker_type=bot
"""


def _load(name, static_dir):
    path = os.path.join(settings.BASE_DIR, "YSE_PZ", "static", name)
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HERE = static_dir
    return module


def _json_response(payload):
    r = mock.Mock(status_code=200, text=json.dumps(payload))
    r.raise_for_status.return_value = None
    return r


class LatestScriptsTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.static_dir = os.path.join(tmp.name, "static")
        os.makedirs(self.static_dir)
        with open(os.path.join(tmp.name, "settings.ini"), "w") as fh:
            fh.write(SETTINGS_INI)

    def test_fields_script_uses_stack_api_and_login(self):
        module = _load("latest_YSE_fields.py", self.static_dir)
        obs = {"results": [{"photometric_band": "https://stack.example/b/1/", "survey_field": "https://stack.example/f/1/",
                            "obs_mjd": 61300.5, "mag_lim": 21.3}]}
        responses = {
            "https://stack.example/b/1/": _json_response({"name": "g"}),
            "https://stack.example/f/1/": _json_response({"field_id": "403.F", "ra_cen": 10.0, "dec_cen": -5.0}),
        }

        def fake_get(url, auth=None, **kwargs):
            self.assertEqual((auth.username, auth.password), ("apiuser", "apipass"))
            if "surveyobservations" in url:
                self.assertTrue(url.startswith("https://stack.example/yse_test/api/surveyobservations/?obs_mjd_gte="))
                return _json_response(obs)
            return responses[url]

        with mock.patch.object(module.requests, "get", side_effect=fake_get):
            module.main()
        with open(os.path.join(self.static_dir, "yse_latest_fields.html")) as fh:
            html = fh.read()
        self.assertIn("403.F", html)
        self.assertIn("<i>g</i>", html)

    def test_tns_script_posts_marker_and_key_to_current_domain(self):
        module = _load("latest_YSE.py", self.static_dir)
        csv = ('"Name","RA","DEC","Obj. Type","Redshift","Host Redshift","Reporting Group/s",'
               '"Discovery Mag/Flux","Discovery Filter","Discovery Date (UT)"\n'
               '"2026abc","01:00:00","+02:00:00","SN Ia","0.05","","YSE","19.1","r","2026-10-05 10:00:00"\n')
        response = mock.Mock(status_code=200, text=csv)
        response.raise_for_status.return_value = None
        with mock.patch.object(module.requests, "post", return_value=response) as post:
            module.main()
        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        self.assertTrue(url.startswith("https://www.wis-tns.org/search?"))
        self.assertEqual(kwargs["data"], {"api_key": "KEY123"})
        self.assertEqual(kwargs["headers"]["User-Agent"], 'tns_marker{"tns_id":42, "type":"bot", "name":"ysebot"}')
        with open(os.path.join(self.static_dir, "yse_latest.html")) as fh:
            self.assertIn("2026abc", fh.read())
