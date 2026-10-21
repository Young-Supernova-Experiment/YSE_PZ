"""Cron overlap guards return instead of calling sys.exit (#757).

``manage.py runcrons`` runs every class in ``CRON_CLASSES`` in one process, so a
``SystemExit`` from one ``do()`` would skip every cron after it.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from unittest import mock

from django.test import SimpleTestCase

DATA_INGEST = pathlib.Path(__file__).resolve().parents[1] / "data_ingest"


def _exit_calls_in_cron_do(path):
    """``sys.exit`` / ``exit`` / ``quit`` calls inside a ``do()`` method of a class in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for fn in cls.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "do"):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "exit"
                        and isinstance(f.value, ast.Name) and f.value.id == "sys") or \
                        (isinstance(f, ast.Name) and f.id in ("exit", "quit")):
                    found.append(f"{path.name}:{node.lineno} {cls.name}.do")
    return found


class NoExitInCronDoTests(SimpleTestCase):
    def test_no_cron_do_calls_sys_exit(self):
        found = []
        for path in sorted(DATA_INGEST.glob("*.py")):
            found += _exit_calls_in_cron_do(path)
        self.assertEqual(found, [])


class TNSCronGuardTests(SimpleTestCase):
    """A second instance of a TNS cron returns, sends the alert and does no work."""

    def test_guard_returns_when_another_instance_runs(self):
        from tendo import singleton

        from YSE_App.data_ingest import TNS_uploads

        for name in ("TNS_emails", "TNS_updates", "TNS_Ignore_updates", "TNS_recent"):
            with self.subTest(cron=name), \
                    mock.patch.object(sys, "argv", ["manage.py", "runcrons"]), \
                    mock.patch.object(TNS_uploads.singleton, "SingleInstance",
                                      side_effect=singleton.SingleInstanceException()), \
                    mock.patch.object(TNS_uploads, "sendemail") as sendemail, \
                    mock.patch.object(TNS_uploads.processTNS, "GetAndUploadAllData") as work, \
                    mock.patch.object(TNS_uploads.processTNS, "UpdateFromTNS") as update, \
                    mock.patch.object(TNS_uploads.processTNS, "GetRecentEvents") as recent, \
                    mock.patch.object(TNS_uploads.processTNS, "ProcessTNSEmails") as emails:
                result = getattr(TNS_uploads, name)().do()
                self.assertIn("skipped", result)
                sendemail.assert_called_once()
                for m in (work, update, recent, emails):
                    m.assert_not_called()
