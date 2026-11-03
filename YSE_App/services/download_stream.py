"""Streamed bodies of the per-object downloads (#929): ``download_data`` and ``download_photometry``.

Both used to build the whole export in memory and send it at the end. These generators yield it
piece by piece (one photometry set, point or spectrum at a time, rows read with
``QuerySet.iterator``), and the bytes are the same as the old ``JsonResponse`` / ``HttpResponse``
bodies. Access is unchanged: the views pass in the same authorized querysets, and each set's points
and each spectrum's pixels come from the same service calls as before.
"""

import json
import zipfile

import numpy as np
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder

from YSE_App.common.utilities import date_to_mjd
from YSE_App.data import PhotometryService, SpectraService

# Rows read and serialized per batch, and the size at which buffered text is sent.
ROW_BATCH = 1000
FLUSH_CHARS = 64 * 1024


def dumps(value):
    """``json.dumps`` as ``JsonResponse`` calls it."""
    return json.dumps(value, cls=DjangoJSONEncoder)


def serialized(objects):
    """``objects`` as the dicts ``serializers.serialize('json', ...)`` gives (natural foreign keys)."""
    return json.loads(serializers.serialize("json", objects, use_natural_foreign_keys=True))


def serialized_rows(queryset, batch=None):
    """Each row of ``queryset`` as a serialized dict, in query order, ``batch`` rows in memory at a time."""
    batch = batch or ROW_BATCH
    pending = []
    for obj in queryset.iterator(chunk_size=batch):
        pending.append(obj)
        if len(pending) >= batch:
            yield from serialized(pending)
            pending = []
    if pending:
        yield from serialized(pending)


def buffered(pieces, size=None):
    """Join small text pieces into chunks of about ``size`` characters, so the server writes fewer, larger blocks."""
    size = size or FLUSH_CHARS
    buf = []
    n = 0
    for piece in pieces:
        buf.append(piece)
        n += len(piece)
        if n >= size:
            yield "".join(buf)
            buf = []
            n = 0
    if buf:
        yield "".join(buf)


def _with_data(head, items):
    """``head`` (a dumped dict) with a trailing ``"data": [...]`` key holding ``items`` (dumped one by one).

    The same text as dumping the dict with ``data`` added last. The key is always written, also for an
    empty list; callers that omit it for no rows check first.
    """
    yield head[:-1] + ', "data": ['
    for i, item in enumerate(items):
        yield (", " if i else "") + dumps(item)
    yield "]}"


def array_pixel_dicts(spectrum_id, arrays):
    """Pixels of an arrays-stored spectrum (#891) as serialized TransientSpecData dicts, one at a time.

    The same per-pixel shape as serialized TransientSpecData rows, so consumers read both alike;
    there is no row, so ``pk`` is None and only the pixel fields are present.
    """
    wave, flux, err = arrays
    for i in range(wave.size):
        flux_err = None
        if err is not None:
            value = float(err[i])
            flux_err = value if np.isfinite(value) else None
        yield {
            'model': 'YSE_App.transientspecdata',
            'pk': None,
            'fields': {
                'spectrum': spectrum_id,
                'wavelength': float(wave[i]),
                'flux': float(flux[i]),
                'wavelength_err': None,
                'flux_err': flux_err,
                'pixel_mask': 0,
            },
        }


def data_json_chunks(user, transient_qs, authorized_phot, authorized_spectra, array_ids):
    """Text chunks of the ``download_data`` JSON.

    ``transient_qs`` is the slug queryset (first row is the object); ``authorized_phot`` and
    ``authorized_spectra`` are lists of the sets and spectra ``user`` may see; ``array_ids`` the ids of
    those spectra that are stored as arrays.
    """
    from YSE_App.common import spectrum_arrays
    from YSE_App.models import Host

    transient = transient_qs[0]
    host = '{}'
    if transient.host:
        host = dumps(serialized(Host.objects.filter(id=transient.host.id)))
    yield '{%s: {"transient": %s, "host": %s, "photometry": ' % (
        dumps(transient.name), dumps(serialized(transient_qs)), host)

    if authorized_phot:
        yield '['
        for n, (p, head) in enumerate(zip(authorized_phot, serialized(authorized_phot))):
            if n:
                yield ', '
            photdata = PhotometryService.GetAuthorizedTransientPhotData_ByPhotometry(user, p.id, includeBadData=True)
            rows = serialized_rows(photdata) if photdata is not None else ()
            yield from _with_data(dumps(head), rows)
        yield ']'
    else:
        yield '{}'

    yield ', "spectra": '
    if authorized_spectra:
        yield '['
        for n, (s, head) in enumerate(zip(authorized_spectra, serialized(authorized_spectra))):
            if n:
                yield ', '
            head = dumps(head)
            if s.id in array_ids:
                arrays = spectrum_arrays.arrays_for([s.id]).get(s.id)
                if arrays is not None:
                    yield from _with_data(head, array_pixel_dicts(s.id, arrays))
                    continue
            specdata = SpectraService.GetAuthorizedTransientSpecData_BySpectrum(user, s.id, includeBadData=True)
            rows = iter(serialized_rows(specdata)) if specdata is not None else iter(())
            first = next(rows, None)
            if first is None:
                # No pixel rows: no "data" key, as before.
                yield head
                continue
            yield from _with_data(head, _prepend(first, rows))
        yield ']'
    else:
        yield '{}'
    yield '}}'


def _prepend(first, rest):
    yield first
    yield from rest


SNANA_VARLIST = "VARLIST:  MJD        FLT  FLUXCAL   FLUXCALERR    MAG     MAGERR     MAGSYS   TELESCOPE    INSTRUMENT   DQ\n"
SNANA_LINEFMT = "OBS:      %.3f  %s  %.3f  %.3f  %.3f  %.3f  %s  %s  %s  %s\n"


def snana_obs_line(fields, telescope, instrument):
    """The ``OBS:`` line of one serialized photometry point, or None when it has no usable flux or magnitude."""
    d = fields
    mjd = date_to_mjd(d['obs_date'])
    if not len(d['data_quality']):
        data_quality = None
    else:
        data_quality = ','.join(d['data_quality'])
    if d['flux'] and d['flux_zero_point'] and not d['mag']:
        flux_err = d['flux_err'] if d['flux_err'] else 0
        flux = d['flux'] * 10 ** (0.4 * (d['flux_zero_point'] - 27.5))
        flux_err = flux_err * 10 ** (0.4 * (d['flux_zero_point'] - 27.5))
        mag = -2.5 * np.log10(flux) + 27.5
        mag_err = 2.5 / np.log(10) * flux_err / flux
        return SNANA_LINEFMT % (mjd, d['band'].split(' - ')[1], flux, flux_err, mag, mag_err, d['mag_sys'], telescope, instrument, data_quality)
    elif not d['flux'] and d['mag']:
        mag_err = d['mag_err'] if d['mag_err'] else 0
        flux = 10 ** (-0.4 * (d['mag'] - 27.5))
        flux_err = 0.4 * np.log(10) * flux * mag_err
        return SNANA_LINEFMT % (mjd, d['band'].split(' - ')[1], flux, flux_err, d['mag'], mag_err, d['mag_sys'], telescope, instrument, data_quality)
    elif d['flux'] and d['flux_zero_point'] and d['mag']:
        flux_err = d['flux_err'] if d['flux_err'] else 0
        mag_err = d['mag_err'] if d['mag_err'] else 0
        flux = d['flux'] * 10 ** (0.4 * (d['flux_zero_point'] - 27.5))
        flux_err = flux_err * 10 ** (0.4 * (d['flux_zero_point'] - 27.5))
        return SNANA_LINEFMT % (mjd, d['band'].split(' - ')[1], flux, flux_err, d['mag'], mag_err, d['mag_sys'], telescope, instrument, data_quality)
    elif d['flux'] and d['mag']:
        # if somebody didn't provide a ZPT, we can still work with this
        mag_err = d['mag_err'] if d['mag_err'] else 0
        flux = 10 ** (-0.4 * (d['mag'] - 27.5))
        flux_err = 0.4 * np.log(10) * flux * mag_err
        return SNANA_LINEFMT % (mjd, d['band'].split(' - ')[1], flux, flux_err, d['mag'], mag_err, d['mag_sys'], telescope, instrument, data_quality)
    return None


def snana_chunks(user, header, phot_sets, keep=None):
    """Text chunks of the ``download_photometry`` SNANA file.

    ``header`` is the comment block; ``phot_sets`` is ``[(set id, telescope, instrument), ...]`` for the
    sets ``user`` may see. Each set's points come from the per-point service call, by observation date.
    ``keep(fields)`` (optional; the download options of #855) leaves out the points it returns False for.
    """
    yield header
    yield "\n"
    yield SNANA_VARLIST
    for phot_id, telescope, instrument in phot_sets:
        photdata = PhotometryService.GetAuthorizedTransientPhotData_ByPhotometry(user, phot_id, includeBadData=True)
        if photdata is None:
            continue
        for row in serialized_rows(photdata.order_by('obs_date')):
            if keep is not None and not keep(row['fields']):
                continue
            line = snana_obs_line(row['fields'], telescope, instrument)
            if line is not None:
                yield line
    yield "# END: \n"


class _ZipSink:
    """A write-only, unseekable file for ``zipfile``: collects bytes until :meth:`take` hands them out."""

    def __init__(self):
        self._parts = []

    def write(self, data):
        self._parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def take(self):
        out = b"".join(self._parts)
        self._parts = []
        return out


def zip_stream(entries):
    """A zip archive of ``entries`` (``(name, text or bytes)`` pairs, read lazily), yielded as bytes.

    Each member is compressed and sent as soon as it is produced, so only one member is in memory
    at a time (#848; the zip counterpart of the streamed JSON / SNANA bodies above). ``zipfile``
    writes the central directory at the end.
    """
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
            chunk = sink.take()
            if chunk:
                yield chunk
    chunk = sink.take()
    if chunk:
        yield chunk
