"""Delete old finder-chart files (#484).

    python manage.py prune_finder_charts --older-than-days 90            # dry run (default)
    python manage.py prune_finder_charts --older-than-days 90 --execute  # really delete

Charts live under view_utils.finder_chart_base_dir() (one directory per transient).
A chart is regenerated on demand the next time it is requested. The default is a
dry run; --execute is required to delete anything.
"""

from __future__ import annotations

import os
import shutil
import time

from django.core.management.base import BaseCommand, CommandError

from YSE_App import view_utils


class Command(BaseCommand):
    help = "Delete per-transient finder chart directories whose files are older than N days (dry run unless --execute)."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-days", type=int, required=True,
                            help="Prune charts whose newest file is older than this many days (>= 1).")
        parser.add_argument("--execute", action="store_true", help="Actually delete (default is a dry run).")

    def handle(self, *args, **options):
        days = options["older_than_days"]
        if days < 1:
            raise CommandError("--older-than-days must be at least 1")
        base = view_utils.finder_chart_base_dir()
        if not os.path.isdir(base):
            self.stdout.write("no finder chart directory at %s; nothing to do" % base)
            return
        cutoff = time.time() - days * 86400
        count = 0
        freed = 0
        for entry in sorted(os.scandir(base), key=lambda e: e.name):
            if not entry.is_dir(follow_symlinks=False):
                continue
            files = [os.path.join(r, f) for r, _, fs in os.walk(entry.path) for f in fs]
            if not files:
                continue
            newest = max(os.path.getmtime(f) for f in files)
            if newest >= cutoff:
                continue
            size = sum(os.path.getsize(f) for f in files)
            count += 1
            freed += size
            if options["execute"]:
                shutil.rmtree(entry.path)
        verb = "deleted" if options["execute"] else "would delete (dry run; add --execute)"
        self.stdout.write("%s %d finder chart directories, %.1f MB" % (verb, count, freed / 1e6))
