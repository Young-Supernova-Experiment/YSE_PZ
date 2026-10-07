"""Run queued background jobs (issue #263).

One pass (default; crontab-friendly)::

    python manage.py run_jobs

Long-running worker (systemd unit in docs/background-jobs.md)::

    python manage.py run_jobs --loop --sleep 5

A pass claims due jobs one at a time (``SELECT ... FOR UPDATE SKIP LOCKED`` on
MySQL 8, conditional UPDATE elsewhere), runs each handler and records the
result or the traceback; a failing job is retried with exponential backoff
until its ``max_attempts``. Several runners (cron pass + loop) may run at once.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from YSE_App.jobs import autodiscover, queue_counts, registered_kinds, run_forever, run_pass
from YSE_App.jobs.runner import default_worker_id


class Command(BaseCommand):
    help = "Run due background jobs once (default) or as a loop (--loop)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running passes until SIGTERM/SIGINT.")
        parser.add_argument("--sleep", type=float, default=5.0, metavar="N",
                            help="Seconds to sleep between passes that found nothing (--loop; default 5).")
        parser.add_argument("--limit", type=int, default=None, metavar="N",
                            help="Max jobs per pass (default JOB_RUNNER_PASS_LIMIT).")
        parser.add_argument("--budget", type=float, default=None, metavar="SECONDS",
                            help="Stop a single pass after this many seconds (default: no budget).")
        parser.add_argument("--kind", action="append", default=None, metavar="KIND",
                            help="Only run jobs of this kind (repeatable).")
        parser.add_argument("--worker-id", default=None, help="Label written to Job.locked_by (default host:pid).")
        parser.add_argument("--max-passes", type=int, default=None, metavar="N",
                            help="With --loop: exit after N passes (testing).")
        parser.add_argument("--status", action="store_true", help="Print queue counts and registered kinds, run nothing.")

    def handle(self, *args, **options):
        autodiscover()
        if options["status"]:
            self.stdout.write(json.dumps({"counts": queue_counts(), "kinds": registered_kinds()}, indent=2))
            return
        worker_id = options["worker_id"] or default_worker_id()
        kinds = options["kind"]
        if options["loop"]:
            if options["sleep"] < 0:
                raise CommandError("--sleep must be >= 0")
            self.stdout.write("run_jobs: loop started as %s (sleep %.1fs)" % (worker_id, options["sleep"]))
            passes = run_forever(sleep_seconds=options["sleep"], worker_id=worker_id, kinds=kinds,
                                 limit=options["limit"], max_passes=options["max_passes"],
                                 log=lambda msg: self.stdout.write("run_jobs: " + msg))
            self.stdout.write("run_jobs: loop stopped after %d pass(es)" % passes)
            return
        result = run_pass(limit=options["limit"], budget_seconds=options["budget"],
                          worker_id=worker_id, kinds=kinds)
        self.stdout.write("run_jobs: " + result.summary())
        for job in result.jobs:
            line = "  #%s %s -> %s" % (job.pk, job.kind, job.status)
            if job.error:
                line += ": " + job.error.strip().splitlines()[-1][:200]
            self.stdout.write(line)
