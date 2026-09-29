"""settings.py: SECRET_KEY, ALLOWED_HOSTS and DB TLS come from env / settings.ini."""

import importlib
import os
import sys
import tempfile
import textwrap
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

import YSE_PZ.settings as live_settings

_BASE_INI = textwrap.dedent(
    """
    [database]
    DATABASE_NAME: x
    DATABASE_USER: x
    DATABASE_PASSWORD: x
    DATABASE_HOST: x
    DATABASE_PORT: 3306
    EXPLORER_USER: x
    EXPLORER_PASSWORD: x
    {database_extra}
    [virtual_directory]
    LOGIN_URL = /login/
    [SMTP_provider]
    SMTP_LOGIN: x
    SMTP_PASSWORD: x
    SMTP_HOST: x
    SMTP_PORT: 1
    [site_settings]
    STATIC: /static/
    IS_DEBUG: {debug}
    {site_extra}
    [main]
    lcogtuser=x
    lcogtpass=x
    tns_bot_name=x
    tns_bot_id=1
    tnsapikey=x
    tns_decam_bot_name=x
    tns_decam_bot_id=1
    tnsdecamapikey=x
    ghost_path=/tmp
    [ztf]
    ztfforcedphotpass=x
    ztfforcedtmpdir=/tmp
    [yse]
    red_yse_filter=y
    """
)


def _load_settings(*, debug, site_extra="", database_extra="", env=None):
    """Import a private copy of YSE_PZ/settings.py against a scratch settings.ini."""
    src = os.path.join(os.path.dirname(live_settings.__file__), "settings.py")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "settings.ini"), "w") as fh:
            fh.write(_BASE_INI.format(debug=debug, site_extra=site_extra, database_extra=database_extra))
        target = os.path.join(tmp, "yse_settings_under_test.py")
        with open(src) as fh:
            open(target, "w").write(fh.read())
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "YSE_EXPLORER_MAX_EXECUTION_MS")}
        clean_env.update(env or {})
        sys.path.insert(0, tmp)
        try:
            with mock.patch.dict(os.environ, clean_env, clear=True):
                sys.modules.pop("yse_settings_under_test", None)
                return importlib.import_module("yse_settings_under_test")
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("yse_settings_under_test", None)


class SecretKeyTests(SimpleTestCase):
    def test_debug_without_key_uses_dev_fallback(self):
        mod = _load_settings(debug="True")
        self.assertTrue(mod.SECRET_KEY)
        self.assertIn("development", mod.SECRET_KEY)

    def test_placeholder_counts_as_unset(self):
        mod = _load_settings(debug="True", site_extra="SECRET_KEY: <django secret key; leave placeholder if using env>")
        self.assertIn("development", mod.SECRET_KEY)

    def test_production_without_key_is_improperly_configured(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _load_settings(debug="False")
        self.assertIn("DJANGO_SECRET_KEY", str(ctx.exception))

    def test_production_reads_ini_key(self):
        mod = _load_settings(debug="False", site_extra="SECRET_KEY: from-the-ini-file")
        self.assertEqual(mod.SECRET_KEY, "from-the-ini-file")

    def test_env_wins_over_ini(self):
        mod = _load_settings(debug="False", site_extra="SECRET_KEY: from-the-ini-file",
                             env={"DJANGO_SECRET_KEY": "from-the-env"})
        self.assertEqual(mod.SECRET_KEY, "from-the-env")

    def test_no_hard_coded_production_key_in_source(self):
        with open(os.path.join(os.path.dirname(live_settings.__file__), "settings.py")) as fh:
            self.assertNotIn("f9zh73k2z", fh.read())


class AllowedHostsTests(SimpleTestCase):
    def test_absent_key_keeps_accept_all(self):
        self.assertEqual(_load_settings(debug="True").ALLOWED_HOSTS, ["*"])

    def test_ini_comma_list(self):
        mod = _load_settings(debug="True", site_extra="ALLOWED_HOSTS: ziggy.ucolick.org, localhost,127.0.0.1,")
        self.assertEqual(mod.ALLOWED_HOSTS, ["ziggy.ucolick.org", "localhost", "127.0.0.1"])

    def test_env_wins_over_ini(self):
        mod = _load_settings(debug="True", site_extra="ALLOWED_HOSTS: ziggy.ucolick.org",
                             env={"DJANGO_ALLOWED_HOSTS": "example.org"})
        self.assertEqual(mod.ALLOWED_HOSTS, ["example.org"])

    def test_forwarded_host_still_honoured(self):
        self.assertTrue(_load_settings(debug="True").USE_X_FORWARDED_HOST)


class DatabaseAndTemplateTests(SimpleTestCase):
    def test_ssl_disabled_defaults_true(self):
        mod = _load_settings(debug="True")
        self.assertIs(mod.DATABASES["default"]["OPTIONS"]["ssl"]["ssl_disabled"], True)

    def test_ssl_disabled_configurable(self):
        mod = _load_settings(debug="True", database_extra="SSL_DISABLED: False")
        self.assertIs(mod.DATABASES["default"]["OPTIONS"]["ssl"]["ssl_disabled"], False)

    def test_context_processors_have_no_duplicates(self):
        processors = _load_settings(debug="True").TEMPLATES[0]["OPTIONS"]["context_processors"]
        self.assertEqual(len(processors), len(set(processors)))
        self.assertIn("django.template.context_processors.request", processors)

    def test_explorer_cap_from_ini_and_env(self):
        self.assertEqual(_load_settings(debug="True").EXPLORER_QUERY_MAX_EXECUTION_MS, 20000)
        self.assertEqual(
            _load_settings(debug="True", site_extra="EXPLORER_QUERY_MAX_EXECUTION_MS: 5000").EXPLORER_QUERY_MAX_EXECUTION_MS,
            5000,
        )
        self.assertEqual(
            _load_settings(debug="True", env={"YSE_EXPLORER_MAX_EXECUTION_MS": "700"}).EXPLORER_QUERY_MAX_EXECUTION_MS,
            700,
        )
