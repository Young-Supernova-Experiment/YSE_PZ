"""``manage.py feeds``: list feed sources, poll one now, consume a stream, or screen a transient (#280)."""

import json

from django.core.management.base import BaseCommand, CommandError

from YSE_App.feeds import registry
from YSE_App.feeds.base import FeedError
from YSE_App.feeds.jobs import run_source
from YSE_App.models.feed_models import FeedSource


class Command(BaseCommand):
    help = "List the configured feed sources and providers; --poll SLUG runs one poll inline."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="machine-readable output")
        parser.add_argument("--poll", metavar="SLUG", help="poll this source now (inline, not through the queue)")
        parser.add_argument("--consume", metavar="SLUG", help="read the source's stream (Kafka) once and process it")
        parser.add_argument("--max", type=int, default=100, help="messages to read with --consume (default 100)")
        parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait with --consume (default 30)")
        parser.add_argument("--screen", metavar="TRANSIENT", help="minor-planet screening of one transient (name)")
        parser.add_argument("--dry-run", action="store_true", help="evaluate without writing candidates / transients")

    def handle(self, *args, **options):
        if options["screen"]:
            return self.screen(options["screen"])
        slug = options["poll"] or options["consume"]
        if slug:
            source = FeedSource.objects.filter(slug=slug).first()
            if source is None:
                raise CommandError("no feed source with slug %r" % slug)
            provider = registry.provider_for(source)
            if provider is None:
                raise CommandError("no provider for kind %r" % source.kind)
            try:
                if options["consume"]:
                    messages = provider.consume(source, max_messages=options["max"], timeout_seconds=options["timeout"])
                    result = provider.run(source, dry_run=options["dry_run"], messages=messages)
                else:
                    result = run_source(source, dry_run=options["dry_run"])
            except FeedError as exc:
                raise CommandError(str(exc))
            self.stdout.write(json.dumps(result, indent=2, default=str) if options["json"] else result["summary"])
            return
        rows = []
        providers = {d["feed_kind"]: d for d in registry.describe_all()}
        for source in FeedSource.objects.select_related("credential").order_by("kind", "name"):
            info = providers.get(source.kind, {})
            rows.append({
                "slug": source.slug, "kind": source.kind, "topic": source.topic, "enabled": source.enabled,
                "credential": bool(source.credential_id), "available": info.get("available", False),
                "can_consume": info.get("can_consume", False), "last_polled": source.last_polled,
                "last_summary": source.last_summary, "last_error": source.last_error[:120],
            })
        if options["json"]:
            self.stdout.write(json.dumps({"providers": list(providers.values()), "sources": rows}, indent=2, default=str))
            return
        self.stdout.write("providers: %s" % ", ".join(
            "%s (%s)" % (k, "available" if v.get("available") else v.get("unavailable_reason")) for k, v in sorted(providers.items())))
        if not rows:
            self.stdout.write("no feed sources configured (admin > Feed sources)")
            return
        header = "%-20s %-7s %-28s %-8s %-19s %s" % ("slug", "kind", "topic", "enabled", "last poll", "summary")
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for r in rows:
            self.stdout.write("%-20s %-7s %-28s %-8s %-19s %s" % (
                r["slug"], r["kind"], r["topic"][:28], "yes" if r["enabled"] else "no",
                r["last_polled"].strftime("%Y-%m-%d %H:%M") if r["last_polled"] else "-", r["last_error"] or r["last_summary"]))

    def screen(self, name):
        from YSE_App.feeds.scout import screen_transient
        from YSE_App.models.transient_models import Transient

        transient = Transient.objects.filter(name=name).first()
        if transient is None:
            raise CommandError("no transient named %r" % name)
        try:
            document = screen_transient(transient)
        except FeedError as exc:
            raise CommandError(str(exc))
        self.stdout.write(json.dumps(document, indent=2, default=str))
