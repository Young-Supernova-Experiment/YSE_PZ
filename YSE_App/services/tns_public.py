"""Data reported to TNS become public, and stay public (#708).

Ryan's rule (2026-10-17): anything on TNS is public, and must stay public in YSE_PZ.
After a confirmed TNS report (the manual dialog, autopublish and the legacy DECam form)
:func:`mark_reported_public` makes exactly the photometry points and spectra that were sent public,
the same way the TNS imports are (#645): the dataset's collaboration groups are exactly ``{Public}``
(``data_sharing.is_public_dataset``). The read-time engine (#648) never hides public data, so no
deny or group removal can restrict them afterwards.

Photometry is sent point by point but stored in sets (one header with a group list). So:

* a set that is already public only gets the Public group (idempotent);
* a restricted set whose points were all sent becomes public as a whole;
* a restricted set of which only some points were sent is split: the sent points move to a new
  header (a copy of the original, public) and the rest stay in the restricted original.

Only call this after TNS confirmed the report (never for a sandbox). Idempotent; logged.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Set

from django.contrib.auth.models import Group
from django.db import transaction

from YSE_App.common.collaboration_groups import get_or_create_public_group
from YSE_App.models import TransientPhotData, TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing

log = logging.getLogger(__name__)


def _make_public(dataset, public: Group) -> bool:
    """Set ``dataset``'s groups to exactly ``{Public}``; True when anything changed."""
    current = set(dataset.groups.values_list("pk", flat=True))
    if current == {public.pk}:
        return False
    removed = sorted(dataset.groups.exclude(pk=public.pk).values_list("name", flat=True))
    dataset.groups.set([public])
    log.info("TNS report: %s %s is now public (groups removed: %s)", type(dataset).__name__, dataset.pk,
             ", ".join(removed) or "none")
    return True


def _split_header(header: TransientPhotometry, point_ids: Set[int]) -> TransientPhotometry:
    """A copy of ``header`` holding ``point_ids`` (moved from it); the copy keeps the header's PIs."""
    pis = list(header.pis.all())
    copy = TransientPhotometry.objects.get(pk=header.pk)
    copy.pk = None
    copy.id = None
    copy._state.adding = True
    copy.save()
    copy.pis.set(pis)
    TransientPhotData.objects.filter(pk__in=point_ids, photometry=header).update(photometry=copy)
    return copy


@transaction.atomic
def mark_reported_public(transient, *, photometry_point_ids: Iterable[int] = (),
                         spectrum_ids: Iterable[int] = ()) -> Dict[str, int]:
    """Make the reported points and spectra of ``transient`` public; returns counts.

    Only ids that belong to ``transient`` are touched.
    """
    from YSE_App.services import photstat

    point_ids = {int(i) for i in photometry_point_ids or ()}
    spec_ids = {int(i) for i in spectrum_ids or ()}
    result = {"points": 0, "sets_public": 0, "sets_split": 0, "spectra": 0}
    if not point_ids and not spec_ids:
        return result
    public = get_or_create_public_group()

    by_set: Dict[int, Set[int]] = {}
    for pk, set_id in TransientPhotData.objects.filter(pk__in=point_ids, photometry__transient=transient
                                                       ).values_list("pk", "photometry_id"):
        by_set.setdefault(set_id, set()).add(pk)
    for header in TransientPhotometry.objects.filter(pk__in=by_set, transient=transient).order_by("pk"):
        sent = by_set[header.pk]
        result["points"] += len(sent)
        if data_sharing.is_public_dataset(header):
            if _make_public(header, public):
                result["sets_public"] += 1
            continue
        all_points = set(TransientPhotData.objects.filter(photometry=header).values_list("pk", flat=True))
        if sent >= all_points:
            _make_public(header, public)
            result["sets_public"] += 1
            continue
        copy = _split_header(header, sent)
        _make_public(copy, public)
        photstat.schedule_recompute(transient.pk, photometry_id=header.pk)
        photstat.schedule_recompute(transient.pk, photometry_id=copy.pk)
        result["sets_split"] += 1
        log.info("TNS report: split %d reported point(s) of photometry set %s into public set %s",
                 len(sent), header.pk, copy.pk)

    for spectrum in TransientSpectrum.objects.filter(pk__in=spec_ids, transient=transient).order_by("pk"):
        _make_public(spectrum, public)
        result["spectra"] += 1
    return result


def reported_ids(payload: dict) -> Dict[str, list]:
    """``{"photometry_point_ids": [...], "spectrum_ids": [...]}`` recorded in a stored report payload."""
    private = (payload or {}).get("_yse") or {}
    spectra = [private["spectrum_id"]] if private.get("spectrum_id") else []
    return {"photometry_point_ids": list(private.get("photometry_point_ids") or []), "spectrum_ids": spectra}

