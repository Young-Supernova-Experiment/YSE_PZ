"""``manage.py brokers``: list the registered broker providers and what each implements (#273)."""

import json

from django.core.management.base import BaseCommand

from YSE_App.brokers import registry
from YSE_App.brokers.base import ALL_CAPABILITIES, CAPABILITY_LABELS


class Command(BaseCommand):
    help = "List enabled broker providers, their availability and capabilities."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="machine-readable output")
        parser.add_argument("--all", action="store_true", help="include providers disabled by BROKERS_ENABLED")

    def handle(self, *args, **options):
        if options["all"]:
            slugs = registry.registered_slugs()
            rows = []
            for slug in slugs:
                cls = registry.provider_class(slug)
                d = cls(credential=registry.credential_for(slug), options=registry.broker_options(slug)).describe()
                d["enabled"] = slug in registry.enabled_slugs()
                rows.append(d)
        else:
            rows = registry.describe_all()
            for d in rows:
                d["enabled"] = True
        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
            return
        if not rows:
            self.stdout.write("no broker providers registered")
            return
        header = "%-10s %-12s %-12s %s" % ("slug", "enabled", "available", "capabilities")
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for d in rows:
            caps = ", ".join(CAPABILITY_LABELS[c] for c in ALL_CAPABILITIES if c in d["capabilities"])
            avail = "yes" if d["available"] else "no (%s)" % d["unavailable_reason"]
            self.stdout.write("%-10s %-12s %-12s %s" % (d["slug"], "yes" if d["enabled"] else "no", avail, caps))
