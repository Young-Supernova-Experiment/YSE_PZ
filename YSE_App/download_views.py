"""Download -> Spectra and Download -> Photometry on a search result or a Selection (#848).

UI preview only (404 otherwise), signed-in users, GET (they change nothing):

* ``search/download/spectra/?params=<search query string>&<options>``
* ``search/download/photometry/?params=<search query string>&<options>``
* ``selections/<slug>/download/spectra/?<options>`` and ``.../download/photometry/?<options>``

The transients are the search's (or the Selection's) rows the user may list, at most
``SEARCH_BULK_PHOTOMETRY_MAX_ROWS`` of them (the object cap of the existing bulk photometry
download). Over the cap, with ``BACKGROUND_EXPORTS_ENABLED`` on, the download is queued as a
background job instead (#855, ``services/background_exports.py``), built from the same functions
below as the requesting user when it runs. Spectra and points are the ones the user may
download today, through the same per-spectrum and per-point calls; options only narrow them
(``services/download_options.py``).

Photometry comes as ``format=csv`` (default: the bulk photometry zip), ``snana`` (a zip of one
SNANA file per transient, as ``download_photometry``) or ``ecsv`` (one combined table, #855).

The spectra zip is streamed (``download_stream.zip_stream``): one CSV per spectrum, the same file
as the transient page's spectra zip, then ``manifest.csv`` and ``README.txt``.
"""

from __future__ import annotations

import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.http import Http404, HttpResponseRedirect, QueryDict, StreamingHttpResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

from YSE_App.common.ui_preview import ui_preview_enabled
from YSE_App.services import background_exports, spectral_phase
from YSE_App.services.download_options import OptionError, PhotometryOptions, SpectraOptions, median_snr

MANIFEST_COLUMNS = (
    'file', 'transient', 'obs_date_utc', 'spectrum_mjd', 'instrument', 'telescope', 'phase_days',
    'phase_reference', 'phase_frame', 'redshift_used', 'transient_redshift', 'median_snr', 'note',
)

HIDDEN_NOTE = 'fit-based phase not shown: the fit used photometry you cannot see'


def object_cap():
    from YSE_App.views import SEARCH_BULK_PHOTOMETRY_MAX_ROWS

    return SEARCH_BULK_PHOTOMETRY_MAX_ROWS


def download_menu_context(spectra_url, photometry_url, params=''):
    """What the Download menu (``YSE_App/partials/download_menu.html``) needs."""
    from YSE_App.models import Instrument, TransientSpectrum

    instruments = (Instrument.objects.filter(Exists(TransientSpectrum.objects.filter(instrument_id=OuterRef('pk'))))
                   .order_by('name').values_list('name', flat=True))
    return {
        'spectra_url': spectra_url,
        'photometry_url': photometry_url,
        'params': params,
        'references': spectral_phase.REFERENCE_CHOICES,
        'instruments': list(instruments),
        'cap': object_cap(),
        'photometry_formats': background_exports.format_choices(background_exports.PHOTOMETRY),
        'background_exports': background_exports.enabled(),
        'retention_days': background_exports.retention_days(),
    }


def _require_preview(request):
    if not ui_preview_enabled(request):
        raise Http404('Downloads with options are part of the new layout preview.')


class _Refused(Exception):
    pass


class _OverCap(_Refused):
    """More transients than the cap: refused, or queued as a background export when those are on."""


def _capped(transients):
    """The transients as a list, or ``_Refused`` (``_OverCap``) when there are more than the cap."""
    cap = object_cap()
    rows = list(transients[:cap + 1])
    if len(rows) > cap:
        raise _OverCap('Too many transients for a download (limit %d); narrow the search first.' % cap)
    if not rows:
        raise _Refused('No transients to download.')
    return rows


def _search_transients(request, params):
    from YSE_App.filters.transient_search import TransientSearchFilterSet, quick_search_params
    from YSE_App.models import Transient
    from YSE_App.services.visibility import filter_listed_transients

    filterset = TransientSearchFilterSet(quick_search_params(params), queryset=Transient.objects.all(),
                                         request=request)
    if not filterset.is_valid():
        raise _Refused('That search is not valid, so nothing was downloaded.')
    return _capped(filter_listed_transients(request.user, filterset.qs))


def _search_params(request):
    from YSE_App.views import _SEARCH_STATE_KEYS

    params = QueryDict(request.GET.get('params', ''), mutable=True)
    for key in _SEARCH_STATE_KEYS:
        params.pop(key, None)
    return params


def _selection_transients(request, slug):
    from YSE_App.selection_views import _visible_or_404
    from YSE_App.services import selections as svc

    selection = _visible_or_404(request, slug)
    try:
        return selection, _capped(svc.selection_queryset(selection, request.user))
    except svc.SelectionError as exc:
        raise _Refused(str(exc))


# ------------------------------------------------------------------ spectra zip


def _zip_name(transient_name, spectrum, used):
    from YSE_App.views import spectrum_csv_name

    name = spectrum_csv_name(transient_name, spectrum).replace('/', '_').replace('\\', '_')
    base, n = name[:-4], 1
    while name in used:
        n += 1
        name = '%s-%d.csv' % (base, n)
    used.add(name)
    return name


def _fmt(value, spec='%.3f'):
    return '' if value is None else spec % value


def spectra_zip_entries(user, transients, options: SpectraOptions):
    """``(name, content)`` pairs of the spectra zip, read one transient at a time."""
    from YSE_App.common import spectrum_arrays
    from YSE_App.common.utc_dates import utc_day
    from YSE_App.data import SpectraService
    from YSE_App.models.phot_stat_models import datetime_to_mjd
    from YSE_App.services.redshift_precedence import best_redshift
    from YSE_App.services.visibility import transients_with_hidden_photometry
    from YSE_App.views import _csv_safe, spectrum_csv_content

    hidden = transients_with_hidden_photometry(user, [t.pk for t in transients])
    manifest = io.StringIO()
    writer = csv.writer(manifest)
    writer.writerow(MANIFEST_COLUMNS)
    used = set()
    n_files = 0
    for transient in transients:
        spectra = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(user, transient.id,
                                                                                  includeBadData=True)
        if options.phase_range:
            spectra = spectral_phase.spectra_in_phase_range(spectra, options.reference, options.phase_min,
                                                            options.phase_max)
        spectra = [s for s in spectra.select_related('instrument', 'instrument__telescope', 'obs_group', 'unit')
                   .order_by('obs_date', 'pk')
                   if not options.instruments or s.instrument.name.lower() in options.instruments]
        if not spectra:
            continue
        ids = [s.id for s in spectra]
        pixels = spectrum_arrays.arrays_for(ids)
        phases = spectral_phase.phases_for(ids, options.reference)
        z_best = best_redshift(transient).value
        for s in spectra:
            arrays = pixels.get(s.id)
            snr = median_snr(arrays) if arrays is not None else None
            if options.snr_min is not None and (snr is None or snr < options.snr_min):
                continue
            row = phases.get(s.id)
            note = ''
            if row is not None and row.reference in spectral_phase.FIT_REFERENCES and transient.pk in hidden:
                row, note = None, HIDDEN_NOTE
            elif row is None:
                note = 'no phase under this reference'
            name = ''
            if arrays is not None:
                name = _zip_name(transient.name, s, used)
                n_files += 1
                yield name, spectrum_csv_content(s, arrays)
            else:
                note = '; '.join(n for n in (note, 'no pixel data, no file') if n)
            writer.writerow([_csv_safe(v) for v in (
                name, transient.name, utc_day(s.obs_date), _fmt(datetime_to_mjd(s.obs_date), '%.4f'),
                s.instrument.name, s.instrument.telescope.name if s.instrument.telescope_id else '',
                _fmt(row.phase if row else None), row.reference if row else options.reference,
                row.frame if row else '', _fmt(row.redshift if row else None, '%.5f'), _fmt(z_best, '%.5f'),
                _fmt(snr, '%.2f'), note)])
    yield 'manifest.csv', manifest.getvalue()
    readme = [
        'YSE-PZ spectra download',
        '',
        'One CSV per spectrum (header comment lines, then wavelength,flux,fluxerr; -99: no error),',
        'the same files as the transient page. manifest.csv lists every spectrum: its phase in days under',
        'the reference, the reference and frame used, the redshift used for the rest frame, the',
        "transient's best redshift, the instrument and the median S/N (flux / error).",
        '',
        'Phase references: default = Bazin peak in the rest frame (observer frame without a redshift),',
        'only when the peak is not before the last non-detection; salt_t0 = SALT t0 in the rest frame;',
        'discovery = days since discovery. Spectra you may not see are not included.',
        '',
        'Options:',
    ] + ['  ' + line for line in options.describe()] + ['', 'Files: %d' % n_files]
    yield 'README.txt', '\n'.join(readme) + '\n'


def _spectra_response(user, transients, options, filename):
    from YSE_App.services.download_stream import zip_stream

    response = StreamingHttpResponse(zip_stream(spectra_zip_entries(user, transients, options)),
                                     content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename=%s' % filename
    return response


# ------------------------------------------------------------------ photometry formats (#855)


def photometry_zip_entries(user, transients, options, fmt='csv'):
    """``(name, content)`` members of the photometry zip in format ``csv`` or ``snana``."""
    from YSE_App.views import BulkPhotometryExport

    if fmt == 'snana':
        return _snana_entries(user, transients, options)
    return BulkPhotometryExport(user, options).zip_entries(transients)


def _snana_entries(user, transients, options):
    from YSE_App.models import Transient
    from YSE_App.views import snana_photometry_chunks

    for transient in transients:
        keep = background_exports.photometry_keep(options, transient)
        chunks = snana_photometry_chunks(user, Transient.objects.filter(pk=transient.pk), keep=keep)
        yield '%s_data.snana.txt' % transient.slug, ''.join(chunks)
    readme = [
        'YSE-PZ photometry download, SNANA format',
        '',
        'One file per transient, the same file as the transient page\'s SNANA download, with the points',
        'you may see. Points without a usable flux or magnitude are left out.',
    ]
    if options is not None and options.active:
        readme += ['', 'Download options (points outside them are left out):']
        readme += ['  ' + line for line in options.describe()]
    yield 'README.txt', '\n'.join(readme) + '\n'


ECSV_COLUMNS = (
    ('transient', str), ('mjd', float), ('flt', str), ('fluxcal', float), ('fluxcalerr', float),
    ('mag', float), ('magerr', float), ('magsys', str), ('telescope', str), ('instrument', str), ('dq', str),
)


def _ecsv_header(options):
    from astropy.table import Table

    table = Table(names=[n for n, _ in ECSV_COLUMNS], dtype=[t for _, t in ECSV_COLUMNS])
    table.meta['comments'] = [
        'YSE-PZ photometry download: one row per point you may see, the rows of the bulk photometry CSV.',
        'FLUXCAL has zero point 27.5; DQ lists data-quality flags separated by ";" (empty: none).',
        'Points without a usable flux or magnitude are left out.',
    ]
    if options is not None and options.active:
        table.meta['comments'].append('Download options: ' + '; '.join(options.describe()))
    out = io.StringIO()
    table.write(out, format='ascii.ecsv', delimiter=',')
    header = out.getvalue()
    names = ','.join(n for n, _ in ECSV_COLUMNS)
    if not header.rstrip('\n').endswith(names):  # the column-name line, also for a table without rows
        header = header.rstrip('\n') + '\n' + names + '\n'
    return header


def _ecsv_value(value, kind):
    if kind is float:
        return '' if value is None else repr(float(value))
    return '' if value is None else str(value)


def photometry_ecsv_chunks(user, transients, options):
    """Text chunks of the combined ECSV table: the header (from astropy), then one CSV row per point."""
    from YSE_App.services.download_stream import buffered
    from YSE_App.views import BulkPhotometryExport

    export = BulkPhotometryExport(user, options)
    kinds = [t for _, t in ECSV_COLUMNS]

    def pieces():
        yield _ecsv_header(options)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\n')
        for transient in transients:
            for row in export.rows(transient):
                writer.writerow([_ecsv_value(v, k) for v, k in zip((transient.name,) + tuple(row), kinds)])
                yield out.getvalue()
                out.seek(0)
                out.truncate()

    return buffered(pieces())


def _photometry_response(user, transients, options, fmt, filename_stem):
    from YSE_App.services.download_stream import zip_stream
    from YSE_App.views import bulk_photometry_zip_response

    if fmt == 'csv':
        return bulk_photometry_zip_response(user, transients, options)
    if fmt == 'ecsv':
        response = StreamingHttpResponse(photometry_ecsv_chunks(user, transients, options),
                                         content_type='text/plain; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename=%s_photometry.ecsv' % filename_stem
        return response
    response = StreamingHttpResponse(zip_stream(photometry_zip_entries(user, transients, options, fmt)),
                                     content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename=%s_photometry_snana.zip' % filename_stem
    return response


def _queue_or_refuse(request, exc, *, data, fmt, source, params='', slug=''):
    """Over the cap: the job's message when background exports are on, else the refusal as today."""
    if not background_exports.enabled():
        return None, str(exc)
    job = background_exports.request_export(
        request.user, data=data, fmt=fmt, source=source, params=params, slug=slug,
        options=background_exports.option_query(request.GET))
    return job, background_exports.queued_message(job)


def _format(request, data):
    try:
        return background_exports.check_format(data, request.GET.get('format'))
    except background_exports.ExportError as exc:
        raise OptionError(str(exc))


# ------------------------------------------------------------------ views


def _back_to_search(params, message):
    from YSE_App.views import _search_bulk_redirect

    return _search_bulk_redirect(params, message)


def _back_to_selection(request, slug, message):
    messages.error(request, message)
    return HttpResponseRedirect(reverse('selection_detail', kwargs={'slug': slug}))


@login_required
@require_GET
def search_download_spectra(request):
    _require_preview(request)
    params = _search_params(request)
    try:
        options = SpectraOptions.from_params(request.GET)
        fmt = _format(request, background_exports.SPECTRA)
        transients = _search_transients(request, params)
    except _OverCap as exc:
        _job, message = _queue_or_refuse(request, exc, data=background_exports.SPECTRA, fmt=fmt,
                                         source='search', params=params.urlencode())
        return _back_to_search(params, message)
    except (OptionError, _Refused) as exc:
        return _back_to_search(params, str(exc))
    return _spectra_response(request.user, transients, options, 'YSEPZ_search_spectra.zip')


@login_required
@require_GET
def search_download_photometry(request):
    _require_preview(request)
    params = _search_params(request)
    try:
        options = PhotometryOptions.from_params(request.GET)
        fmt = _format(request, background_exports.PHOTOMETRY)
        transients = _search_transients(request, params)
    except _OverCap as exc:
        _job, message = _queue_or_refuse(request, exc, data=background_exports.PHOTOMETRY, fmt=fmt,
                                         source='search', params=params.urlencode())
        return _back_to_search(params, message)
    except (OptionError, _Refused) as exc:
        return _back_to_search(params, str(exc))
    return _photometry_response(request.user, transients, options, fmt, 'YSEPZ_search')


@login_required
@require_GET
def selection_download_spectra(request, slug):
    _require_preview(request)
    try:
        options = SpectraOptions.from_params(request.GET)
        fmt = _format(request, background_exports.SPECTRA)
        selection, transients = _selection_transients(request, slug)
    except _OverCap as exc:
        return _selection_over_cap(request, slug, exc, background_exports.SPECTRA, fmt)
    except (OptionError, _Refused) as exc:
        return _back_to_selection(request, slug, str(exc))
    return _spectra_response(request.user, transients, options, 'YSEPZ_%s_spectra.zip' % selection.slug)


@login_required
@require_GET
def selection_download_photometry(request, slug):
    _require_preview(request)
    try:
        options = PhotometryOptions.from_params(request.GET)
        fmt = _format(request, background_exports.PHOTOMETRY)
        selection, transients = _selection_transients(request, slug)
    except _OverCap as exc:
        return _selection_over_cap(request, slug, exc, background_exports.PHOTOMETRY, fmt)
    except (OptionError, _Refused) as exc:
        return _back_to_selection(request, slug, str(exc))
    return _photometry_response(request.user, transients, options, fmt, 'YSEPZ_%s' % selection.slug)


def _selection_over_cap(request, slug, exc, data, fmt):
    job, message = _queue_or_refuse(request, exc, data=data, fmt=fmt, source='selection', slug=slug)
    if job is None:
        return _back_to_selection(request, slug, message)
    messages.success(request, message)
    return HttpResponseRedirect(reverse('selection_detail', kwargs={'slug': slug}))
