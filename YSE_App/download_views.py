"""Download -> Spectra and Download -> Photometry on a search result or a Selection (#848).

UI preview only (404 otherwise), signed-in users, GET (they change nothing):

* ``search/download/spectra/?params=<search query string>&<options>``
* ``search/download/photometry/?params=<search query string>&<options>``
* ``selections/<slug>/download/spectra/?<options>`` and ``.../download/photometry/?<options>``

The transients are the search's (or the Selection's) rows the user may list, at most
``SEARCH_BULK_PHOTOMETRY_MAX_ROWS`` of them (the object cap of the existing bulk photometry
download; #855 lifts caps later through jobs). Spectra and points are the ones the user may
download today, through the same per-spectrum and per-point calls; options only narrow them
(``services/download_options.py``).

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
from YSE_App.services import spectral_phase
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
    }


def _require_preview(request):
    if not ui_preview_enabled(request):
        raise Http404('Downloads with options are part of the new layout preview.')


class _Refused(Exception):
    pass


def _capped(transients):
    """The transients as a list, or ``_Refused`` when there are more than the cap."""
    cap = object_cap()
    rows = list(transients[:cap + 1])
    if len(rows) > cap:
        raise _Refused('Too many transients for a download (limit %d); narrow the search first.' % cap)
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
        transients = _search_transients(request, params)
    except (OptionError, _Refused) as exc:
        return _back_to_search(params, str(exc))
    return _spectra_response(request.user, transients, options, 'YSEPZ_search_spectra.zip')


@login_required
@require_GET
def search_download_photometry(request):
    from YSE_App.views import bulk_photometry_zip_response

    _require_preview(request)
    params = _search_params(request)
    try:
        options = PhotometryOptions.from_params(request.GET)
        transients = _search_transients(request, params)
    except (OptionError, _Refused) as exc:
        return _back_to_search(params, str(exc))
    return bulk_photometry_zip_response(request.user, transients, options)


@login_required
@require_GET
def selection_download_spectra(request, slug):
    _require_preview(request)
    try:
        options = SpectraOptions.from_params(request.GET)
        selection, transients = _selection_transients(request, slug)
    except (OptionError, _Refused) as exc:
        return _back_to_selection(request, slug, str(exc))
    return _spectra_response(request.user, transients, options, 'YSEPZ_%s_spectra.zip' % selection.slug)


@login_required
@require_GET
def selection_download_photometry(request, slug):
    from YSE_App.views import bulk_photometry_zip_response

    _require_preview(request)
    try:
        options = PhotometryOptions.from_params(request.GET)
        _selection, transients = _selection_transients(request, slug)
    except (OptionError, _Refused) as exc:
        return _back_to_selection(request, slug, str(exc))
    return bulk_photometry_zip_response(request.user, transients, options)
