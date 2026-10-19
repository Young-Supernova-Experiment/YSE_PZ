"""Re-sync transients from TNS, and import TNS objects we lack (#438). Dry run by default.

Each transient is fetched from TNS with its photometry and spectra; only the differences are
applied (``YSE_App.services.tns_resync``): the TNS classification, the redshift when ours is
empty or from TNS, new photometry points and new spectra (public, with the TNS reference).

Resumable: with ``--apply`` every transient done is recorded as a ``tns`` ExternalServiceRun,
and transients re-synced within ``--skip-synced-days`` (7) are skipped, so re-running the
same command continues where it stopped. A dry run writes nothing (and so does not resume).

Examples::

  manage.py tns_resync --names 2024abc,2024abd            # what would change
  manage.py tns_resync --since 2025-01-01 --until 2025-04-01 --max 200 --apply
  manage.py tns_resync --status Ignore --max 500 --apply
  manage.py tns_resync --missing-only --since 2025-01-01 --apply   # gap import only
"""

from __future__ import annotations

import datetime
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from YSE_App.services import tns_resync
from YSE_App.sharing import tns

MAX_RATE_LIMIT_WAITS = 5


def _date(value):
    if not value:
        return None
    try:
        day = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise CommandError("dates are YYYY-MM-DD, not %r" % value) from exc
    return timezone.make_aware(day, datetime.timezone.utc)


def _split(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Command(BaseCommand):
    help = "Re-sync classifications, photometry and spectra from TNS; import missing TNS objects (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--names", default="", help="Comma-separated transient or TNS names.")
        parser.add_argument("--status", default="", help="Comma-separated transient statuses (e.g. Ignore,New).")
        parser.add_argument("--since", default="", help="YYYY-MM-DD: transients discovered on or after this day; "
                                                        "with --import-missing / --missing-only also the start "
                                                        "of the TNS public-timestamp window.")
        parser.add_argument("--until", default="", help="YYYY-MM-DD: transients discovered before this day.")
        parser.add_argument("--max", type=int, default=None, help="At most this many transients (and imports).")
        parser.add_argument("--never-synced", action="store_true", help="Only transients never re-synced.")
        parser.add_argument("--skip-synced-days", type=float, default=7.0,
                            help="Skip transients re-synced within this many days (0: none skipped). Default 7.")
        parser.add_argument("--import-missing", action="store_true",
                            help="Also import TNS objects made public since --since that we lack.")
        parser.add_argument("--missing-only", action="store_true",
                            help="Only the gap import (needs --since); no re-sync of existing transients.")
        parser.add_argument("--no-spectra", action="store_true", help="Do not download TNS spectra.")
        parser.add_argument("--sleep", type=float, default=None,
                            help="Seconds between TNS requests (default [sharing] TNS_REQUEST_INTERVAL_SECONDS).")

    def handle(self, *args, **opts):
        dry_run = not opts["apply"]
        since, until = _date(opts["since"]), _date(opts["until"])
        names = _split(opts["names"])
        if (opts["missing_only"] or (opts["import_missing"] and not names)) and since is None:
            raise CommandError("--missing-only and --import-missing need --since (or --names).")
        try:
            client = tns_resync.default_client()
            user = None if dry_run else tns_resync.service_user()
        except tns_resync.ResyncError as exc:
            raise CommandError(str(exc)) from exc
        throttle = tns_resync.Throttle(opts["sleep"])
        spectra = not opts["no_spectra"]
        counts = {}
        self.stdout.write("TNS re-sync (%s)" % ("dry run; nothing is written" if dry_run else "applying"))

        def report(result):
            counts[result.action] = counts.get(result.action, 0) + 1
            if result.changed or result.action in ("error", "not_found") or opts["verbosity"] > 1:
                self.stdout.write("  " + result.summary())

        def guarded(func, *a, **kw):
            for _attempt in range(MAX_RATE_LIMIT_WAITS):
                try:
                    return func(*a, **kw)
                except tns.TNSRateLimited as exc:
                    wait = min(int(exc.retry_after or 60), 900)
                    self.stdout.write("  TNS rate limit: waiting %ds" % wait)
                    time.sleep(wait)
                except tns.TNSTransportError as exc:
                    self.stdout.write("  TNS unreachable (%s): waiting 60s" % exc)
                    time.sleep(60)
            raise CommandError("TNS kept refusing requests; stopped. Re-run the same command to resume.")

        try:
            if names:
                # named objects we lack: imported with --import-missing, otherwise reported
                missing = tns_resync.missing_names(names)
                for name in missing:
                    if opts["import_missing"] or opts["missing_only"]:
                        report(guarded(tns_resync.resync_object, name, dry_run=dry_run, client=client,
                                       user=user, spectra=spectra, throttle=throttle))
                    else:
                        self.stdout.write("  %s: not in YSE-PZ (add --import-missing to import it)" % name)
            elif opts["missing_only"] or opts["import_missing"]:
                public = guarded(tns_resync.tns_names_since, client, since, throttle=throttle)
                missing = tns_resync.missing_names(public)
                self.stdout.write("Gap import: %d TNS objects public since %s, %d not in YSE-PZ"
                                  % (len(public), opts["since"], len(missing)))
                for name in missing[: opts["max"]] if opts["max"] else missing:
                    report(guarded(tns_resync.resync_object, name, dry_run=dry_run, client=client, user=user,
                                   spectra=spectra, throttle=throttle))
            if not opts["missing_only"]:
                qs = tns_resync.select_transients(
                    names=names, statuses=_split(opts["status"]), since=since, until=until,
                    never_synced=opts["never_synced"], skip_synced_days=opts["skip_synced_days"],
                    limit=opts["max"])
                transients = list(qs)
                self.stdout.write("Re-sync: %d transients" % len(transients))
                for transient in transients:
                    report(guarded(tns_resync.resync_object, transient, dry_run=dry_run, client=client,
                                   user=user, spectra=spectra, throttle=throttle, import_missing=False))
        except tns_resync.ResyncError as exc:
            raise CommandError(str(exc)) from exc

        total = sum(counts.values())
        detail = ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())) or "nothing to do"
        self.stdout.write("Done: %d objects (%s); %d TNS requests." % (total, detail, throttle.requests))
        if dry_run and total:
            self.stdout.write("Dry run: re-run with --apply to write.")
