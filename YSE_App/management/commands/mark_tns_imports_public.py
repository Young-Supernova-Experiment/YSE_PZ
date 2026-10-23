"""
Backfill for TNS imports and forced photometry (#645, #447).

Dry run by default; ``--apply`` writes. Three things, each idempotent:

1. Add the Public collaboration group to TNS-named transients' photometry and spectra with no groups.
2. Add the Public group to ZTF and ATLAS photometry sets that contain forced photometry and have no groups.
3. Set ``reference = "TNS <name>"`` on TNS-named transients' public photometry sets and spectra that have
   none. A photometry set that also holds forced points is left alone: the old import mixed TNS points into
   the survey's shared header, and moving them out is not done here (new TNS imports use a dedicated
   ``TNS`` header). Those sets are counted so they can be reviewed.

Run against the default database::

  docker exec ysepz_web_container python3 manage.py mark_tns_imports_public
  docker exec ysepz_web_container python3 manage.py mark_tns_imports_public --apply
"""

from __future__ import annotations

import re

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db.models import Q

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import TransientPhotData, TransientPhotometry, TransientSpectrum

# Standard TNS IAU names (e.g. 2026fba) — same filter used in TNS_uploads ingest.
TNS_TRANSIENT_NAME_RE = re.compile(r"^20[0-9]{2}[a-zA-Z][a-zA-Z0-9]*$")

# Observation groups whose forced photometry gets the Public group (Ryan, 2026-10-16, #645).
FORCED_PUBLIC_OBS_GROUPS = ("ZTF", "ATLAS")


def tns_transient_name_q() -> Q:
    return Q(transient__name__regex=TNS_TRANSIENT_NAME_RE.pattern)


def _blank_reference_q() -> Q:
    return Q(reference__isnull=True) | Q(reference="")


class Command(BaseCommand):
    help = (
        "Add the Public collaboration group to TNS transient photometry and spectra "
        "that have no groups M2M rows yet."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without it this is a dry run that only reports counts.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Accepted for old runbooks; a dry run is now the default.",
        )

    def handle(self, *args, **options):
        apply = options["apply"] and not options["dry_run"]
        public_group = Group.objects.filter(name=PUBLIC_COLLABORATION_GROUP_NAME).first()
        if public_group is None and apply:
            public_group = Group.objects.create(name=PUBLIC_COLLABORATION_GROUP_NAME)
            self.stdout.write(f"Created collaboration group {PUBLIC_COLLABORATION_GROUP_NAME!r}")

        phot_qs = (
            TransientPhotometry.objects.filter(tns_transient_name_q())
            .filter(groups__isnull=True)
            .distinct()
        )
        spec_qs = (
            TransientSpectrum.objects.filter(tns_transient_name_q())
            .filter(groups__isnull=True)
            .distinct()
        )
        forced_ids = TransientPhotData.objects.filter(forced=True).values("photometry_id")
        forced_qs = (
            TransientPhotometry.objects.filter(
                obs_group__name__in=FORCED_PUBLIC_OBS_GROUPS, id__in=forced_ids
            )
            .filter(groups__isnull=True)
            .distinct()
        )

        self.stdout.write(
            f"TNS photometry headers to tag: {phot_qs.count()}; spectra: {spec_qs.count()}; "
            f"ZTF/ATLAS forced photometry headers to tag: {forced_qs.count()}"
        )

        tagged = {"phot": 0, "spec": 0, "forced": 0}
        if apply:
            for key, qs in (("phot", phot_qs), ("spec", spec_qs), ("forced", forced_qs)):
                for row in list(qs):
                    row.groups.add(public_group)
                    tagged[key] += 1

        # References: only public rows (no groups, or only Public) with none yet.
        non_public = Group.objects.exclude(name=PUBLIC_COLLABORATION_GROUP_NAME)
        ref_phot = (
            TransientPhotometry.objects.filter(tns_transient_name_q())
            .filter(_blank_reference_q())
            .exclude(groups__in=non_public)
            .select_related("transient")
            .distinct()
        )
        ref_spec = (
            TransientSpectrum.objects.filter(tns_transient_name_q())
            .filter(_blank_reference_q())
            .exclude(groups__in=non_public)
            .select_related("transient")
            .distinct()
        )
        mixed_ids = set(forced_ids.values_list("photometry_id", flat=True))
        phot_rows = [p for p in ref_phot if p.id not in mixed_ids]
        skipped_mixed = ref_phot.count() - len(phot_rows)
        spec_rows = list(ref_spec)
        self.stdout.write(
            f"TNS references to set: {len(phot_rows)} photometry header(s), {len(spec_rows)} spectrum(s); "
            f"left alone (header also holds forced photometry): {skipped_mixed}"
        )

        if not apply:
            self.stdout.write(self.style.WARNING("Dry run - no changes made. Re-run with --apply to write."))
            return

        for row in phot_rows + spec_rows:
            row.reference = f"TNS {row.transient.name}"
            row.save(update_fields=["reference"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Tagged {tagged['phot']} TNS photometry header(s), {tagged['spec']} spectrum(s) and "
                f"{tagged['forced']} forced photometry header(s) with {PUBLIC_COLLABORATION_GROUP_NAME!r}; "
                f"set {len(phot_rows)} photometry and {len(spec_rows)} spectrum reference(s)."
            )
        )
