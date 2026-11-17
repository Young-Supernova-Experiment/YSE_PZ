"""
Duplicated photometry and spectra within one transient (#890): find them, flag all but one copy, undo.

Copies of one observation (a repeat upload, or our report that came back in a TNS import) are found
with the same-origin tests of :mod:`matching` (:func:`matching.duplicate_photometry`,
:func:`matching.duplicate_spectra`). Of each group one copy is kept, preferring, in order:

1. our own reduction over a copy from TNS (header ``obs_group`` ``TNS``, or a ``reference`` starting
   ``TNS``);
2. a copy with errors over one without;
3. the earliest upload.

The others get the REJECTED quality bit, and a :class:`DuplicateDataDecision` row records the
decision and the previous ``quality_mask``. Only ``quality_mask`` changes: no point or spectrum moves
between headers, and no groups or allocations change (access is per point).

Never considered: points and spectra imported from a catalogue (they have a ``catalog_info`` row, or
their header has one; the catalogue import matches those itself), rows already flagged REJECTED,
and rows the clean-up flagged before (unless reverted), so a person who clears the flag by hand is
not overruled by the next run.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from YSE_App.catalog_import import matching
from YSE_App.common.quality_mask import PhotFlag, SpecFlag
from YSE_App.common.tns_photometry_map import TNS_OBS_GROUP_NAME

PHOTOMETRY, SPECTRUM = 'photometry', 'spectrum'
PHOT_REJECTED = int(PhotFlag.REJECTED)
SPEC_REJECTED = int(SpecFlag.REJECTED)


@dataclass
class Copy:
    """One copy of an observation and what decides whether it is kept."""
    obs: matching.Obs
    tns: bool
    errors: bool
    created: Optional[datetime.datetime]
    quality_mask: int
    access: Tuple  # (group ids, allocation id): who may see it


@dataclass
class Flag:
    kind: str
    copy: Copy
    kept: Copy
    reason: str
    weak: bool
    access_differs: bool

    @property
    def time_difference(self) -> float:
        return abs(self.copy.obs.mjd - self.kept.obs.mjd)


def is_tns(obs_group_name: Optional[str], reference: Optional[str]) -> bool:
    return (obs_group_name or '') == TNS_OBS_GROUP_NAME or (reference or '').strip().upper().startswith('TNS')


def _keep_order(c: Copy):
    created = c.created or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return (c.tns, not c.errors, created, c.obs.key)


def _reason(flagged: Copy, kept: Copy) -> str:
    from YSE_App.models import DuplicateDataDecision as D

    if flagged.tns and not kept.tns:
        return D.TNS_COPY
    if kept.errors and not flagged.errors:
        return D.NO_ERRORS
    return D.LATER_UPLOAD


def choose(kind: str, group: List[Copy], unknown_id: Optional[int]) -> List[Flag]:
    """Keep the best copy of one group; flag the rest."""
    ordered = sorted(group, key=_keep_order)
    kept = ordered[0]
    return [Flag(kind, c, kept, _reason(c, kept),
                 weak=any(o.instrument_id is None or o.instrument_id == unknown_id for o in (c.obs, kept.obs)),
                 access_differs=c.access != kept.access)
            for c in ordered[1:]]


def unknown_instrument_id() -> Optional[int]:
    from YSE_App.models import Instrument

    return Instrument.objects.filter(name='Unknown').order_by('id').values_list('id', flat=True).first()


def _decided(transient_id: int, field: str) -> set:
    from YSE_App.models import DuplicateDataDecision

    return set(DuplicateDataDecision.objects.filter(transient_id=transient_id, reverted_date__isnull=True)
               .exclude(**{field: None}).values_list(field, flat=True))


def _groups_of(rows: Iterable[Tuple[int, Optional[int]]]) -> Dict[int, frozenset]:
    out: Dict[int, set] = {}
    for pk, group_id in rows:
        out.setdefault(pk, set())
        if group_id is not None:
            out[pk].add(group_id)
    return {pk: frozenset(g) for pk, g in out.items()}


def photometry_flags(transient_id: int, unknown_id: Optional[int], *,
                     tolerance: float = matching.DUPLICATE_PHOT_TIME_TOLERANCE) -> List[Flag]:
    from YSE_App.models import TransientPhotData, TransientPhotometry
    from YSE_App.models.catalog_models import TransientPhotDataCatalogInfo, TransientPhotometryCatalogInfo

    headers = {pk: (group, ref, alloc) for pk, group, ref, alloc in TransientPhotometry.objects.filter(
        transient_id=transient_id).values_list('id', 'obs_group__name', 'reference', 'allocation_id')}
    if not headers:
        return []
    catalog_headers = set(TransientPhotometryCatalogInfo.objects.filter(photometry_id__in=list(headers))
                          .values_list('photometry_id', flat=True))
    header_groups = _groups_of(TransientPhotometry.objects.filter(id__in=list(headers)).values_list('id', 'groups'))
    skip = set(TransientPhotDataCatalogInfo.objects.filter(phot_data__photometry__transient_id=transient_id)
               .values_list('phot_data_id', flat=True)) | _decided(transient_id, 'phot_data_id')
    meta = {}
    for pk, header, mag_err, flux_err, created, mask, alloc in (
            TransientPhotData.objects.filter(photometry__transient_id=transient_id)
            .exclude(photometry_id__in=list(catalog_headers))
            .values_list('id', 'photometry_id', 'mag_err', 'flux_err', 'created_date', 'quality_mask', 'allocation_id')):
        if pk in skip or (mask or 0) & PHOT_REJECTED:
            continue
        group, ref, header_alloc = headers[header]
        meta[pk] = (is_tns(group, ref), mag_err is not None or flux_err is not None, created, mask or 0,
                    (header_groups.get(header, frozenset()), alloc or header_alloc))
    obs = [o for o in matching.existing_photometry(transient_id, catalog_headers) if o.key in meta]
    flags = []
    for group in matching.duplicate_photometry(obs, unknown_id, tolerance=tolerance):
        flags.extend(choose(PHOTOMETRY, [Copy(o, *meta[o.key]) for o in group], unknown_id))
    return flags


def spectrum_flags(transient_id: int, unknown_id: Optional[int], *,
                   tolerance: float = matching.DUPLICATE_SPEC_TIME_TOLERANCE) -> List[Flag]:
    from YSE_App.common.spectrum_arrays import with_flux_errors
    from YSE_App.models import TransientSpectrum
    from YSE_App.models.catalog_models import TransientSpectrumCatalogInfo

    rows = list(TransientSpectrum.objects.filter(transient_id=transient_id).values_list(
        'id', 'obs_group__name', 'reference', 'created_date', 'quality_mask', 'allocation_id'))
    if len(rows) < 2:
        return []
    ids = [r[0] for r in rows]
    skip = set(TransientSpectrumCatalogInfo.objects.filter(spectrum_id__in=ids).values_list('spectrum_id', flat=True))
    skip |= _decided(transient_id, 'spectrum_id')
    skip |= {pk for pk, _, _, _, mask, _ in rows if (mask or 0) & SPEC_REJECTED}
    groups = _groups_of(TransientSpectrum.objects.filter(id__in=ids).values_list('id', 'groups'))
    errors = with_flux_errors(i for i in ids if i not in skip)
    meta = {pk: (is_tns(group, ref), pk in errors, created, mask or 0, (groups.get(pk, frozenset()), alloc))
            for pk, group, ref, created, mask, alloc in rows if pk not in skip}
    obs = matching.existing_spectra(transient_id, skip)
    flags = []
    for group in matching.duplicate_spectra(obs, unknown_id, tolerance=tolerance):
        flags.extend(choose(SPECTRUM, [Copy(o, *meta[o.key]) for o in group], unknown_id))
    return flags


def apply(transient_id: int, flags: List[Flag]) -> int:
    """Flag the copies REJECTED and record each decision, in one transaction. Returns the number flagged."""
    from YSE_App.models import DuplicateDataDecision, TransientPhotData, TransientSpectrum

    done = 0
    with transaction.atomic():
        for f in flags:
            model, bit = (TransientPhotData, PHOT_REJECTED) if f.kind == PHOTOMETRY else (TransientSpectrum, SPEC_REJECTED)
            current = model.objects.select_for_update().filter(pk=f.copy.obs.key).values_list(
                'quality_mask', flat=True).first()
            if current is None or current & bit:
                continue
            model.objects.filter(pk=f.copy.obs.key).update(quality_mask=current | bit)
            photometry = f.kind == PHOTOMETRY
            DuplicateDataDecision.objects.create(
                transient_id=transient_id, kind=f.kind, reason=f.reason, weak=f.weak,
                time_difference=f.time_difference, previous_quality_mask=current,
                phot_data_id=f.copy.obs.key if photometry else None,
                kept_phot_data_id=f.kept.obs.key if photometry else None,
                spectrum_id=None if photometry else f.copy.obs.key,
                kept_spectrum_id=None if photometry else f.kept.obs.key)
            done += 1
    return done


def revert(decisions) -> int:
    """Undo these decisions: clear the REJECTED bit where the previous mask did not have it. Returns the
    number reverted. One transaction per transient."""
    from YSE_App.models import TransientPhotData, TransientSpectrum

    by_transient: Dict[int, list] = {}
    for d in decisions.filter(reverted_date__isnull=True).order_by('transient_id', 'id'):
        by_transient.setdefault(d.transient_id, []).append(d)
    done = 0
    for items in by_transient.values():
        with transaction.atomic():
            for d in items:
                if d.phot_data_id:
                    model, pk, bit = TransientPhotData, d.phot_data_id, PHOT_REJECTED
                else:
                    model, pk, bit = TransientSpectrum, d.spectrum_id, SPEC_REJECTED
                current = model.objects.select_for_update().filter(pk=pk).values_list('quality_mask', flat=True).first()
                if current is not None and current & bit and not d.previous_quality_mask & bit:
                    model.objects.filter(pk=pk).update(quality_mask=current & ~bit)
                d.reverted_date = timezone.now()
                d.save(update_fields=['reverted_date'])
                done += 1
    return done
