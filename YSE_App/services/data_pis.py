"""Principal investigators of private photometry and spectra (#446).

A dataset (``TransientPhotometry`` / ``TransientSpectrum``) has zero or more
PIs through its ``pis`` M2M to :class:`PrincipalInvestigator`. A PI row may be
linked to a YSE-PZ account (``PrincipalInvestigator.user``, set by staff in the
admin); only linked PIs can be notified or decide data-access requests, while
the PI *name* is what the pages show.

Everything that needs "the PIs of this dataset" goes through :func:`pis_for`
(rows) or :func:`pi_users_for` (accounts), so a second source of PIs can be
added in one place:

* **Programs (Stage 5, decision-trail schema).** When ``ScienceProgram`` and the
  nullable ``program`` FK on the data exist, :func:`_program_pis` returns the
  program's PI(s) and :func:`pis_for` merges them with the explicit ones. Until
  then it returns nothing.

* **Allocations (#447), a read-time fallback.** A dataset with no explicit
  (or program) PI takes the PI of the allocation it was observed under:
  ``followup`` -> the follow-up's ToO / classical / queued resource -> that
  resource's ``Allocation`` PI (or the resource's own PI), or the follow-up's
  ``FacilityRequest`` -> its ``Allocation`` PI. Nothing is written; see
  :data:`ALLOCATION_PI_PATHS` and :func:`effective_pi_q` (the same rule in SQL).

The PIs are separate from group owners (``GroupProfile.owners``, #445): owners
manage a team's membership, PIs lead a dataset. Only PIs and the deputies they
designate (``DataSharingDelegate``, #447) may share private data.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from django.contrib.auth.models import User
from django.db.models import Q

from YSE_App.models import PrincipalInvestigator, TransientPhotometry, TransientSpectrum

DATASET_MODELS = (TransientPhotometry, TransientSpectrum)


def _program_pis(dataset) -> List[PrincipalInvestigator]:
    """PIs derived from the dataset's program.

    TODO(Stage 5, decision-trail schema): return ``dataset.program``'s PI(s)
    once ``ScienceProgram`` and the nullable ``program`` FK on
    ``TransientPhotometry`` / ``TransientSpectrum`` exist. The bulk lookups
    that read ``pis`` directly in SQL (the PI names in
    ``data_access.hidden_summary``, the Exists() filter in
    ``data_access.requests_to_decide``, the ``pis`` prefetch on the detail-page
    tables) need the same extension then.
    """
    return []


# Read-time fallback (#447): where a dataset's allocation PI is found, as ORM paths from the dataset.
ALLOCATION_PI_PATHS = (
    "followup__too_resource__allocation__principal_investigator",
    "followup__classical_resource__allocation__principal_investigator",
    "followup__queued_resource__allocation__principal_investigator",
    "followup__facility_requests__allocation__principal_investigator",
    "followup__too_resource__principal_investigator",
    "followup__classical_resource__principal_investigator",
    "followup__queued_resource__principal_investigator",
)


def _allocation_pi_ids(dataset) -> List[int]:
    """Ids of the allocation PIs of one dataset (one query; none without a follow-up)."""
    if not getattr(dataset, "pk", None) or not getattr(dataset, "followup_id", None):
        return []
    ids = set()
    for row in type(dataset).objects.filter(pk=dataset.pk).values_list(*ALLOCATION_PI_PATHS):
        ids.update(v for v in row if v)
    return sorted(ids)


def allocation_pis_for(dataset) -> List[PrincipalInvestigator]:
    """The PIs of the allocation(s) a dataset was observed under (the fallback, whether or not it applies)."""
    ids = _allocation_pi_ids(dataset)
    if not ids:
        return []
    return list(PrincipalInvestigator.objects.filter(pk__in=ids).select_related("user"))


def explicit_pis_for(dataset) -> List[PrincipalInvestigator]:
    """The explicit ``pis`` plus (later) the program's, de-duplicated (no fallback)."""
    if not getattr(dataset, "pk", None):
        rows = []
    elif "pis" in getattr(dataset, "_prefetched_objects_cache", {}):
        rows = list(dataset.pis.all())
    else:
        rows = list(dataset.pis.select_related("user"))
    seen = {r.pk for r in rows}
    for row in _program_pis(dataset):
        if row.pk not in seen:
            seen.add(row.pk)
            rows.append(row)
    return rows


def pis_for(dataset) -> List[PrincipalInvestigator]:
    """The PI rows of one dataset, by name: the explicit ``pis`` plus (later) the program's; when there are
    none, the PIs of the dataset's allocation (#447, read-time fallback)."""
    rows = explicit_pis_for(dataset) or allocation_pis_for(dataset)
    return sorted(rows, key=lambda r: (r.name or "", r.pk))


def effective_pi_q(model, pi_ids) -> Q:
    """``Q`` on ``model`` (photometry or spectra): datasets whose :func:`pis_for` includes one of ``pi_ids``.

    The SQL form of :func:`pis_for` (explicit PIs, else the allocation fallback), as ``pk__in``
    subqueries so it combines with other filters on the same model without sharing their joins.
    TODO(Stage 5): add the program-derived PIs here too.
    """
    pi_ids = list(pi_ids)
    explicit = model.objects.filter(pis__in=pi_ids).values("pk")
    has_explicit = model.objects.filter(pis__isnull=False).values("pk")
    fallback_q = Q()
    for path in ALLOCATION_PI_PATHS:
        fallback_q |= Q(**{"%s__in" % path: pi_ids})
    fallback = model.objects.filter(fallback_q).values("pk")
    return Q(pk__in=explicit) | (~Q(pk__in=has_explicit) & Q(pk__in=fallback))


def pi_users_for(datasets) -> List[User]:
    """Active YSE-PZ accounts of the PIs of one dataset or several (de-duplicated, ordered by id)."""
    if hasattr(datasets, "_meta"):  # one model instance
        datasets = [datasets]
    users: Dict[int, User] = {}
    for dataset in datasets:
        for pi in pis_for(dataset):
            user = pi.user if pi.user_id else None
            if user is not None and user.is_active:
                users[user.pk] = user
    return [users[k] for k in sorted(users)]


def resolve_pis(refs) -> Tuple[List[PrincipalInvestigator], List[str]]:
    """Turn upload input into PI rows: ids (ints or digit strings), a comma-separated string, or PI rows.

    Returns ``(pis, unknown)``; unknown references are reported, never created.
    """
    if refs is None or refs == "":
        return [], []
    if isinstance(refs, (str, int)):
        refs = [refs]
    pis: List[PrincipalInvestigator] = []
    unknown: List[str] = []
    ids: List[int] = []
    for ref in refs:
        if isinstance(ref, PrincipalInvestigator):
            pis.append(ref)
            continue
        for part in str(ref).split(","):
            part = part.strip()
            if not part:
                continue
            if part.isdigit():
                ids.append(int(part))
            else:
                unknown.append(part)
    if ids:
        found = {p.pk: p for p in PrincipalInvestigator.objects.filter(pk__in=ids)}
        for pk in ids:
            if pk in found:
                pis.append(found[pk])
            else:
                unknown.append(str(pk))
    unique: Dict[int, PrincipalInvestigator] = {p.pk: p for p in pis}
    return list(unique.values()), unknown


def set_pis(dataset, pis: Sequence[PrincipalInvestigator], *, replace: bool = True) -> None:
    """Set (``replace``) or add the dataset's explicit PIs."""
    if not getattr(dataset, "pk", None):
        return
    if replace:
        dataset.pis.set(list(pis))
    elif pis:
        dataset.pis.add(*pis)


def apply_upload_pis(dataset, refs) -> List[str]:
    """Add the PIs named in an upload (``pis`` key) to a saved dataset; return the unknown references."""
    pis, unknown = resolve_pis(refs)
    set_pis(dataset, pis, replace=False)
    return unknown


def user_can_edit_pis(user, dataset) -> bool:
    """Staff; for restricted data that already has PIs (explicit or by the allocation fallback), only those PIs'
    accounts (#447: the PIs decide who shares, so members cannot make themselves PIs); for restricted data
    without a PI, a member of one of its groups; public data: any signed-in writer.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    if not getattr(dataset, "pk", None):
        return True
    group_ids = set(dataset.groups.values_list("id", flat=True))
    if not group_ids:
        return True
    current = pis_for(dataset)
    if current:
        return any(p.user_id == user.pk for p in current)
    return user.groups.filter(pk__in=group_ids).exists()


# ---------------------------------------------------------------------------
# optional backfill (manage.py backfill_data_pis; dry run by default)


def _allocation_pis_for_spectrum(spectrum, allocations) -> List[PrincipalInvestigator]:
    """PIs of the allocations that cover a spectrum: same instrument (or telescope when the allocation names none),
    observed between the allocation's start and end."""
    found = {}
    for alloc in allocations:
        if alloc.instrument_id is not None:
            if alloc.instrument_id != spectrum.instrument_id:
                continue
        elif alloc.telescope_id != spectrum.instrument.telescope_id:
            continue
        if spectrum.obs_date is None or not (alloc.start_date <= spectrum.obs_date <= alloc.end_date):
            continue
        found[alloc.principal_investigator_id] = alloc.principal_investigator
    return list(found.values())


def backfill_plan(*, include_public: bool = False, max_pis: int = 1, sources=("allocation", "obs_group")) -> dict:
    """What a backfill would set, without writing anything.

    Only datasets with no PIs yet are considered (restricted ones only, unless
    ``include_public``). Per dataset the first source that yields between 1 and
    ``max_pis`` PIs wins:

    * ``allocation``: spectra only, an ``Allocation`` with a PI on the same
      instrument (or telescope) whose dates cover the spectrum's ``obs_date``
      (photometry sets span many epochs, so they are not matched by date);
    * ``obs_group``: the PIs linked to the dataset's observation group
      (``PrincipalInvestigator.obs_group``).

    Returns ``{"assign": [(model, dataset_id, [pi ids])], "counts": {...}}``;
    the counts are ``assigned_<source>``, ``ambiguous`` (more than ``max_pis``)
    and ``unmatched``.
    """
    from YSE_App.models import Allocation

    obs_group_pis: Dict[int, List[PrincipalInvestigator]] = {}
    for pi in PrincipalInvestigator.objects.prefetch_related("obs_group"):
        for og in pi.obs_group.all():
            obs_group_pis.setdefault(og.pk, []).append(pi)
    allocations = list(
        Allocation.objects.filter(principal_investigator__isnull=False).select_related("principal_investigator")
    )
    counts = {"assigned_allocation": 0, "assigned_obs_group": 0, "ambiguous": 0, "unmatched": 0}
    assign = []
    for model in DATASET_MODELS:
        qs = model.objects.filter(pis__isnull=True)
        if not include_public:
            qs = qs.filter(groups__isnull=False)
        qs = qs.distinct().select_related("instrument")
        for dataset in qs.iterator():
            chosen, ambiguous = None, False
            for source in sources:
                if source == "allocation":
                    if model is not TransientSpectrum:
                        continue
                    candidates = _allocation_pis_for_spectrum(dataset, allocations)
                elif source == "obs_group":
                    candidates = obs_group_pis.get(dataset.obs_group_id, [])
                else:
                    raise ValueError("unknown backfill source %r" % (source,))
                if not candidates:
                    continue
                if len(candidates) > max_pis:
                    ambiguous = True
                    continue
                chosen = (source, candidates)
                break
            if chosen is None:
                counts["ambiguous" if ambiguous else "unmatched"] += 1
                continue
            source, candidates = chosen
            counts["assigned_%s" % source] += 1
            assign.append((model, dataset.pk, sorted(p.pk for p in candidates)))
    return {"assign": assign, "counts": counts}


def apply_backfill(plan: dict) -> int:
    """Write a :func:`backfill_plan` (adds PIs; never removes any). Returns the number of datasets changed."""
    n = 0
    for model, dataset_id, pi_ids in plan["assign"]:
        dataset = model.objects.filter(pk=dataset_id).first()
        if dataset is None or dataset.pis.exists():
            continue
        dataset.pis.add(*pi_ids)
        n += 1
    return n
