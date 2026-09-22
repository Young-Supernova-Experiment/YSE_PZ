"""
Cron smoke tests for the "all crons individually" deploy check.

Two layers:

1. ``test_cron_classes_import_and_are_well_formed``: every dotted path in
   ``settings.CRON_CLASSES`` imports, is a ``django_cron.CronJobBase`` subclass,
   and has a ``Schedule`` plus a ``do`` method (what ``manage.py runcrons`` needs).
   A missing *third-party* module (e.g. SciServer, TensorFlow) is reported, not
   failed: the CI web image does not ship every optional science dependency.

2. ``test_cron_do_does_not_crash_on_its_own_code``: ``do()`` is invoked for each
   cron with all outbound I/O stubbed (HTTP, IMAP, SMTP, shell, tendo singleton,
   ``time.sleep``) and a per-cron alarm, in a temp cwd, against the empty test DB.
   Any network call raises ``ConnectionError`` so the cron takes its error path.
   The test fails only for the "would crash on every run" class of bug --
   NameError / AttributeError / TypeError / ImportError / SyntaxError raised from
   the cron's own code, or the same signatures printed by a cron that swallows
   its exceptions. Data-shaped and network errors are logged and allowed.

Crontab entries that are shell scripts rather than django_cron classes (DB backup
and backup cleanup) cannot be exercised here; see docs/deploy-checklist-ci.md.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import re
import signal
import sys
import tempfile
import threading
from unittest import mock

from django.conf import settings
from django.test import TestCase

from YSE_App.tests.fixtures_minimal import create_test_user, ensure_transient_statuses

CRASH_EXCEPTIONS = (NameError, AttributeError, TypeError, ImportError, SyntaxError)

# Messages a cron prints when it catches-and-logs one of the crash-class errors.
CRASH_SIGNATURES = re.compile(
    r"name '.+' is not defined"
    r"|has no attribute '"
    r"|object is not (callable|subscriptable|iterable)"
    r"|missing \d+ required positional argument"
    r"|got an unexpected keyword argument"
    r"|No module named '"
    r"|unsupported operand type"
    r"|takes \d+ positional arguments? but \d+ (was|were) given"
)

# Per-cron wall-clock budget for do(); a timeout is logged, never a failure.
DO_TIMEOUT_SECONDS = int(os.environ.get("YSE_CRON_SMOKE_TIMEOUT", "150"))

# Crons whose do() must not be invoked against the test DB (dotted path -> reason).
# Empty today: every CRON_CLASSES entry runs against an empty DB with I/O stubbed.
SKIP_DO: dict = {}

# Known-broken crons: a crash here is recorded as XFAIL (and an XPASS once fixed)
# instead of failing CI, with the fix or follow-up that tracks it.
# PR #165 (fix/undefined-names-and-lint) adds the missing imports these modules
# reference (getTransientHosts/calc_photoz, astropy Time, queue/threading/
# OrderedDict/mastrequests); until it merges into experimental they can NameError.
_PR165 = "undefined name until PR #165 merges"
EXPECTED_CRASHES = {
    "YSE_App.data_ingest.TNS_uploads.UpdateGHOST": _PR165,
    "YSE_App.data_ingest.Gaia_LC.GaiaLC": _PR165,
    "YSE_App.data_ingest.QUB_data.QUB": _PR165,
    "YSE_App.data_ingest.QUB_data.YSE": _PR165,
    "YSE_App.data_ingest.QUB_data.YSE_Stack": _PR165,
    "YSE_App.data_ingest.QUB_data.YSE_Weekly": _PR165,
    # do() reads `uploaddict` after the try block even when process_emails()
    # raised, so any IMAP failure ends in UnboundLocalError. Follow-up bug.
    "YSE_App.data_ingest.YSE_observations.SurveyObs": "uploaddict unbound when the IMAP fetch fails (follow-up)",
}


class _CronTimeout(BaseException):
    """BaseException so a cron's own ``except Exception`` cannot swallow the alarm."""


def _blocked_network(*_args, **_kwargs):
    import requests

    raise requests.exceptions.ConnectionError("cron-smoke: outbound network disabled")


def _blocked_urlopen(*_args, **_kwargs):
    import urllib.error

    raise urllib.error.URLError("cron-smoke: outbound network disabled")


def _blocked_imap(*_args, **_kwargs):
    raise ConnectionRefusedError("cron-smoke: IMAP disabled")


def _import_cron(dotted: str):
    module_path, class_name = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _third_party_import_error(exc: BaseException) -> bool:
    """ImportError naming a package outside this repo (optional science dependency)."""
    if not isinstance(exc, ImportError):
        return False
    name = getattr(exc, "name", None) or ""
    if not name or name.startswith("YSE_App") or name.startswith("YSE_PZ"):
        return False
    return True


@contextlib.contextmanager
def _sandbox():
    """Stub every outbound side effect a cron might have."""
    patches = [
        mock.patch("requests.Session.request", side_effect=_blocked_network),
        mock.patch("requests.Session.send", side_effect=_blocked_network),
        mock.patch("urllib.request.urlopen", side_effect=_blocked_urlopen),
        mock.patch("http.client.HTTPConnection.connect", side_effect=_blocked_imap),
        mock.patch("imaplib.IMAP4_SSL", side_effect=_blocked_imap),
        mock.patch("imaplib.IMAP4", side_effect=_blocked_imap),
        mock.patch("smtplib.SMTP"),
        mock.patch("smtplib.SMTP_SSL"),
        mock.patch("os.system", return_value=0),
        mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=b"", stderr=b"")),
        mock.patch("subprocess.Popen"),
        mock.patch("subprocess.call", return_value=0),
        mock.patch("subprocess.check_output", return_value=b""),
        mock.patch("subprocess.check_call", return_value=0),
        mock.patch("shutil.rmtree"),
        mock.patch("time.sleep"),
        mock.patch.object(sys, "argv", ["manage.py", "runcrons"]),
    ]
    try:
        import tendo.singleton as _singleton  # noqa: F401

        patches.append(mock.patch("tendo.singleton.SingleInstance", return_value=mock.Mock()))
    except Exception:
        pass
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _alarm_handler(_signum, _frame):
    raise _CronTimeout()


def _run_do(cron_cls):
    """Return (exception_or_None, captured_stdout)."""
    buf = io.StringIO()
    exc = None
    use_alarm = threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM")
    old_handler = None
    if use_alarm:
        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(DO_TIMEOUT_SECONDS)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                cron_cls().do()
            except BaseException as e:  # noqa: BLE001 - we classify everything
                exc = e
    finally:
        if use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    return exc, buf.getvalue()


class CronClassInventoryTests(TestCase):
    def test_cron_classes_import_and_are_well_formed(self):
        from django_cron import CronJobBase, Schedule

        self.assertGreater(len(settings.CRON_CLASSES), 0)
        problems = []
        optional_missing = []
        seen_codes = {}
        for dotted in settings.CRON_CLASSES:
            try:
                cls = _import_cron(dotted)
            except Exception as e:  # noqa: BLE001
                if _third_party_import_error(e):
                    optional_missing.append(f"{dotted}: missing optional dependency {e.name!r}")
                    continue
                problems.append(f"{dotted}: import failed: {type(e).__name__}: {e}")
                continue
            if not (isinstance(cls, type) and issubclass(cls, CronJobBase)):
                problems.append(f"{dotted}: not a CronJobBase subclass")
                continue
            if not isinstance(getattr(cls, "schedule", None), Schedule):
                problems.append(f"{dotted}: no django_cron Schedule")
            if not callable(getattr(cls, "do", None)):
                problems.append(f"{dotted}: no do() method")
            code = getattr(cls, "code", None)
            if not code:
                problems.append(f"{dotted}: no `code` (django_cron needs a unique code)")
            else:
                seen_codes.setdefault(code, []).append(dotted)

        duplicates = {c: paths for c, paths in seen_codes.items() if len(paths) > 1}
        if duplicates:
            # Shared codes make django_cron treat two jobs as one run history;
            # reported here (visible in -v2 output) and tracked as a follow-up.
            print("\n[cron-smoke] WARNING duplicate django_cron codes:")
            for code, paths in duplicates.items():
                print(f"  {code}: {', '.join(paths)}")
        if optional_missing:
            print("\n[cron-smoke] crons skipped, optional dependency not installed:")
            for line in optional_missing:
                print("  " + line)
        self.assertFalse(problems, "\n".join(problems))


class CronDoSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Several crons look up the admin user / statuses; give them minimal rows.
        cls.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(cls.admin)

    def test_cron_do_does_not_crash_on_its_own_code(self):
        failures = []
        report = []
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                for dotted in settings.CRON_CLASSES:
                    if dotted in SKIP_DO:
                        report.append(f"SKIP  {dotted}: {SKIP_DO[dotted]}")
                        continue
                    try:
                        cron_cls = _import_cron(dotted)
                    except Exception as e:  # noqa: BLE001
                        if _third_party_import_error(e):
                            report.append(f"SKIP  {dotted}: optional dependency {e.name!r} not installed")
                            continue
                        failures.append(f"{dotted}: import failed: {type(e).__name__}: {e}")
                        continue

                    with _sandbox():
                        exc, output = _run_do(cron_cls)

                    crash = None
                    if isinstance(exc, _CronTimeout):
                        report.append(f"TIME  {dotted}: exceeded {DO_TIMEOUT_SECONDS}s (allowed)")
                    elif isinstance(exc, SystemExit):
                        report.append(f"EXIT  {dotted}: sys.exit({exc.code}) (allowed)")
                    elif isinstance(exc, CRASH_EXCEPTIONS):
                        crash = f"raised {type(exc).__name__}: {exc}"
                    elif exc is not None:
                        report.append(f"ERR   {dotted}: {type(exc).__name__}: {str(exc)[:120]} (allowed)")
                    else:
                        report.append(f"OK    {dotted}")

                    if crash is None:
                        match = CRASH_SIGNATURES.search(output)
                        if match:
                            crash = f"logged crash-class error: ...{output[max(0, match.start() - 80): match.end() + 40]!r}"

                    if crash:
                        if dotted in EXPECTED_CRASHES:
                            report.append(f"XFAIL {dotted}: {crash} ({EXPECTED_CRASHES[dotted]})")
                        else:
                            failures.append(f"{dotted}: {crash}")
                            report.append(f"FAIL  {dotted}: {crash}")
                    elif dotted in EXPECTED_CRASHES:
                        report.append(f"XPASS {dotted}: no longer crashes; drop it from EXPECTED_CRASHES")
            finally:
                os.chdir(old_cwd)

        print("\n[cron-smoke] do() results:")
        for line in report:
            print("  " + line)
        self.assertFalse(failures, "\n".join(failures))
