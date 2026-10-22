"""List possible duplicate transients, or merge one reviewed pair (#752).

The ``CheckDuplicates`` cron only reports pairs (unless ``DUPLICATE_AUTO_MERGE``
is on); this command is the confirmation step.

Examples::

  manage.py transient_duplicates list
  manage.py transient_duplicates merge <duplicate id or name> <keep id or name>         # dry run
  manage.py transient_duplicates merge <duplicate id or name> <keep id or name> --yes   # do it

A merge moves photometry, spectra, alternate names and tags to the kept
transient, keeps the duplicate's name as an alternate name and deletes the
duplicate, all in one transaction. The dry run lists the other rows that point
at the duplicate; those are deleted (or unlinked) with it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from YSE_App.models import Transient
from YSE_App.services import duplicates


def _lookup(value: str):
    qs = Transient.objects.filter(pk=int(value)) if value.isdigit() else Transient.objects.none()
    found = list(qs) or list(Transient.objects.filter(slug=value)) or list(
        Transient.objects.filter(name=value).order_by("pk"))
    if not found:
        raise CommandError("no transient %r" % value)
    if len(found) > 1:
        raise CommandError("%r matches %d transients; use the id (%s)" % (
            value, len(found), ", ".join(str(t.pk) for t in found)))
    return found[0]


class Command(BaseCommand):
    help = "List possible duplicate transients, or merge one pair after review."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list", help="Show the candidate pairs (changes nothing).")
        merge = sub.add_parser("merge", help="Merge DUPLICATE into KEEP (dry run without --yes).")
        merge.add_argument("duplicate", help="Id, slug or name of the transient to remove.")
        merge.add_argument("keep", help="Id, slug or name of the transient to keep.")
        merge.add_argument("--yes", action="store_true", help="Really merge.")

    def handle(self, *args, **opts):
        if opts["action"] == "list":
            pairs = duplicates.find_candidates()
            for pair in pairs:
                self.stdout.write("%s  ->  merge %s %s" % (pair.describe(), pair.duplicate.pk, pair.keep.pk))
            self.stdout.write("%d candidate pair(s)" % len(pairs))
            return

        duplicate, keep = _lookup(opts["duplicate"]), _lookup(opts["keep"])
        if duplicate.pk == keep.pk:
            raise CommandError("both arguments are the same transient")
        self.stdout.write("remove: %s (id %s, slug %s)" % (duplicate.name, duplicate.pk, duplicate.slug))
        self.stdout.write("keep:   %s (id %s, slug %s)" % (keep.name, keep.pk, keep.slug))
        unmoved = duplicates.unmoved_related_counts(duplicate)
        if unmoved:
            self.stdout.write("not moved (deleted or unlinked with the duplicate):")
            for label, n in sorted(unmoved.items()):
                self.stdout.write("  %s: %d" % (label, n))
        if not opts["yes"]:
            self.stdout.write("dry run: nothing changed; add --yes to merge")
            return
        counts = duplicates.merge(duplicate, keep)
        self.stdout.write("merged: %s" % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
