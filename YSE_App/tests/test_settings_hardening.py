"""settings.py: SECRET_KEY, ALLOWED_HOSTS, DB TLS and the cache backend come from env / settings.ini."""

import base64
import importlib
import os
import sys
import tempfile
import textwrap
import types
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


# With IS_DEBUG False a CREDENTIALS_KEY is required too (#264); tests of other
# production-only settings append this to site_extra.
_CREDENTIALS_SECTION = "\n[secrets]\ncredentials_key: " + base64.urlsafe_b64encode(b"0" * 32).decode()


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
                     if k not in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "YSE_EXPLORER_MAX_EXECUTION_MS",
                                  "REDIS_URL")}
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
        mod = _load_settings(debug="False", site_extra="SECRET_KEY: from-the-ini-file" + _CREDENTIALS_SECTION)
        self.assertEqual(mod.SECRET_KEY, "from-the-ini-file")

    def test_env_wins_over_ini(self):
        mod = _load_settings(debug="False", site_extra="SECRET_KEY: from-the-ini-file" + _CREDENTIALS_SECTION,
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

    def test_explorer_cap_off_by_default_and_from_ini_and_env(self):
        self.assertEqual(_load_settings(debug="True").EXPLORER_QUERY_MAX_EXECUTION_MS, 0)
        self.assertEqual(
            _load_settings(debug="True", site_extra="EXPLORER_QUERY_MAX_EXECUTION_MS: 5000").EXPLORER_QUERY_MAX_EXECUTION_MS,
            5000,
        )
        self.assertEqual(
            _load_settings(debug="True", env={"YSE_EXPLORER_MAX_EXECUTION_MS": "700"}).EXPLORER_QUERY_MAX_EXECUTION_MS,
            700,
        )


class CacheBackendTests(SimpleTestCase):
    """REDIS_URL must never select a backend the installed Django cannot import (#338)."""

    _LOCMEM = "django.core.cache.backends.locmem.LocMemCache"
    _DJANGO_REDIS = "django_redis.cache.RedisCache"
    _URL = "redis://127.0.0.1:6379/9"

    def test_without_redis_url_uses_locmem(self):
        cache = _load_settings(debug="True").CACHES["default"]
        self.assertEqual(cache["BACKEND"], self._LOCMEM)

    def test_redis_url_with_django_redis_installed(self):
        fake = types.ModuleType("django_redis")
        with mock.patch.dict(sys.modules, {"django_redis": fake}):
            cache = _load_settings(debug="True", env={"REDIS_URL": self._URL}).CACHES["default"]
        self.assertEqual(cache["BACKEND"], self._DJANGO_REDIS)
        self.assertEqual(cache["LOCATION"], self._URL)

    def test_redis_url_without_django_redis_warns_and_falls_back(self):
        # sys.modules[name] = None makes `import name` raise ImportError.
        with mock.patch.dict(sys.modules, {"django_redis": None, "redis": None}):
            with self.assertLogs("yse_settings_under_test", level="WARNING") as logs:
                cache = _load_settings(debug="True", env={"REDIS_URL": self._URL}).CACHES["default"]
        self.assertEqual(cache["BACKEND"], self._LOCMEM)
        self.assertEqual(cache["LOCATION"], "yse-default")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("REDIS_URL is set but no Redis cache backend is importable", logs.output[0])
        self.assertIn("django-redis", logs.output[0])

    def test_never_selects_django4_builtin_backend_on_django3(self):
        import django

        if django.VERSION >= (4, 0):
            self.skipTest("built-in Redis backend exists on this Django")
        fake_redis = types.ModuleType("redis")
        with mock.patch.dict(sys.modules, {"django_redis": None, "redis": fake_redis}):
            with self.assertLogs("yse_settings_under_test", level="WARNING"):
                cache = _load_settings(debug="True", env={"REDIS_URL": self._URL}).CACHES["default"]
        self.assertEqual(cache["BACKEND"], self._LOCMEM)
