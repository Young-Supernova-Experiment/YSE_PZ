"""
Cron smoke tests for the "all crons individually" deploy check.

Two layers:

1. ``test_cron_classes_import_and_are_well_formed``: every dotted path in
   ``settings.CRON_CLASSES`` imports, is a ``django_cron.CronJobBase`` subclass,
   and has a ``Schedule`` plus a ``do`` method (what ``manage.py runcrons`` needs).
   An import that fails for *environment* reasons is reported, not failed: a
   missing or broken third-party package (SciServer; TensorFlow whose protobuf
   pin is incompatible in the web image), a network fetch at import time
   (astro_ghost), or a DB row read at import. Only a crash-class exception
   raised by the cron module's own code (or an ImportError naming a repo
   module) fails the inventory. See ``_import_failure_reason``.

2. ``test_cron_do_does_not_crash_on_its_own_code``: ``do()`` is invoked for each
   cron with all outbound I/O stubbed (HTTP, IMAP, SMTP, shell, tendo singleton,
   ``time.sleep``, the SFD dust map) and a per-cron alarm, in a temp cwd, against
   the empty test DB. Any network call raises ``ConnectionError`` so the cron
   takes its error path.
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
import traceback
from typing import Optional
from unittest import mock

from django.conf import settings
from django.test import TestCase

import YSE_App
from YSE_App.tests.fixtures_minimal import create_test_user, ensure_transient_statuses

# Repo checkout root (parent of the YSE_App package): frames under it are "our code".
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(YSE_App.__file__)))
_IMPORT_STMT_RE = re.compile(r"^\s*(from\s+\S+\s+import\b|import\s)")

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
# The undefined-name crashes in TNS_uploads/Gaia_LC/QUB_data were fixed by PR #165,
# which is now on experimental, so those entries are gone.
EXPECTED_CRASHES = {
    # do() reads `uploaddict` after the try block even when process_emails()
    # raised, so any IMAP failure ends in UnboundLocalError. Follow-up bug.
    "YSE_App.data_ingest.YSE_observations.SurveyObs": "uploaddict unbound when the IMAP fetch fails (follow-up)",
    # TNS_uploads.search()/get() swallow any requests exception and return
    # `[None, 'Error message ...']` instead of a Response; GetRecentEvents()
    # then reads `response.status_code` (TNS_uploads.py:825; same pattern at
    # 842/863/878/1069) -> AttributeError: 'list' object has no attribute
    # 'status_code'. TNS_recent/TNS_updates wrap the call in try/except and
    # email the error; TNS_recent_realtime.do() has that try/except commented
    # out, so every TNS outage crashes this cron. Follow-up bug.
    "YSE_App.data_ingest.TNS_uploads.TNS_recent_realtime": (
        "search()/get() return a list on error and GetRecentEvents reads .status_code (follow-up)"
    ),
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


def _is_repo_file(filename: str) -> bool:
    if not filename:
        return False
    path = os.path.abspath(filename)
    if "site-packages" in path or "dist-packages" in path:
        return False
    return path.startswith(_REPO_ROOT + os.sep)


def _import_failure_reason(exc: BaseException) -> Optional[str]:
    """
    Classify a failed import of a CRON_CLASSES entry.

    Returns a short reason when the failure is *environmental* -- the cron is then
    reported as skipped -- or ``None`` when it is the cron module's own bug and
    must fail the test.

    Environmental: an ImportError naming a package outside this repo (optional
    science dependency); any non-crash-class exception (dust map data missing,
    a network fetch or a DB lookup at import time); a SyntaxError in a
    dependency's file; a crash-class exception raised beneath an ``import``
    statement in repo code (a dependency such as TensorFlow/protobuf that is
    installed but broken in this image) or with no repo frame at all.

    Own bug: an ImportError naming a YSE_App/YSE_PZ module, a SyntaxError in a
    repo file, or a crash-class exception whose innermost repo frame is ordinary
    module-level code.
    """
    text = str(exc).strip()
    label = f"{type(exc).__name__}: {text.splitlines()[0] if text else ''}"
    if isinstance(exc, ImportError):
        name = getattr(exc, "name", None) or ""
        if name.startswith("YSE_App") or name.startswith("YSE_PZ"):
            return None
        if name:
            return f"missing optional dependency {name!r}"
    if not isinstance(exc, CRASH_EXCEPTIONS):
        return f"import needs data/network/DB not available here: {label}"
    if isinstance(exc, SyntaxError):
        # The broken file is not on the traceback; SyntaxError names it itself.
        return None if _is_repo_file(getattr(exc, "filename", "") or "") else f"raised inside a dependency: {label}"
    repo_frames = [f for f in traceback.extract_tb(exc.__traceback__) if _is_repo_file(f.filename)]
    if not repo_frames:
        return f"raised inside a dependency: {label}"
    if _IMPORT_STMT_RE.match(repo_frames[-1].line or ""):
        return f"dependency failed to import: {label}"
    return None


def _load_cron(dotted: str):
    """Return ``(cls, skip_reason, failure)``; exactly one of the three is set."""
    try:
        with _sandbox():
            return _import_cron(dotted), None, None
    except Exception as e:  # noqa: BLE001 - classified below
        reason = _import_failure_reason(e)
        if reason is not None:
            return None, reason, None
        return None, None, f"{dotted}: import failed: {type(e).__name__}: {e}"


class _FakeSFDQuery:
    """Stand-in for ``dustmaps.sfd.SFDQuery``: the web image ships no SFD map data.

    QUB_data / Query_ZTF / DECam_upload build one at import time and only ever
    call it as ``sfd(skycoord) * 0.86`` on a single coordinate, so a zero E(B-V)
    keeps them on their normal path.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return 0.0


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
    try:
        import dustmaps.sfd as _sfd  # noqa: F401

        patches.append(mock.patch("dustmaps.sfd.SFDQuery", _FakeSFDQuery))
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
    @classmethod
    def setUpTestData(cls):
        # PS1_cutouts does `User.objects.get(username='admin')` at import time.
        cls.admin = create_test_user("admin", is_staff=True, is_superuser=True)

    def test_cron_classes_import_and_are_well_formed(self):
        from django_cron import CronJobBase, Schedule

        self.assertGreater(len(settings.CRON_CLASSES), 0)
        problems = []
        unavailable = []
        seen_codes = {}
        for dotted in settings.CRON_CLASSES:
            cls, skip_reason, failure = _load_cron(dotted)
            if failure:
                problems.append(failure)
                continue
            if skip_reason:
                unavailable.append(f"{dotted}: {skip_reason}")
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
        if unavailable:
            print("\n[cron-smoke] crons not importable in this environment (skipped):")
            for line in unavailable:
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
                    cron_cls, skip_reason, failure = _load_cron(dotted)
                    if failure:
                        failures.append(failure)
                        report.append(f"FAIL  {failure}")
                        continue
                    if skip_reason:
                        report.append(f"SKIP  {dotted}: {skip_reason}")
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
