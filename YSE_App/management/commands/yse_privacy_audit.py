"""
Report (and optionally fix) YSE photometry and spectra that a public-only account can see.

Read-only by default. Prints transient names, set ids, instruments, observation
groups and collaboration-group names only (no people, no data values)::

  python manage.py yse_privacy_audit --transient 2026aetq
  python manage.py yse_privacy_audit --transient 2026aetq --user <USERNAME>
  python manage.py yse_privacy_audit                      # every transient: summary counts
  python manage.py yse_privacy_audit --apply              # label the reported YSE sets

Two reports:

* every GPC1 / GPC2 set a public-only account can see, counted by observation
  group and groups (YSE survey data should never appear here; public PS1/PS2
  data reported to TNS may);
* "YSE sets" not labelled YSE-only: sets of the ``YSE`` observation group, or of
  any observation group named with ``--obs-group`` (repeatable), that lack the
  ``YSE`` group or also carry ``Public``.

``--apply`` labels the second list: it adds the ``YSE`` collaboration group to
each such set and removes ``Public`` from it. Nothing else changes.
"""

from __future__ import annotations

import os
from collections import Counter

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import TransientPhotometry, TransientSpectrum
from YSE_App.services.visibility import (
    YSE_COLLABORATION_GROUP_NAME,
    YSE_OBSERVATION_GROUP_NAME,
    dataset_group_q,
    user_group_names,
)


class Command(BaseCommand):
    help = "Report (or with --apply, label) YSE photometry and spectra visible to a public-only account."

    def add_arguments(self, parser):
        parser.add_argument("--transient", help="Only this transient (name); lists every set on it.")
        parser.add_argument("--user", help="Also show what this account can see (its group names are printed).")
        parser.add_argument("--obs-group", action="append", default=[],
                            help="Another observation group whose data are YSE data (repeatable).")
        parser.add_argument("--apply", action="store_true",
                            help="Add the YSE group to the reported YSE sets and remove Public from them.")

    def handle(self, *args, **options):
        import YSE_App

        self.stdout.write("Code checkout: %s" % os.path.dirname(os.path.dirname(os.path.abspath(YSE_App.__file__))))
        yse_obs_groups = {YSE_OBSERVATION_GROUP_NAME, *options["obs_group"]}
        public_only = [PUBLIC_COLLABORATION_GROUP_NAME]
        viewer = None
        if options["user"]:
            viewer = User.objects.filter(username=options["user"]).first()
            if viewer is None:
                raise CommandError("No account named %r." % options["user"])
            self.stdout.write("Groups of %s: %s" % (viewer.username, sorted(user_group_names(viewer)) or "(none)"))

        unlabelled = []
        public_ps = Counter()
        for model, label in ((TransientPhotometry, "photometry"), (TransientSpectrum, "spectrum")):
            qs = model.objects.all()
            if options["transient"]:
                qs = qs.filter(transient__name=options["transient"])
            public_q = dataset_group_q(model, None, names=public_only)
            rows = qs.select_related("transient", "instrument", "obs_group").prefetch_related("groups")
            if options["transient"]:
                visible_public = set(qs.filter(public_q).values_list("pk", flat=True))
                visible_viewer = None
                if viewer is not None:
                    visible_viewer = set(qs.filter(dataset_group_q(model, viewer)).values_list("pk", flat=True))
                self.stdout.write("\n%s sets on %s:" % (label, options["transient"]))
                for row in rows.order_by("pk"):
                    self.stdout.write(self._describe(row, label, visible_public, visible_viewer))
            for row in rows.filter(public_q, instrument__name__in=("GPC1", "GPC2")).distinct():
                public_ps[(label, row.obs_group.name, self._groups(row))] += 1
            for row in rows.filter(obs_group__name__in=yse_obs_groups):
                names = {g.name for g in row.groups.all()}
                if YSE_COLLABORATION_GROUP_NAME not in names or PUBLIC_COLLABORATION_GROUP_NAME in names:
                    unlabelled.append(row)

        self.stdout.write("\nGPC1/GPC2 sets a public-only account can see: %d" % sum(public_ps.values()))
        for (label, og, groups), n in sorted(public_ps.items()):
            self.stdout.write("  %6d %s  obs_group=%s  groups=%s" % (n, label, og, groups))
        unlabelled_kinds = Counter((type(row).__name__, row.obs_group.name, self._groups(row)) for row in unlabelled)
        self.stdout.write("\nYSE sets not labelled YSE-only (obs groups %s): %d"
                          % (sorted(yse_obs_groups), len(unlabelled)))
        for (label, og, groups), n in sorted(unlabelled_kinds.items()):
            self.stdout.write("  %6d %s  obs_group=%s  groups=%s" % (n, label, og, groups))
        if not options["apply"]:
            if unlabelled:
                self.stdout.write(self.style.WARNING("Read-only: nothing changed. Re-run with --apply to label them."))
            return

        yse_group = Group.objects.filter(name=YSE_COLLABORATION_GROUP_NAME).first()
        if yse_group is None:
            raise CommandError("There is no %r group; nothing changed." % YSE_COLLABORATION_GROUP_NAME)
        public_group = Group.objects.filter(name=PUBLIC_COLLABORATION_GROUP_NAME).first()
        with transaction.atomic():
            for row in unlabelled:
                row.groups.add(yse_group)
                if public_group is not None:
                    row.groups.remove(public_group)
        self.stdout.write(self.style.SUCCESS("Labelled %d set(s) YSE-only." % len(unlabelled)))

    @staticmethod
    def _groups(row):
        return ",".join(sorted(g.name for g in row.groups.all())) or "(none)"

    @classmethod
    def _describe(cls, row, label, visible_public, visible_viewer):
        parts = [
            "  %s %d" % (label, row.pk),
            "instrument=%s" % row.instrument.name,
            "obs_group=%s" % row.obs_group.name,
            "groups=%s" % cls._groups(row),
            "public-only sees it: %s" % ("YES" if row.pk in visible_public else "no"),
        ]
        if visible_viewer is not None:
            parts.append("--user sees it: %s" % ("YES" if row.pk in visible_viewer else "no"))
        return "  ".join(parts)
