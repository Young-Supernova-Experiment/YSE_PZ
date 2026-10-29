# utils for generating webpage views

import django
from django.conf import settings as djangoSettings
from django.core.cache import cache
from django.contrib.auth.decorators import login_required, permission_required
from django.db import models, connection, reset_queries
from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.generic import TemplateView

import calendar
import datetime
import logging
import threading
import time

from .models import *
from .data import PhotometryService, SpectraService, ObservingResourceService
from .serializers import *
from .common.bandpassdict import bandpassdict
from .common.filter_display import (
    band_display_color,
    display_filter_label,
    plot_legend_label,
    telescope_display_name,
    telescope_display_symbol,
)
from .common.band_order import (
    group_consecutive,
    legend_column_key,
    legend_sort_key,
)
from .common.legend_layout import (
    GLYPH_HEIGHT as LEGEND_GLYPH_HEIGHT,
    GLYPH_WIDTH as LEGEND_GLYPH_WIDTH,
    LABEL_STANDOFF as LEGEND_LABEL_STANDOFF,
    legend_block_height_px,
    legend_column_blocks,
    legend_column_margin,
    legend_label_width_px,
    requested_plot_width,
)
from .common.quality_mask import is_legacy_flagged
from .common.utilities import date_to_mjd
from .common.utc_dates import utc_day
from .services.visibility import group_access_plot_cache_token
from .services import bazin as bazin_fits
from .services import phot_points

import copy
import functools
import itertools
import numpy as np
import random
import signal
import os

from rest_framework.request import Request
from django.templatetags.static import static
from .util import mkFinderChart
from YSE_App.models import Transient

logger = logging.getLogger(__name__)

_HEAVY_PLOT_LOADED = False
_ASTROPY_COORDS_LOADED = False

# Names populated by _load_heavy_plot_stack() / _load_astropy_coords()
bokeh = file_html = CDN = palette = figure = ColumnDataSource = None
Range1d = Span = LinearAxis = Label = Title = HoverTool = Legend = FontSizeSpec = None
cosmo = hp = plt = cm = mpimg = FigureCanvas = Figure = DateFormatter = rcParams = None
matplotlib = sncosmo = plot_airmass = None
Time = SkyCoord = get_moon = u = EarthLocation = Angle = Table = Observer = None


def _load_astropy_coords():
    global _ASTROPY_COORDS_LOADED, Time, SkyCoord, get_moon, u, EarthLocation, Angle, Table, Observer
    if _ASTROPY_COORDS_LOADED:
        return
    from astropy.coordinates import EarthLocation, get_moon, SkyCoord, Angle
    from astropy.table import Table
    from astropy.time import Time
    from astropy.utils import iers
    import astropy.units as u
    from astroplan import Observer
    # Web workers never wait on an IERS download (#466): YSE_App/apps.py turns downloads
    # off at startup; set it again here in case anything turned them back on.
    iers.conf.auto_download = False
    _ASTROPY_COORDS_LOADED = True


def _load_heavy_plot_stack():
    global _HEAVY_PLOT_LOADED
    global bokeh, file_html, CDN, palette, figure, ColumnDataSource
    global Range1d, Span, LinearAxis, Label, Title, HoverTool, Legend, FontSizeSpec
    global cosmo, hp, plt, cm, mpimg, FigureCanvas, Figure, DateFormatter, rcParams
    global matplotlib, sncosmo, plot_airmass
    if _HEAVY_PLOT_LOADED:
        return
    _load_astropy_coords()
    import bokeh as _bokeh
    from bokeh.core.properties import FontSizeSpec as _FontSizeSpec
    from bokeh.embed import file_html as _file_html
    from bokeh.palettes import Dark2_5 as _palette
    from bokeh.plotting import figure as _figure, ColumnDataSource as _ColumnDataSource
    from bokeh.models import (
        Range1d as _Range1d,
        Span as _Span,
        LinearAxis as _LinearAxis,
        Label as _Label,
        Title as _Title,
        HoverTool as _HoverTool,
        Legend as _Legend,
    )
    from bokeh.resources import CDN as _CDN
    from astropy.cosmology import FlatLambdaCDM
    from astroplan.plots import plot_airmass as _plot_airmass
    import healpy as _hp
    import matplotlib as _matplotlib
    import matplotlib.pyplot as _plt
    import matplotlib.cm as _cm
    import matplotlib.image as _mpimg
    from matplotlib import rcParams as _rcParams
    from matplotlib.backends.backend_agg import FigureCanvasAgg as _FigureCanvas
    from matplotlib.dates import DateFormatter as _DateFormatter
    from matplotlib.figure import Figure as _Figure
    import sncosmo as _sncosmo

    bokeh = _bokeh
    FontSizeSpec = _FontSizeSpec
    file_html = _file_html
    palette = _palette
    figure = _figure
    ColumnDataSource = _ColumnDataSource
    Range1d, Span, LinearAxis, Label, Title, HoverTool, Legend = (
        _Range1d, _Span, _LinearAxis, _Label, _Title, _HoverTool, _Legend,
    )
    CDN = _CDN
    cosmo = FlatLambdaCDM(70, 0.3)
    plot_airmass = _plot_airmass
    hp = _hp
    matplotlib = _matplotlib
    plt = _plt
    cm = _cm
    mpimg = _mpimg
    rcParams = _rcParams
    FigureCanvas = _FigureCanvas
    DateFormatter = _DateFormatter
    Figure = _Figure
    sncosmo = _sncosmo
    _HEAVY_PLOT_LOADED = True


def _bokeh_ajax_response(ax, title="plot"):
    """Embed Bokeh using components(); page already loads bokeh-2.4.2.min.js."""
    _load_heavy_plot_stack()
    from bokeh.embed import components

    script, div = components(ax)
    html = f"{div}{script}"
    return HttpResponse(html.replace("width: 90%", "width: 100%"))


SALT_FIT_UNAVAILABLE_TEXT = "SALT fit unavailable"


def _note_salt2_fit_failure(ax, exc, transient_id):
    """Log a failed SALT2/SALT3 fit and annotate the plot instead of raising.

    A missing model download, a band that is not in ``bandpassdict``, or too
    few points for the fitter all end up here; the light curve itself still
    renders and the caller returns it without the fit (#189).
    """
    logger.warning(
        "SALT fit failed for transient %s (%s: %s); returning plot without fit",
        transient_id, type(exc).__name__, exc, exc_info=True,
    )
    try:
        ax.add_layout(Label(
            x=10, y=280, x_units='screen', y_units='screen',
            render_mode='css', text_font_size='10pt', text_color='#b00020',
            text=f"{SALT_FIT_UNAVAILABLE_TEXT} ({type(exc).__name__})",
        ))
    except Exception:
        logger.debug("could not annotate plot with SALT fit failure", exc_info=True)


SALT_FIT_NONE_TEXT = "No stored SALT3 fit yet: use Refit on the Summary tab"


def _overlay_stored_salt_fit(ax, transient_id, series, today, flux_space=False, salt_run=None, user=None):
    """Draw the last successful ``sncosmo_fit`` run (#315) instead of refitting in the request.

    ``series`` is ``[(bandkey, color)]`` with ``bandkey`` the ``bandpassdict``
    key (``"Band: <instrument> - <band>"``). The run's ``model_curves.json``
    holds magnitudes on zero point 27.5 per sncosmo band; ``flux_space``
    converts them to the flux plot's units. Returns the run drawn, or ``None``
    when there is no stored fit (the plot then says so and never blocks).
    """
    from YSE_App.services import fit_status

    run = salt_run if salt_run is not None else fit_status.stored_salt_fit(transient_id, user=user)
    if run is None:
        ax.add_layout(Label(
            x=10, y=280, x_units='screen', y_units='screen',
            render_mode='css', text_font_size='10pt', text_color='#666666',
            text=SALT_FIT_NONE_TEXT,
        ))
        return None
    curves = fit_status.model_curves(run)
    grid = np.asarray(curves.get('mjd') or [], dtype=float)
    drawn = set()
    for bandkey, color in series:
        sband = bandpassdict.get(bandkey)
        model = curves.get('bands', {}).get(sband) if sband else None
        if not model or sband in drawn or len(model) != len(grid):
            continue
        drawn.add(sband)
        mag = np.array([np.nan if v is None else float(v) for v in model], dtype=float)
        y = 10 ** (-0.4 * (mag - 27.5)) if flux_space else mag
        ok = np.isfinite(y)
        if ok.any():
            ax.line(grid[ok], y[ok], color=color)
    for i, text in enumerate(fit_status.salt_fit_labels(run, today)):
        ax.add_layout(Label(x=10, y=280 - 15 * i, x_units='screen', y_units='screen',
                            render_mode='css', text_font_size='10pt', text=text))
    return run


MAX_LC_DISPLAY_POINTS = int(os.environ.get('YSE_LC_PLOT_MAX_POINTS', '3000'))
MAX_SPEC_DISPLAY_PIXELS = int(os.environ.get('YSE_SPEC_PLOT_MAX_PIXELS', '800'))
PLOT_HTML_CACHE_TIMEOUT = int(os.environ.get('YSE_PLOT_CACHE_SECONDS', '900'))


BAZIN_FIT_UNAVAILABLE_TEXT = "Bazin fit unavailable (fewer than %d detections)" % bazin_fits.MIN_DETECTIONS
BAZIN_LEGEND_LABEL = "Bazin fit"
# Legend column of the today line and fit overlays: after every instrument column.
MARKER_LEGEND_COLUMN = "markers"
# Flagged points (#84): ``quality_mask & LEGACY_EXCLUDE != 0``, the rows the
# "good data" readers, the statistics and the fits leave out. They are drawn
# hollow as their own glyph set, muted until "flagged" is switched on.
FLAGGED_LEGEND_LABEL = "flagged"
FLAGGED_LEGEND_COLOR = "#7f7f7f"
FLAGGED_LINE_ALPHA = 0.8
FLAGGED_MUTED_ALPHA = 0.15
FLAGGED_SPECTRUM_SUFFIX = " (flagged)"


def _draw_bazin_fits(ax, series, today):
    """Overlay per-band Bazin curves on the detail light-curve plot (#225).

    ``series`` holds one dict per plotted band: ``label``, ``band_name``,
    ``color``, ``leader`` (the detection renderer, so hiding the band in the
    legend hides its fit), ``mjd`` / ``mag`` / ``magerr`` (flagged points
    already removed).  All bands are fitted together (wavelength-correlated
    shape, see ``services/bazin.py``).  The fitted span is drawn solid, the extrapolation past the
    last detection dashed, in the band's colour, and labels list each band's
    extrapolated magnitude today.  Returns ``(renderers, x_end)``: the fit
    renderers for one shared legend entry and the last MJD of the
    extrapolation grid (so the caller can widen the x-range); when no band
    has a usable fit a note is drawn instead and ``([], None)`` returned.
    """
    renderers, notes = [], []
    x_end = None
    fits = bazin_fits.fit_bands(
        {k: list(zip(e['mjd'], e['mag'], e['magerr'])) for k, e in enumerate(series)},
        {k: bazin_fits.band_effective_wavelength(e.get('band_name')) for k, e in enumerate(series)},
    )
    for k, entry in enumerate(series):
        fit = fits.get(k)
        if fit is None:
            continue
        fitted, extrapolated = bazin_fits.bazin_plot_grid(fit, today)
        fitted_mag = bazin_fits.flux_to_mag(fit.flux_at(fitted))
        extrapolated_mag = bazin_fits.flux_to_mag(fit.flux_at(extrapolated))
        # non-positive model flux has no magnitude; drop those grid points
        ok_f, ok_e = np.isfinite(fitted_mag), np.isfinite(extrapolated_mag)
        solid = ax.line(
            fitted[ok_f].tolist(), fitted_mag[ok_f].tolist(),
            color=entry['color'], line_width=2, muted_alpha=0.2,
        )
        dashed = ax.line(
            extrapolated[ok_e].tolist(), extrapolated_mag[ok_e].tolist(),
            color=entry['color'], line_width=2, line_dash='dashed', muted_alpha=0.2,
        )
        _link_series_visibility(entry['leader'], [solid, dashed])
        renderers.extend([solid, dashed])
        x_end = float(extrapolated[-1]) if x_end is None else max(x_end, float(extrapolated[-1]))
        mag_today = fit.mag_at(today)
        notes.append(
            "%s: %s today" % (entry['label'], "%.2f" % mag_today if mag_today is not None else "no flux")
        )
    if not renderers:
        ax.add_layout(Label(
            x=10, y=280, x_units='screen', y_units='screen',
            render_mode='css', text_font_size='10pt', text_color='#b00020',
            text=BAZIN_FIT_UNAVAILABLE_TEXT,
        ))
        return renderers, None
    y = 280
    header = "Bazin fit (dashed = extrapolated)"
    if any(fit.method == 'shared' for fit in fits.values()):
        header = "Bazin fit, shared shape (dashed = extrapolated)"
    for text in [header] + notes[:8]:
        ax.add_layout(Label(
            x=10, y=y, x_units='screen', y_units='screen',
            render_mode='css', text_font_size='10pt', text=text,
        ))
        y -= 15
    return renderers, x_end


def _plot_html_cache_enabled():
    return os.environ.get('YSE_PLOT_HTML_CACHE', '1') != '0'


def _phot_data_quality_label(phot_row):
    """Tooltip ``dq`` text: the DataQuality names, or ``Good``.

    Reads ``data_quality.all()`` so the views' ``prefetch_related('data_quality')``
    answers it without a query per row.
    """
    dq_names = [dq.name for dq in phot_row.data_quality.all()]
    return 'Good' if not dq_names else ','.join(dq_names)


def _phot_flagged(phot_row):
    """Whether a TransientPhotData row is flagged (#84): NOT ``legacy_good`` (spec v7).

    The per-row form of the photometry table's split (#85) and the statistics'
    rule: ``quality_mask & LEGACY_EXCLUDE != 0``, so NOT_PUBLISHED alone is good.
    """
    return is_legacy_flagged(getattr(phot_row, 'quality_mask', 0), 'TransientPhotData')


def _spectrum_flagged(spectrum):
    """Whether a TransientSpectrum is flagged (#84): the "good data" readers' rule."""
    return is_legacy_flagged(getattr(spectrum, 'quality_mask', 0), 'TransientSpectrum')


def _downsample_phot_rows_for_display(phot_rows, max_points=MAX_LC_DISPLAY_POINTS):
    if len(phot_rows) <= max_points:
        return phot_rows
    sorted_rows = sorted(phot_rows, key=lambda row: row.obs_date)
    discovery_ids = {id(row) for row in sorted_rows if row.discovery_point}
    stride = max(1, len(sorted_rows) // max_points)
    kept = [
        row for idx, row in enumerate(sorted_rows)
        if id(row) in discovery_ids or idx % stride == 0
    ]
    if len(kept) > max_points:
        kept = sorted(kept, key=lambda row: row.obs_date)[-max_points:]
    return kept


def _transient_phot_cache_token(transient_id):
    from django.db.models import Count, Max

    agg = TransientPhotData.objects.filter(
        photometry__transient_id=transient_id,
    ).aggregate(latest=Max('modified_date'), n=Count('id'))
    latest = agg['latest']
    latest_key = latest.isoformat() if latest else 'none'
    return f"{agg['n']}:{latest_key}"


def _transient_spectrum_cache_token(transient_id):
    from django.db.models import Count, Max, Sum

    # Spectra stored as arrays (#492): writing, replacing or deleting an arrays row changes the token
    # too. The arrays row is one-to-one with its spectrum, so joining it in costs no extra query and
    # does not multiply the counts.
    spec_agg = TransientSpectrum.objects.filter(
        transient_id=transient_id,
    ).aggregate(latest=Max('modified_date'), n=Count('id'), arr_latest=Max('arrays__modified_date'),
                arr_n=Count('arrays__spectrum_id'), arr_px=Sum('arrays__n_pixels'))
    data_agg = TransientSpecData.objects.filter(
        spectrum__transient_id=transient_id,
    ).aggregate(latest=Max('modified_date'), n=Count('id'))
    spec_latest = spec_agg['latest'].isoformat() if spec_agg['latest'] else 'none'
    data_latest = data_agg['latest'].isoformat() if data_agg['latest'] else 'none'
    arr_latest = spec_agg['arr_latest'].isoformat() if spec_agg['arr_latest'] else 'none'
    return (f"s{spec_agg['n']}:{spec_latest}:d{data_agg['n']}:{data_latest}"
            f":a{spec_agg['arr_n']}:{spec_agg['arr_px'] or 0}:{arr_latest}")


def _cached_plot_http_response(cache_key, html):
    if _plot_html_cache_enabled() and html:
        cache.set(cache_key, html, timeout=PLOT_HTML_CACHE_TIMEOUT)
    return django.http.HttpResponse(html)


py2bokeh_symboldict = {"^":"triangle",
                       "+":"cross",
                       "s":"square",
                       "*":"asterisk",
                       "D":"diamond",
                       "d":"diamond",
                       "o":"circle",
                       "h":"hex"}

def query_debugger(func):

    @functools.wraps(func)
    def inner_func(*args, **kwargs):
        reset_queries()
        
        start_queries = len(connection.queries)
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        end_queries = len(connection.queries)

        logger.info("Function : %s", func.__name__)
        logger.info("Number of Queries : %s", end_queries - start_queries)
        logger.info("Finished in : %.2fs", end - start)
        return result

    return inner_func

def get_recent_phot_for_host(user, host_id=None):
    allowed_phot = PhotometryService.GetAuthorizedHostPhotometry_ByUser_ByHost(user, host_id)

    photdata = HostPhotData.objects.filter(photometry__in=allowed_phot).order_by('-obs_date').first()
    return photdata

def get_all_phot_for_transient(user, transient_id=None):
    photdata = PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(
        user, transient_id, includeBadData=True)
    return(photdata)

def get_recent_phot_for_transient(user, transient_id=None):
    photdata = get_all_phot_for_transient(user, transient_id)
    if photdata:
        photdata = photdata.order_by('-obs_date')
        return(photdata[0])
    else:
        return(None)

def get_disc_mag_from_photdata(photdata_qs):
    """Discovery / first mag from an existing photdata queryset (avoids duplicate DB fetch)."""
    # Same answer as walking every row in obs_date order (the earliest
    # discovery point, else the latest point with a non-zero magnitude), but in
    # at most two LIMIT 1 queries instead of loading all photometry.
    discovery = photdata_qs.filter(discovery_point=True).order_by('obs_date').first()
    if discovery is not None:
        return discovery
    return (
        photdata_qs.filter(mag__isnull=False).exclude(mag=0)
        .order_by('-obs_date').first()
    )


def get_disc_mag_for_transient(user, transient_id=None):
    photdata = PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(
        user, transient_id, includeBadData=False
    )
    return get_disc_mag_from_photdata(photdata)

def getMoonAngle(observingdate,telescope,ra,dec):
    _load_astropy_coords()
    if observingdate:
        obstime = Time(observingdate,scale='utc')
    else:
        obstime = Time(datetime.datetime.now())
    mooncoord = get_moon(obstime)
    cs = SkyCoord(ra,dec,unit=u.deg)
    return('%.1f'%cs.separation(mooncoord).deg)

def get_obs_nights_happening_soon(user):
    allowed_nights = ObservingResourceService.GetAuthorizedClassicalObservingDate_ByUser(user).select_related()
    allowed_nights_happening_soon = []
    for an in allowed_nights:
        if an.happening_soon():
            allowed_nights_happening_soon.append(an)

    return allowed_nights_happening_soon

def get_too_resources(user):
    allowed_too_resource = ObservingResourceService.GetAuthorizedToOResource_ByUser(user)
    return allowed_too_resource

def getTimeUntilRiseSet(ra,dec,date,lat,lon,elev,utc_off):
    _load_astropy_coords()
    if date:
        time = Time(date)
    else:
        time = Time(datetime.datetime.now())
    sc = SkyCoord(ra,dec,unit=u.deg)

    location = EarthLocation.from_geodetic(
        lon*u.deg,lat*u.deg,
        elev*u.m)
    tel = Observer(location=location, timezone="UTC")
    #night_start = tel.twilight_evening_civil(time,which="previous")
    #night_end = tel.twilight_morning_civil(time,which="previous")
    target_rise_time = tel.target_rise_time(time,sc,horizon=18*u.deg,which="previous")
    target_set_time = tel.target_set_time(time,sc,horizon=18*u.deg,which="previous")
    
#    start_obs = False
#    starttime,endtime = None,None
#    for jd in np.arange(night_start.mjd,night_end.mjd,0.05):
#        time = Time(jd,format="mjd")
#        target_up = tel.target_is_up(time,sc,horizon=18*u.deg)
#        if target_up and not start_obs:
#            start_obs = True
#            starttime = copy.copy(time)
#        if not target_up and start_obs:
#            can_obs = False
#            endtime = copy.copy(time)
#            break

    if target_rise_time:
        returnstarttime = target_rise_time.isot.split('T')[-1]
    else: returnstarttime = None
    if target_set_time:
        returnendtime = target_set_time.isot.split('T')[-1]
    else: returnendtime = None

    
    return(returnstarttime,returnendtime)

    
# def telescope_can_observe(ra,dec,date,lat,lon,elev,utc_off):
#   if date:
#       time = Time(date)
#   else:
#       time = Time(datetime.datetime.now())
#   sc = SkyCoord(ra,dec,unit=u.deg)
#
#   location = EarthLocation.from_geodetic(
#       lon*u.deg,lat*u.deg,
#       elev*u.m)
#   tel = Observer(location=location, timezone="UTC")
#
#   night_start = tel.twilight_evening_astronomical(time,which="previous")
#   night_end = tel.twilight_morning_astronomical(time,which="previous")
#   can_obs = False
#   for jd in np.arange(night_start.mjd,night_end.mjd,0.02):
#       time = Time(jd,format="mjd")
#       target_up = tel.target_is_up(time,sc,horizon=18*u.deg)
#       if target_up:
#           can_obs = True
#           break
#
#   return(can_obs)


from django.utils.decorators import method_decorator  # noqa: E402

# Finder chart rendering resolution (#484). The charts are shown at 240px (transient
# detail) and 600px (finderim) wide on a 6.4x4.8in figure, so 150 dpi (960x720 px)
# is enough; the previous dpi=1000 wrote ~6400x4800 px PNGs for every transient.
FINDER_CHART_DPI = 150
_FINDER_CHART_RELDIR = 'YSE_App/images/findercharts'


def finder_chart_safe_name(transient_name):
    """The transient name as one file-name component for finder charts (#500)."""
    import re

    name = re.sub(r'[^A-Za-z0-9_.+-]', '_', str(transient_name or '')).lstrip('.')
    return name or 'unnamed'


_safe_finder_name = finder_chart_safe_name


def finder_chart_base_dir():
    """Directory holding all finder charts (settings.FINDER_CHART_DIR).

    Defaults to <STATIC_ROOT>/YSE_App/images/findercharts so the transient detail
    page keeps serving the images as static files.
    """
    configured = getattr(djangoSettings, 'FINDER_CHART_DIR', None)
    return configured or os.path.join(djangoSettings.STATIC_ROOT, _FINDER_CHART_RELDIR)


def finder_chart_paths(transient_name, create=True):
    """THE single place that decides where a finder chart is written.

    Returns (directory, offset_txt_path, finder_png_path) for a transient, and creates the
    per-transient directory when create is True. The name is sanitised and the
    result is checked to stay inside finder_chart_base_dir().
    """
    base = os.path.realpath(finder_chart_base_dir())
    safe = _safe_finder_name(transient_name)
    outdir = os.path.realpath(os.path.join(base, safe))
    if os.path.dirname(outdir) != base:
        raise ValueError('finder chart path escapes the finder chart directory')
    if create:
        os.makedirs(outdir, exist_ok=True)
    return (outdir,
            os.path.join(outdir, f"{safe}.offsetstars.txt"),
            os.path.join(outdir, f"{safe}.finder.png"))


def finder_chart_static_relpath(transient_name):
    safe = _safe_finder_name(transient_name)
    return f"{_FINDER_CHART_RELDIR}/{safe}/{safe}.finder.png"


def static_asset_available(relative_path):
    """True if a static file exists in finders or under STATIC_ROOT (post-collectstatic)."""
    from django.contrib.staticfiles import finders

    if finders.find(relative_path):
        return True
    return os.path.isfile(os.path.join(djangoSettings.STATIC_ROOT, relative_path))


class Finder(TemplateView):
    template_name = 'YSE_App/finder.html'

    @method_decorator(login_required)
    def finderchart(self, request, transient_id, clobber=False):
        _load_heavy_plot_stack()
        transient = get_object_or_404(Transient, pk=transient_id)
        _outdir, output_offset_file, output_finder_file = finder_chart_paths(transient.name)

        find = mkFinderChart.finder()
        parser = find.add_options(usage='')
        options, args = parser.parse_args()
        options.ra = str(transient.ra)
        options.dec = str(transient.dec)
        options.snid = transient.name
        options.outputOffsetFileName = output_offset_file
        options.outputFinderFileName = output_finder_file
        find.options = options
        
        fig, ax = plt.subplots()
        canvas = FigureCanvas(fig)

        if not (os.path.exists(options.outputFinderFileName) and os.path.exists(options.outputOffsetFileName)) or clobber:
            ax, offdictlist = find.mkChart(options.ra, options.dec, options.outputFinderFileName, ax=ax, saveImg=False, clobber=clobber)
            fig.savefig(options.outputFinderFileName, dpi=FINDER_CHART_DPI)
        else:
            image = mpimg.imread(options.outputFinderFileName)
            ax.imshow(image)
            ax.set_xticks([])
            ax.set_yticks([])

            offid, ra_str, dec_str, ra_off, dec_off, mag = np.loadtxt(output_offset_file, dtype='str', unpack=True)
            offdictlist = []
            for i in range(len(offid))[1:]:
                offdict = {'id':offid[i],
                           'ra':ra_str[i],
                           'dec':dec_str[i],
                           'ra_off':'%.3f'%float(ra_off[i]),
                           'dec_off':'%.3f'%float(dec_off[i]),
                           'mag':'%.3f'%float(mag[i])}
                offdictlist += [offdict]

        context = {
            't': transient,
            'offsets': offdictlist
        }

        return render(request, 'YSE_App/finder.html', context)    
    
    @method_decorator(login_required)
    def finderim(self, request, transient_id, clobber=False):
        transient = get_object_or_404(Transient, pk=transient_id)
        _outdir, output_offset_file, output_finder_file = finder_chart_paths(transient.name)

        find = mkFinderChart.finder()
        parser = find.add_options(usage='')
        options,  args = parser.parse_args()
        options.ra = str(transient.ra)
        options.dec = str(transient.dec)
        options.snid = transient.name
        options.outputOffsetFileName = output_offset_file
        options.outputFinderFileName = output_finder_file
        find.options = options

        fig=Figure()
        if not os.path.exists(options.outputFinderFileName) or \
           not os.path.exists(options.outputOffsetFileName) or \
           clobber:
            ax = fig.add_axes([0.2,0.3,0.6,0.6])
            canvas=FigureCanvas(fig)
        
            ax,offdictlist = find.mkChart(options.ra,options.dec,
                                          options.outputFinderFileName,
                                          ax=ax,saveImg=True)
        else:
            image = mpimg.imread(options.outputFinderFileName)
            ax = fig.add_axes([0,0,1,1])
            ax.set_xticks([]); ax.set_yticks([])
            canvas=FigureCanvas(fig)
            ax.imshow(image)


        response=django.http.HttpResponse(content_type='image/png')
        canvas.print_jpg(response)

        return response

    def finderchart_noview(self, transient_id, clobber=False):
        import os
        from .util import mkFinderChart

        from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
        from matplotlib.figure import Figure
    
        transient = Transient.objects.get(pk=transient_id)
        _outdir, outputOffsetFileName, outputFinderFileName = finder_chart_paths(transient.name)
        #if os.path.exists(outputOffsetFileName) and\
        #   os.path.exists(outputFinderFileName):
        #   return HttpResponseRedirect(reverse('transient_detail',
        #                                       args=(transient.id,)))

        find = mkFinderChart.finder()
        parser = find.add_options(usage='')
        options,  args = parser.parse_args()
        options.ra = str(transient.ra)
        options.dec = str(transient.dec)
        options.snid = transient.name
        options.outputOffsetFileName = outputOffsetFileName
        options.outputFinderFileName = outputFinderFileName
        find.options = options
        import pylab as plt

        fig=Figure()
        if not os.path.exists(options.outputFinderFileName) or \
           not os.path.exists(options.outputOffsetFileName) or \
           clobber:
            ax = fig.add_axes([0.2,0.3,0.6,0.6])
            canvas=FigureCanvas(fig)
            ax,offdictlist = find.mkChart(options.ra,options.dec,
                                          options.outputFinderFileName,
                                          ax=ax,saveImg=False,
                                          clobber=clobber)
            fig.savefig(options.outputFinderFileName,dpi=FINDER_CHART_DPI)
        else:
            image = mpimg.imread(options.outputFinderFileName)
            ax = fig.add_axes([0,0,1,1])
            ax.set_xticks([]); ax.set_yticks([])
            canvas=FigureCanvas(fig)
            ax.imshow(image)

            offid,ra_str,dec_str,ra_off,dec_off,mag = \
                np.loadtxt(outputOffsetFileName,dtype='str',unpack=True)
            offdictlist = []
            for i in range(len(offid))[1:]:
                offdict = {'id':offid[i],
                           'ra':ra_str[i],
                           'dec':dec_str[i],
                           'ra_off':'%.3f'%float(ra_off[i]),
                           'dec_off':'%.3f'%float(dec_off[i]),
                           'mag':'%.3f'%float(mag[i])}
                offdictlist += [offdict]

        return offdictlist, outputFinderFileName

def finder():
    return Finder()


## We should refactor this so that it takes:
# - transient
# - observatory (or maybe array of observatory)
# - 
# And it can returns the data which can be plotted on the front end
# i.e. a tuple of (datetime, airmass) that ChartJS can plot on the 
# client 
    
@login_required
def airmassplot(request, transient_id, obs_id, telescope_id):
    _load_heavy_plot_stack()
    #font = {'family' : 'normal',
    #       'weight' : 'normal',
    #       'size'   : 2}
    #matplotlib.rc('font', **font)

    #rcParams['figure.figsize'] = (2,2)
        
    transient = Transient.objects.get(pk=transient_id)
    if int(obs_id):
        obsnight = ClassicalObservingDate.objects.get(pk=obs_id)
        obs_date = obsnight.obs_date
    else:
        obs_date = datetime.date.today()
            
    telescope = Telescope.objects.get(pk=telescope_id)
    
    target = SkyCoord(transient.ra,transient.dec,unit=u.deg)
    time = Time(str(obs_date).split('+')[0], format='iso')

    location = EarthLocation.from_geodetic(telescope.longitude*u.deg, telescope.latitude*u.deg,telescope.elevation*u.m)
    tel = Observer(location=location, name=telescope.name, timezone="UTC")
        
    fig=Figure()
    ax=fig.add_subplot(111)
    canvas=FigureCanvas(fig)

    ax.set_title("%s, %s, %s"%(telescope.tostring(),transient.name, obs_date))

    night_start = tel.twilight_evening_astronomical(time,which="previous")
    night_end = tel.twilight_morning_astronomical(time,which="previous")
    if night_end < night_start:
        night_end = tel.twilight_morning_astronomical(time,which="next")
    delta_t = night_end - night_start
    observe_time = night_start +datetime.timedelta(hours=6)#+ datetime.timedelta(night_end-night_end #delta_t*np.linspace(0, 1, 75)
    plot_airmass(target, tel, observe_time, ax=ax)    

    yr,mn,day,hr,minu,sec = night_start.iso.replace(':',' ').replace('-',' ').split()
    starttime = datetime.datetime(int(yr),int(mn),int(day),int(hr),int(minu))
    if int(hr) == 0:
        xlow = datetime.datetime(int(yr),int(mn),int(day)-1,23,int(minu))
    else:
        xlow = datetime.datetime(int(yr),int(mn),int(day),int(hr)-1,int(minu))
    yr,mn,day,hr,minu,sec = night_end.iso.replace(':',' ').replace('-',' ').split()
    endtime = datetime.datetime(int(yr),int(mn),int(day),int(hr),int(minu))
    xhi = datetime.datetime(int(yr),int(mn),int(day),int(hr)+1,int(minu))
    ax.axvline(Time(starttime).plot_date,color='r',label='18 deg twilight')
    ax.axvline(Time(endtime).plot_date,color='r')
    ax.legend(loc='lower right')
    ax.set_xlim([Time(xlow).plot_date,Time(xhi).plot_date])

    response=django.http.HttpResponse(content_type='image/png')
    canvas.print_jpg(response)
    return response

@login_required
def view_yse_fields(request):
    _load_heavy_plot_stack()

    cmap = cm.gray
    cmap.set_bad('grey')
    cmap.set_under('white')

    obs_date_now = datetime.datetime.utcnow().strftime('%Y-%m-%d 00:00:00')
    survey_obs = SurveyObservation.objects.filter(
        Q(mjd_requested__gte = date_to_mjd(obs_date_now)) | Q(obs_mjd__gte = date_to_mjd(obs_date_now))).\
        filter(Q(mjd_requested__lte = date_to_mjd(obs_date_now)+1) | Q(obs_mjd__lte = date_to_mjd(obs_date_now)+1)).\
        filter(survey_field__instrument__name__startswith = 'GPC').select_related()
    field_pk = survey_obs.values('survey_field').distinct()
    survey_fields_tonight = SurveyField.objects.filter(pk__in = field_pk).filter(~Q(obs_group__name='ZTF')).select_related()

    obs_date_last = datetime.datetime.utcnow().strftime('%Y-%m-%d 00:00:00')
    survey_obs = SurveyObservation.objects.filter(
        Q(mjd_requested__gte = date_to_mjd(obs_date_last)-1) | Q(obs_mjd__gte = date_to_mjd(obs_date_last)-1)).\
        filter(Q(mjd_requested__lte = date_to_mjd(obs_date_last)) | Q(obs_mjd__lte = date_to_mjd(obs_date_last))).\
        filter(survey_field__instrument__name__startswith = 'GPC').select_related()
    field_pk = survey_obs.values('survey_field').distinct()
    survey_fields_last_night = SurveyField.objects.filter(pk__in = field_pk).filter(~Q(obs_group__name='ZTF')).select_related()

    obs_date_last = datetime.datetime.utcnow().strftime('%Y-%m-%d 00:00:00')
    survey_obs = SurveyObservation.objects.filter(
        Q(mjd_requested__gte = date_to_mjd(obs_date_last)-2) | Q(obs_mjd__gte = date_to_mjd(obs_date_last)-2)).\
        filter(Q(mjd_requested__lte = date_to_mjd(obs_date_last)-1) | Q(obs_mjd__lte = date_to_mjd(obs_date_last)-1)).\
        filter(survey_field__instrument__name__startswith = 'GPC').select_related()
    field_pk = survey_obs.values('survey_field').distinct()
    survey_fields_two_nights_ago = SurveyField.objects.filter(pk__in = field_pk).filter(~Q(obs_group__name='ZTF')).select_related()
    
    metric_map_path = os.path.join(djangoSettings.STATIC_ROOT, 'YSE_metric_map.fits')
    if os.path.exists(metric_map_path):
        mapdata = hp.fitsfunc.read_map(metric_map_path, h=False, verbose=True)
    else:
        mapdata = np.zeros(hp.nside2npix(128), dtype=np.float64)

    npix = hp.nside2npix(128)
    newmap = np.zeros(npix, dtype=np.float32)
    fieldmap = hp.ma(np.arange(hp.nside2npix(128), dtype=np.double))
    fieldmap.data[:] = 0

    def map_survey_fields(f,metric_value=1.0):
        sc = SkyCoord(f.ra_cen,f.dec_cen,unit=u.deg)
        
        width_corr = 1.65/np.abs(np.cos(f.dec_cen*np.pi/180.))
        ra_offset = Angle(width_corr, unit=u.deg)
        dec_offset = Angle(1.65, unit=u.deg)
        PS_SW = SkyCoord(sc.ra - ra_offset, sc.dec - dec_offset)
        PS_NW = SkyCoord(sc.ra - ra_offset, sc.dec + dec_offset)
        PS_SE = SkyCoord(sc.ra + ra_offset, sc.dec - dec_offset)
        PS_NE = SkyCoord(sc.ra + ra_offset, sc.dec + dec_offset)
        
        corners = np.asarray([PS_SW,PS_NW,PS_NE,PS_SE])
        xyz_vertices = []
        for c in corners:
            galcoord = c.transform_to('galactic')
            if galcoord.l.degree > 180.:
                theta2 = galcoord.l.degree-360.
            else:
                theta2 = galcoord.l.degree
            phi2 = galcoord.b.degree
            xyz_vertices.append(hp.ang2vec(theta2, phi2, lonlat=True))
        corner_xyz = np.asarray(xyz_vertices)
        internal_pix = hp.query_polygon(128, corner_xyz, inclusive=False)
        fieldmap.data[internal_pix] = metric_value
    
    for f in survey_fields_tonight:
        map_survey_fields(f,metric_value=0.01)
    for f in survey_fields_last_night:
        map_survey_fields(f,metric_value=0.1)
    for f in survey_fields_two_nights_ago:
        map_survey_fields(f,metric_value=0.2)

    fieldmap.data[fieldmap.data == 0] = hp.pixelfunc.UNSEEN
    hp.mollview(mapdata, norm='hist', coord=('G','C'),
                title='YSE Fields',fig=1,cbar=False,cmap=cmap,notext=True)      
    hp.mollview(fieldmap.data, norm='hist', coord=('G','C'),
                title=None,fig=1,badcolor='none',cbar=False,cmap='Paired')
    hp.graticule()
    fig = plt.gcf()
    canvas=FigureCanvas(fig)

    
    response=django.http.HttpResponse(content_type='image/png')
    canvas.print_jpg(response)
    return response


@login_required
def salt3plot(request, transient_id, salt3fit):

    response = lightcurveplot_detail(request,transient_id,salt2=int(salt3fit))
    return response

@login_required
def salt3fluxplot(request, transient_id, salt3fit):

    response = lightcurveplot_flux(request,transient_id,salt2=int(salt3fit))
    return response

def _redirect_to_salt3(request, route_name, transient_id, fit):
    """Permanent redirect from an old ``salt2*`` URL to its SALT3 name, keeping the query string."""
    from django.shortcuts import redirect
    url = reverse(route_name, args=[transient_id, fit])
    query = request.META.get('QUERY_STRING')
    if query:
        url = '%s?%s' % (url, query)
    return redirect(url, permanent=True)

@login_required
def salt2plot_redirect(request, transient_id, salt2fit):
    return _redirect_to_salt3(request, 'salt3plot', transient_id, salt2fit)

@login_required
def salt2fluxplot_redirect(request, transient_id, salt2fit):
    return _redirect_to_salt3(request, 'salt3fluxplot', transient_id, salt2fit)

@login_required
def bazinplot(request, transient_id, bazinfit):
    """Detail light-curve plot with (1) or without (0) the per-band Bazin overlay (#225)."""
    return lightcurveplot_detail(request, transient_id, bazin=int(bazinfit))

def _band_instrument_names(band_obj):
    """(instrument name, telescope name) of a PhotometricBand, ``None`` where unset."""
    inst = band_obj.instrument if getattr(band_obj, 'instrument_id', None) else None
    tel_name = (
        inst.telescope.name
        if inst is not None and getattr(inst, 'telescope_id', None)
        else None
    )
    return (inst.name if inst is not None else None, tel_name)


def _band_legend_key(band_obj):
    """Stable legend sort key for a PhotometricBand (instrument group, then wavelength; #91).

    The order is the one in ``YSE_App/common/band_order.py``: PS1/2, DECam,
    Swope, LSST, ZTF, ATLAS, Swift, other instruments alphabetically, and
    bands bluest to reddest within each.
    """
    inst_name, tel_name = _band_instrument_names(band_obj)
    return legend_sort_key(
        getattr(band_obj, 'name', None),
        instrument_name=inst_name,
        telescope_name=tel_name,
    )


def _band_column_key(band_obj):
    """Legend column (one per instrument label) a PhotometricBand's series belongs to."""
    return legend_column_key(*_band_instrument_names(band_obj))


def _legend_series_order(band_objs):
    """Indices that put band objects in stable legend order (#91).

    ``np.unique`` orders series by primary key (or by ``str(band)``), which
    changes from transient to transient; sorting by telescope family and
    filter wavelength keeps ``PS1 g`` before ``PS1 r`` and every ZTF entry
    after every PS1 entry no matter which bands have data.
    """
    band_objs = list(band_objs)
    return np.array(
        sorted(range(len(band_objs)), key=lambda k: _band_legend_key(band_objs[k])),
        dtype=int,
    )


def _link_series_visibility(leader, followers):
    """Hide/show ``followers`` (error bars, upper limits) with the legend-controlled ``leader`` (#249)."""
    from bokeh.models import CustomJS

    followers = [renderer for renderer in followers if renderer is not None]
    if not followers:
        return
    leader.js_on_change(
        'visible',
        CustomJS(
            args=dict(followers=followers),
            code="for (const r of followers) { r.visible = cb_obj.visible; }",
        ),
    )


def _flagged_glyph_kwargs(color):
    """Glyph options of a flagged (#84) series: the band's marker, hollow, muted until shown."""
    return dict(
        line_color=color, fill_color=color, fill_alpha=0.0, line_alpha=FLAGGED_LINE_ALPHA,
        muted_line_color=color, muted_fill_alpha=0.0, muted_line_alpha=FLAGGED_MUTED_ALPHA,
        muted=True,
    )


def _flagged_error_bar_kwargs(color):
    """Error bars of a flagged (#84) series."""
    return dict(
        color=color, line_alpha=FLAGGED_LINE_ALPHA, muted_alpha=FLAGGED_MUTED_ALPHA, muted=True,
    )


# Flagged renderers follow their band's legend entry (hidden with it) and the
# "flagged" switch (muted while it is off).
_FLAGGED_SYNC_JS = """
const show = proxy.visible;
for (let i = 0; i < flagged.length; i++) {
    flagged[i].visible = leaders[i].visible;
    flagged[i].muted = !show;
}
"""


def _add_flagged_switch(ax, pairs, n_points, legend_items, *, toggle=False):
    """Legend entry (and optional "Show flagged" button) for the flagged glyph sets (#84).

    ``pairs`` are ``(band leader, flagged renderer)``: markers, limits and
    error bars of the flagged points, drawn muted. Nothing is added when there
    are none. The legend entry is an empty hollow-circle renderer whose
    ``visible`` is the switch, hidden (off) by default, so clicking it in the
    ``hide``-policy legend un-mutes every flagged renderer; hiding a band
    still hides its flagged points. Returns the ``Toggle`` when ``toggle`` is
    set (kept in step with the legend entry), else ``None``.
    """
    from bokeh.models import CustomJS

    pairs = [(leader, renderer) for leader, renderer in pairs if renderer is not None]
    if not pairs or not n_points:
        return None
    proxy = ax.circle(
        [], [], size=7, line_color=FLAGGED_LEGEND_COLOR, fill_alpha=0.0,
        line_alpha=FLAGGED_LINE_ALPHA, visible=False,
    )
    sync = CustomJS(
        args=dict(
            proxy=proxy,
            leaders=[leader for leader, _r in pairs],
            flagged=[renderer for _l, renderer in pairs],
        ),
        code=_FLAGGED_SYNC_JS,
    )
    proxy.js_on_change('visible', sync)
    seen = set()
    for leader, _renderer in pairs:
        if leader.id not in seen:
            seen.add(leader.id)
            leader.js_on_change('visible', sync)
    legend_items.append(('%s (%d)' % (FLAGGED_LEGEND_LABEL, n_points), [proxy], MARKER_LEGEND_COLUMN))
    if not toggle:
        return None
    from bokeh.models import Toggle

    button = Toggle(label='Show flagged (%d)' % n_points, active=False, width=150)
    button.js_link('active', proxy, 'visible')
    proxy.js_link('visible', button, 'active')
    return button


def _requested_plot_width(request, default):
    """Nominal figure width: the container width the page sends as ``?w=``, else ``default``."""
    return requested_plot_width(request.GET.get('w'), default)


def _add_legend_grid(ax, legend_items, plot_width):
    """Lay ``legend_items`` out below ``ax``, one column per instrument (#226, #372).

    ``legend_items`` are ``(label, renderers, column)`` in legend order;
    consecutive items with the same ``column`` key form one column that
    reads down (``PS1 g`` over ``PS1 r`` over ``PS1 i`` ...).  Every column
    is its own vertical ``Legend`` added ``below`` the plot, as wide as its
    own longest label, at the x offset :func:`legend_column_blocks` packs it
    to; a new block of columns starts under the previous one only when the
    next column would not fit ``plot_width``.  A column legend's margin
    collapses its side panel so all columns of a block share one top edge,
    and an empty legend after each block reserves the block's height (see
    ``YSE_App/common/legend_layout.py``).  Returns the height in pixels the
    legend adds below the plot so the caller can size the figure.
    """
    from bokeh.models import LegendItem

    columns = [
        [(label, renderers) for label, renderers, _column in items]
        for _column, items in group_consecutive(legend_items, key=lambda item: item[2])
    ]
    blocks = legend_column_blocks([[label for label, _r in column] for column in columns], plot_width)
    common = dict(
        click_policy='hide',
        label_height=LEGEND_GLYPH_HEIGHT,
        glyph_width=LEGEND_GLYPH_WIDTH,
        glyph_height=LEGEND_GLYPH_HEIGHT,
        label_standoff=LEGEND_LABEL_STANDOFF,
        spacing=0,
        padding=0,
        border_line_color=None,
    )
    height = 0
    for block in blocks:
        for index, x in block:
            column = columns[index]
            margin = legend_column_margin(len(column))
            ax.add_layout(Legend(
                items=[LegendItem(label=label, renderers=renderers) for label, renderers in column],
                orientation='vertical',
                location=(x, 2 * margin),
                label_width=legend_label_width_px(label for label, _r in column),
                margin=margin,
                **common,
            ), 'below')
        block_height = legend_block_height_px(max(len(columns[index]) for index, _x in block))
        # no items: draws nothing, its panel is 2 * margin tall
        ax.add_layout(Legend(items=[], margin=block_height // 2, **common), 'below')
        height += block_height
    return height


@login_required
def lightcurveplot_summary(request, transient_id, salt2=False):
    _load_heavy_plot_stack()

    # Same 900 s plot-HTML cache as the detail plots (#460): the summary page
    # fires this request once per transient card on every load.
    cache_key = None
    if _plot_html_cache_enabled():
        user_key = group_access_plot_cache_token(request.user)
        cache_key = (
            f'lc_summary_v2_{transient_id}_{_transient_phot_cache_token(transient_id)}'
            f'_{user_key}_w{_requested_plot_width(request, 500)}' + ('_salt' if salt2 else '')
        )
        cached_html = cache.get(cache_key)
        if cached_html is not None:
            return HttpResponse(cached_html)

    transient = get_object_or_404(Transient, pk=transient_id)

    # Fetch photometry data with necessary related fields (no created_by /
    # modified_by joins: the plot never reads them; mag_sys is read per row).
    photdata = (
        get_all_phot_for_transient(request.user, transient_id)
        .select_related(
            "band__instrument__telescope",
            "mag_sys",
        )
        .prefetch_related("data_quality")
        .order_by("-modified_date")
    )

    phot_rows = _downsample_phot_rows_for_display(list(photdata))
    if not phot_rows:
        return HttpResponse("")

    # Pre-fetch related band data to avoid repeated lookups
    band_ids = {p.band_id for p in phot_rows}
    band_data = PhotometricBand.objects.filter(pk__in=band_ids).select_related(
        "instrument", "instrument__telescope"
    )
    band_lookup = {b.pk: b for b in band_data}

    phot_values = [
        {
            "id": p.id,
            "flux": p.flux,
            "flux_err": p.flux_err,
            "flux_zero_point": p.flux_zero_point,
            "mag": p.mag,
            "mag_err": p.mag_err,
            "discovery_point": p.discovery_point,
            "obs_date": p.obs_date,
            "band": p.band_id,
            "mag_sys__name": p.mag_sys.name if p.mag_sys_id else None,
            "dq_label": _phot_data_quality_label(p),
            "flagged": _phot_flagged(p),
        }
        for p in phot_rows
    ]

    # Convert data to NumPy arrays
    flux = np.array([p["flux"] for p in phot_values], dtype=float)
    flux_err = np.array([p["flux_err"] for p in phot_values], dtype=float)
    flux_zero_point = np.array([p["flux_zero_point"] or 27.5 for p in phot_values], dtype=float)
    mag = np.array([p["mag"] for p in phot_values], dtype=float)
    mag_err = np.array([p["mag_err"] or 0.01 for p in phot_values], dtype=float)
    discovery_point = np.array([p["discovery_point"] for p in phot_values], dtype=bool)
    obs_date = np.array([p["obs_date"] for p in phot_values], dtype="datetime64")
    mag_sys = np.array([p["mag_sys__name"] or "None" for p in phot_values])
    dq_labels = np.array([p["dq_label"] for p in phot_values])
    flagged = np.array([p["flagged"] for p in phot_values], dtype=bool)
    band_ids = np.array([p["band"] for p in phot_values], dtype=int)

    # Convert observation dates to MJD
    mjd = date_to_mjd(obs_date)
    obs_date_str = [utc_day(p["obs_date"]) for p in phot_values]

    # Initialize plot
    colorlist = ['#8dd3c7', '#bebada', '#fb8072', '#80b1d3', '#fdb462', '#b3de69', '#fccde5', '#d9d9d9']
    TOOLTIPS = [
        ("mag", "$y"),
        ("telescope", "@telescope"),
        ("filter", "@filter"),
        ("date (UTC)", "@date"),
        ("dq", "@data_quality"),
        ("magsys", "@magsys"),
    ]

    plot_width = _requested_plot_width(request, 500)
    ax = figure(plot_width=plot_width, plot_height=400, sizing_mode="scale_width")

    # Group by unique bands, in stable legend order (#91)
    unique_bands = np.unique(band_ids)
    unique_bands = unique_bands[_legend_series_order([band_lookup[b] for b in unique_bands])]

    legend_items = []
    flagged_pairs, n_flagged = [], 0
    salt2mjd, flux, fluxerr, salt2band, zpsys = [], [], [], [], []

    # Upper limits (for the y-range): once, not once per band (#254)
    upperlimmjd, upperlimmag = [], []
    for idx, p in enumerate(phot_values):
        if p["flux"] and p["flux_zero_point"] and p["flux"] + 3 * p["flux_err"] > 0:
            upperlimmjd.append(mjd[idx])
            upperlimmag.append(-2.5 * np.log10(p["flux"] + 3 * p["flux_err"]) + p["flux_zero_point"])

    # Rows without a magnitude (forced-photometry non-detections) would put
    # NaN into the ColumnDataSource, which Bokeh cannot serialise (#254).
    has_mag = np.isfinite(mag)

    for count, band_id in enumerate(unique_bands):
        band_obj = band_lookup[band_id]
        inst_name = band_obj.instrument.name if band_obj.instrument_id else None
        tel_name = (
            band_obj.instrument.telescope.name
            if band_obj.instrument_id and band_obj.instrument.telescope_id
            else None
        )
        legend_label = plot_legend_label(
            band_obj.name,
            instrument_name=inst_name,
            telescope_name=tel_name,
        )
        short_filter = display_filter_label(band_obj.name)
        tel_label = telescope_display_name(inst_name, tel_name)
        color = band_display_color(band_obj.name, band_obj.disp_color, fallback_index=count)
        symbol = telescope_display_symbol(inst_name, band_obj.disp_symbol, tel_name)
        if symbol and symbol != 'inverted_triangle':
            try:
                plotmethod = getattr(ax, symbol)
            except AttributeError:
                plotmethod = getattr(ax, 'triangle')  # Default to triangle
        else:
            plotmethod = getattr(ax, 'triangle')  # Default to triangle if bsym is not valid
        size = 20 if symbol == "asterisk" else 7

        # Filter data for this band; flagged points are their own glyph set (#84)
        band_all = (band_ids == band_id) & has_mag
        band_filter = band_all & ~flagged
        # The SALT2 input is the points the plot draws (#607, after #84): the
        # viewer's own photometry, flagged points left out.
        band_mag = mag[band_filter]
        band_mag_err = mag_err[band_filter]

        def _summary_source(sel):
            n = int(sel.sum())
            return ColumnDataSource(
                data=dict(
                    x=mjd[sel].tolist(),
                    y=mag[sel].tolist(),
                    date=np.array(obs_date_str)[sel].tolist(),
                    data_quality=dq_labels[sel].tolist(),
                    magsys=mag_sys[sel].tolist(),
                    telescope=[tel_label] * n,
                    filter=[short_filter] * n,
                )
            )

        def _summary_err(sel, **kwargs):
            err_xs = [(x, x) for x in mjd[sel]]
            err_ys = [(y - yerr, y + yerr) for y, yerr in zip(mag[sel], mag_err[sel])]
            return ax.multi_line(err_xs, err_ys, **kwargs)

        # Plot data
        source = _summary_source(band_filter)
        plot_func = getattr(ax, symbol, ax.triangle)
        plot = plot_func("x", "y", source=source, color=color, size=size, muted_alpha=0.2)
        hover = HoverTool(renderers=[plot], tooltips=TOOLTIPS, toggleable=False)
        ax.add_tools(hover)

        # Error bars
        p_err = _summary_err(band_filter, color=color, muted_alpha=0.2)
        _link_series_visibility(plot, [p_err])

        band_flagged = band_all & flagged
        if band_flagged.any():
            n_flagged += int(band_flagged.sum())
            p_flag = plot_func("x", "y", source=_summary_source(band_flagged), size=size,
                               **_flagged_glyph_kwargs(color))
            ax.add_tools(HoverTool(renderers=[p_flag], tooltips=TOOLTIPS, toggleable=False))
            p_flag_err = _summary_err(band_flagged, **_flagged_error_bar_kwargs(color))
            flagged_pairs += [(plot, p_flag), (plot, p_flag_err)]

        legend_items.append((legend_label, [plot], _band_column_key(band_obj)))

        # SALT2 processing
        if salt2 and str(band_obj.name) in bandpassdict.keys() and band_filter.any():
            salt2mjd.append(mjd[band_filter])
            flux_single = 10 ** (-0.4 * (band_mag - 27.5))
            flux.append(flux_single)
            fluxerr.append(flux_single * band_mag_err * 0.4 * np.log(10))
            salt2band.append(bandpassdict[str(band_obj.name)])
            zpsys.append("Vega" if "bessell" in bandpassdict[str(band_obj.name)] else "AB")

    # Add "today" marker
    today_mjd = Time(datetime.datetime.today()).mjd
    today_line = Span(location=today_mjd, dimension="height", line_color="black", line_width=3)
    ax.add_layout(today_line)

    # Flagged points (#84): muted until the "flagged" legend entry is clicked
    _add_flagged_switch(ax, flagged_pairs, n_flagged, legend_items)

    # Configure legend: columns sized to the longest label (#226)
    _add_legend_grid(ax, legend_items, plot_width)

    # Adaptive x/y ranges
    if discovery_point.any():
        limmjd = mjd[discovery_point].min() - 30
        mjd_range = (limmjd, mjd.max() + 10)
    else:
        limmjd = None
        mjd_range = (mjd.min() - 10, mjd.max() + 10)
    ax.x_range = Range1d(*mjd_range)
    finite_mag = mag[has_mag]
    if len(upperlimmag):
        y_hi = float(np.max(upperlimmag)) + 0.25
    elif len(finite_mag):
        y_hi = float(np.max(finite_mag)) + 0.5
    else:
        y_hi = 20.0
    y_lo = float(np.min(finite_mag)) - 0.5 if len(finite_mag) else y_hi - 2.0
    ax.y_range = Range1d(y_hi, y_lo)

    majorticks = []
    overridedict = {}
    mjdrange = range(int(np.min(mjd) - 100), int(np.max(mjd) + 100))
    times = Time(mjdrange, format='mjd')

    if (not limmjd and np.max(mjd) + 10 - (np.min(mjd) - 10) > 300) or \
       (limmjd and np.max(mjd) + 10 - limmjd > 300):
        count = 0
        for m, t in zip(mjdrange, times):
            tm_list = t.iso.split()[0].split('-')
            if tm_list[2] == '01': count += 1
            if count % 3: continue
            if tm_list[2] == '01':
                majorticks.append(m)
                overridedict[m] = f"{calendar.month_name[int(tm_list[1])][:3]} {int(tm_list[2])}, {int(tm_list[0])}"
    else:
        for m, t in zip(mjdrange, times):
            tm_list = t.iso.split()[0].split('-')
            if tm_list[2] == '01':
                majorticks.append(m)
                overridedict[m] = f"{calendar.month_name[int(tm_list[1])][:3]} {int(tm_list[2])}, {int(tm_list[0])}"

    # Apply ticks and overrides
    ax.xaxis[0].ticker = majorticks
    ax.xaxis[0].major_label_overrides = overridedict

    if salt2:
        try:
            model = sncosmo.Model(source='salt3')
        
            best_z = transient.best_z()  # full precedence list (#736)
            if best_z.value is not None and best_z.origin != 'host photo-z':
                model.set(z=best_z.value)
                fitparams = ['t0', 'x0', 'x1', 'c']
            else:
                fitparams = ['z', 't0', 'x0', 'x1', 'c']
        
            zp = np.array([27.5] * len(salt2band))
            data = Table([salt2mjd, salt2band, flux, fluxerr, zp, zpsys],
                         names=['mjd', 'band', 'flux', 'fluxerr', 'zp', 'zpsys'],
                         meta={'t0': salt2mjd[flux == np.max(flux)]})
        
            result, fitted_model = sncosmo.fit_lc(
                data, model, fitparams,
                bounds={
                    't0': (salt2mjd[flux == np.max(flux)] - 10, salt2mjd[flux == np.max(flux)] + 10),
                    'z': (0.0, 0.7), 'x1': (-3, 3), 'c': (-0.3, 0.3)
                }
            )
        
            count = 0
            plotmjd = np.arange(result['parameters'][1] - 20, result['parameters'][1] + 50, 0.5)
            for band_id in unique_bands:
                band_obj = band_lookup[band_id]
                color = band_display_color(band_obj.name, band_obj.disp_color, fallback_index=count)
                count += 1
                bandkey = f'Band: {band_obj.instrument.name} - {band_obj.name}'
                if bandkey in bandpassdict.keys() and bandpassdict[bandkey] in salt2band:
                    salt2flux = fitted_model.bandflux(
                        bandpassdict[bandkey], plotmjd,
                        zp=27.5, zpsys=zpsys[bandpassdict[bandkey] == salt2band][0]
                    )
                    ax.line(plotmjd, -2.5 * np.log10(salt2flux) + 27.5, color=color)
        
            # Calculate phase and add annotations
            lcphase = today - result['parameters'][1]
            lcphase_str = f"+{lcphase:.1f}" if lcphase > 0 else f"{lcphase:.1f}"
        
            annotations = [
                ("phase = %s days" % lcphase_str, 280),
                ("\uD835\uDE3B  = %.3f" % result.parameters[0], 265),
                ("\uD835\uDC61\u2080  = %i" % result['parameters'][1], 250),
                ("\uD835\uDC5A\u2088 = %.2f" % (10.635 - 2.5 * np.log10(result['parameters'][2])), 235),
                ("\uD835\uDC65\u2081 = %.2f" % result['parameters'][3], 220),
                ("\uD835\uDC50  = %.2f" % result['parameters'][4], 205)
            ]
        
            for text, y in annotations:
                label = Label(x=10, y=y, x_units='screen', y_units='screen',
                              render_mode='css', text_font_size='10pt', text=text)
                ax.add_layout(label)
        except Exception as exc:
            _note_salt2_fit_failure(ax, exc, transient_id)

    response = _bokeh_ajax_response(ax, "my plot")
    if cache_key:
        return _cached_plot_http_response(cache_key, response.content.decode('utf-8'))
    return response


def _limit_or_nan(limit):
    return np.nan if limit is None else limit


@login_required
def lightcurveplot_detail(request, transient_id, salt2=False, bazin=False):
    _load_heavy_plot_stack()

    plot_width = _requested_plot_width(request, 400)
    cache_key = None
    salt_run = None
    if salt2:
        # The SALT3 overlay is the stored run's curves (#315): cheap and
        # deterministic per run, so it is cached under the run's id.
        from YSE_App.services import fit_status
        salt_run = fit_status.stored_salt_fit(transient_id, user=request.user)
    if _plot_html_cache_enabled():
        user_key = group_access_plot_cache_token(request.user)
        cache_key = (
            f'lc_detail_v7_{transient_id}_{_transient_phot_cache_token(transient_id)}'
            f'_{user_key}_w{plot_width}' + ('_bazin' if bazin else '')
            + (f'_salt{salt_run.pk if salt_run is not None else 0}' if salt2 else '')
        )
        cached_html = cache.get(cache_key)
        if cached_html is not None:
            return django.http.HttpResponse(cached_html)

    transient = get_object_or_404(Transient, pk=transient_id)
    photdata = (
        get_all_phot_for_transient(request.user, transient_id)
        .select_related('band__instrument', 'mag_sys')
        .prefetch_related('data_quality')
        .order_by('-modified_date')
    )
    phot_rows = _downsample_phot_rows_for_display(list(photdata))
    if not phot_rows:
        return django.http.HttpResponse('')

    limmjd = None
    salt2mjd = np.array([])
    salt2flux = np.array([])
    salt2fluxerr = np.array([])
    zpsys = np.array([])
    salt2band = np.array([])
    bazin_series = []

    band_lookup = {
        b.pk: b
        for b in PhotometricBand.objects.filter(
            pk__in={p.band_id for p in phot_rows}
        ).select_related('instrument', 'instrument__telescope')
    }

    mags = np.array([p.mag for p in phot_rows], dtype=object)
    mag_errs = np.array([p.mag_err for p in phot_rows], dtype=object)
    disc_points = np.array([p.discovery_point for p in phot_rows], dtype=bool)
    obs_dates_str = np.array([utc_day(p.obs_date) for p in phot_rows])
    mjds = date_to_mjd(np.array([p.obs_date for p in phot_rows]))
    data_quality = np.array([_phot_data_quality_label(p) for p in phot_rows])
    flagged = np.array([_phot_flagged(p) for p in phot_rows], dtype=bool)
    mag_sys = np.array([p.mag_sys.name if p.mag_sys else 'None' for p in phot_rows])
    band = np.array([p.band_id for p in phot_rows], dtype=int)
    band_name = np.array([band_lookup[b].name for b in band])
    instrument_name = np.array([band_lookup[b].instrument.name for b in band])
    disp_symbol = np.array([band_lookup[b].disp_symbol for b in band])
    disp_color = np.array([band_lookup[b].disp_color for b in band])
    # Detection / upper-limit classification shared with the stored
    # statistics (services.phot_points, #368): the triangles this plot draws
    # are the limits the Photometry statistics block counts.
    is_det = np.array(
        [phot_points.is_detection(p.mag, p.mag_err, p.flux, p.flux_err) for p in phot_rows],
        dtype=bool,
    )
    ulim_mags = np.array(
        [_limit_or_nan(phot_points.limiting_mag(p.flux, p.flux_err, p.flux_zero_point)) for p in phot_rows],
        dtype=float,
    )
    is_ulim = np.isfinite(ulim_mags)

    colorlist = ['#8dd3c7','#bebada','#fb8072','#80b1d3','#fdb462','#b3de69','#fccde5','#d9d9d9']
    TOOLTIPS = [
        ('mag','$y'),
        ('telescope','@telescope'),
        ('filter','@filter'),
        ('date (UTC)','@date'),
        ('dq','@data_quality'),
        ('magsys','@magsys')]
    ax=figure(plot_width=240,plot_height=240,sizing_mode='scale_width')#,tooltips=TOOLTIPS)#'stretch_both')
    bandunq,idx = np.unique(band,return_index=True)
    series_order = _legend_series_order([band_lookup[b] for b in bandunq])
    bandunq, idx = bandunq[series_order], idx[series_order]
    count = 0
    legend_it = []
    flagged_pairs, n_flagged = [], 0
    upperlimmag,upperlimmjd = np.array([]),np.array([])
    for bs,bn,b,bc,bsym,inn in zip(
            bandunq,band_name[idx],band[idx],disp_color[idx],disp_symbol[idx],instrument_name[idx]):
        band_obj = band_lookup[b]
        tel_name = (
            band_obj.instrument.telescope.name
            if band_obj.instrument.telescope_id
            else None
        )
        legend_label = plot_legend_label(bn, instrument_name=inn, telescope_name=tel_name)
        short_filter = display_filter_label(bn)
        tel_label = telescope_display_name(inn, tel_name)
        color = band_display_color(bn, bc, fallback_index=count)
        count += 1
        symbol = telescope_display_symbol(inn, bsym, tel_name)
        if symbol and symbol != 'inverted_triangle':
            try:
                plotmethod = getattr(ax, symbol)
            except AttributeError:
                plotmethod = getattr(ax, 'triangle')
        else:
            plotmethod = getattr(ax, 'triangle')
        if bsym != 'asterisk': size=7
        else: size=20

        if salt2:
            bandkey = 'Band: %s - %s'%(inn,bn)
            if bandkey in bandpassdict.keys():
                iBand = (band_name == bn) & (mags != None)
                salt2mag_errs = mag_errs[iBand]
                salt2mag_errs[(salt2mag_errs < 0.01) | (salt2mag_errs == None)] = 0.01
                salt2mjd = np.append(salt2mjd,mjds[iBand])
                fluxes_single = 10**(-0.4*(mags[iBand]-27.5))
                salt2flux = np.append(salt2flux,fluxes_single)
                salt2fluxerr = np.append(salt2fluxerr,fluxes_single*salt2mag_errs*0.4*np.log(10))
                salt2band = np.append(salt2band,[bandpassdict[bandkey]]*len(salt2mag_errs))
                if 'bessell' in bandpassdict[bandkey]: zpsys = np.append(zpsys,['Vega']*len(mag_errs[iBand]))
                else: zpsys = np.append(zpsys,['AB']*len(mag_errs[iBand]))

        iSeries = (band_name == bn) & (instrument_name == inn)
        # all of the band's points: the y range and the SALT2 input as before #84
        iPlotAll = iSeries & is_det
        iUlimAll = iSeries & is_ulim
        # flagged points are their own (muted) glyph set (#84)
        iPlot = iPlotAll & ~flagged
        iUlim = iUlimAll & ~flagged
        upperlimmag = np.append(upperlimmag,ulim_mags[iUlimAll])
        upperlimmjd = np.append(upperlimmjd,mjds[iUlimAll].tolist())

        def _detail_source(sel, y):
            n = int(sel.sum())
            return ColumnDataSource(data=dict(x=mjds[sel].tolist(),
                                              y=y[sel].tolist(),
                                              date=obs_dates_str[sel].tolist(),
                                              data_quality=data_quality[sel].tolist(),
                                              magsys=mag_sys[sel].tolist(),
                                              telescope=[tel_label]*n,
                                              filter=[short_filter]*n))

        def _detail_err(sel, **kwargs):
            err_xs,err_ys = [],[]
            for x,y,yerr in zip(mjds[sel].tolist(),mags[sel].tolist(),mag_errs[sel].tolist()):
                err_xs.append((x, x))
                err_ys.append((y - yerr, y + yerr))
            return ax.multi_line(err_xs, err_ys, **kwargs)

        p_det = plotmethod('x','y',source=_detail_source(iPlot, mags),
                   color=color,size=size, muted_alpha=0.2)
        g1_hover = HoverTool(renderers=[p_det],
                             tooltips=TOOLTIPS,toggleable=False)
        ax.add_tools(g1_hover)

        p_ulim = None
        if iUlim.any():
            # same size as the detections (asterisk series use 20 px for the
            # thin marker; a 20 px triangle would swamp the plot)
            p_ulim = ax.inverted_triangle('x','y',source=_detail_source(iUlim, ulim_mags),
                                     color=color,size=7,muted_alpha=0.2)
            g1_hover = HoverTool(renderers=[p_ulim],
                                 tooltips=TOOLTIPS,toggleable=False)
            ax.add_tools(g1_hover)
        p_err = _detail_err(iPlot, color=color, muted_alpha=0.2)
        _link_series_visibility(p_det, [p_err, p_ulim])

        iPlotFlag = iPlotAll & flagged
        iUlimFlag = iUlimAll & flagged
        if iPlotFlag.any():
            p_flag = plotmethod('x','y',source=_detail_source(iPlotFlag, mags),
                                size=size, **_flagged_glyph_kwargs(color))
            ax.add_tools(HoverTool(renderers=[p_flag], tooltips=TOOLTIPS, toggleable=False))
            p_flag_err = _detail_err(iPlotFlag, **_flagged_error_bar_kwargs(color))
            flagged_pairs += [(p_det, p_flag), (p_det, p_flag_err)]
        if iUlimFlag.any():
            p_flag_ulim = ax.inverted_triangle('x','y',source=_detail_source(iUlimFlag, ulim_mags),
                                               size=7, **_flagged_glyph_kwargs(color))
            ax.add_tools(HoverTool(renderers=[p_flag_ulim], tooltips=TOOLTIPS, toggleable=False))
            flagged_pairs.append((p_det, p_flag_ulim))
        n_flagged += int(iPlotFlag.sum() + iUlimFlag.sum())

        legend_it.append((legend_label, [p_det], _band_column_key(band_obj)))
        if bazin:
            # unflagged detections only (the statistics' rule, #84)
            iFit = iPlot
            bazin_series.append(dict(
                label=legend_label, band_name=bn, color=color, leader=p_det,
                mjd=mjds[iFit].astype(float), mag=mags[iFit].astype(float),
                magerr=mag_errs[iFit].astype(float),
            ))

    today = Time(datetime.datetime.today()).mjd
    x_end = np.max(mjds) + 10
    if bazin:
        bazin_renderers, bazin_x_end = _draw_bazin_fits(ax, bazin_series, today)
        if bazin_renderers:
            legend_it.append((BAZIN_LEGEND_LABEL, bazin_renderers, MARKER_LEGEND_COLUMN))
        if bazin_x_end is not None:
            x_end = max(x_end, bazin_x_end)
    # Flagged points (#84): muted until "Show flagged" or the legend entry is clicked
    flagged_toggle = _add_flagged_switch(ax, flagged_pairs, n_flagged, legend_it, toggle=True)
    p_today = ax.line(today,20,line_width=3,line_color='black')
    legend_it.append(('today (%i)'%today, [p_today], MARKER_LEGEND_COLUMN))
    vline = Span(location=today, dimension='height', line_color='black',
                 line_width=3)
    ax.add_layout(vline)
    legend_height = _add_legend_grid(ax, legend_it, plot_width)

    ax.xaxis.axis_label = 'MJD'
    ax.yaxis.axis_label = 'Mag'
    if len(mjds[disc_points == 1]):
        limmjd = np.min(mjds[disc_points == 1])-30
        ax.x_range=Range1d(limmjd,x_end)
        ax.extra_x_ranges = {"dateax": Range1d(limmjd,x_end)}
        ax.y_range = Range1d(np.max(np.append(mags[mags != None][mjds[mags != None] > limmjd],upperlimmag[upperlimmjd > limmjd]))+0.25,
                             np.min(mags[mags != None][mjds[mags != None] > limmjd])-0.5)
    else:
        if transient.disc_date is not None:
            minmjd = np.max([np.min(mjds)-10,date_to_mjd(transient.disc_date)-30])
        else:
            minmjd = np.min(mjds)-10
        ax.x_range=Range1d(minmjd,x_end)
        ax.extra_x_ranges = {"dateax": Range1d(minmjd,x_end)}
        ax.y_range=Range1d(np.max(np.append(mags[mags != None],upperlimmag))+0.25,np.min(mags[mags != None])-0.5)
        
    #ax.y_range=Range1d(np.max(mags[mags != None])+0.25,np.min(mags[mags != None])-0.5)
    ax.add_layout(LinearAxis(x_range_name="dateax"), 'above')

    ax.plot_height = 400 + legend_height
    ax.plot_width = plot_width

    majorticks = []; overridedict = {}
    mjdrange = range(int(np.min(mjds)-100),int(np.max(mjds)+100))
    times = Time(mjdrange,format='mjd')
    if (not limmjd and np.max(mjds)+10 - (np.min(mjds)-10) > 300) or \
       (limmjd and np.max(mjds)+10 - limmjd > 300):
        count = 0
        for m,t in zip(mjdrange,times):
            tm_list = t.iso.split()[0].split('-')
            if tm_list[2] == '01': count += 1
            if count % 3: continue
            if tm_list[2] == '01':
                majorticks += [m]
                overridedict[m] = '%s %i, %i'%(calendar.month_name[int(tm_list[1])][:3],int(tm_list[2]),int(tm_list[0]))
    else:
        for m,t in zip(mjdrange,times):
            tm_list = t.iso.split()[0].split('-')
            if tm_list[2] == '01':
                majorticks += [m]
                overridedict[m] = '%s %i, %i'%(calendar.month_name[int(tm_list[1])][:3],int(tm_list[2]),int(tm_list[0]))

    ax.xaxis[0].ticker = majorticks
    ax.xaxis[0].major_label_overrides = overridedict

    # add absolute mag axis
    z = transient.z_or_hostz()  # full precedence list, host photo-z last (#736)
    if z is not None:
        mu = cosmo.distmod(z).value
        ax.extra_y_ranges = {"Abs. Mag": Range1d(start=ax.y_range.start-mu, end=ax.y_range.end-mu)}
        ax.add_layout(LinearAxis(y_range_name="Abs. Mag", axis_label="Abs. Mag"), 'right')

    if salt2:
        # Stored SALT3 fit (#315): no sncosmo call in the request.
        series = []
        count = 0
        bandunq, idx = np.unique(band, return_index=True)
        for bn, bc, inn in zip(band_name[idx], disp_color[idx], instrument_name[idx]):
            color = band_display_color(bn, bc, fallback_index=count)
            count += 1
            series.append(('Band: %s - %s' % (inn, bn), color))
        _overlay_stored_salt_fit(ax, transient_id, series, today, flux_space=False, salt_run=salt_run,
                                 user=request.user)

    # the plot stays the first root (the page and the tests read it); the
    # "Show flagged" button follows it
    roots = [ax, flagged_toggle] if flagged_toggle is not None else ax
    html = file_html(roots, CDN, "my plot").replace('width: 90%', 'width: 100%')
    if cache_key:
        return _cached_plot_http_response(cache_key, html)
    return django.http.HttpResponse(html)


@login_required
def lightcurveplot_flux(request, transient_id, salt2=False):
    _load_heavy_plot_stack()
    import time
    tstart = time.time()

    plot_width = _requested_plot_width(request, 400)
    transient = get_object_or_404(Transient, pk=transient_id)
    photdata = (
        get_all_phot_for_transient(request.user, transient_id)
        .select_related(
            'created_by', 'modified_by', 'band', 'unit', 'photometry',
            'band__instrument', 'band__instrument__telescope', 'mag_sys',
        )
        .prefetch_related('data_quality')
    )
    phot_rows = _downsample_phot_rows_for_display(list(photdata))
    if not phot_rows:
        return django.http.HttpResponse('')

    #ax=figure()
    mjd,salt2mjd,date,mag,magerr,magsys,flux,fluxerr,salt2flux,salt2fluxerr,\
        zpsys,salt2band,band,bandstr,bandcolor,bandsym,data_quality = \
        np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),\
        np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),\
        np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),\
        np.array([]),np.array([])
    upperlimmjd,upperlimdate,upperlimmag,upperlimband,upperlimbandstr,upperlimbandcolor,upperlimmagsys = \
        np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),np.array([]),np.array([])
    limmjd = None
    flagged = []

    for p in phot_rows:
        dbmjd = date_to_mjd(p.obs_date)
        if p.flux and np.abs(p.flux) > 1e10: continue

        if p.flux or (not p.flux and p.mag):
            if p.discovery_point:
                limmjd = dbmjd-30
                
            mjd = np.append(mjd,[dbmjd])
            date = np.append(date,[utc_day(p.obs_date)])
            mag = np.append(mag,[p.mag])
            if p.mag_sys is None: magsys = np.append(magsys,['None'])
            else: magsys = np.append(magsys,[p.mag_sys])

            data_quality = np.append(data_quality, [_phot_data_quality_label(p)])
            flagged.append(_phot_flagged(p))

            if not p.flux or not p.flux_err:
                flux_single = 10**(-0.4*(p.mag-27.5))
                flux = np.append(flux,flux_single)
                if p.mag_err: mag_err = p.mag_err
                else: mag_err = 0.02
                fluxerr = np.append(fluxerr,flux_single*mag_err*0.4*np.log(10))
            else:
                flux = np.append(flux,p.flux)
                fluxerr = np.append(fluxerr,p.flux_err)
            if p.mag_err: magerr = np.append(magerr,p.mag_err)
            else: magerr = np.append(magerr,0)
            bandstr = np.append(bandstr,str(p.band))
            inst_name = p.band.instrument.name if p.band.instrument_id else None
            bandcolor = np.append(
                bandcolor,
                band_display_color(p.band.name, p.band.disp_color),
            )
            point_tel = (p.band.instrument.telescope.name
                         if inst_name and p.band.instrument.telescope_id else None)
            sym = telescope_display_symbol(inst_name, p.band.disp_symbol, point_tel)
            if sym in py2bokeh_symboldict.keys():
                bandsym = np.append(bandsym, str(py2bokeh_symboldict[sym]))
            else:
                bandsym = np.append(bandsym, str(sym))
            band = np.append(band,p.band)
            if salt2:
                if str(p.band) in bandpassdict.keys():
                    if p.mag_err: mag_err = p.mag_err
                    else: mag_err = 0.02
                    flux_single = 10**(-0.4*(p.mag-27.5))
                    salt2mjd = np.append(salt2mjd,[dbmjd])
                    salt2flux = np.append(salt2flux,flux_single)
                    salt2fluxerr = np.append(salt2fluxerr,flux_single*mag_err*0.4*np.log(10))
                    salt2band = np.append(salt2band,bandpassdict[str(p.band)])
                    if 'bessell' in bandpassdict[str(p.band)]: zpsys = np.append(zpsys,'Vega')
                    else: zpsys = np.append(zpsys,'AB')
                
        elif p.flux and p.flux_zero_point and p.flux + 3*p.flux_err > 0:
            upperlimmjd = np.append(upperlimmjd,[dbmjd])
            upperlimdate = np.append(upperlimdate,[utc_day(p.obs_date)])
            upperlimmag = np.append(upperlimmag,[-2.5*np.log10(p.flux + 3*p.flux_err) + p.flux_zero_point])
            upperlimbandstr = np.append(upperlimbandstr,str(p.band))
            upperlimband = np.append(upperlimband,p.band)
            if p.mag_sys is None: upperlimmagsys = np.append(upperlimmagsys,'None')
            else: upperlimmagsys = np.append(upperlimmagsys,p.mag_sys)
            upperlimbandcolor = np.append(
                upperlimbandcolor,
                band_display_color(p.band.name, p.band.disp_color),
            )
            if salt2:
                if str(p.band) in bandpassdict.keys():
                    salt2mjd = np.append(salt2mjd,[dbmjd])
                    salt2flux = np.append(salt2flux,p.flux)
                    salt2fluxerr = np.append(salt2fluxerr,p.flux_err)
                    salt2band = np.append(salt2band,bandpassdict[str(p.band)])
                    if 'bessell' in bandpassdict[str(p.band)]: zpsys = np.append(zpsys,'Vega')
                    else: zpsys = np.append(zpsys,'AB')

    if transient.non_detect_limit and transient.non_detect_band:
        upperlimmjd = np.append(upperlimmjd,[date_to_mjd(transient.non_detect_date)])
        upperlimdate = np.append(upperlimdate,[utc_day(transient.non_detect_date)])
        upperlimmag = np.append(upperlimmag,transient.non_detect_limit)
        upperlimmagsys = np.append(upperlimmagsys,'None')
        upperlimbandstr = np.append(upperlimbandstr,str(transient.non_detect_band))
        upperlimband = np.append(upperlimband,transient.non_detect_band)
        upperlimbandcolor = np.append(
            upperlimbandcolor,
            band_display_color(
                transient.non_detect_band.name,
                transient.non_detect_band.disp_color,
            ),
        )
        
    colorlist = ['#8dd3c7','#bebada','#fb8072','#80b1d3','#fdb462','#b3de69','#fccde5','#d9d9d9']
    count = 0
    allband = np.append(band,upperlimband)
    allbandstr = np.append(bandstr,upperlimbandstr)
    allbandcolor = np.append(bandcolor,upperlimbandcolor)
    allbandsym = np.append(bandsym,[None]*len(upperlimbandcolor))
    bandunq,idx = np.unique(allbandstr,return_index=True)
    series_order = _legend_series_order(allband[idx])
    bandunq, idx = bandunq[series_order], idx[series_order]

    TOOLTIPS = [
        ('mag','$y'),
        ('telescope','@telescope'),
        ('filter','@filter'),
        ('date (UTC)','@date'),
        ('dq','@data_quality'),
        ('magsys','@magsys')]
    ax=figure(plot_width=240,plot_height=240,sizing_mode='scale_width')#,tooltips=TOOLTIPS)#'stretch_both')

    
    flagged = np.array(flagged, dtype=bool)
    legend_it = []
    flagged_pairs, n_flagged = [], 0
    for bs,b,bc,bsym in zip(bandunq,allband[idx],allbandcolor[idx],allbandsym[idx]):
        raw_band_name = b.name if hasattr(b, 'name') else str(bs)
        short_filter = display_filter_label(raw_band_name)
        tel_label = telescope_display_name(
            b.instrument.name if hasattr(b, 'instrument_id') and b.instrument_id else None,
            b.instrument.telescope.name
            if hasattr(b, 'instrument') and getattr(b.instrument, 'telescope_id', None)
            else None,
        )
        legend_label = f'{tel_label} {short_filter}'
        color = band_display_color(raw_band_name, bc, fallback_index=count)
        count += 1
        inst_name = b.instrument.name if hasattr(b, 'instrument_id') and b.instrument_id else None
        tel_name = (b.instrument.telescope.name
                    if inst_name and getattr(b.instrument, 'telescope_id', None) else None)
        symbol = telescope_display_symbol(inst_name, bsym if bsym != 'None' else None, tel_name)
        if symbol and symbol != 'inverted_triangle':
            try:
                plotmethod = getattr(ax, symbol)
            except AttributeError:
                plotmethod = getattr(ax, 'triangle')
        else:
            plotmethod = getattr(ax, 'triangle')
        if bsym != 'asterisk': size=7
        else: size=20
            
        def _flux_source(sel):
            n = int(sel.sum())
            return ColumnDataSource(data=dict(x=mjd[sel].tolist(),
                                              y=flux[sel].tolist(),
                                              date=date[sel].tolist(),
                                              data_quality=data_quality[sel].tolist(),
                                              magsys=magsys[sel].tolist(),
                                              telescope=[tel_label]*n,
                                              filter=[short_filter]*n))

        def _flux_err(sel, **kwargs):
            err_xs,err_ys = [],[]
            for x,y,yerr in zip(mjd[sel].tolist(),flux[sel].tolist(),fluxerr[sel].tolist()):
                err_xs.append((x, x))
                err_ys.append((y - yerr, y + yerr))
            return ax.multi_line(err_xs, err_ys, **kwargs)

        # flagged points are their own (muted) glyph set (#84)
        iBand = bandstr == bs
        iGood = iBand & ~flagged
        p = plotmethod('x','y',source=_flux_source(iGood),
                   color=color,size=size, muted_alpha=0.2)#,legend='%s - %s'%(

        g1_hover = HoverTool(renderers=[p],
                             tooltips=TOOLTIPS,toggleable=False)
        ax.add_tools(g1_hover)

        p_err = _flux_err(iGood, color=color, muted_alpha=0.2)
        _link_series_visibility(p, [p_err])

        iFlag = iBand & flagged
        if iFlag.any():
            n_flagged += int(iFlag.sum())
            p_flag = plotmethod('x','y',source=_flux_source(iFlag), size=size,
                                **_flagged_glyph_kwargs(color))
            ax.add_tools(HoverTool(renderers=[p_flag], tooltips=TOOLTIPS, toggleable=False))
            p_flag_err = _flux_err(iFlag, **_flagged_error_bar_kwargs(color))
            flagged_pairs += [(p, p_flag), (p, p_flag_err)]

        legend_it.append((legend_label, [p], _band_column_key(b)))
        
    _add_flagged_switch(ax, flagged_pairs, n_flagged, legend_it)
    today = Time(datetime.datetime.today()).mjd
    p = ax.line(today,20,line_width=3,line_color='black')
    legend_it.append(('today (%i)'%today, [p], MARKER_LEGEND_COLUMN))
    vline = Span(location=today, dimension='height', line_color='black',
                 line_width=3)
    ax.add_layout(vline)
    hline = Span(location=0, dimension='width', line_color='black',
                 line_width=3)
    ax.add_layout(hline)
    legend_height = _add_legend_grid(ax, legend_it, plot_width)
    

    
    ax.xaxis.axis_label = 'MJD'
    ax.yaxis.axis_label = 'Flux (ZP = 27.5)'

    if limmjd:
        ax.x_range = Range1d(limmjd,np.max(mjd)+10)
        ax.y_range = Range1d(0-np.max(flux[mjd > limmjd])*0.03,
                             np.max(flux[mjd > limmjd])*1.1)

        ax.extra_x_ranges = {"dateax": Range1d(limmjd,np.max(mjd)+10)}
        ax.add_layout(LinearAxis(x_range_name="dateax"), 'above')
        
    else:
        ax.x_range=Range1d(np.min(mjd)-10,np.max(mjd)+10)
        ax.y_range=Range1d(0-np.max(flux)*0.03,np.max(flux)*1.1)
        ax.extra_x_ranges = {"dateax": Range1d(np.min(mjd)-10,np.max(mjd)+10)}
        ax.add_layout(LinearAxis(x_range_name="dateax"), 'above')

    ax.plot_height = 200 + legend_height
    ax.plot_width = plot_width

    majorticks = []; overridedict = {}
    mjdrange = range(int(np.min(mjd)-100),int(np.max(mjd)+100))
    times = Time(mjdrange,format='mjd')
    if (not limmjd and np.max(mjd)+10 - (np.min(mjd)-10) > 300) or \
       (limmjd and np.max(mjd)+10 - limmjd > 300):
        count = 0
        for m,t in zip(mjdrange,times):
            tm_list = t.iso.split()[0].split('-')
            if tm_list[2] == '01': count += 1
            if count % 3: continue
            if tm_list[2] == '01':
                majorticks += [m]
                overridedict[m] = '%s %i, %i'%(calendar.month_name[int(tm_list[1])][:3],int(tm_list[2]),int(tm_list[0]))
    else:
        for m,t in zip(mjdrange,times):
            tm_list = t.iso.split()[0].split('-')
            if tm_list[2] == '01':
                majorticks += [m]
                overridedict[m] = '%s %i, %i'%(calendar.month_name[int(tm_list[1])][:3],int(tm_list[2]),int(tm_list[0]))

    ax.xaxis[0].ticker = majorticks
    ax.xaxis[0].major_label_overrides = overridedict
    
    if salt2:
        # Stored SALT3 fit (#315) in flux units; no sncosmo call in the request.
        series = []
        count = 0
        bandunq, idx = np.unique(bandstr, return_index=True)
        for bs, bc in zip(bandunq, bandcolor[idx]):
            if bc != 'None' and bc:
                color = bc
            else:
                color = colorlist[count % len(np.unique(colorlist))]
                count += 1
            series.append((str(bs), color))
        _overlay_stored_salt_fit(ax, transient_id, series, today, flux_space=True, user=request.user)

    g = file_html(ax,CDN,"my plot")
    return HttpResponse(g.replace('width: 90%','width: 100%'))

def _spectrum_plot_empty_message(n_spectra_without_points):
    """Empty-state HTML for ``spectrumplot``.

    The Summary-tab toolbar counts TransientSpectrum rows while the plot needs
    TransientSpecData points, so say which of the two is missing (#208).
    """
    if n_spectra_without_points == 0:
        text = 'No spectrum data on file for this transient.'
    elif n_spectra_without_points == 1:
        text = '1 spectrum on file, but it has no wavelength/flux points to plot.'
    else:
        text = (
            f'{n_spectra_without_points} spectra on file, but none has '
            'wavelength/flux points to plot.'
        )
    return f'<p class="text-muted yse-plot-empty">{text}</p>'


def _spec_points_by_spectrum(spectrum_ids):
    """``{spectrum_id: (wave, flux)}`` numpy arrays for the given spectra.

    Reads through ``common.spectrum_arrays.arrays_for`` (#891): one arrays row per
    spectrum when present, else the per-pixel TransientSpecData rows in one
    ``values_list`` query (#479), never one model instance per pixel. Pixels come
    back sorted by wavelength (rows: by wavelength, then pk); spectra without
    pixels are absent from the dict.
    """
    from .common import spectrum_arrays

    return {
        sid: (wave, flux)
        for sid, (wave, flux, _err) in spectrum_arrays.arrays_for(spectrum_ids, with_errors=False).items()
        if wave.size
    }


def _spectra_for_plot(dbspectra):
    """Turn TransientSpectrum rows into plottable dicts, skipping empty ones.

    Returns ``(spectra, n_without_points)``: spectra without TransientSpecData
    points are counted instead of plotted, so a transient whose spectra were
    registered without wavelength/flux rows gets the empty-state message rather
    than a traceback (#343). Each spectrum is resampled to at most
    ``MAX_SPEC_DISPLAY_PIXELS`` points. Points are read with one ``values_list``
    query for all spectra (#479), not one ORM object per pixel. A flagged
    spectrum (#84) carries ``flagged`` and a " (flagged)" legend label.
    """
    spectra = []
    n_without_points = 0
    dbspectra = list(dbspectra)
    points = _spec_points_by_spectrum(spectrum.id for spectrum in dbspectra)
    empty = np.array([], dtype=float)
    for spectrum in dbspectra:
        wave, flux = points.get(spectrum.id, (empty, empty))
        if wave.size == 0 or flux.size == 0:
            n_without_points += 1
            continue

        sort_idx = np.argsort(wave)
        wave = wave[sort_idx]
        flux = flux[sort_idx]

        n_display = min(MAX_SPEC_DISPLAY_PIXELS, max(2, wave.size))
        wave_interp = np.linspace(np.min(wave), np.max(wave), n_display)
        flux_interp = np.interp(wave_interp, wave, flux)

        flagged = _spectrum_flagged(spectrum)
        label = f'{spectrum.instrument.name} - {utc_day(spectrum.obs_date)}'
        spectra.append({
            'wave': wave_interp,
            'flux': flux_interp,
            'mjd': date_to_mjd(spectrum.obs_date.isoformat().split('+')[0]),
            'label': label + (FLAGGED_SPECTRUM_SUFFIX if flagged else ''),
            'flagged': flagged,
        })
    return spectra, n_without_points


def _spectrum_line_kwargs(spec):
    """A flagged spectrum (#84) is dashed and starts muted; its legend entry un-mutes it."""
    if not spec.get('flagged'):
        return {}
    return dict(line_dash='dashed', muted=True)


def _normalized_plot_flux(flux, offset):
    """Scale ``flux`` onto the 5th-95th percentile range and stack it at ``offset``."""
    n_pix = len(flux)
    if n_pix == 0:
        return flux
    sort_flux = np.sort(flux)
    # Clamp: round(n * 0.95) is n itself for short spectra (fewer than about ten pixels).
    minval = sort_flux[min(round(n_pix * 0.05), n_pix - 1)] * 0.5
    maxval = sort_flux[min(round(n_pix * 0.95), n_pix - 1)] * 1.1
    scale = maxval - minval
    if scale == 0:
        scale = 1.0
    return (flux - minval) / scale + offset


@login_required
def spectrumplot(request, transient_id):
    cache_key = None
    if _plot_html_cache_enabled():
        user_key = group_access_plot_cache_token(request.user)
        cache_key = (
            f'specplot_v3_{transient_id}_{_transient_spectrum_cache_token(transient_id)}_{user_key}'
        )
        cached_html = cache.get(cache_key)
        if cached_html is not None:
            return django.http.HttpResponse(cached_html)

    transient = get_object_or_404(Transient, pk=transient_id)
    dbspectra = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(
        request.user, transient_id, includeBadData=True
    ).select_related('instrument')
    
    spectra, n_without_points = _spectra_for_plot(dbspectra)
    if not spectra:
        return django.http.HttpResponse(_spectrum_plot_empty_message(n_without_points))

    # bokeh/matplotlib/sncosmo are imported only now: a transient with nothing
    # to plot answers in milliseconds instead of paying the cold import.
    _load_heavy_plot_stack()

    # Process spectra and compute offsets
    dates = np.array([spec['mjd'] for spec in spectra])
    sorted_idx = np.argsort(-dates)
    offsets = np.argsort(sorted_idx)
    
    # Prepare plot
    ax = figure(plot_width=240, plot_height=240, sizing_mode='stretch_width', y_range=(-0.15, len(spectra) + 0.15))
    colors = itertools.cycle(palette)
    legend_items = []
    
    for spec, color, offset in zip(spectra, colors, offsets):
        norm_flux = _normalized_plot_flux(spec['flux'], offset)
        # Step plot centred on each pixel, so individual pixels are visible.
        p = ax.step(spec['wave'], norm_flux, mode='center', color=color, muted_alpha=0.2,
                    **_spectrum_line_kwargs(spec))
        legend_items.append((spec['label'], [p]))
    
    # Add legend
    legend = Legend(items=legend_items, location="bottom_right")
    legend.click_policy = "mute"
    ax.add_layout(legend)
    
    # Configure plot dimensions
    ax.plot_height = 400 + 50 * len(spectra)
    ax.plot_width = 400
    ax.x_range = Range1d(3000, 10000)
    ax.xaxis.axis_label = r'Wavelength (Angstrom)'
    ax.yaxis.axis_label = 'Flux'
    
    html = file_html(ax, CDN, "spectrum plot").replace('width: 90%', 'width: 100%')
    if cache_key:
        return _cached_plot_http_response(cache_key, html)
    return django.http.HttpResponse(html)

@login_required
def spectrumplot_summary(request, transient_id):
    # Same 900 s plot-HTML cache as spectrumplot (#460); the summary page asks
    # for one of these per transient card.
    cache_key = None
    if _plot_html_cache_enabled():
        user_key = group_access_plot_cache_token(request.user)
        cache_key = (
            f'specsummary_v2_{transient_id}_{_transient_spectrum_cache_token(transient_id)}_{user_key}'
        )
        cached_html = cache.get(cache_key)
        if cached_html is not None:
            return HttpResponse(cached_html)

    transient = get_object_or_404(Transient, pk=transient_id)
    dbspectra = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(
        request.user, transient_id, includeBadData=True
    ).select_related('instrument')

    spectra, n_without_points = _spectra_for_plot(dbspectra)
    if not spectra:
        if n_without_points == 0:
            # The summary card stays blank for a transient without spectra;
            # the blank answer is cached too (the token changes with any spectrum).
            if cache_key:
                cache.set(cache_key, '', timeout=PLOT_HTML_CACHE_TIMEOUT)
            return django.http.HttpResponse('')
        # Same empty state as spectrumplot instead of an UnboundLocalError (#343).
        empty_html = _spectrum_plot_empty_message(n_without_points)
        if cache_key:
            cache.set(cache_key, empty_html, timeout=PLOT_HTML_CACHE_TIMEOUT)
        return django.http.HttpResponse(empty_html)

    _load_heavy_plot_stack()

    # Newest spectrum at the top of the stack, legend in date order.
    dates = np.array([spec['mjd'] for spec in spectra])
    offsets = np.argsort(np.argsort(-dates))
    ax = figure(plot_width=240, plot_height=240, sizing_mode='stretch_width',
                y_range=(-0.15, len(spectra) + 0.15))

    colors = itertools.cycle(palette)
    legend_it = [None] * len(spectra)
    for spec, color, offset in zip(spectra, colors, offsets):
        norm_flux = _normalized_plot_flux(spec['flux'], offset)
        # Step plot centred on each pixel, so individual pixels are visible.
        p = ax.step(spec['wave'], norm_flux, mode='center', color=color, muted_alpha=0.2,
                    **_spectrum_line_kwargs(spec))
        legend_it[len(spectra) - 1 - offset] = (spec['label'], [p])

    legend = Legend(items=legend_it, location="bottom_right")
    legend.click_policy = "mute"
    legend.label_height = 1
    legend.glyph_height = 20
    ax.add_layout(legend)

    ax.plot_height = 200
    ax.plot_width = 400

    ax.xaxis.axis_label = r'Wavelength (Angstrom)'
    ax.yaxis.axis_label = 'Flux'
    g = file_html(ax, CDN, "spectrum plot").replace('width: 90%', 'width: 100%')
    if cache_key:
        return _cached_plot_http_response(cache_key, g)
    return HttpResponse(g)


#def spectrumplot(request, transient_id):
#   tstart = time.time()
#   transient = Transient.objects.get(pk=transient_id)
#   spectrum = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(request.user, transient_id, includeBadData=True)
#
#   if not len(spectrum):
#       return django.http.HttpResponse('')
#   else:
#       spectrum = spectrum[0]
#   spec = TransientSpecData.objects.filter(spectrum=spectrum)
#   
#   if not len(spec):
#       return django.http.HttpResponse('')
#
#   #figure is a function in the bokeh module
#   #HELLO
#   wave,flux = [],[]
#   for s in spec:
#       wave += [s.wavelength]
#       flux += [s.flux]
#
#   bottom = np.percentile(flux,5)*0.5; top = np.percentile(flux,95)*1.3
#   if np.percentile(flux,2) < 0:
#       bottom = np.percentile(flux,2)*1.5
#   ax=figure(plot_width=240,plot_height=240,sizing_mode='stretch_both',
#             y_range=(bottom, top))
#   ax.line(np.sort(wave),np.array(flux)[np.argsort(wave)],color='black')
#
#   ax.title.text = "%s, %s, %s, Phase: %s, DQ: %s"%(transient.name,spectrum.obs_date.strftime('%m/%d/%Y'),spectrum.instrument,spectrum.spec_phase,spectrum.data_quality)
#   ax.xaxis.axis_label = r'Wavelength (Angstrom)'
#   ax.yaxis.axis_label = 'Flux'
#   g = file_html(ax,CDN,"spectrum plot")
#   time.sleep(5)
#   return HttpResponse(g.replace('width: 90%','width: 100%'))

@login_required
def spectrumplotsingle(request, transient_id, spec_id):
    _load_heavy_plot_stack()
    
    transient = get_object_or_404(Transient, pk=transient_id)
    spectra = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(request.user, transient_id, includeBadData=True)
    spectrum = spectra.filter(id=spec_id)
    
    if not len(spectrum):
        return django.http.HttpResponse(
            '<p class="text-muted yse-plot-empty">'
            'That spectrum is not on file for this transient (or you are not '
            'authorized to see it).</p>'
        )
    else:
        spectrum = spectrum[0]
    wave, flux = _spec_points_by_spectrum([spectrum.id]).get(spectrum.id, ([], []))

    if not len(wave):
        return django.http.HttpResponse(_spectrum_plot_empty_message(1))

    bottom = np.percentile(flux,5)*0.5; top = np.percentile(flux,95)*1.3
    if np.percentile(flux,2) < 0:
        bottom = np.percentile(flux,2)*1.5

    ax=figure(plot_width=240,plot_height=240,sizing_mode='stretch_both',
              y_range=(bottom, top))
    # Step plot centred on each pixel, so individual pixels are visible.
    ax.step(np.sort(wave),np.array(flux)[np.argsort(wave)],mode='center',color='black')
        
    ax.title.text = "%s, %s, %s, Phase: %s, DQ: %s"%(transient.name,utc_day(spectrum.obs_date),spectrum.instrument,spectrum.spec_phase,','.join([d.name for d in spectrum.data_quality.all()]))
    ax.xaxis.axis_label = r'Wavelength (Angstrom)'
    ax.yaxis.axis_label = 'Flux'
    g = file_html(ax,CDN,"my plot")

    return HttpResponse(g.replace('width: 90%','width: 100%'))

def _classical_night_rise_set(request_transient_id, obs_id):
    """(rise, set) strings for a transient on a classical night, solved inline (#463).

    The old views forked a Process per request and read the answer from one
    module-global Queue with no timeout, so concurrent requests could receive each
    other's times. This is a plain function call, cached per (telescope, night,
    coordinates), so there is nothing shared between requests.
    """
    from YSE_App.services.night_astro import target_rise_set

    transient = Transient.objects.filter(id=request_transient_id)[0]
    coords = transient.CoordString()
    obsnight = ClassicalObservingDate.objects.get(pk=obs_id)
    return target_rise_set(obsnight.resource.telescope, obsnight.obs_date, coords)


@login_required
def rise_time(request,transient_id,obs_id):
    return JsonResponse({'rise_time': _classical_night_rise_set(transient_id, obs_id)[0]})


@login_required
def set_time(request,transient_id,obs_id):
    return JsonResponse({'set_time': _classical_night_rise_set(transient_id, obs_id)[1]})


@login_required
def moon_angle(request,transient_id,obs_id):
    _load_astropy_coords()
    transient = Transient.objects.filter(id=transient_id)[0]
    coords = transient.CoordString()

    obsnight = ClassicalObservingDate.objects.get(pk=obs_id)
    
    obstime = Time(str(obsnight.obs_date).split()[0],scale='utc')

    mooncoord = get_moon(obstime)
    cs = SkyCoord('%s %s'%(coords[0],coords[1]),unit=(u.hourangle,u.deg))
    moondict = {'moon_angle':'%.1f deg'%cs.separation(mooncoord).deg}
    return JsonResponse(moondict)

@login_required
def tonight_rise_time(request,transient_id,too_id):
    _load_astropy_coords()
    transient = Transient.objects.filter(id=transient_id)[0]
    coords = transient.CoordString()

    obsnight = ToOResource.objects.filter(id=too_id)[0]
    
    time = Time(datetime.datetime.now())
    sc = SkyCoord('%s %s'%(coords[0],coords[1]),unit=(u.hourangle,u.deg))

    location = EarthLocation.from_geodetic(
        obsnight.telescope.longitude*u.deg,obsnight.telescope.latitude*u.deg,
        obsnight.telescope.elevation*u.m)
    tel = Observer(location=location, timezone="UTC")

    target_rise_time = tel.target_rise_time(time,sc,horizon=18*u.deg,which="previous")

    if target_rise_time:
        try: returnstarttime = target_rise_time.isot.split('T')[-1]
        except (AttributeError, ValueError): returnstarttime = None
    else: returnstarttime = None

    risedict = {'rise_time':returnstarttime}
    return JsonResponse(risedict)

@login_required
def tonight_set_time(request,transient_id,too_id):
    _load_astropy_coords()
    transient = Transient.objects.filter(id=transient_id)[0]
    coords = transient.CoordString()

    obsnight = ToOResource.objects.filter(id=too_id)[0]
    
    time = Time(datetime.datetime.now())
    sc = SkyCoord('%s %s'%(coords[0],coords[1]),unit=(u.hourangle,u.deg))

    location = EarthLocation.from_geodetic(
        obsnight.telescope.longitude*u.deg,obsnight.telescope.latitude*u.deg,
        obsnight.telescope.elevation*u.m)
    tel = Observer(location=location, timezone="UTC")

    target_set_time = tel.target_set_time(time,sc,horizon=18*u.deg,which="previous")

    if target_set_time:
        try: returnstarttime = target_set_time.isot.split('T')[-1]
        except (AttributeError, ValueError): returnstarttime = None
    else: returnstarttime = None

    setdict = {'set_time':returnstarttime}
    return JsonResponse(setdict)
    
@login_required
def tonight_moon_angle(request,transient_id,too_id):
    _load_astropy_coords()
    transient = Transient.objects.filter(id=transient_id)[0]
    coords = transient.CoordString()

    obstime = Time(datetime.datetime.now())
    mooncoord = get_moon(obstime)
    cs = SkyCoord('%s %s'%(coords[0],coords[1]),unit=(u.hourangle,u.deg))
    moondict = {'moon_angle':'%.1f deg'%cs.separation(mooncoord).deg}
    return JsonResponse(moondict)

# The PS1 cutout page for a position never changes; a found URL is kept for a
# month, a "no image" answer for a day, and a failed/timed-out scrape only
# briefly so the next load retries it (#460).
PS1_IMAGE_CACHE_SECONDS = 30 * 24 * 3600
PS1_NO_IMAGE_CACHE_SECONDS = 24 * 3600
PS1_FAILURE_CACHE_SECONDS = 60


@login_required
def get_ps1_image(request,transient_id):

    try:
        t = Transient.objects.only('ra', 'dec').get(pk=transient_id)
    except Transient.DoesNotExist:
        raise Http404("Transient id does not exist")

    cache_key = 'ps1_image_v1_%s_%.7f_%.7f' % (transient_id, t.ra, t.dec)
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    ps1url = ("https://ps1images.stsci.edu/cgi-bin/ps1cutouts?pos=%.7f+%.7f&filter=color" % (t.ra,t.dec))
    try:
        response = requests.get(url=ps1url,timeout=5)
    except requests.RequestException as exc:
        logger.warning("PS1 cutout request failed for transient %s: %s", transient_id, exc)
        failure = {"jpegurl":"","msg":"timeout"}
        cache.set(cache_key, failure, PS1_FAILURE_CACHE_SECONDS)
        return(JsonResponse(failure))
    response_text = response.content.decode('utf-8')
    if "<td><img src=" in response_text:
        jpegurl = response_text.split('<td><img src="')[1].split('" width="240" height="240" /></td>')[0]
        jpegurl = "http:%s"%jpegurl
    else:
        jpegurl=""

    jpegurldict = {"jpegurl":jpegurl,"msg":"success"}
    if response.status_code == 200:
        cache.set(cache_key, jpegurldict,
                  PS1_IMAGE_CACHE_SECONDS if jpegurl else PS1_NO_IMAGE_CACHE_SECONDS)
    else:
        cache.set(cache_key, jpegurldict, PS1_FAILURE_CACHE_SECONDS)
    return(JsonResponse(jpegurldict))

ARCHIVE_STATUS_TIMEOUT_SECONDS = int(os.environ.get('YSE_ARCHIVE_STATUS_TIMEOUT', '8'))
ARCHIVE_STATUS_CACHE_SECONDS = 3600
# A failed or timed-out archive lookup is remembered only briefly, so a
# reload retries it instead of showing a stale negative for an hour.
ARCHIVE_STATUS_FAILURE_CACHE_SECONDS = 60


# One bounded pool for every archive lookup. A lookup that outlives its timeout
# keeps its worker until MAST answers, but the pool caps how many can pile up
# (a fresh unbounded thread per request, shut down with wait=False, let them
# accumulate when an archive was slow; #467).
_ARCHIVE_STATUS_POOL = None
_ARCHIVE_STATUS_POOL_LOCK = threading.Lock()
ARCHIVE_STATUS_MAX_WORKERS = int(os.environ.get('YSE_ARCHIVE_STATUS_WORKERS', '8'))


def _archive_status_pool():
    global _ARCHIVE_STATUS_POOL
    if _ARCHIVE_STATUS_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        with _ARCHIVE_STATUS_POOL_LOCK:
            if _ARCHIVE_STATUS_POOL is None:
                _ARCHIVE_STATUS_POOL = ThreadPoolExecutor(
                    max_workers=ARCHIVE_STATUS_MAX_WORKERS, thread_name_prefix='archive-status')
    return _ARCHIVE_STATUS_POOL


def _archive_status_with_timeout(work, *, timeout_seconds=None):
    """Run a blocking archive lookup with a wall-clock timeout.

    Returns ``None`` when the lookup does not finish in time. The caller does not
    wait for a lookup that is already running (a ``ThreadPoolExecutor`` used as a
    context manager would ``shutdown(wait=True)`` and make the timeout cosmetic);
    one still queued behind busy workers is cancelled.
    """
    from concurrent.futures import TimeoutError as FuturesTimeout

    if timeout_seconds is None:
        timeout_seconds = ARCHIVE_STATUS_TIMEOUT_SECONDS
    future = _archive_status_pool().submit(work)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeout:
        future.cancel()
        return None


# Transient columns that the archive lookups may write (``_record_archive_flag``).
ARCHIVE_FLAG_FIELDS = frozenset({'has_hst', 'has_jwst', 'has_chandra'})


def _record_archive_flag(transient_id, field, has_data):
    """Store a fresh archive answer on ``Transient.<field>`` (``has_hst`` / ``has_jwst`` / ``has_chandra``).

    One ``UPDATE`` only when the stored value differs; bypasses ``save()`` so
    ``modified_date`` and the save signals are left alone (this is a lookup,
    not an edit). A failed write never breaks the tab label.
    """
    if has_data is None or not field:
        return False
    if field not in ARCHIVE_FLAG_FIELDS:  # only the three archive columns (#540)
        logger.warning('archive flag %r is not an archive column; not recorded', field)
        return False
    try:
        return bool(Transient.objects.filter(pk=transient_id).exclude(**{field: bool(has_data)})
                    .update(**{field: bool(has_data)}))
    except Exception:  # pragma: no cover - a DB hiccup must not break the label
        logger.warning('could not record %s for transient %s', field, transient_id, exc_info=True)
        return False


def _archive_status_payload(cache_key, lookup, archive_name, transient_id=None, flag_field=None):
    """JSON payload for the HST/JWST/Chandra tab labels.

    ``has_data`` is ``True``/``False`` only when the archive answered; when the
    lookup timed out or raised it is ``None`` and ``error`` says why, so the
    page can say "lookup failed" instead of "No HST" (an upstream outage is
    not the same as no data). Failures are cached for a minute, answers for
    an hour. A fresh answer is also written to ``Transient.<flag_field>``
    (``has_jwst`` for JWST, which nothing else sets) so it can be searched.
    """
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        count = _archive_status_with_timeout(lookup)
    except Exception as exc:
        logger.warning('%s archive status lookup failed: %s', archive_name, exc)
        payload = {
            'has_data': None, 'count': 0, 'error': 'lookup_failed',
            'message': f'{archive_name} archive lookup failed; open the tab to retry.',
        }
        cache.set(cache_key, payload, timeout=ARCHIVE_STATUS_FAILURE_CACHE_SECONDS)
        return payload
    if count is None:
        payload = {
            'has_data': None, 'count': 0, 'error': 'timeout', 'timed_out': True,
            'message': (
                f'{archive_name} archive did not answer within '
                f'{ARCHIVE_STATUS_TIMEOUT_SECONDS} s; open the tab to retry.'
            ),
        }
        cache.set(cache_key, payload, timeout=ARCHIVE_STATUS_FAILURE_CACHE_SECONDS)
        return payload
    payload = {'has_data': count > 0, 'count': count}
    cache.set(cache_key, payload, timeout=ARCHIVE_STATUS_CACHE_SECONDS)
    if transient_id is not None and flag_field:
        _record_archive_flag(transient_id, flag_field, payload['has_data'])
    return payload


@login_required
def get_hst_status(request, transient_id):
    """Lightweight HST availability for tab label (no JPG fetch)."""
    try:
        t = Transient.objects.get(pk=transient_id)
    except Transient.DoesNotExist:
        raise Http404("Transient id does not exist")

    def _lookup():
        from . import common
        hst = common.mast_query.hstImages(t.ra, t.dec, 'Object')
        hst.getObstable()
        count = int(getattr(hst, 'Nimages', 0) or 0)
        if not count and getattr(hst, 'obstable', None) is not None:
            count = len(hst.obstable)
        return count

    return JsonResponse(_archive_status_payload(f'hst_status_v3_{transient_id}', _lookup, 'HST',
                                                transient_id=t.pk, flag_field='has_hst'))


@login_required
def get_chandra_status(request, transient_id):
    """Lightweight Chandra availability for tab label (no image fetch)."""
    try:
        t = Transient.objects.get(pk=transient_id)
    except Transient.DoesNotExist:
        raise Http404("Transient id does not exist")

    def _lookup():
        from . import common
        chr = common.chandra_query.chandraImages(t.ra, t.dec, 'Object')
        chr.search_chandra_database()
        return int(getattr(chr, 'n_obsid', 0) or 0)

    return JsonResponse(
        _archive_status_payload(f'chandra_status_v3_{transient_id}', _lookup, 'Chandra',
                                transient_id=t.pk, flag_field='has_chandra')
    )


@login_required
def get_jwst_status(request, transient_id):
    """Lightweight JWST availability for the tab label.

    The same images-only selection as the tab body (``jwstObservations``,
    #387); the answer also sets ``Transient.has_jwst``. Cache key ``v2``: the
    ``v1`` answers were counted before NIRSpec / MIRI spectroscopy was excluded.
    """
    try:
        t = Transient.objects.get(pk=transient_id)
    except Transient.DoesNotExist:
        raise Http404("Transient id does not exist")

    def _lookup():
        from . import common
        jwst = common.mast_query.jwstObservations(t.ra, t.dec)
        jwst.query()
        return int(jwst.count)

    return JsonResponse(_archive_status_payload(f'jwst_status_v2_{transient_id}', _lookup, 'JWST',
                                                transient_id=t.pk, flag_field='has_jwst'))


# The tab body (full observation list) gets longer than the label lookup: the
# user asked for it by opening the tab, and the page offers Retry on failure.
ARCHIVE_TABLE_TIMEOUT_SECONDS = int(os.environ.get('YSE_ARCHIVE_TABLE_TIMEOUT', '45'))


def _archive_table_response(transient_id, archive_name, lookup, empty_payload, cache_key=None):
    """JSON for an archive tab body (HST images, JWST observations).

    ``lookup`` runs in a worker with a wall-clock timeout and returns the
    payload dict. On a MAST failure the answer is HTTP 502 (timeout: 504)
    carrying ``error`` and ``message`` plus the empty payload shape, so the
    page can say "lookup failed" with a Retry link instead of swallowing a
    500. Successful answers are cached for an hour under ``cache_key``.
    """
    if cache_key:
        cached = cache.get(cache_key)
        if cached is not None:
            return JsonResponse(cached)
    started = datetime.datetime.now()
    try:
        payload = _archive_status_with_timeout(lookup, timeout_seconds=ARCHIVE_TABLE_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning('%s lookup failed for transient %s: %s', archive_name, transient_id, exc)
        body = dict(empty_payload)
        body.update({
            'error': 'lookup_failed',
            'message': f'{archive_name} archive (MAST) lookup failed; retry in a moment.',
        })
        return JsonResponse(body, status=502)
    if payload is None:
        logger.warning('%s lookup timed out for transient %s after %s s',
                       archive_name, transient_id, ARCHIVE_TABLE_TIMEOUT_SECONDS)
        body = dict(empty_payload)
        body.update({
            'error': 'timeout', 'timed_out': True,
            'message': (f'{archive_name} archive (MAST) did not answer within '
                        f'{ARCHIVE_TABLE_TIMEOUT_SECONDS} s; retry in a moment.'),
        })
        return JsonResponse(body, status=504)
    logger.debug('%s lookup for transient %s took %.1f s', archive_name, transient_id,
                 (datetime.datetime.now() - started).total_seconds())
    if cache_key:
        cache.set(cache_key, payload, timeout=ARCHIVE_STATUS_CACHE_SECONDS)
    return JsonResponse(payload)


HST_IMAGE_EMPTY = {"jpegurl": [], "fitsurl": [], "obsdate": [], "filters": [], "inst": []}


@login_required
def get_hst_image(request,transient_id):
    try:
        t = Transient.objects.get(pk=transient_id)
    except Transient.DoesNotExist:
        raise Http404("Transient id does not exist")

    def _lookup():
        from . import common
        hst = common.mast_query.hstImages(t.ra, t.dec, 'Object')
        hst.getObstable()
        hst.getJPGurl()
        if not len(hst.jpglist):
            return dict(HST_IMAGE_EMPTY)
        fitsurllist = [
            "https://hla.stsci.edu/cgi-bin/getdata.cgi?config=ops&amp;dataset=%s" % str(obs_id).lower()
            for obs_id in hst.obstable["obs_id"][:len(hst.jpglist)]
        ]
        return {"jpegurl": list(hst.jpglist),
                "fitsurl": fitsurllist,
                "obsdate": list(Time(hst.obstable["t_min"], format='mjd').iso),
                "filters": list(hst.obstable["filters"]),
                "inst": list(hst.obstable["instrument_name"])}

    return _archive_table_response(transient_id, 'HST', _lookup, HST_IMAGE_EMPTY)


JWST_OBSERVATION_FIELDS = ('obs_id', 'inst', 'filters', 'obsdate', 'mjd', 'exptime', 'program',
                           'pi', 'target', 'product', 'calib_level', 'previewurl', 'dataurl',
                           'portalurl')


@login_required
def get_jwst_observations(request, transient_id):
    """JWST observations at the transient position for the JWST tab body.

    Payload: ``{"count": N, "rows": [{obs_id, inst, filters, obsdate, mjd,
    exptime, program, pi, target, product, calib_level, previewurl, dataurl,
    portalurl}]}``.
    """
    try:
        t = Transient.objects.get(pk=transient_id)
    except Transient.DoesNotExist:
        raise Http404("Transient id does not exist")

    def _lookup():
        from . import common
        jwst = common.mast_query.jwstObservations(t.ra, t.dec)
        rows = [
            {
                'obs_id': r.get('obs_id'), 'inst': r.get('instrument_name'),
                'filters': r.get('filters'), 'obsdate': r.get('obsdate'), 'mjd': r.get('t_min'),
                'exptime': r.get('t_exptime'), 'program': r.get('proposal_id'),
                'pi': r.get('proposal_pi'), 'target': r.get('target_name'),
                'product': r.get('dataproduct_type'), 'calib_level': r.get('calib_level'),
                'previewurl': r.get('previewurl'), 'dataurl': r.get('dataurl'),
                'portalurl': r.get('portalurl'),
            }
            for r in jwst.query()
        ]
        return {'count': len(rows), 'rows': rows}

    return _archive_table_response(
        transient_id, 'JWST', _lookup, {'count': 0, 'rows': []},
        cache_key=f'jwst_observations_v2_{transient_id}',
    )

@login_required
def get_chandra_image(request,transient_id):
    
    try:
        t = Transient.objects.get(pk=transient_id)
    except t.DoesNotExist:
        raise Http404("Transient id does not exist")

    startTime = datetime.datetime.now()
    from . import common
    chr=common.chandra_query.chandraImages(t.ra,t.dec,'Object')
    chr.search_chandra_database()
    logger.info("Found %s Chandra images", chr.n_obsid)

    #fitsurllist = []
    #for jpg,i in zip(chr.jpglist,range(len(chr.jpglist))):
    #   print(jpg)
    #   fitsurllist += ["https://hla.stsci.edu/cgi-bin/getdata.cgi?config=ops&amp;dataset=%s"%str(chr.obstable["obs_id"][i]).lower()]
    logger.info("Chandra query run time was %s seconds", (datetime.datetime.now() - startTime).total_seconds())

    if len(chr.jpegs):
        jpegurldict = {"jpegurl":list(chr.jpegs.data.astype(str)),
                       "fitsurl":list(chr.images.data.astype(str)),
                       "obsdate":list(chr.obs_dates.data.astype(str)),
                       "exptime":list(chr.exp_times.data.astype(str)),
                       "totalexp":chr.total_exp}
    else:
        jpegurldict = {"jpegurl":[],
                       "fitsurl":[],
                       "obsdate":[],
                       "exptime":[],
                       "totalexp":0}

    return(JsonResponse(jpegurldict))


@login_required
def get_legacy_image(request,transient_id):
    
    try:
        t = Transient.objects.get(pk=transient_id)
    except t.DoesNotExist:
        raise Http404("Transient id does not exist")

    jpegurl = "https://www.legacysurvey.org/viewer/jpeg-cutout?ra=%.7f&dec=%.7f&pixscale=0.27&bands=grz"%(
        t.ra,t.dec)

    #fitsurl = "https://www.legacysurvey.org/viewer/fits-cutout?ra=%.7f&dec=%.7f&layer=dr8&pixscale=0.27&bands=grz"%(
    #   t.ra,t.dec)
    fitsurl = "https://www.legacysurvey.org/viewer?ra=%.7f&dec=%.7f"%(t.ra,t.dec)
    
    logger.debug("%s %s", jpegurl, fitsurl)
    jpegurldict = {"jpegurl":jpegurl,
                   "fitsurl":fitsurl}

    return(JsonResponse(jpegurldict))

@login_required
def delta_too_hours(request,too_id):
    too = ToOResource.objects.get(id=too_id)
    diff = (too.awarded_too_hours - too.used_too_hours)

    diffdict = {'delta_too_hours': diff}
    return JsonResponse(diffdict)

def get_authorized_classical_resources(user):
    allowed_resources = ObservingResourceService.GetAuthorizedClassicalResource_ByUser(user)

    return allowed_resources

def get_authorized_too_resources(user):
    allowed_resources = ObservingResourceService.GetAuthorizedToOResource_ByUser(user)

    return allowed_resources

def get_authorized_queued_resources(user):
    allowed_resources = ObservingResourceService.GetAuthorizedQueuedResource_ByUser(user)

    return allowed_resources

def get_authorized_phot_resources(user):
    allowed_resources = PhotometryService.GetAuthorizedTransientPhotometry_ByUser(user)

    return allowed_resources

