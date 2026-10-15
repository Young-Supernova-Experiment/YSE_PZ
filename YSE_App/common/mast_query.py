import json
import re
import sys, numpy as np
from urllib.parse import quote
from astroquery.mast import Observations
from astropy.coordinates import SkyCoord
from astropy.table import Table,unique
from astropy import units as u
from datetime import datetime
from astropy.time import Time

# Uses references to astroquery.mast fields: https://mast.stsci.edu/api/v0/_c_a_o_mfields.html
instrument_defaults = {
        'radius': 1 * u.arcsec,
        'jpg': 'https://hla.stsci.edu/cgi-bin/fitscut.cgi?red={id}'+\
            '&amp;RA={ra}&amp;DEC={dec}&amp;size=256&amp;format=jpg'+\
            '&amp;config=ops&amp;asinh=1&amp;autoscale=90',
        'mask': {'instrument_name': ['WFPC2/WFC','PC/WFC','ACS/WFC','ACS/HRC',
                                     'ACS/SBC','WFC3/UVIS','WFC3/IR'],
                 't_exptime': 40,
                 'obs_collection': ['HST','HLA'],
                 'filters': ['F220W','F250W','F330W','F344N','F435W','F475W',
                      'F550M','F555W','F606W','F625W','F658N','F660N','F660N',
                      'F775W','F814W','F850LP','F892N','F098M','F105W','F110W',
                      'F125W','F126N','F127M','F128N','F130N','F132N','F139M',
                      'F140W','F153M','F160W','F164N','F167N','F200LP','F218W',
                      'F225W','F275W','F280N','F300X','F336W','F343N','F350LP',
                      'F373N','F390M','F390W','F395N','F410M','F438W','F467M',
                      'F469N','F475X','F487N','F502N','F547M','F600LP','F621M',
                      'F625W','F631N','F645N','F656N','F657N','F658N','F665N',
                      'F673N','F680N','F689M','F763M','F845M','F953N','F122M',
                      'F160BW','F185W','F218W','F255W','F300W','F375N','F380W',
                      'F390N','F437N','F439W','F450W','F569W','F588N','F622W',
                      'F631N','F673N','F675W','F702W','F785LP','F791W','F953N',
                      'F1042M','F502N']
                }
}

class hstImages():
    def __init__(self,ra,dec,obj):
        # Transient information/search criteria
        if (':' in str(ra) and ':' in str(dec)):
            self.coord = SkyCoord(ra, dec, unit = (u.hour, u.deg))
        else:
            self.coord = SkyCoord(ra, dec, unit = (u.deg, u.deg))
        self.ra=self.coord.ra.degree
        self.dec=self.coord.dec.degree
        self.obstable = None
        self.object=obj
        self.radius=0.001

        ## Selection criteria
        self.options = instrument_defaults

        self.Nimages = 0
        self.jpglist = []

    def getObstable(self):
        options = self.options
        table=Observations.query_region(self.coord,
            radius=self.options['radius'])

        # HST-specific masks
        filmask = [table['filters'] == good
              for good in options['mask']['filters']]
        filmask = [any(l) for l in list(map(list,zip(*filmask)))]
        expmask = table['t_exptime'] > options['mask']['t_exptime']
        obsmask = [table['obs_collection'] == good
            for good in options['mask']['obs_collection']]
        obsmask = [any(l) for l in list(map(list,zip(*obsmask)))]
        detmask = [table['instrument_name'] == good
            for good in options['mask']['instrument_name']]
        detmask = [any(l) for l in list(map(list,zip(*detmask)))]

        # Construct and apply mask
        mask = [all(l) for l in zip(filmask,expmask,obsmask,detmask)]
        self.obstable = table[mask]

        self.Nimages=0
        if self.obstable:
          if len(self.obstable)>0:

            self.obstable['t_min']=np.around(self.obstable['t_min'], decimals=4)
            self.obstable['t_max']=np.around(self.obstable['t_max'], decimals=4)

            self.obstable.sort('obs_collection')

            self.obstable = unique(self.obstable, keys=['t_min'], keep='last')
            self.obstable = unique(self.obstable, keys=['t_max'], keep='last')

            self.Nimages=len(self.obstable)

    def getJPGurl(self):
        if len(self.obstable) == 0:
            print('There are no HST images!!!')
            return(0)

        url = self.options['jpg']
        for obsid in self.obstable['obs_id']:
            url = self.options['jpg'].format(id=obsid,ra=self.coord.ra.degree,
                dec=self.coord.dec.degree)
            self.jpglist.append(url)


MAST_DOWNLOAD_URL = 'https://mast.stsci.edu/api/v0.1/Download/file?uri={uri}'
MAST_PORTAL_URL = 'https://mast.stsci.edu/portal/Mashup/Clients/Mast/Portal.html?searchQuery={query}'


def _plain(value):
    """Return a JSON-friendly Python value for an astropy table cell.

    Masked cells and the strings MAST uses for "nothing" become ``None``;
    numpy scalars become their Python equivalents.
    """
    if value is None or value is np.ma.masked:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, str):
        value = value.strip()
        if value in ('', '--', 'nan', 'None'):
            return None
    return value


def mast_file_url(uri):
    """Turn a ``mast:...`` product URI into a browser-openable download link.

    MAST lists JWST previews and products as ``mast:JWST/product/<file>``
    URIs; the ``Download/file`` endpoint serves them (public data without
    login). Plain http(s) links are returned unchanged.
    """
    uri = _plain(uri)
    if not uri:
        return None
    if str(uri).lower().startswith(('http://', 'https://')):
        return str(uri)
    return MAST_DOWNLOAD_URL.format(uri=quote(str(uri), safe=''))


def mast_portal_url(obs_id):
    """MAST Portal deep link showing one observation by its ``obs_id``."""
    query = json.dumps({
        'service': 'CAOMFILTERED',
        'inputText': [{'paramName': 'obs_id', 'niceName': 'obs_id', 'values': [str(obs_id)]}],
        'paramsService': 'Mast.Caom.Filtered',
        'title': f'MAST observation {obs_id}',
        'columns': '*',
    }, separators=(',', ':'))
    return MAST_PORTAL_URL.format(query=quote(query, safe=''))


class MastObservations():
    """Archive-agnostic MAST observation search at a position.

    ``hstImages`` above is the original HST-only lookup. This class covers
    the collections MAST serves through the same CAOM interface (JWST first,
    #329): one ``query_criteria`` cone search at the transient position (the
    same 1 arcsec radius as the HST lookup) restricted to ``collections``,
    science data products of the given ``product_types`` (images only by
    default: the archive tabs show images, not spectra, #356), returned as
    plain dicts the views can serialise without knowing about astropy tables.
    Rows whose ``dataproduct_type`` is not in ``product_types`` are dropped
    even when MAST returns them, and subclasses narrow the selection further
    through ``accepts`` (``JwstImages`` keeps imaging modes only, #387).

    Nothing here catches exceptions: callers wrap the lookup in the
    archive-status timeout/error handling in ``view_utils``.
    """

    #: Observation-table columns copied into each row (missing ones are None).
    columns = ('obs_id', 'obsid', 'obs_collection', 'instrument_name', 'filters',
               't_min', 't_max', 't_exptime', 'proposal_id', 'proposal_pi',
               'target_name', 'dataproduct_type', 'calib_level', 'jpegURL',
               'dataURL', 'dataRights')

    #: MAST ``instrument_name`` values the query is restricted to (None: any).
    instrument_names = None

    def __init__(self, ra, dec, collections, radius=None, product_types=('image',),
                 intent_type='science'):
        if (':' in str(ra) and ':' in str(dec)):
            self.coord = SkyCoord(ra, dec, unit=(u.hour, u.deg))
        else:
            self.coord = SkyCoord(ra, dec, unit=(u.deg, u.deg))
        self.ra = self.coord.ra.degree
        self.dec = self.coord.dec.degree
        self.collections = list(collections)
        self.radius = radius if radius is not None else instrument_defaults['radius']
        self.product_types = list(product_types) if product_types else None
        self.intent_type = intent_type
        self.obstable = None
        self.rows = []

    @property
    def count(self):
        return len(self.rows)

    def query(self):
        """Run the MAST search and fill ``obstable`` / ``rows``; returns ``rows``."""
        criteria = {'coordinates': self.coord, 'radius': self.radius,
                    'obs_collection': self.collections}
        if self.product_types:
            criteria['dataproduct_type'] = self.product_types
        if self.intent_type:
            criteria['intentType'] = self.intent_type
        if self.instrument_names:
            criteria['instrument_name'] = sorted(self.instrument_names)
        return self.set_table(Observations.query_criteria(**criteria))

    def set_table(self, table):
        """Take a MAST answer: keep the rows of the wanted product types that
        ``accepts`` lets through (the MAST criteria are not trusted on their
        own: the same rules are applied to whatever comes back)."""
        self.obstable = table
        rows = self.rows_from_table(table)
        if self.product_types:
            wanted = {p.lower() for p in self.product_types}
            rows = [r for r in rows if (r.get('dataproduct_type') or '').lower() in wanted]
        self.rows = [r for r in rows if self.accepts(r)]
        return self.rows

    @classmethod
    def accepts(cls, row):
        """Extra per-row selection; the base class keeps every row."""
        return True

    @classmethod
    def rows_from_table(cls, table):
        """Plain dict rows (sorted by start time) from a MAST observation table."""
        if table is None or len(table) == 0:
            return []
        names = set(table.colnames)
        rows = []
        for record in table:
            row = {name: _plain(record[name]) if name in names else None for name in cls.columns}
            t_min = row.get('t_min')
            row['obsdate'] = Time(t_min, format='mjd').iso[:19] if t_min is not None else None
            row['previewurl'] = mast_file_url(row.get('jpegURL'))
            row['dataurl'] = mast_file_url(row.get('dataURL'))
            row['portalurl'] = mast_portal_url(row['obs_id']) if row.get('obs_id') else None
            rows.append(row)
        rows.sort(key=lambda r: (r.get('t_min') is None, r.get('t_min') or 0.0, r.get('obs_id') or ''))
        return rows


# JWST imaging modes as MAST names them in ``instrument_name`` (#387). This is
# an allowlist: anything not listed is out, which covers every NIRSpec mode
# (MSA, SLIT, IFU and its IMAGE/target-acquisition frames), MIRI MRS (IFU) and
# LRS (SLIT, SLITLESS), NIRCam WFSS (GRISM), NIRISS WFSS and SOSS, the TARGACQ
# frames of every instrument, and any mode MAST adds later. MIRI and NIRCam
# have both imaging and spectroscopic modes, so the instrument alone is not
# enough: the mode after the slash is what counts.
JWST_IMAGING_MODES = frozenset({
    'NIRCAM/IMAGE', 'NIRCAM/CORON',
    'MIRI/IMAGE', 'MIRI/CORON',
    'NIRISS/IMAGE', 'NIRISS/AMI',
})

# Filter / grating names that mark a dispersive element. MAST's
# ``dataproduct_type`` is not reliable for JWST (spectra can be labelled
# ``image``), so a row whose ``filters`` names one of these is dropped even
# when its instrument mode is allowed: the MIRI LRS prism (P750L) and MRS
# grating settings (SHORT / MEDIUM / LONG), NIRCam grisms (GRISMR / GRISMC),
# NIRISS grisms (GR150R / GR150C, GR700XD), and the NIRSpec gratings and prism
# (G140M ... G395H, PRISM).
JWST_SPECTRAL_FILTER_TOKENS = frozenset({'P750L', 'SHORT', 'MEDIUM', 'LONG', 'PRISM'})
JWST_SPECTRAL_FILTER_PREFIXES = ('GRISM', 'GR150', 'GR700')
_JWST_NIRSPEC_GRATING = re.compile(r'^G\d{3}[MH]$')


def jwst_filter_tokens(filters):
    """The individual filter / grating / mask names in a MAST ``filters`` cell
    (``'CLEAR;F150W'`` -> ``['CLEAR', 'F150W']``), upper-cased."""
    if filters is None:
        return []
    return [tok for tok in re.split(r'[;,/\s|+&]+', str(filters).upper()) if tok]


def jwst_filters_are_spectroscopic(filters):
    """True when the ``filters`` cell names a dispersive element (see above)."""
    for tok in jwst_filter_tokens(filters):
        if tok in JWST_SPECTRAL_FILTER_TOKENS or tok.startswith(JWST_SPECTRAL_FILTER_PREFIXES):
            return True
        if _JWST_NIRSPEC_GRATING.match(tok):
            return True
    return False


def is_jwst_image(row):
    """Images-only rule for the JWST tab, its label and ``has_jwst`` (#356, #387).

    ``row`` is a ``MastObservations`` row dict (or any mapping with
    ``instrument_name``, ``filters`` and ``dataproduct_type``). A row is an
    image only when all three hold: ``dataproduct_type`` is ``image``,
    ``instrument_name`` is one of ``JWST_IMAGING_MODES`` (so every NIRSpec mode
    and every MIRI / NIRCam / NIRISS spectroscopic mode is out, whatever MAST
    calls the product) and ``filters`` names no dispersive element.
    """
    if (row.get('dataproduct_type') or '').strip().lower() != 'image':
        return False
    inst = (row.get('instrument_name') or '').strip().upper()
    if inst not in JWST_IMAGING_MODES:
        return False
    return not jwst_filters_are_spectroscopic(row.get('filters'))


class JwstImages(MastObservations):
    """JWST science images at a position: ``MastObservations`` restricted to
    the JWST collection and, both in the MAST query and on the rows that come
    back, to the imaging modes in ``JWST_IMAGING_MODES`` (``is_jwst_image``).
    Every consumer of JWST data (tab body, tab label, ``has_jwst``) goes
    through this class, so they cannot disagree about what counts as an image.
    """

    instrument_names = JWST_IMAGING_MODES

    def __init__(self, ra, dec, radius=None):
        super().__init__(ra, dec, collections=['JWST'], radius=radius, product_types=('image',))

    @classmethod
    def accepts(cls, row):
        return is_jwst_image(row)


def jwstObservations(ra, dec, radius=None):
    """JWST science images covering the position (no spectra: #356, #387)."""
    return JwstImages(ra, dec, radius=radius)


## TEST TEST TEST
if __name__=='__main__':
    startTime = datetime.now()
    hst=hstImages(199.8674542,-13.7236833,'Object')
    hst.getObstable()
    hst.getJPGurl()
    print("I found",hst.Nimages,"HST images of",hst.object,"located at coordinates",hst.ra,hst.dec)
