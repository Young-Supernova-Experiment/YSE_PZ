"""Which program (``Allocation``) a dataset was taken under (#648, PR 3).

``TransientPhotometry.allocation`` / ``TransientSpectrum.allocation`` say "these
data were taken under this program". The link gives the program's access
rules (its standing grants, PR 2) authority over the data, so setting it is a
sharing action.

**Where the link comes from**

* **At ingest, derived** (:func:`link_at_ingest`, a ``post_save`` receiver for new
  datasets): the follow-up's ToO / classical / queued resource's allocation, or
  the follow-up's ``FacilityRequest`` allocation, or, with the planner on, the
  allocation of the classical night of the matching AstroScheduler
  ``ObservedBlock``. Only an unambiguous result is written.
* **Facility results** (``facility_requests.ingest_points``): the request's own
  allocation.
* **Uploads naming an allocation** (``allocation``: an id, a proposal id or an
  allocation name, in the photometry / spectrum dict or header):
  :func:`apply_upload_allocation`. A new dataset may be linked by the program's
  PI, its program deputies or staff; an existing one only by someone who may
  share it (``access_actions.dataset_role``: its PIs and their deputies, the
  program's PI or deputies when it is already linked, or staff). Otherwise
  the link is skipped and the response says so.
* **Manually** (:func:`link_dataset`, ``POST /api/datasets/<kind>/<id>/program/``):
  whoever may share the dataset.
* **Backfill** (``manage.py backfill_data_programs``, dry run by default):
  :func:`backfill_plan` writes only unambiguous links.

**After a link** the program's active standing grants are applied to the
dataset's existing points (``access_actions.apply_standing_grants_to_set`` /
``apply_standing_grants_to_spectrum``): at ingest with the ingest rules, later
with the rules of an action (a restriction is not written for points the
grantee can already see). Removing or changing a link removes no rule:
shared points stay shared.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

logger = logging.getLogger(__name__)

# The follow-up's allocations, as ORM paths from a dataset.
FOLLOWUP_ALLOCATION_PATHS = (
    "followup__too_resource__allocation_id",
    "followup__classical_resource__allocation_id",
    "followup__queued_resource__allocation_id",
    "followup__facility_requests__allocation_id",
)


def _is_spectrum(dataset) -> bool:
    from YSE_App.models import TransientSpectrum

    return isinstance(dataset, TransientSpectrum)


def _observed_block_allocation_ids(dataset) -> Set[int]:
    """Allocations of the classical nights of the AstroScheduler observations that match ``dataset``."""
    from YSE_App.models import ObservedBlock
    from YSE_App.services.night_planner import enabled

    if not enabled():
        return set()
    blocks = ObservedBlock.objects.all()
    if getattr(dataset, "followup_id", None):
        blocks = blocks.filter(followup_id=dataset.followup_id)
    elif _is_spectrum(dataset) and dataset.obs_date is not None:
        # The same night rule as observations.data_uploaded (a night dated D is observed after D).
        blocks = blocks.filter(transient_id=dataset.transient_id,
                               night__obs_date__gt=dataset.obs_date - datetime.timedelta(days=2),
                               night__obs_date__lte=dataset.obs_date)
    else:
        return set()
    return {a for a in blocks.values_list("night__resource__allocation_id", flat=True) if a}


def derived_allocation_ids(dataset) -> Set[int]:
    """Every allocation the dataset's follow-up or matching observations point to (may be several)."""
    ids: Set[int] = set()
    if getattr(dataset, "followup_id", None):
        for row in type(dataset).objects.filter(pk=dataset.pk).values_list(*FOLLOWUP_ALLOCATION_PATHS):
            ids.update(v for v in row if v)
    ids |= _observed_block_allocation_ids(dataset)
    return ids


def derive_allocation_id(dataset) -> Optional[int]:
    """The one allocation derived for ``dataset``, or ``None`` when there is none or more than one."""
    ids = derived_allocation_ids(dataset)
    return next(iter(ids)) if len(ids) == 1 else None


# ------------------------------------------------------------------ linking


@dataclass
class LinkResult:
    changed: bool = False
    previous_id: Optional[int] = None
    allocation_id: Optional[int] = None
    grants: Dict[str, int] = field(default_factory=dict)


def link_role(actor, dataset, allocation, *, new_dataset: bool = False) -> Optional[str]:
    """The capacity in which ``actor`` may link (or unlink) ``dataset``, or ``None``.

    Existing datasets: whoever may share them (``access_actions.dataset_role``). A
    dataset created by the same upload may also be linked by the program's PI or
    deputies (they are naming their own program's data).
    """
    from YSE_App.services import access_actions

    role = access_actions.dataset_role(actor, dataset) if getattr(dataset, "pk", None) else None
    if role is None and new_dataset and allocation is not None:
        role = access_actions.program_role(actor, allocation)
    return role


def _set_link(dataset, allocation_id, *, new_dataset: bool) -> LinkResult:
    from YSE_App.services import access_actions

    result = LinkResult(previous_id=dataset.allocation_id, allocation_id=allocation_id)
    if dataset.allocation_id == allocation_id:
        return result
    with transaction.atomic():
        dataset.allocation_id = allocation_id
        if dataset.pk:
            type(dataset).objects.filter(pk=dataset.pk).update(allocation_id=allocation_id)
            if allocation_id:
                apply = (access_actions.apply_standing_grants_to_spectrum if _is_spectrum(dataset)
                         else access_actions.apply_standing_grants_to_set)
                result.grants = apply(dataset, linked=not new_dataset).as_dict()
    result.changed = True
    return result


def link_dataset(actor, dataset, allocation, *, new_dataset: bool = False, trusted: bool = False) -> LinkResult:
    """Link ``dataset`` to ``allocation`` (``None`` unlinks) and apply the program's standing grants.

    ``trusted``: a derivation by the system (ingest, backfill, facility results), no
    permission check. Otherwise :func:`link_role` decides; :class:`PermissionDenied` if none.
    Removing or changing a link removes no access rule (shared points stay shared).
    """
    if not trusted and link_role(actor, dataset, allocation, new_dataset=new_dataset) is None:
        raise PermissionDenied({"message": "Only someone who may share these data (their PIs or deputies, the "
                                           "program's PI or deputies, or an administrator) may link them to a "
                                           "program."})
    result = _set_link(dataset, allocation.pk if allocation is not None else None, new_dataset=new_dataset)
    if result.changed and not trusted:
        _audit(dataset, actor, result)
    return result


def _audit(dataset, actor, result: LinkResult) -> None:
    try:
        import json

        from auditlog.models import LogEntry

        LogEntry.objects.log_create(
            dataset, action=LogEntry.Action.UPDATE, actor=actor if getattr(actor, "pk", None) else None,
            changes=json.dumps({"allocation": [str(result.previous_id), str(result.allocation_id)]}))
    except Exception:  # noqa: BLE001
        logger.exception("program link not written to the audit log")


def link_at_ingest(dataset) -> Optional[int]:
    """``post_save`` of a new dataset with no program: link it when one allocation is derived."""
    if getattr(dataset, "allocation_id", None):
        return dataset.allocation_id
    allocation_id = derive_allocation_id(dataset)
    if allocation_id is None:
        return None
    _set_link(dataset, allocation_id, new_dataset=True)
    return allocation_id


# ------------------------------------------------------------------ uploads


def resolve_allocation(value):
    """An ``Allocation`` from an upload's ``allocation`` value: an id, a proposal id or a name (unique).

    ``None`` for an empty value; :class:`ValidationError` when nothing, or more than one, matches.
    """
    from YSE_App.models import Allocation

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValidationError({"allocation": "Give an allocation id, proposal id or name."})
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        row = Allocation.objects.filter(pk=int(value)).first()
        if row is not None:
            return row
    text = str(value).strip()
    rows = list(Allocation.objects.filter(proposal_id=text)[:2]) or list(Allocation.objects.filter(name=text)[:2])
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ValidationError({"allocation": "No allocation matches %r." % text[:64]})
    raise ValidationError({"allocation": "More than one allocation matches %r; give its id." % text[:64]})


def apply_upload_allocation(user, dataset, value, *, new_dataset: bool) -> Optional[str]:
    """Link a dataset to the allocation an upload names; returns why it was skipped, or ``None``."""
    if value is None or value == "":
        return None
    try:
        allocation = resolve_allocation(value)
    except ValidationError as exc:
        return str(exc.detail.get("allocation", exc.detail)) if isinstance(exc.detail, dict) else str(exc.detail)
    if allocation is None or dataset.allocation_id == allocation.pk:
        return None
    try:
        link_dataset(user, dataset, allocation, new_dataset=new_dataset)
    except PermissionDenied:
        return ("Program link skipped: only someone who may share these data (or, for new data, the program's PI "
                "or deputies) may link them to allocation %s." % allocation.pk)
    return None


def new_dataset_allocation_id(user, value) -> Tuple[Optional[int], Optional[str]]:
    """For a dataset an upload is about to create unsaved (bulk path): ``(allocation_id, skipped message)``."""
    from YSE_App.services import access_actions

    if value is None or value == "":
        return None, None
    try:
        allocation = resolve_allocation(value)
    except ValidationError as exc:
        return None, str(exc.detail)
    if allocation is None:
        return None, None
    if access_actions.program_role(user, allocation) is None:
        return None, ("Program link skipped: only the program's PI or deputies (or an administrator) may link new "
                      "data to allocation %s." % allocation.pk)
    return allocation.pk, None


# ------------------------------------------------------------------ backfill


def _date_matches(spectrum, allocations) -> Set[int]:
    """Allocations on the spectrum's instrument (or telescope when the allocation names none) covering its date."""
    found = set()
    if spectrum.obs_date is None:
        return found
    for alloc in allocations:
        if alloc.instrument_id is not None:
            if alloc.instrument_id != spectrum.instrument_id:
                continue
        elif alloc.telescope_id != spectrum.instrument.telescope_id:
            continue
        if alloc.start_date <= spectrum.obs_date <= alloc.end_date:
            found.add(alloc.pk)
    return found


def backfill_plan(*, include_public: bool = False, sources=("followup", "observed", "date"),
                  limit: Optional[int] = None) -> dict:
    """What a backfill would link, without writing anything.

    Datasets with no ``allocation`` (restricted ones only, unless
    ``include_public``). Per dataset the first source with exactly one
    allocation wins:

    * ``followup``: the follow-up's resource or facility-request allocation;
    * ``observed``: the classical night of a matching AstroScheduler observation;
    * ``date``: spectra only, one allocation on the same instrument (or telescope) whose dates
      cover the spectrum (a photometry set spans many nights, so it is not matched by date).

    Returns ``{"link": [(model, dataset_id, allocation_id)], "counts": {...}}``; the counts are
    ``linked_<source>``, ``ambiguous`` and ``unmatched``.
    """
    from YSE_App.models import Allocation, ObservedBlock, TransientPhotometry, TransientSpectrum

    counts = {"linked_followup": 0, "linked_observed": 0, "linked_date": 0, "ambiguous": 0, "unmatched": 0}
    link: List[tuple] = []
    allocations = list(Allocation.objects.all()) if "date" in sources else []
    by_followup_block: Dict[int, Set[int]] = {}
    if "observed" in sources:
        for followup_id, alloc_id in ObservedBlock.objects.filter(followup__isnull=False).values_list(
                "followup_id", "night__resource__allocation_id"):
            if alloc_id:
                by_followup_block.setdefault(followup_id, set()).add(alloc_id)
    for model in (TransientPhotometry, TransientSpectrum):
        qs = model.objects.filter(allocation__isnull=True)
        if not include_public:
            qs = qs.filter(groups__isnull=False)
        qs = qs.distinct().order_by("pk")
        followup_allocs: Dict[int, Set[int]] = {}
        if "followup" in sources:
            for row in qs.filter(followup__isnull=False).values_list("pk", *FOLLOWUP_ALLOCATION_PATHS):
                followup_allocs.setdefault(row[0], set()).update(v for v in row[1:] if v)
        if model is TransientSpectrum:
            qs = qs.select_related("instrument")
        n = 0
        for dataset in qs.iterator():
            n += 1
            if limit and n > limit:
                break
            chosen, ambiguous = None, False
            for source in sources:
                if source == "followup":
                    candidates = followup_allocs.get(dataset.pk, set())
                elif source == "observed":
                    candidates = set(by_followup_block.get(dataset.followup_id, set())) if dataset.followup_id \
                        else set()
                    if not candidates and model is TransientSpectrum and not dataset.followup_id:
                        candidates = _observed_for_spectrum(dataset)
                elif source == "date":
                    candidates = _date_matches(dataset, allocations) if model is TransientSpectrum else set()
                else:
                    raise ValueError("unknown backfill source %r" % (source,))
                if not candidates:
                    continue
                if len(candidates) > 1:
                    ambiguous = True
                    continue
                chosen = (source, next(iter(candidates)))
                break
            if chosen is None:
                counts["ambiguous" if ambiguous else "unmatched"] += 1
                continue
            counts["linked_%s" % chosen[0]] += 1
            link.append((model, dataset.pk, chosen[1]))
    return {"link": link, "counts": counts}


def _observed_for_spectrum(spectrum) -> Set[int]:
    from YSE_App.models import ObservedBlock

    if spectrum.obs_date is None:
        return set()
    return {a for a in ObservedBlock.objects.filter(
        transient_id=spectrum.transient_id, night__obs_date__gt=spectrum.obs_date - datetime.timedelta(days=2),
        night__obs_date__lte=spectrum.obs_date).values_list("night__resource__allocation_id", flat=True) if a}


def apply_backfill(plan: dict) -> int:
    """Write a :func:`backfill_plan` (only datasets still unlinked) and apply standing grants. Returns the count."""
    n = 0
    for model, dataset_id, allocation_id in plan["link"]:
        dataset = model.objects.filter(pk=dataset_id, allocation__isnull=True).first()
        if dataset is None:
            continue
        _set_link(dataset, allocation_id, new_dataset=False)
        n += 1
    return n
