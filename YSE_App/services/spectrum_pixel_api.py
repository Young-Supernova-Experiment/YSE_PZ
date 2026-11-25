"""Per-pixel REST records for spectra stored as arrays (#891, #492).

``/api/transientspecdata/`` and ``/api/hostspecdata/`` list one record per pixel row. A spectrum
stored only as an arrays row (``TransientSpectrumArrays`` / ``HostSpectrumArrays``, no pixel rows)
is listed as synthesized per-pixel records with the same serializer and fields, after all real rows,
by spectrum id and then wavelength, so ``limit`` / ``offset`` pagination still covers everything.

What a synthesized record cannot reproduce:

* ``id``: a stable negative pseudo id, ``-(spectrum_id * PSEUDO_ID_BASE + pixel index + 1)``
  (index in wavelength order). ``GET /api/transientspecdata/<pseudo id>/`` returns the same record;
  writes (PUT / PATCH) to it are refused, since there is no row to change.
* ``created_by`` / ``modified_by`` / ``created_date``: the spectrum's; ``modified_date``: the arrays
  row's.
* ``wavelength_err``: None; ``pixel_mask``: 0 (arrays do not store them).

A spectrum with both an arrays row and pixel rows (converted, rows not yet deleted; the conversion
verifies they hold the same pixels) is listed from its rows, with their real ids.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
from django.db.models import Exists, OuterRef

from YSE_App.common import spectrum_arrays

#: Pixels per spectrum the pseudo ids leave room for.
PSEUDO_ID_BASE = 10 ** 7


def pseudo_pixel_id(spectrum_id: int, index: int) -> int:
    return -(int(spectrum_id) * PSEUDO_ID_BASE + int(index) + 1)


def decode_pseudo_pixel_id(value) -> Optional[Tuple[int, int]]:
    """``(spectrum_id, index)`` for a pseudo id, None for anything else (a real row id, junk)."""
    try:
        v = int(str(value))
    except (TypeError, ValueError):
        return None
    if v >= 0:
        return None
    sid, index = divmod(-v - 1, PSEUDO_ID_BASE)
    return (sid, index) if sid > 0 else None


def array_only_spectra(arrays_model, rows_model, authorized_spectra):
    """Arrays rows of authorized spectra that have no pixel rows."""
    return (arrays_model.objects.filter(spectrum_id__in=authorized_spectra.values('id'))
            .exclude(Exists(rows_model.objects.filter(spectrum_id=OuterRef('spectrum_id')))))


def _pixel(rows_model, spectrum, modified_date, index, wave, flux, err):
    e = None
    if err is not None and np.isfinite(err[index]):
        e = float(err[index])
    return rows_model(
        id=pseudo_pixel_id(spectrum.pk, index), spectrum=spectrum,
        wavelength=float(wave[index]), flux=float(flux[index]), flux_err=e, wavelength_err=None, pixel_mask=0,
        created_by_id=spectrum.created_by_id, modified_by_id=spectrum.modified_by_id,
        created_date=spectrum.created_date, modified_date=modified_date,
    )


def synthesize(rows_model, spec_model, arrays_model, spectrum_ids, *, host, ranges=None) -> List:
    """Unsaved ``rows_model`` instances for the pixels of these array-stored spectra.

    ``ranges``: optional ``{spectrum_id: (start, stop)}`` pixel-index range per spectrum.
    """
    spectrum_ids = list(spectrum_ids)
    if not spectrum_ids:
        return []
    pixels = spectrum_arrays.arrays_for(spectrum_ids, host=host)
    spectra = spec_model.objects.in_bulk(spectrum_ids)
    modified = dict(arrays_model.objects.filter(spectrum_id__in=spectrum_ids)
                    .values_list('spectrum_id', 'modified_date'))
    out = []
    for sid in spectrum_ids:
        if sid not in pixels or sid not in spectra:
            continue
        wave, flux, err = pixels[sid]
        start, stop = (ranges or {}).get(sid, (0, wave.size))
        for i in range(max(start, 0), min(stop, wave.size)):
            out.append(_pixel(rows_model, spectra[sid], modified.get(sid), i, wave, flux, err))
    return out


class PixelList:
    """Real pixel rows followed by synthesized pixels of array-only spectra, sliceable and countable
    like a queryset so DRF's ``LimitOffsetPagination`` pages through both."""

    def __init__(self, rows, arrays, *, rows_model, spec_model, arrays_model, host):
        self.rows = rows
        self.models = (rows_model, spec_model, arrays_model)
        self.host = host
        self.arrays = list(arrays.order_by('spectrum_id').values_list('spectrum_id', 'n_pixels'))
        self._n_rows = None

    def _rows_count(self):
        if self._n_rows is None:
            self._n_rows = self.rows.count()
        return self._n_rows

    def count(self):
        return self._rows_count() + sum(n for _sid, n in self.arrays)

    def __len__(self):
        return self.count()

    def __iter__(self):
        return iter(self[0:self.count()])

    def __getitem__(self, key):
        if not isinstance(key, slice) or key.step not in (None, 1):
            raise TypeError('PixelList supports only plain slices')
        total = self.count()
        start, stop, _step = key.indices(total)
        n_rows = self._rows_count()
        out = list(self.rows[start:min(stop, n_rows)]) if start < n_rows else []
        vstart, vstop = max(start - n_rows, 0), stop - n_rows
        if vstop <= vstart:
            return out
        ranges, offset = {}, 0
        for sid, n in self.arrays:
            if offset >= vstop:
                break
            if offset + n > vstart:
                ranges[sid] = (max(vstart - offset, 0), min(vstop - offset, n))
            offset += n
        rows_model, spec_model, arrays_model = self.models
        out.extend(synthesize(rows_model, spec_model, arrays_model, list(ranges), host=self.host, ranges=ranges))
        return out


def pseudo_pixel(rows_model, spec_model, arrays_model, authorized_spectra, pk, *, host):
    """The synthesized record for pseudo id ``pk`` if its spectrum is authorized and array-only, else None."""
    decoded = decode_pseudo_pixel_id(pk)
    if decoded is None:
        return None
    sid, index = decoded
    if not array_only_spectra(arrays_model, rows_model, authorized_spectra).filter(spectrum_id=sid).exists():
        return None
    found = synthesize(rows_model, spec_model, arrays_model, [sid], host=host, ranges={sid: (index, index + 1)})
    return found[0] if found else None
