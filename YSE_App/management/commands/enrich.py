"""Run any enrichment service over a selection (#439). Dry run by default.

Lists (dry run) or runs, per object, one registered enrichment service
(``YSE_App.services.enrichment``). Resumable: with ``--apply`` every attempt is recorded as an
``ExternalServiceRun``, and objects that are not due (run recently, or failed too recently or too
often) are skipped, so re-running the same command continues where it stopped. ``--rerun``
ignores that and runs every selected object again.

Examples::

  manage.py enrich --list
  manage.py enrich --service prost --max 20                       # what would run
  manage.py enrich --service prost --since 2026-01-01 --max 200 --apply
  manage.py enrich --service ps1-cutouts --retry-failed --apply
  manage.py enrich --service blast --names 2026abc,2026abd --apply
  manage.py enrich --service sdss-photoz --max 500 --queue --batch-size 100
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from YSE_App.services import enrichment


def _split(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _date(value, option):
    if not value:
        return None
    try:
        day = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise CommandError("%s takes YYYY-MM-DD" % option) from exc
    return timezone.make_aware(day, datetime.timezone.utc)


class Command(BaseCommand):
    help = "Run an enrichment service over a selection of transients or hosts (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="List the registered services and exit.")
        parser.add_argument("--service", default="", help="Registry name (see --list).")
        parser.add_argument("--apply", action="store_true", help="Run and record (default: list only).")
        parser.add_argument("--names", default="", help="Comma-separated transient names (or their hosts).")
        parser.add_argument("--ids", default="", help="Comma-separated object ids (Transient or Host, per service).")
        parser.add_argument("--status", default="", help="Comma-separated transient statuses.")
        parser.add_argument("--since", default="", help="Discovered on or after YYYY-MM-DD.")
        parser.add_argument("--until", default="", help="Discovered before YYYY-MM-DD.")
        parser.add_argument("--all-candidates", action="store_true",
                            help="Also objects that already have the service's data (default: missing only).")
        parser.add_argument("--retry-failed", action="store_true", help="Only objects whose last run failed.")
        parser.add_argument("--never-run", action="store_true", help="Only objects never run.")
        parser.add_argument("--rerun", action="store_true", help="Ignore the run log's cadence and retry policy.")
        parser.add_argument("--max", type=int, default=None, help="At most this many objects.")
        parser.add_argument("--queue", action="store_true",
                            help="With --apply: queue enrich.objects jobs instead of running here.")
        parser.add_argument("--batch-size", type=int, default=50, help="Objects per queued job (default 50).")

    def handle(self, *args, **opts):
        if opts["list"]:
            for name in enrichment.names():
                s = enrichment.get(name)
                flag = (" [needs %s]" % s.enabled_setting) if s.enabled_setting and not s.is_enabled() else ""
                self.stdout.write("%-12s %-5s %s%s" % (name, s.target, s.title, flag))
            return
        if not opts["service"]:
            raise CommandError("--service is required (see --list)")
        try:
            service = enrichment.get(opts["service"])
        except enrichment.EnrichmentError as exc:
            raise CommandError(str(exc)) from exc
        try:
            ids = [int(i) for i in _split(opts["ids"])]
        except ValueError as exc:
            raise CommandError("--ids takes numbers") from exc
        selection = dict(missing_only=not opts["all_candidates"], names=_split(opts["names"]), ids=ids,
                         statuses=_split(opts["status"]), since=_date(opts["since"], "--since"),
                         until=_date(opts["until"], "--until"), retry_failed=opts["retry_failed"],
                         never_run=opts["never_run"], rerun=opts["rerun"])
        narrowed = any((selection["names"], ids, selection["statuses"], selection["since"], selection["until"]))
        if service.needs_selection and not narrowed:
            raise CommandError("%s needs --names, --ids, --status, --since or --until" % service.name)
        qs = enrichment.select(service, **selection)
        if opts["max"]:
            qs = qs[: opts["max"]]
        objs = list(qs)
        dry_run = not opts["apply"]
        self.stdout.write("%s %s: %d %s(s)" % ("Dry run" if dry_run else "Applying", service.name, len(objs),
                                               service.target))
        if dry_run:
            for obj in objs[:200]:
                self.stdout.write("  " + enrichment._label(obj))
            if len(objs) > 200:
                self.stdout.write("  ... and %d more" % (len(objs) - 200))
            return
        if not service.is_enabled():
            raise CommandError("%s is off: set %s" % (service.name, service.enabled_setting))
        if opts["queue"]:
            size = max(1, int(opts["batch_size"] or 50))
            jobs = 0
            for start in range(0, len(objs), size):
                if enrichment.queue_objects(service.name, [o.pk for o in objs[start:start + size]]) is not None:
                    jobs += 1
            self.stdout.write("Queued %d enrich.objects job(s)" % jobs)
            return
        try:
            summary = enrichment.run(service, objs, dry_run=False)
        except enrichment.RateLimited as exc:
            raise CommandError("rate limited (%s); objects done so far are recorded, run again later" % exc)
        except enrichment.EnrichmentError as exc:
            raise CommandError(str(exc)) from exc
        for line in summary.lines:
            self.stdout.write(line)
        self.stdout.write("Done: %s" % (", ".join("%s %d" % kv for kv in sorted(summary.counts.items()))
                                        or "nothing to do"))
