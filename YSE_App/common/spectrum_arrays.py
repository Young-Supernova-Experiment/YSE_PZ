"""
Spectrum pixels as arrays (#492, #891): the one way to write and read a spectrum's wavelength, flux and error.

A spectrum's pixels are stored either as one ``TransientSpectrumArrays`` / ``HostSpectrumArrays`` row
(compressed float64 arrays) or, for spectra written before #891, as per-pixel ``TransientSpecData`` /
``HostSpecData`` rows. :func:`arrays_for` reads both, so callers need not know which.

Design (#891, #492): every reader of pixels goes through :func:`arrays_for` / :func:`pixel_counts`
(dual read; an arrays row wins). Every writer of a spectrum's pixels goes through
:func:`write_pixels`, which replaces whatever the spectrum had and writes an arrays row, or per-pixel
rows while the setting ``SPECTRUM_PIXEL_STORAGE`` is ``rows`` (the rollback switch). The REST pixel lists (``/api/transientspecdata/``,
``/api/hostspecdata/``) stay per-pixel: ``services.spectrum_pixel_api`` synthesizes records with
negative pseudo ids for spectra stored only as arrays, and lists a spectrum that has both from its
rows (real ids; ``convert_spectra_to_arrays`` verifies both hold the same pixels). Backfill:
``manage.py convert_spectra_to_arrays``.
"""

from __future__ import annotations

import zlib
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

Arrays = Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]

_DTYPE = '<f8'


def pack(values) -> bytes:
    return zlib.compress(np.ascontiguousarray(values, dtype=_DTYPE).tobytes(), 6)


def unpack(blob) -> np.ndarray:
    return np.frombuffer(zlib.decompress(bytes(blob)), dtype=_DTYPE)


def _models(host: bool):
    from YSE_App.models.spectra_models import HostSpecData, HostSpectrumArrays, TransientSpecData, TransientSpectrumArrays

    return (HostSpectrumArrays, HostSpecData) if host else (TransientSpectrumArrays, TransientSpecData)


def build(spectrum, wavelength: Sequence[float], flux: Sequence[float], flux_err: Optional[Sequence[float]] = None, *,
          host: bool = False):
    """An unsaved arrays row for ``spectrum``: pixels sorted by wavelength, non-finite wavelength or flux dropped.

    ``flux_err`` may be None, or contain None / NaN for pixels without an error (stored as NaN).
    """
    w = np.asarray(wavelength, dtype=float)
    f = np.asarray(flux, dtype=float)
    e = None
    if flux_err is not None:
        e = np.array([np.nan if v is None else v for v in flux_err], dtype=float)
    keep = np.isfinite(w) & np.isfinite(f)
    order = np.argsort(w[keep], kind='stable')
    w, f = w[keep][order], f[keep][order]
    if e is not None:
        e = e[keep][order]
        if not np.isfinite(e).any():
            e = None
    if not len(w):
        raise ValueError('spectrum has no finite pixels')
    model = _models(host)[0]
    return model(spectrum=spectrum, wavelength=pack(w), flux=pack(f), flux_err=pack(e) if e is not None else None,
                 n_pixels=len(w), wavelength_min=float(w[0]), wavelength_max=float(w[-1]))


def save(spectrum, wavelength, flux, flux_err=None, *, host: bool = False):
    """Write (or replace) the arrays of a saved spectrum."""
    row = build(spectrum, wavelength, flux, flux_err, host=host)
    row.save()
    return row


def arrays_for(spectrum_ids: Iterable[int], *, host: bool = False, with_errors: bool = True) -> Dict[int, Arrays]:
    """``{spectrum id: (wavelength, flux, flux_err or None)}`` sorted by wavelength, for the ids that have pixels.

    Array rows win; spectra without one are read from their per-pixel rows (one query for all).
    """
    ids = list(spectrum_ids)
    if not ids:
        return {}
    arrays_model, rows_model = _models(host)
    out: Dict[int, Arrays] = {}
    fields = ['spectrum_id', 'wavelength', 'flux'] + (['flux_err'] if with_errors else [])
    for row in arrays_model.objects.filter(spectrum_id__in=ids).values_list(*fields):
        err = unpack(row[3]) if with_errors and row[3] is not None else None
        out[row[0]] = (unpack(row[1]), unpack(row[2]), err)
    missing = [i for i in ids if i not in out]
    if missing:
        collected: Dict[int, list] = {}
        for sid, w, f, e in rows_model.objects.filter(spectrum_id__in=missing).order_by(
                'spectrum_id', 'wavelength', 'id').values_list('spectrum_id', 'wavelength', 'flux', 'flux_err'):
            collected.setdefault(sid, []).append((w, f, e))
        for sid, pixels in collected.items():
            w = np.array([p[0] for p in pixels], dtype=float)
            f = np.array([p[1] for p in pixels], dtype=float)
            e = np.array([np.nan if p[2] is None else p[2] for p in pixels], dtype=float) if with_errors else None
            if e is not None and not np.isfinite(e).any():
                e = None
            out[sid] = (w, f, e)
    return out


def pixel_counts(spectrum_ids: Iterable[int], *, host: bool = False) -> Dict[int, int]:
    """``{spectrum id: number of pixels}`` from either storage, without unpacking arrays."""
    from django.db.models import Count

    ids = list(spectrum_ids)
    arrays_model, rows_model = _models(host)
    out = dict(arrays_model.objects.filter(spectrum_id__in=ids).values_list('spectrum_id', 'n_pixels'))
    rest = [i for i in ids if i not in out]
    if rest:
        out.update(rows_model.objects.filter(spectrum_id__in=rest).values('spectrum_id').annotate(
            n=Count('id')).values_list('spectrum_id', 'n'))
    return out


def discard(spectrum_ids: Iterable[int], *, host: bool = False) -> int:
    """Delete the arrays rows of these spectra (when their pixels are being rewritten as rows), so stale
    arrays never win over the new rows. Returns the number deleted."""
    ids = list(spectrum_ids)
    if not ids:
        return 0
    return _models(host)[0].objects.filter(spectrum_id__in=ids).delete()[0]


def has_pixels(spectrum_id: int, *, host: bool = False) -> bool:
    """True when the spectrum has pixels in either storage."""
    arrays_model, rows_model = _models(host)
    return (arrays_model.objects.filter(spectrum_id=spectrum_id).exists()
            or rows_model.objects.filter(spectrum_id=spectrum_id).exists())


def storage() -> str:
    """``arrays`` (default) or ``rows``: how :func:`write_pixels` stores new pixels."""
    from django.conf import settings

    value = str(getattr(settings, 'SPECTRUM_PIXEL_STORAGE', 'arrays') or 'arrays').strip().lower()
    return 'rows' if value == 'rows' else 'arrays'


def write_pixels(spectrum, pixels: Iterable[Sequence], *, user=None, host: bool = False) -> int:
    """Replace a saved spectrum's pixels with ``pixels`` (``(wavelength, flux[, flux_err])`` each).

    Deletes its old pixel rows and arrays row first. Returns the number of pixels written (pixels
    with a non-finite wavelength or flux are dropped from arrays, as before they were rejected by
    MySQL). Writes nothing for an empty list.
    """
    arrays_model, rows_model = _models(host)
    rows_model.objects.filter(spectrum=spectrum).delete()
    discard([spectrum.pk], host=host)
    pixels = [tuple(p) for p in pixels]
    if not pixels:
        return 0
    errors = [p[2] if len(p) > 2 else None for p in pixels]
    if storage() == 'rows':
        audit = {'created_by': user, 'modified_by': user} if user is not None else {}
        rows_model.objects.bulk_create([
            rows_model(spectrum=spectrum, wavelength=p[0], flux=p[1], flux_err=e, **audit)
            for p, e in zip(pixels, errors)], batch_size=2000)
        return len(pixels)
    try:
        row = save(spectrum, [p[0] for p in pixels], [p[1] for p in pixels],
                   errors if any(e is not None for e in errors) else None, host=host)
    except ValueError:
        return 0
    return row.n_pixels
