"""Rewrite stored ``http://`` URLs to ``https://`` for our own hosts and known
https-capable services (#441).

    python manage.py https_urls           # dry run: counts per field and host
    python manage.py https_urls --apply   # write the changes

Only the scheme changes; hosts not on the known list (``url_utils``), URLs with
an explicit port and comment text are never rewritten (comments are only
counted). Updates use ``QuerySet.update``, so modified dates and signals are
not touched. No migration; safe to re-run.
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from YSE_App.common.url_utils import https_url, url_host
from YSE_App.models import (
    FacilityRequest,
    ExternalService,
    ExternalServiceRun,
    GWCandidate,
    HostWebResource,
    Log,
    Telescope,
    TransientWebResource,
)

# (model, field) pairs that hold a URL.
URL_FIELDS = [
    (TransientWebResource, "resource_url"),
    (HostWebResource, "resource_url"),
    (GWCandidate, "websniff_url"),
    (Telescope, "weather_url"),
    (Telescope, "weather_link"),
    (Telescope, "skycam_url"),
    (FacilityRequest, "external_url"),
    (ExternalService, "base_url"),
    (ExternalServiceRun, "artifact_url"),
]


def scan_field(model, field, apply=False):
    """Return (rewritten_by_host, kept_by_host) for one field; write if ``apply``."""
    rewritten, kept = Counter(), Counter()
    qs = model.objects.filter(**{field + "__istartswith": "http://"}).values_list("pk", field)
    for pk, value in qs.iterator():
        new = https_url(value)
        host = url_host(value) or "(unparsable)"
        if new == value:
            kept[host] += 1
            continue
        rewritten[host] += 1
        if apply:
            model.objects.filter(pk=pk, **{field: value}).update(**{field: new})
    return rewritten, kept


class Command(BaseCommand):
    help = "Rewrite stored http:// URLs to https:// for our own hosts and known https-capable services."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run).")
        parser.add_argument("--dry-run", action="store_true", help="Explicit dry run (the default); cannot be combined with --apply.")

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("Use either --apply or --dry-run, not both.")
        apply = options["apply"]
        total_rewritten = total_kept = 0
        for model, field in URL_FIELDS:
            label = "%s.%s" % (model.__name__, field)
            rewritten, kept = scan_field(model, field, apply=apply)
            n_rw, n_kept = sum(rewritten.values()), sum(kept.values())
            total_rewritten += n_rw
            total_kept += n_kept
            self.stdout.write("%s: %d %s, %d left on http" % (
                label, n_rw, "rewritten" if apply else "rewritable", n_kept))
            for host, count in sorted(rewritten.items()):
                self.stdout.write("    %s %s: %d" % ("rewrote" if apply else "would rewrite", host, count))
            for host, count in sorted(kept.items()):
                self.stdout.write("    left %s: %d" % (host, count))
        comments = Log.objects.filter(comment__icontains="http://").count()
        self.stdout.write("comment text containing http:// (counted only, never changed): %d" % comments)
        self.stdout.write("%s: %d URLs %s, %d left on http" % (
            "applied" if apply else "dry run", total_rewritten,
            "rewritten" if apply else "would be rewritten", total_kept))
        if not apply and total_rewritten:
            self.stdout.write("Re-run with --apply to write the changes.")
