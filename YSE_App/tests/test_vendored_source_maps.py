"""Vendored Bootstrap/AdminLTE bundles must not point at source maps we do not ship (#232)."""

import os

from django.test import SimpleTestCase

import YSE_App

_VENDOR = os.path.join(os.path.dirname(YSE_App.__file__), "static", "YSE_App", "vendor")
_ASSETS = [
    "bootstrap-4.6/css/bootstrap.min.css",
    "bootstrap-4.6/js/bootstrap.bundle.min.js",
    "adminlte-3.2/css/adminlte.min.css",
    "adminlte-3.2/js/adminlte.min.js",
]


class VendoredSourceMapTests(SimpleTestCase):
    def test_no_dangling_source_map_references(self):
        for rel in _ASSETS:
            path = os.path.join(_VENDOR, rel)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            if "sourceMappingURL" in content:
                map_name = content.rsplit("sourceMappingURL=", 1)[1].split()[0].rstrip("*/")
                self.assertTrue(
                    os.path.exists(os.path.join(os.path.dirname(path), map_name)),
                    "%s references %s, which is not shipped" % (rel, map_name),
                )
