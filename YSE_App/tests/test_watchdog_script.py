"""scripts/ops/yse-watchdog.sh (#769): run the real script with stub system commands.

Every external command the script calls (mysql, curl, systemctl, mail, ...) is a
small stub on PATH that records its arguments and answers from files the test
writes, so each branch runs exactly as it would on the server without touching
any service.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from django.conf import settings
from django.test import SimpleTestCase

SCRIPT = Path(settings.BASE_DIR) / "scripts" / "ops" / "yse-watchdog.sh"
DB_URL = "https://example.test/yse/healthz/"
STATIC_URL = "https://example.test/static/probe.css"

# Stubs read their answers from $STUB/<name>; after any systemctl call they
# switch to the "<name>_after" answer, which models the service recovering.
STUBS: Dict[str, str] = {
    "mysql": r"""#!/bin/bash
echo "mysql $*" >> "$STUB/calls"
pick() { if [[ -e "$STUB/acted" && -e "$STUB/$1_after" ]]; then cat "$STUB/$1_after"; else cat "$STUB/$1"; fi; }
[[ "$(pick db)" == ok ]] || exit 1
case "$*" in
  *"SELECT 1"*) echo 1 ;;
  *"information_schema.PROCESSLIST"*) cat "$STUB/kill_ids" 2>/dev/null ;;
esac
exit 0
""",
    "curl": r"""#!/bin/bash
url="${@: -1}"
echo "curl $url" >> "$STUB/calls"
pick() { if [[ -e "$STUB/acted" && -e "$STUB/$1_after" ]]; then cat "$STUB/$1_after"; else cat "$STUB/$1"; fi; }
if [[ "$url" == *healthz* ]]; then printf '%s' "$(pick web_db)"; else printf '%s' "$(pick web_static)"; fi
""",
    "systemctl": r"""#!/bin/bash
echo "systemctl $*" >> "$STUB/calls"
touch "$STUB/acted"
exit "$(cat "$STUB/systemctl_rc" 2>/dev/null || echo 0)"
""",
    "mail": r"""#!/bin/bash
{ echo "SUBJECT: $2"; echo "TO: $3"; cat; echo "---"; } >> "$STUB/mail"
""",
    # Run the wrapped command without a time limit; drop the duration argument.
    "timeout": "#!/bin/bash\nshift\nexec \"$@\"\n",
    "flock": "#!/bin/bash\nexit 0\n",
    "logger": "#!/bin/bash\nexit 0\n",
    "journalctl": "#!/bin/bash\necho journal\n",
    "dmesg": "#!/bin/bash\necho dmesg\n",
    "hostname": "#!/bin/bash\necho testhost\n",
}


class WatchdogScriptTests(SimpleTestCase):
    """Each test runs the script once (or twice) in a private temp directory."""

    def setUp(self) -> None:
        """Create stubs, a state directory and a config file; default to all healthy."""
        if shutil.which("bash") is None:
            self.skipTest("bash is not installed")
        self.tmp = Path(tempfile.mkdtemp(prefix="yse-watchdog-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stub = self.tmp / "stub"
        self.bin = self.tmp / "bin"
        self.state = self.tmp / "state"
        for d in (self.stub, self.bin):
            d.mkdir()
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.answers(db="ok", web_db="200", web_static="200")
        self.conf_values: Dict[str, str] = {
            "DB_HEALTH_URL": DB_URL,
            "STATIC_URL": STATIC_URL,
            "MYCNF": str(self.tmp / "my.cnf"),
            "STATE": str(self.state),
            "NOTIFY": "ops@example.test",
            "POST_ACTION_WAIT": "0",
            "MYSQL_SETTLE": "0",
        }

    # -- helpers -------------------------------------------------------------
    def answers(self, **values: str) -> None:
        """Write stub answers, e.g. db="fail", web_db_after="200".

        Parameters:
            values: stub file name -> content.
        """
        for key, value in values.items():
            (self.stub / key).write_text(value)

    def run_script(self, **conf: str) -> subprocess.CompletedProcess:
        """Write the config file and run the watchdog once.

        Parameters:
            conf: extra or overriding config keys for this run.

        Returns:
            The finished process (stdout holds the script's log lines).
        """
        values = dict(self.conf_values, **conf)
        conf_file = self.tmp / "watchdog.conf"
        conf_file.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB": str(self.stub),
            "YSE_WATCHDOG_CONF": str(conf_file),
            "HOME": str(self.tmp),
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def calls(self, prefix: str = "") -> List[str]:
        """Return recorded stub calls, optionally only those starting with prefix."""
        path = self.stub / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def mail(self) -> str:
        """Return everything the mail stub received ('' if nothing was sent)."""
        path = self.stub / "mail"
        return path.read_text() if path.exists() else ""

    def state_file(self, name: str) -> Optional[str]:
        """Return a state file's stripped content, or None if it does not exist."""
        path = self.state / name
        return path.read_text().strip() if path.exists() else None

    def fail_until_action(self, **conf: str) -> subprocess.CompletedProcess:
        """Run twice without acting (below FAILS_NEEDED=3), then the acting run."""
        for _ in range(2):
            result = self.run_script(**conf)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.calls("systemctl"), [])
        return self.run_script(**conf)

    # -- tests ---------------------------------------------------------------
    def test_healthy_resets_counter_and_does_nothing(self) -> None:
        (self.state).mkdir()
        (self.state / "fails").write_text("2\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_file("fails"), "0")
        self.assertEqual(self.calls("systemctl"), [])
        self.assertEqual(self.mail(), "")
        self.assertIn(f"curl {DB_URL}", self.calls("curl"))

    def test_single_failure_only_counts(self) -> None:
        self.answers(web_db="000")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.state_file("fails"), "1")
        self.assertIn("unhealthy: db_direct=ok web_db=000 web_static=200 consecutive=1", result.stdout)
        self.assertEqual(self.calls("systemctl"), [])

    def test_stuck_django_gets_graceful_apache_reload(self) -> None:
        self.answers(web_db="000", web_db_after="200")
        result = self.fail_until_action()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls("systemctl"), ["systemctl reload apache2"])
        self.assertIn("SUBJECT: YSE-PZ watchdog: recovered after reload apache2", self.mail())
        self.assertEqual(self.state_file("fails"), "0")
        self.assertEqual(len(self.state_file("history").splitlines()), 1)
        snaps = list((self.state / "snapshots").iterdir())
        self.assertEqual(len(snaps), 1)
        for name in ("status.txt", "processlist.txt", "innodb.txt", "mysql-status.txt", "df.txt"):
            self.assertTrue((snaps[0] / name).exists(), name)

    def test_mysql_down_restarts_mysql_then_reloads_apache(self) -> None:
        self.answers(db="fail", db_after="ok", web_db="503", web_db_after="200")
        result = self.fail_until_action()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls("systemctl"), ["systemctl restart mysql", "systemctl reload apache2"])
        self.assertIn("recovered after restart mysql reload apache2", self.mail())

    def test_static_failure_means_full_apache_restart(self) -> None:
        self.answers(web_db="000", web_static="000", web_db_after="200", web_static_after="200")
        result = self.fail_until_action()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls("systemctl"), ["systemctl restart apache2"])

    def test_failed_mysql_restart_stops_and_reports(self) -> None:
        self.answers(db="fail", web_db="503", systemctl_rc="1")
        result = self.fail_until_action()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls("systemctl"), ["systemctl restart mysql"])
        self.assertIn("SUBJECT: YSE-PZ watchdog: MySQL restart FAILED", self.mail())

    def test_still_unhealthy_after_action_exits_1(self) -> None:
        self.answers(web_db="000")
        result = self.fail_until_action()
        self.assertEqual(result.returncode, 1)
        self.assertIn("STILL UNHEALTHY after reload apache2", self.mail())

    def test_cooldown_blocks_a_second_action(self) -> None:
        self.state.mkdir()
        (self.state / "fails").write_text("5\n")
        (self.state / "last_action").write_text(f"{int(time.time()) - 60}\n")
        self.answers(web_db="000")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertIn("in cooldown", result.stdout)
        self.assertEqual(self.calls("systemctl"), [])

    def test_hourly_cap_stops_and_mails_once(self) -> None:
        self.state.mkdir()
        now = int(time.time())
        (self.state / "fails").write_text("5\n")
        (self.state / "last_action").write_text(f"{now - 1000}\n")
        (self.state / "history").write_text("".join(f"{now - s}\n" for s in (1000, 1500, 2000)))
        self.answers(web_db="000")
        first = self.run_script()
        second = self.run_script()
        self.assertEqual((first.returncode, second.returncode), (1, 1))
        self.assertEqual(self.calls("systemctl"), [])
        self.assertEqual(self.mail().count("SUBJECT: YSE-PZ watchdog: manual attention needed"), 1)

    def test_old_history_does_not_count_toward_the_cap(self) -> None:
        self.state.mkdir()
        now = int(time.time())
        (self.state / "fails").write_text("5\n")
        (self.state / "history").write_text("".join(f"{now - s}\n" for s in (4000, 5000, 6000)))
        self.answers(web_db="000", web_db_after="200")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls("systemctl"), ["systemctl reload apache2"])

    def test_dry_run_changes_nothing_but_reports(self) -> None:
        self.answers(db="fail", web_db="000")
        result = self.fail_until_action(DRY_RUN="1")
        self.assertEqual(self.calls("systemctl"), [])
        self.assertIn("dry run: would run: timeout 180 systemctl restart mysql", result.stdout)
        self.assertIn("SUBJECT: [dry run] YSE-PZ watchdog:", self.mail())

    def test_kills_long_queries_from_listed_users_before_reload(self) -> None:
        self.answers(web_db="000", web_db_after="200", kill_ids="12\n34\n")
        result = self.fail_until_action(KILL_QUERY_SECONDS="120", KILL_QUERY_USERS="explorer django")
        self.assertEqual(result.returncode, 0, result.stdout)
        mysql_calls = self.calls("mysql")
        select = [c for c in mysql_calls if "information_schema.PROCESSLIST" in c]
        self.assertEqual(len(select), 1)
        self.assertIn("time > 120 AND user IN ('explorer','django')", select[0])
        self.assertIn("KILL QUERY 12", " ".join(mysql_calls))
        self.assertIn("KILL QUERY 34", " ".join(mysql_calls))
        self.assertEqual(self.calls("systemctl"), ["systemctl reload apache2"])

    def test_no_kill_when_disabled(self) -> None:
        self.answers(web_db="000", web_db_after="200", kill_ids="12\n")
        self.fail_until_action()
        self.assertNotIn("KILL QUERY", " ".join(self.calls("mysql")))

    def test_missing_url_is_a_config_error(self) -> None:
        result = self.run_script(DB_HEALTH_URL="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("DB_HEALTH_URL is empty", result.stdout)

    def test_non_numeric_setting_is_a_config_error(self) -> None:
        result = self.run_script(COOLDOWN="15m")
        self.assertEqual(result.returncode, 2)

    def test_old_snapshots_are_pruned(self) -> None:
        old = self.state / "snapshots" / "1000"
        old.mkdir(parents=True)
        stamp = time.time() - 40 * 86400
        os.utime(old, (stamp, stamp))
        recent = self.state / "snapshots" / "2000"
        recent.mkdir()
        self.run_script()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
