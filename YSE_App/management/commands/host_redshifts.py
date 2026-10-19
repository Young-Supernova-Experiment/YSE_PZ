"""Backfill host redshifts from NED and DESI (#436, #437). Dry run by default.

Each host linked to a transient is checked at its position against NED (NED::API) and
DESI (Astro Data Lab), highest rank first, and the match is applied under the one
precedence list (``YSE_App.services.redshift_precedence``): never over an override or a
higher-ranked value, always with its source. A dry run prints what would change.

Resumable and idempotent: with ``--apply`` every check is recorded as a ``ned-redshift`` or
``desi-redshift`` ExternalServiceRun; hosts checked within ``--skip-checked-days`` (30) are skipped,
so re-running the same command continues where it stopped, and a host whose value is
already the catalogue's is reported ``unchanged``.

Examples::

  manage.py host_redshifts --max 50                       # what would change
  manage.py host_redshifts --transients 2025abc,2025abd
  manage.py host_redshifts --services ned --max 500 --apply
  manage.py host_redshifts --only-empty --max 1000 --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from YSE_App.services import redshift_catalogs as rc
from YSE_App.services.tns_resync import ResyncError


def _split(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Command(BaseCommand):
    help = "Fill host redshifts from NED and DESI with provenance (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--services", default="", help="Comma-separated: ned,desi (default: [redshifts] "
                                                          "REDSHIFT_CATALOG_SERVICES).")
        parser.add_argument("--hosts", default="", help="Comma-separated Host ids.")
        parser.add_argument("--transients", default="", help="Comma-separated transient names (their hosts).")
        parser.add_argument("--only-empty", action="store_true", help="Only hosts without a redshift.")
        parser.add_argument("--max", type=int, default=None, help="At most this many hosts.")
        parser.add_argument("--skip-checked-days", type=float, default=30.0,
                            help="Skip hosts checked within this many days (0: none skipped). Default 30.")

    def handle(self, *args, **opts):
        dry_run = not opts["apply"]
        services = _split(opts["services"]) or rc.enabled_services()
        unknown = [s for s in services if s not in rc.SERVICES]
        if unknown:
            raise CommandError("unknown service(s): %s (use ned, desi)" % ", ".join(unknown))
        try:
            host_ids = [int(h) for h in _split(opts["hosts"])]
        except ValueError as exc:
            raise CommandError("--hosts takes numeric Host ids") from exc
        try:
            user = None if dry_run else rc.service_user()
        except ResyncError as exc:
            raise CommandError(str(exc)) from exc

        skip = opts["skip_checked_days"]
        qs = rc.select_hosts(services=services, recheck_days=skip if skip and skip > 0 else None,
                             host_ids=host_ids, transient_names=_split(opts["transients"]),
                             only_empty=opts["only_empty"])
        if opts["max"]:
            qs = qs[:opts["max"]]
        hosts = list(qs)
        self.stdout.write("%s: %d hosts, services %s" % ("Dry run" if dry_run else "Applying", len(hosts),
                                                        ",".join(services)))
        clients = rc.default_clients()
        totals = {}
        for host in hosts:
            for result in rc.update_host(host, services=services, dry_run=dry_run, clients=clients, user=user):
                totals[result.action] = totals.get(result.action, 0) + 1
                if result.action in ("updated", "kept", "error"):
                    self.stdout.write(result.summary())
        requests = ", ".join("%s %d" % (slug, getattr(c, "requests", 0)) for slug, c in clients.items())
        self.stdout.write("Done: %s; requests: %s" % (
            ", ".join("%s %d" % kv for kv in sorted(totals.items())) or "nothing to do", requests))
