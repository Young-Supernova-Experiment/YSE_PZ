"""Keep ``quality_mask`` in step with the DataQuality ManyToMany (#81).

The bit rules live in ``quality_mask.py`` (``merge_data_quality``,
``apply_wavelength_medium``); this module reads the through tables and writes
the masks. It works on the live models and on a migration's historical models
alike (only ``_meta`` and managers are used), so migration 0036 and the
``backfill_quality_mask`` command share it.

Writes use ``QuerySet.update``: no ``save()``/``post_save`` runs, so the
PhotStat refresh is left to the signal that called us.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional

from YSE_App.common.quality_mask import apply_wavelength_medium, merge_data_quality

#: Models that carry ``quality_mask`` and a ``data_quality`` ManyToMany.
MASKED_MODELS = ('TransientPhotData', 'HostPhotData', 'TransientSpectrum', 'HostSpectrum')


def _through_owner_attname(model) -> str:
    """Attribute of ``model.data_quality.through`` that holds ``model``'s pk (e.g. ``transientphotdata_id``)."""
    field = model._meta.get_field('data_quality')
    return '%s_id' % field.m2m_field_name()


def _has_wavelength_medium(model) -> bool:
    return any(f.name == 'wavelength_medium' for f in model._meta.concrete_fields)


def data_quality_names(model, pks: Iterable[int]) -> Dict[int, List[str]]:
    """``{pk: [DataQuality name, ...]}`` for the rows of ``model`` in ``pks`` that have any."""
    pks = list(pks)
    out: Dict[int, List[str]] = defaultdict(list)
    if not pks:
        return out
    through = model._meta.get_field('data_quality').remote_field.through
    owner = _through_owner_attname(model)
    rows = through.objects.filter(**{'%s__in' % owner: pks}).values_list(owner, 'dataquality__name')
    for pk, name in rows:
        out[pk].append(name)
    return out


def desired_mask(model, current: int, names: Iterable[str], medium=None, *, with_medium: bool) -> int:
    mask = merge_data_quality(current, names, model._meta.object_name)
    if with_medium:
        mask = apply_wavelength_medium(mask, medium)
    return mask


def _write(model, changes: Dict[int, int]) -> None:
    """One UPDATE per distinct new value (flag combinations are few)."""
    by_value: Dict[int, List[int]] = defaultdict(list)
    for pk, value in changes.items():
        by_value[value].append(pk)
    for value, pks in by_value.items():
        for i in range(0, len(pks), 1000):
            model.objects.filter(pk__in=pks[i:i + 1000]).update(quality_mask=value)


def recompute(model, pks: Iterable[int]) -> Dict[int, int]:
    """Recompute the DataQuality-managed bits of ``model`` rows ``pks``; returns ``{pk: mask}``.

    Only rows whose mask changes are written. Bits that DataQuality does not
    manage (NOT_PUBLISHED, WAVELENGTH_MEDIUM_UNKNOWN, flags set directly) are kept.
    """
    return _recompute(model, pks)[0]


def _recompute(model, pks: Iterable[int]):
    pks = [pk for pk in set(pks) if pk is not None]
    if not pks:
        return {}, 0
    names = data_quality_names(model, pks)
    result: Dict[int, int] = {}
    changes: Dict[int, int] = {}
    for pk, current in model.objects.filter(pk__in=pks).values_list('pk', 'quality_mask'):
        new = merge_data_quality(current, names.get(pk, ()), model._meta.object_name)
        result[pk] = new
        if new != current:
            changes[pk] = new
    _write(model, changes)
    return result, len(changes)


def scan(model, *, apply: bool, batch_size: int = 5000,
         on_change: Optional[Callable[[int, int, int, List[str]], None]] = None) -> dict:
    """Check (and with ``apply``, fix) every row of ``model``, in primary-key batches.

    Returns ``{'rows', 'changed', 'flagged', 'by_name'}``. ``on_change(pk, old,
    new, names)`` is called for each row whose mask differs.
    """
    with_medium = _has_wavelength_medium(model)
    fields = ['pk', 'quality_mask'] + (['wavelength_medium'] if with_medium else [])
    stats = {'rows': 0, 'changed': 0, 'flagged': 0, 'by_name': defaultdict(int)}
    last_pk = None
    while True:
        qs = model.objects.order_by('pk')
        if last_pk is not None:
            qs = qs.filter(pk__gt=last_pk)
        batch = list(qs.values_list(*fields)[:batch_size])
        if not batch:
            break
        last_pk = batch[-1][0]
        names = data_quality_names(model, [row[0] for row in batch])
        changes: Dict[int, int] = {}
        for row in batch:
            pk, current = row[0], row[1]
            medium = row[2] if with_medium else None
            row_names = names.get(pk, [])
            stats['rows'] += 1
            if row_names:
                stats['flagged'] += 1
                for name in row_names:
                    stats['by_name'][name] += 1
            new = desired_mask(model, current, row_names, medium, with_medium=with_medium)
            if new != current:
                changes[pk] = new
                if on_change is not None:
                    on_change(pk, current, new, row_names)
        stats['changed'] += len(changes)
        if apply:
            _write(model, changes)
    stats['by_name'] = dict(stats['by_name'])
    return stats


def backfill_flagged(model) -> int:
    """Set the mask of every row that has a DataQuality (migration 0036). Returns rows written."""
    through = model._meta.get_field('data_quality').remote_field.through
    owner = _through_owner_attname(model)
    pks = list(through.objects.values_list(owner, flat=True).distinct())
    written = 0
    for i in range(0, len(pks), 5000):
        written += _recompute(model, pks[i:i + 5000])[1]
    return written


def backfill_wavelength_medium(model) -> int:
    """Set WAVELENGTH_MEDIUM_UNKNOWN on every spectrum whose medium is NULL or unknown (migration 0036)."""
    from django.db.models import F

    from YSE_App.common.quality_mask import SpecFlag, WavelengthMedium

    bit = int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN)
    known = [WavelengthMedium.AIR.value, WavelengthMedium.VACUUM.value]
    unknown = model.objects.exclude(wavelength_medium__in=known)
    return unknown.update(quality_mask=F('quality_mask').bitor(bit))
