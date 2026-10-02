"""``manage.py broker_ingest``: long-running Kafka / ANTARES stream consumer (issue #278).

Reads a ``BrokerConnection`` (admin: *Broker connections*; must be enabled) with
``--workers N`` processes in one consumer group, evaluates the broker's enabled
``BrokerFilter`` rows on every message and registers candidates. ``--from-beginning``
/ ``--since`` replay a backlog, ``--max-messages`` / ``--timeout`` bound a run
(tests, smoke checks), ``--dry-run`` evaluates without writing. ``--list`` prints the
connections and their heartbeats. Run it as a service (docker ``brokers`` profile,
systemd unit in docs/broker-streams.md); nothing starts it by itself.
"""

import datetime
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from YSE_App.brokers import registry, streams
from YSE_App.brokers.base import BrokerError, BrokerUnavailable
from YSE_App.models.candidate_models import BrokerConnection


def parse_since(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise CommandError("--since needs an ISO date/time, got %r" % value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


class Command(BaseCommand):
    help = "Consume a broker stream (BrokerConnection) into candidates; --list shows connections and heartbeats."

    def add_arguments(self, parser):
        parser.add_argument("--connection", metavar="SLUG", help="BrokerConnection slug to consume")
        parser.add_argument("--list", action="store_true", help="list connections and heartbeats and exit")
        parser.add_argument("--json", action="store_true", help="machine-readable output")
        parser.add_argument("--workers", type=int, default=1, help="consumer processes in the same group (default 1)")
        parser.add_argument("--from-beginning", action="store_true", help="replay from the earliest retained offset")
        parser.add_argument("--since", metavar="ISO", help="replay from this UTC time (Kafka offsets_for_times)")
        parser.add_argument("--max-messages", type=int, default=None, help="stop after this many messages (per worker)")
        parser.add_argument("--timeout", type=float, default=None, help="stop after this many seconds (per worker)")
        parser.add_argument("--idle-exit", action="store_true", help="stop at the first empty poll (smoke tests)")
        parser.add_argument("--dry-run", action="store_true", help="evaluate only; write no candidates, commit no offsets")
        parser.add_argument("--force", action="store_true", help="consume even when the connection is disabled")

    def handle(self, *args, **options):
        if options["list"] or not options["connection"]:
            return self.list_connections(options["json"])
        connection = BrokerConnection.objects.filter(slug=options["connection"]).select_related("credential").first()
        if connection is None:
            raise CommandError("no BrokerConnection with slug %r (known: %s)" % (
                options["connection"], ", ".join(BrokerConnection.objects.values_list("slug", flat=True)) or "none"))
        if registry.provider_class(connection.broker) is None:
            raise CommandError("connection %s names unknown broker %r" % (connection.slug, connection.broker))
        kwargs = dict(max_messages=options["max_messages"], timeout_seconds=options["timeout"],
                      from_beginning=options["from_beginning"], since=parse_since(options["since"]),
                      dry_run=options["dry_run"], idle_exit=options["idle_exit"], force=options["force"])
        workers = max(1, int(options["workers"] or 1))
        if workers == 1:
            try:
                result = streams.consume(connection, worker=0, **kwargs)
            except (BrokerError, BrokerUnavailable) as exc:
                raise CommandError(str(exc))
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return
        self.stdout.write("starting %d workers on %s (%s)" % (workers, connection.slug, ", ".join(connection.topic_list())))
        procs = streams.run_workers(connection, workers, **kwargs)
        failed = [p for p in procs if getattr(p, "exitcode", 0)]
        self.stdout.write("workers finished: %d ok, %d failed" % (len(procs) - len(failed), len(failed)))
        if failed:
            raise CommandError("%d worker(s) exited with an error; see the log and the heartbeats" % len(failed))

    def list_connections(self, as_json):
        rows = []
        for c in BrokerConnection.objects.select_related("credential").order_by("broker", "name"):
            rows.append({"slug": c.slug, "broker": c.broker, "kind": c.kind, "enabled": c.enabled,
                         "bootstrap_servers": c.bootstrap_servers, "topics": c.topic_list(), "group_id": c.group_id,
                         "format": c.message_format, "credential": bool(c.credential_id),
                         "last_message_at": c.last_message_at, "last_error": c.last_error[:200]})
        status = streams.stream_status()
        if as_json:
            self.stdout.write(json.dumps({"connections": rows, "heartbeats": status}, indent=2, default=str))
            return
        if not rows:
            self.stdout.write("no broker connections configured (admin > Broker connections)")
            return
        header = "%-20s %-8s %-8s %-8s %-30s %s" % ("slug", "broker", "kind", "enabled", "topics", "last error")
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for r in rows:
            self.stdout.write("%-20s %-8s %-8s %-8s %-30s %s" % (
                r["slug"], r["broker"], r["kind"], "yes" if r["enabled"] else "no", ",".join(r["topics"])[:30], r["last_error"][:60]))
        if status:
            self.stdout.write("")
            self.stdout.write("heartbeats:")
            now = timezone.now()
            for h in status:
                self.stdout.write("  %s/%s#%d %s age %.0fs msgs %d cand %d saved %d err %d lag %s%s" % (
                    h["connection"], h["topic"] or "*", h["worker"], h["status"], h["age_seconds"], h["messages"],
                    h["candidates"], h["saved"], h["errors"], h["lag"] if h["lag"] is not None else "-",
                    " STALE" if h["stale"] else ""))
            del now
