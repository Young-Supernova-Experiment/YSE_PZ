"""
Open Supernova Catalog (OSC) entries -> an import plan, without Django or a database.

An OSC entry is one JSON file, ``{"SN2011fe": {...}}``, in the schema described at
https://github.com/astrocatalogs/schema (and the key lists of the ``astrocats`` package).
:func:`parse_entry` turns it into a plain dict (the "plan") that ``osc_writer`` applies to the
database and that ``describe_plan`` summarises for a dry run. Everything in the entry ends up
somewhere in the plan: keys with no column of their own go into ``extra`` dicts.

Conventions:

* Numbers are kept as the strings the OSC gives (``value``) and also parsed (``value_float``).
* Times are MJD. ``u_time`` other than MJD (JD) is converted; a ``[start, stop]`` time becomes
  its midpoint, with both ends kept.
* Model data are not imported yet (Ryan, 2026-10-28): the ``models`` list and every photometry row,
  spectrum and quantity that cites a model are skipped and only counted (``plan['model_rows']``).

Run a dry run on local files without Django::

  python -m YSE_App.catalog_import.osc SN2011fe.json SN1987A.json
"""

from __future__ import annotations

import datetime
import gzip
import json
import math
import re
from collections import Counter, OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

CATALOG = 'OSC'
OBS_GROUP_NAME = 'OSC'
MJD_EPOCH = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)
# MySQL DATETIME starts at year 1000: earlier dates (SN 185, SN 386) stay in the quantities only.
MIN_DATETIME = datetime.datetime(1000, 1, 1, tzinfo=datetime.timezone.utc)
MIN_MJD = (MIN_DATETIME - MJD_EPOCH).total_seconds() / 86400.0

# Keys of an entry that are not lists of sourced quantities.
STRUCTURE_KEYS = ('schema', 'name', 'sources', 'photometry', 'spectra', 'models')

# Fields of a quantity (``astrocats.catalog.quantity.QUANTITY``) with their own column.
QUANTITY_FLOAT_FIELDS = ('e_value', 'e_lower_value', 'e_upper_value', 'probability')
QUANTITY_TEXT_FIELDS = ('u_value', 'u_e_value', 'description')
QUANTITY_BOOL_FIELDS = ('derived', 'upperlimit', 'lowerlimit')
QUANTITY_TEXT_LENGTHS = {'u_value': 64, 'u_e_value': 64}

SOURCE_FIELDS = ('name', 'bibcode', 'doi', 'arxivid', 'url', 'reference', 'acknowledgment', 'secondary', 'private',
                 'refereed')

# Journals (ADS bibstems) whose papers are refereed. Anything else (ATel, CBET, IAUC, TNS reports,
# arXiv, conference abstracts, catalogues, theses, ...) counts as not refereed.
REFEREED_BIBSTEMS = frozenset('''
    ApJ ApJL ApJS AJ MNRAS A&A A&AS PASP PASJ PASA Natur NatAs Sci AN AcA ARA&A Ap&SS AstL ARep AZh SvA SvAL
    RMxAA NewA JCAP PhRvD PhRvL ChJAA RAA JApA BaltA MmSAI ApL Obs JRASC AJS PZ IBVS JAVSO NatPh PNAS
'''.split())

# Above this distance a Prost/Blast host supersedes the catalogue's (#893).
HOST_DISTANCE_LIMIT_MPC = 20.0
SPEED_OF_LIGHT_KMS = 299792.458
HUBBLE_CONSTANT = 70.0

# Photometry fields that go to TransientPhotDataCatalogInfo columns of the same name.
PHOT_INFO_FLOAT_FIELDS = (
    'e_time', 'e_lower_time', 'e_upper_time',
    'e_lower_magnitude', 'e_upper_magnitude', 'zeropoint', 'upperlimitsigma',
    'countrate', 'e_countrate', 'e_lower_countrate', 'e_upper_countrate',
    'flux', 'e_flux', 'e_lower_flux', 'e_upper_flux',
    'unabsorbedflux', 'e_unabsorbedflux', 'e_lower_unabsorbedflux', 'e_upper_unabsorbedflux',
    'fluxdensity', 'e_fluxdensity', 'e_lower_fluxdensity', 'e_upper_fluxdensity',
    'luminosity', 'e_luminosity', 'e_lower_luminosity', 'e_upper_luminosity',
    'frequency', 'wavelength', 'photonindex', 'nhmw',
    'exposuretime', 'offaxisangle', 'extractionradius', 'airmass',
)
PHOT_INFO_TEXT_FIELDS = (
    'band', 'bandset', 'system', 'telescope', 'instrument', 'observatory', 'survey', 'mode',
    'observer', 'reducer', 'description',
    'u_countrate', 'u_flux', 'u_fluxdensity', 'u_luminosity', 'u_energy', 'u_frequency', 'u_wavelength',
    'u_exposuretime', 'u_offaxisangle', 'u_extractionradius',
)
# Column lengths of the text fields (TextField columns are not listed).
PHOT_INFO_TEXT_LENGTHS = dict(
    [(f, 64) for f in ('band', 'bandset', 'system')]
    + [(f, 128) for f in ('telescope', 'instrument', 'observatory', 'survey', 'mode')]
    + [(f, 32) for f in PHOT_INFO_TEXT_FIELDS if f.startswith('u_')])
PHOT_INFO_BOOL_FIELDS = (
    'upperlimit', 'lowerlimit', 'kcorrected', 'scorrected', 'mcorrected', 'synthetic', 'simulated',
    'restframe', 'host', 'includeshost', 'hostnhcorr',
)
# Handled separately: core columns, time, energy, sources and model links.
PHOT_SPECIAL_FIELDS = ('time', 'u_time', 'magnitude', 'e_magnitude', 'energy', 'source', 'model', 'realization')
PHOT_INFO_FIELDS = (PHOT_INFO_FLOAT_FIELDS + PHOT_INFO_TEXT_FIELDS + PHOT_INFO_BOOL_FIELDS
                    + ('time_start', 'time_end', 'u_time', 'e_magnitude', 'energy_low', 'energy_high', 'extra'))

SPEC_INFO_FLOAT_FIELDS = ('e_time', 'e_lower_time', 'e_upper_time', 'airmass', 'snr')
SPEC_INFO_TEXT_FIELDS = (
    'u_wavelengths', 'u_fluxes', 'u_errors', 'telescope', 'instrument', 'observatory', 'survey',
    'observer', 'reducer', 'reduction', 'filename',
)
SPEC_INFO_TEXT_LENGTHS = dict(
    [(f, 64) for f in ('u_wavelengths', 'u_fluxes', 'u_errors')]
    + [(f, 128) for f in ('telescope', 'instrument', 'observatory', 'survey', 'reduction')]
    + [('filename', 512)])
SPEC_INFO_BOOL_FIELDS = ('deredshifted', 'dereddened', 'vacuumwavelengths', 'host', 'includeshost')
SPEC_SPECIAL_FIELDS = ('time', 'u_time', 'data', 'wavelengths', 'fluxes', 'errors', 'redshift', 'source',
                       'exclude')
SPEC_INFO_FIELDS = (SPEC_INFO_FLOAT_FIELDS + SPEC_INFO_TEXT_FIELDS + SPEC_INFO_BOOL_FIELDS
                    + ('time_missing', 'time_start', 'time_end', 'u_time', 'exclude', 'extra'))

# Wavelength units -> factor to Angstrom.
WAVELENGTH_TO_ANGSTROM = {
    'angstrom': 1.0, 'angstroms': 1.0, 'a': 1.0, 'å': 1.0,
    'nm': 10.0, 'nanometer': 10.0, 'nanometers': 10.0,
    'micron': 1.0e4, 'microns': 1.0e4, 'um': 1.0e4, 'µm': 1.0e4, 'μm': 1.0e4,
}

# OSC claimed types -> YSE TransientClass names, where they differ by more than an "SN " prefix.
CLASS_NAME_MAP = {
    'Candidate': None,
    'Unknown': None,
    'Other': None,
    'Ia Pec': 'SN Ia-pec',
    'Ia-Pec': 'SN Ia-pec',
    'Ia-91T': 'SN Ia-91T-like',
    'Ia-91bg': 'SN Ia-91bg-like',
    'Ia-SC': 'SN Ia-SC',
    'Ia-CSM': 'SN Ia-CSM',
    'Iax[02cx-like]': 'SN Iax[02cx-like]',
    'Ic BL': 'SN Ic-BL',
    'Ic-BL': 'SN Ic-BL',
}
_SN_TYPE_RE = re.compile(r'^(I|II|III|IV)')
_IAU_NAME_RE = re.compile(r'^(?:SN|AT)\s*(\d{4}[A-Za-z]{1,3})$')
_HTML_TAG_RE = re.compile(r'<[^>]*>')
_DATE_RE = re.compile(r'^\s*(\d{3,4})(?:[/-](\d{1,2})(?:[/-](\d{1,2})(\.\d*)?)?)?\s*$')


# ---- small value helpers ---------------------------------------------------------------------

def to_float(value) -> Optional[float]:
    """A finite float from an OSC number (usually a string), else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, dict)):
        return None
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def to_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ('true', '1', 'yes', 't'):
        return True
    if text in ('false', '0', 'no', 'f', ''):
        return False
    return None


def to_text(value, max_length: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        text = json.dumps(value)
    else:
        text = str(value).strip()
    if text == '':
        return None
    if max_length and len(text) > max_length:
        text = text[:max_length]
    return text


def is_refereed(bibcode: Optional[str]) -> Optional[bool]:
    """From the journal of an ADS bibcode (``2011ApJ...737..103S`` -> ApJ); None without a bibcode."""
    if not bibcode or len(bibcode) < 9:
        return None
    return bibcode[4:9].strip('.') in REFEREED_BIBSTEMS


def clean_name(value) -> Optional[str]:
    """An object name without the HTML anchors some OSC aliases carry (``<a id="x">x</a>`` -> ``x``)."""
    text = to_text(value)
    if text is None:
        return None
    return to_text(_HTML_TAG_RE.sub('', text), 64)


def source_aliases(value) -> Tuple[str, ...]:
    """``"1,3"`` -> ``('1', '3')``."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value).split(',')
    return tuple(p.strip() for p in (str(x) for x in parts) if p.strip())


def sexagesimal_to_degrees(value, unit: Optional[str], is_ra: bool) -> Optional[float]:
    """OSC ``ra`` (hours unless ``u_value`` says degrees) or ``dec`` (degrees) -> decimal degrees."""
    text = to_text(value)
    if text is None:
        return None
    unit = (unit or ('hours' if is_ra else 'degrees')).lower()
    parts = re.split(r'[:\s]+', text.strip())
    sign = -1.0 if parts[0].startswith('-') else 1.0
    try:
        nums = [abs(float(p)) for p in parts if p not in ('', '+', '-')]
    except ValueError:
        return None
    if not nums:
        return None
    base = nums[0] + (nums[1] / 60.0 if len(nums) > 1 else 0.0) + (nums[2] / 3600.0 if len(nums) > 2 else 0.0)
    base *= sign
    if is_ra and len(nums) == 1 and unit.startswith('deg'):
        return base % 360.0
    if is_ra and unit.startswith('hour'):
        return (base * 15.0) % 360.0
    return base


def time_to_mjd(value, unit: Optional[str]) -> Optional[float]:
    t = to_float(value)
    if t is None:
        return None
    unit = (unit or 'MJD').strip().upper()
    if unit == 'JD':
        return t - 2400000.5
    return t


def mjd_to_datetime(mjd: float) -> datetime.datetime:
    return MJD_EPOCH + datetime.timedelta(days=mjd)


def parse_osc_date(value) -> Tuple[Optional[datetime.datetime], Optional[str]]:
    """``"2011/08/24.167"`` -> (datetime, 'day'); ``"1987/02"`` -> (1 Feb, 'month'); ``"1572"`` -> (1 Jan, 'year')."""
    text = to_text(value)
    if text is None:
        return None, None
    m = _DATE_RE.match(text)
    if not m:
        return None, None
    year, month, day, frac = m.groups()
    try:
        dt = datetime.datetime(int(year), int(month or 1), int(day or 1), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None, None
    if frac and frac != '.':
        dt += datetime.timedelta(days=float(frac))
    if dt < MIN_DATETIME:
        return None, None
    precision = 'day' if day else ('month' if month else 'year')
    return dt, precision


def yse_name(osc_name: str) -> str:
    """The YSE/TNS style name: ``SN2011fe`` -> ``2011fe``, ``SN1987A`` -> ``1987A``; others unchanged."""
    m = _IAU_NAME_RE.match(osc_name.strip())
    return m.group(1) if m else osc_name.strip()


def yse_class_name(claimed: str) -> Optional[str]:
    """OSC claimed type -> YSE TransientClass name (``Ia`` -> ``SN Ia``), or None for non-types."""
    claimed = (claimed or '').strip()
    if not claimed:
        return None
    if claimed in CLASS_NAME_MAP:
        return CLASS_NAME_MAP[claimed]
    if claimed.endswith('?'):
        return None
    if _SN_TYPE_RE.match(claimed):
        return 'SN ' + claimed
    return claimed


# ---- parsing --------------------------------------------------------------------------------

def load_entries(path: str) -> List[Tuple[str, dict]]:
    """``(name, entry)`` pairs from an OSC JSON file (``.json`` or ``.json.gz``)."""
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as handle:
        data = json.load(handle, object_pairs_hook=OrderedDict)
    return [(name, entry) for name, entry in data.items() if isinstance(entry, dict)]


def _parse_source(raw: dict) -> dict:
    out = {'alias': to_text(raw.get('alias'))}
    for field in SOURCE_FIELDS:
        if field in ('secondary', 'private'):
            out[field] = to_bool(raw.get(field))
        else:
            out[field] = to_text(raw.get(field))
    if not out['name']:
        out['name'] = out['reference'] or out['bibcode'] or out['url'] or 'unnamed source'
    out['name'] = out['name'][:512]
    out['reference'] = to_text(raw.get('reference'), 512)
    out['url'] = to_text(raw.get('url'), 1024)
    out['bibcode'] = to_text(raw.get('bibcode'), 32)
    out['refereed'] = is_refereed(out['bibcode'])
    out['doi'] = to_text(raw.get('doi'), 255)
    out['arxivid'] = to_text(raw.get('arxivid'), 64)
    extra = {k: v for k, v in raw.items() if k not in SOURCE_FIELDS and k != 'alias'}
    out['extra'] = extra or None
    return out


def _parse_quantity(key: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raw = {'value': raw}
    value = raw.get('value')
    out = {
        'key': key,
        'value': to_text(value),
        'value_float': to_float(value),
        'kind': to_text(raw.get('kind'), 128),
        'correlations': raw.get('correlations', raw.get('correlation')),
        'source_aliases': source_aliases(raw.get('source')),
    }
    for field in QUANTITY_FLOAT_FIELDS:
        out[field] = to_float(raw.get(field))
    for field in QUANTITY_TEXT_FIELDS:
        out[field] = to_text(raw.get(field), QUANTITY_TEXT_LENGTHS.get(field))
    for field in QUANTITY_BOOL_FIELDS:
        out[field] = to_bool(raw.get(field))
    known = set(QUANTITY_FLOAT_FIELDS + QUANTITY_TEXT_FIELDS + QUANTITY_BOOL_FIELDS) | {
        'value', 'kind', 'correlations', 'correlation', 'source'}
    extra = {k: v for k, v in raw.items() if k not in known}
    out['extra'] = extra or None
    return out


def _time_fields(raw: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """(mjd, start, end, non-MJD unit) of a photometry or spectrum row."""
    unit = to_text(raw.get('u_time'))
    t = raw.get('time')
    if isinstance(t, (list, tuple)):
        ends = [time_to_mjd(x, unit) for x in t]
        ends = [x for x in ends if x is not None]
        if not ends:
            return None, None, None, unit
        start, end = min(ends), max(ends)
        mjd = 0.5 * (start + end)
    else:
        mjd = time_to_mjd(t, unit)
        start = end = None
    keep_unit = unit[:16] if unit and unit.upper() != 'MJD' else None
    return mjd, start, end, keep_unit


def _band_label(raw: dict, info: dict) -> str:
    band = to_text(raw.get('band'), 64)
    if band:
        return band
    if info.get('energy_low') is not None:
        lo, hi = info['energy_low'], info['energy_high']
        rng = ('%g' % lo) if hi is None or hi == lo else ('%g-%g' % (lo, hi))
        return ('%s %s' % (rng, info.get('u_energy') or 'keV'))[:64]
    if info.get('frequency') is not None:
        return ('%g %s' % (info['frequency'], info.get('u_frequency') or 'GHz'))[:64]
    if info.get('wavelength') is not None:
        return ('%g %s' % (info['wavelength'], info.get('u_wavelength') or 'Angstrom'))[:64]
    return 'Unknown'


def point_kind(point: dict) -> str:
    """'magnitude', 'limit', 'xray', 'radio' or 'other', for reports."""
    info = point['info']
    if info.get('energy_low') is not None or info.get('flux') is not None:
        return 'xray'
    if info.get('frequency') is not None or (point['mag'] is None and info.get('fluxdensity') is not None):
        return 'radio'
    if point['mag'] is not None:
        return 'limit' if info.get('upperlimit') else 'magnitude'
    return 'other'


def _parse_phot_point(raw: dict, warnings: List[str]) -> Optional[dict]:
    mjd, start, end, unit = _time_fields(raw)
    if mjd is None or mjd < MIN_MJD:
        warnings.append('photometry row without a usable time skipped: %s' % _short(raw))
        return None
    info: Dict[str, Any] = {'time_start': start, 'time_end': end, 'u_time': unit}
    for field in PHOT_INFO_FLOAT_FIELDS:
        info[field] = to_float(raw.get(field))
    for field in PHOT_INFO_TEXT_FIELDS:
        info[field] = to_text(raw.get(field), PHOT_INFO_TEXT_LENGTHS.get(field))
    for field in PHOT_INFO_BOOL_FIELDS:
        info[field] = to_bool(raw.get(field))
    energy = raw.get('energy')
    if isinstance(energy, (list, tuple)):
        vals = [to_float(e) for e in energy]
        vals = [v for v in vals if v is not None]
        info['energy_low'] = min(vals) if vals else None
        info['energy_high'] = max(vals) if vals else None
    else:
        info['energy_low'] = info['energy_high'] = to_float(energy)

    mag = to_float(raw.get('magnitude'))
    e_mag = to_float(raw.get('e_magnitude'))
    if e_mag is None and mag is not None:
        lo, hi = info['e_lower_magnitude'], info['e_upper_magnitude']
        if lo is not None and hi is not None:
            e_mag = 0.5 * (lo + hi)
    limit = bool(info['upperlimit'] or info['lowerlimit'])
    point = {
        'mjd': mjd,
        'band': _band_label(raw, info),
        'mag': mag,
        # A limit's error stays in the info row: the light curve counts mag + mag_err as a detection.
        'mag_err': None if limit else e_mag,
        'mag_sys': info['system'],
        'flux': None,
        'flux_err': None,
        'flux_zero_point': None,
        'quality_flags': ['NOT_DIFFERENCED'] if info['includeshost'] else [],
        'info': info,
        'source_aliases': source_aliases(raw.get('source')),
    }
    info['e_magnitude'] = e_mag if limit else None
    # Optical flux densities and count rates with a zero point are YSE's flux / zero-point pair
    # (mag = -2.5 log10(flux) + zp); the light curve then draws their limits too.
    if mag is not None and info['zeropoint'] is not None:
        for value_key, err_key in (('fluxdensity', 'e_fluxdensity'), ('countrate', 'e_countrate')):
            if info[value_key] is not None:
                point['flux'] = info[value_key]
                point['flux_err'] = info[err_key]
                point['flux_zero_point'] = info['zeropoint']
                break
    known = set(PHOT_INFO_FLOAT_FIELDS + PHOT_INFO_TEXT_FIELDS + PHOT_INFO_BOOL_FIELDS + PHOT_SPECIAL_FIELDS)
    extra = {k: v for k, v in raw.items() if k not in known}
    info['extra'] = extra or None
    return point


def _spectrum_pixels(raw: dict, warnings: List[str], label: str) -> List[Tuple[float, float, Optional[float]]]:
    """``[(wavelength in Angstrom, flux, flux error or None), ...]``, non-finite pixels dropped."""
    rows = raw.get('data')
    if rows is None and raw.get('wavelengths') is not None:
        waves, fluxes = raw.get('wavelengths') or [], raw.get('fluxes') or []
        errors = raw.get('errors') or [None] * len(waves)
        rows = list(zip(waves, fluxes, errors))
    rows = rows or []
    unit = (to_text(raw.get('u_wavelengths')) or 'Angstrom').strip().lower()
    factor = WAVELENGTH_TO_ANGSTROM.get(unit)
    if factor is None:
        warnings.append('%s: wavelength unit %r kept as is' % (label, unit))
        factor = 1.0
    pixels, dropped = [], 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            dropped += 1
            continue
        w, f = to_float(row[0]), to_float(row[1])
        e = to_float(row[2]) if len(row) > 2 else None
        if w is None or f is None:
            dropped += 1
            continue
        pixels.append((w * factor, f, e))
    if dropped:
        warnings.append('%s: %d non-numeric pixels dropped' % (label, dropped))
    return pixels


def _parse_spectrum(raw: dict, index: int, fallback_mjd: Optional[float], warnings: List[str]) -> Optional[dict]:
    label = 'spectrum %d (%s)' % (index, to_text(raw.get('filename')) or 'no filename')
    mjd, start, end, unit = _time_fields(raw)
    info: Dict[str, Any] = {'time_start': start, 'time_end': end, 'u_time': unit, 'time_missing': None}
    if mjd is not None and mjd < MIN_MJD:
        mjd = None
    if mjd is None:
        if fallback_mjd is None:
            warnings.append('%s: no time and no discovery/peak date to stand in; skipped' % label)
            return None
        mjd = fallback_mjd
        info['time_missing'] = True
        warnings.append('%s: no time; discovery/peak date used' % label)
    for field in SPEC_INFO_FLOAT_FIELDS:
        info[field] = to_float(raw.get(field))
    for field in SPEC_INFO_TEXT_FIELDS:
        info[field] = to_text(raw.get(field), SPEC_INFO_TEXT_LENGTHS.get(field))
    for field in SPEC_INFO_BOOL_FIELDS:
        info[field] = to_bool(raw.get(field))
    info['exclude'] = raw.get('exclude')
    pixels = _spectrum_pixels(raw, warnings, label)
    if not pixels:
        warnings.append('%s: no usable pixels; skipped' % label)
        return None
    flags = []
    if info['deredshifted']:
        flags.append('DEREDSHIFTED')
    if info['dereddened']:
        flags.append('DEREDDENED')
    if (info['u_fluxes'] or '').lower().startswith('uncalibrated'):
        flags.append('NOT_FLUX_CALIBRATED')
    if info['includeshost']:
        flags.append('HOST_CONTAMINATED')
    known = set(SPEC_INFO_FLOAT_FIELDS + SPEC_INFO_TEXT_FIELDS + SPEC_INFO_BOOL_FIELDS + SPEC_SPECIAL_FIELDS)
    extra = {k: v for k, v in raw.items() if k not in known}
    info['extra'] = extra or None
    return {
        'mjd': mjd,
        'redshift': to_float(raw.get('redshift')),
        'wavelength_medium': 'vacuum' if info['vacuumwavelengths'] else 'unknown',
        'host_subtraction': 'none' if info['includeshost'] else None,
        'quality_flags': flags,
        'telescope': info['telescope'],
        'instrument': info['instrument'],
        'source_aliases': source_aliases(raw.get('source')),
        'info': info,
        'pixels': pixels,
    }


def _best(quantities: List[dict], prefer_kinds: Iterable[Optional[str]] = (), skip_kinds: Iterable[str] = ()) -> Optional[dict]:
    """The quantity to use for a single-valued YSE field.

    Preferred kinds first (in order), then measured over derived values, then the most-cited.
    """
    skip = set(skip_kinds)
    usable = [q for q in quantities if q['value'] is not None and (q['kind'] or '') not in skip]
    if not usable:
        return None
    order = list(prefer_kinds)

    def rank(q):
        kind = q['kind']
        kind_rank = order.index(kind) if kind in order else len(order)
        return (kind_rank, bool(q['derived']), -len(q['source_aliases']))

    return sorted(usable, key=rank)[0]


def _transient_fields(name: str, quantities: Dict[str, List[dict]], warnings: List[str]) -> dict:
    """The single-valued ``Transient`` fields the catalogue can fill."""
    out: Dict[str, Any] = {'osc_name': name, 'name': yse_name(clean_name(name) or name)}
    ra_q, dec_q = _best(quantities.get('ra', [])), _best(quantities.get('dec', []))
    out['ra'] = sexagesimal_to_degrees(ra_q['value'], ra_q['u_value'], True) if ra_q else None
    out['dec'] = sexagesimal_to_degrees(dec_q['value'], dec_q['u_value'], False) if dec_q else None
    if out['ra'] is None or out['dec'] is None:
        warnings.append('no usable position')

    aliases = []
    for q in quantities.get('alias', []):
        alias = clean_name(q['value'])
        if alias and alias not in aliases:
            aliases.append(alias)
    out['aliases'] = aliases

    disc = _best(quantities.get('discoverdate', []))
    out['disc_date'], out['disc_date_precision'] = parse_osc_date(disc['value']) if disc else (None, None)

    z = _best(quantities.get('redshift', []), prefer_kinds=('heliocentric', 'spectroscopic', None, 'host'),
              skip_kinds=('cmb', 'photometric'))
    out['redshift'] = z['value_float'] if z else None
    out['redshift_err'] = z['e_value'] if z else None
    out['redshift_kind'] = z['kind'] if z else None

    # Distance for the host rule: the luminosity distance (Mpc), else cz/H0.
    lumdist = _best(quantities.get('lumdist', []), prefer_kinds=(None, 'host'))
    if lumdist and lumdist['value_float'] is not None and (lumdist['u_value'] or 'Mpc').lower() == 'mpc':
        out['distance_mpc'], out['distance_basis'] = lumdist['value_float'], 'lumdist'
    elif out['redshift'] is not None and out['redshift'] > 0:
        out['distance_mpc'] = out['redshift'] * SPEED_OF_LIGHT_KMS / HUBBLE_CONSTANT
        out['distance_basis'] = 'redshift'
    else:
        out['distance_mpc'], out['distance_basis'] = None, None

    # IAU-style names (TNS names) among the name and aliases, for the TNS check.
    iau = []
    for candidate in [name] + aliases:
        if _IAU_NAME_RE.match(candidate.strip()) and yse_name(candidate) not in iau:
            iau.append(yse_name(candidate))
    out['iau_names'] = iau

    ebv = _best(quantities.get('ebv', []))
    out['mw_ebv'] = ebv['value_float'] if ebv else None

    out['spec_class'] = None
    out['claimed_type'] = None
    for q in sorted(quantities.get('claimedtype', []), key=lambda q: (bool(q['derived']), -(q['probability'] or 1.0))):
        cls = yse_class_name(q['value'])
        if cls:
            out['spec_class'], out['claimed_type'] = cls, q['value']
            break

    # The visual-band peak if there is one; ``maxabsmag`` is the brightest point in any band (often UV).
    prefix = 'maxvisual' if quantities.get('maxvisualabsmag') else 'max'
    peak = _best(quantities.get(prefix + 'absmag', []))
    out['abs_mag_peak'] = peak['value_float'] if peak else None
    peak_date = _best(quantities.get(prefix + 'date', []))
    out['abs_mag_peak_date'] = parse_osc_date(peak_date['value'])[0] if peak_date else None
    peak_band = _best(quantities.get(prefix + 'band', []))
    out['abs_mag_peak_band'] = peak_band['value'] if peak_band else None

    host = _best(quantities.get('host', []), skip_kinds=('cluster',))
    hra, hdec = _best(quantities.get('hostra', [])), _best(quantities.get('hostdec', []))
    hz = _best(quantities.get('hostredshift', []), skip_kinds=('cmb',))
    out['host'] = {
        'name': host['value'][:64] if host else None,
        'ra': sexagesimal_to_degrees(hra['value'], hra['u_value'], True) if hra else None,
        'dec': sexagesimal_to_degrees(hdec['value'], hdec['u_value'], False) if hdec else None,
        'redshift': hz['value_float'] if hz else None,
        'redshift_err': hz['e_value'] if hz else None,
    }
    return out


def parse_entry(name: str, entry: dict, origin: Optional[str] = None) -> dict:
    """The import plan for one OSC entry. Pure Python: safe to run without a database."""
    warnings: List[str] = []
    sources = []
    for raw in entry.get('sources') or []:
        if isinstance(raw, dict):
            sources.append(_parse_source(raw))
    known_aliases = {s['alias'] for s in sources}

    # Model data are not imported yet: rows citing a model are only counted.
    model_rows = Counter(fits=len([m for m in entry.get('models') or [] if isinstance(m, dict)]))

    def is_model_row(raw) -> bool:
        return isinstance(raw, dict) and raw.get('model') not in (None, '')

    quantities: Dict[str, List[dict]] = OrderedDict()
    extra: Dict[str, Any] = {}
    for key, value in entry.items():
        if key in STRUCTURE_KEYS:
            continue
        if isinstance(value, list):
            kept = [item for item in value if not is_model_row(item)]
            model_rows['quantities'] += len(value) - len(kept)
            if kept:
                quantities[key] = [_parse_quantity(key, item) for item in kept]
        else:
            extra[key] = value

    phot_sets: Dict[tuple, dict] = OrderedDict()
    for raw in entry.get('photometry') or []:
        if not isinstance(raw, dict):
            continue
        if is_model_row(raw):
            model_rows['photometry'] += 1
            continue
        point = _parse_phot_point(raw, warnings)
        if point is None:
            continue
        info = point['info']
        key = (info['telescope'], info['instrument'], info['observatory'], info['survey'], point['source_aliases'])
        phot_set = phot_sets.get(key)
        if phot_set is None:
            phot_set = phot_sets[key] = {
                'telescope': info['telescope'], 'instrument': info['instrument'],
                'observatory': info['observatory'], 'survey': info['survey'],
                'source_aliases': point['source_aliases'], 'points': []}
        phot_set['points'].append(point)

    fields = _transient_fields(name, quantities, warnings)
    fallback_mjd = None
    for candidate in (fields['disc_date'], fields['abs_mag_peak_date']):
        if candidate is not None:
            fallback_mjd = (candidate - MJD_EPOCH).total_seconds() / 86400.0
            break
    spectra = []
    for index, raw in enumerate(entry.get('spectra') or []):
        if is_model_row(raw):
            model_rows['spectra'] += 1
        elif isinstance(raw, dict):
            spectrum = _parse_spectrum(raw, index, fallback_mjd, warnings)
            if spectrum is not None:
                spectra.append(spectrum)

    cited = set()
    for qs in quantities.values():
        for q in qs:
            cited.update(q['source_aliases'])
    for phot_set in phot_sets.values():
        cited.update(phot_set['source_aliases'])
    for spectrum in spectra:
        cited.update(spectrum['source_aliases'])
    missing = sorted(cited - known_aliases, key=lambda a: (len(a), a))
    if missing:
        warnings.append('data cite source aliases not in the source list: %s' % ', '.join(missing))

    return {
        'catalog': CATALOG,
        'entry_name': name,
        'schema': to_text(entry.get('schema'), 512),
        'origin': origin,
        'sources': sources,
        'transient': fields,
        'quantities': [q for qs in quantities.values() for q in qs],
        'photometry_sets': list(phot_sets.values()),
        'spectra': spectra,
        'model_rows': dict((k, v) for k, v in model_rows.items() if v),
        'extra': extra or None,
        'warnings': warnings,
    }


# ---- dry-run report --------------------------------------------------------------------------

def _short(raw: dict, limit: int = 120) -> str:
    text = json.dumps(raw)
    return text if len(text) <= limit else text[:limit] + '...'


def plan_counts(plan: dict) -> dict:
    kinds = Counter()
    for phot_set in plan['photometry_sets']:
        for point in phot_set['points']:
            kinds[point_kind(point)] += 1
    return {
        'sources': len(plan['sources']),
        'quantities': len(plan['quantities']),
        'quantity_keys': dict(Counter(q['key'] for q in plan['quantities'])),
        'photometry_sets': len(plan['photometry_sets']),
        'photometry_points': sum(kinds.values()),
        'photometry_by_kind': dict(kinds),
        'spectra': len(plan['spectra']),
        'spectrum_pixels': sum(len(s['pixels']) for s in plan['spectra']),
        'model_rows_skipped': plan['model_rows'],
        'unmapped_top_level_keys': sorted((plan['extra'] or {}).keys()),
    }


def describe_plan(plan: dict, max_warnings: int = 10) -> str:
    t = plan['transient']
    c = plan_counts(plan)
    lines = [
        '%s -> transient %r' % (plan['entry_name'], t['name']),
        '  position: ra=%s dec=%s' % (_fmt(t['ra'], '%.6f'), _fmt(t['dec'], '%.6f')),
        '  discovered: %s (%s)   class: %s (OSC %r)' % (
            t['disc_date'].isoformat() if t['disc_date'] else '-', t['disc_date_precision'] or '-',
            t['spec_class'] or '-', t['claimed_type']),
        '  redshift: %s (%s)   MW E(B-V): %s   peak abs mag: %s %s' % (
            _fmt(t['redshift'], '%g'), t['redshift_kind'] or '-', _fmt(t['mw_ebv'], '%g'),
            _fmt(t['abs_mag_peak'], '%g'), t['abs_mag_peak_band'] or ''),
        '  host: %s ra=%s dec=%s z=%s' % (t['host']['name'] or '-', _fmt(t['host']['ra'], '%.5f'),
                                         _fmt(t['host']['dec'], '%.5f'), _fmt(t['host']['redshift'], '%g')),
        '  aliases (%d): %s' % (len(t['aliases']), ', '.join(t['aliases'][:12]) + (' ...' if len(t['aliases']) > 12 else '')),
        '  sources: %d   quantities: %d over %d keys' % (c['sources'], c['quantities'], len(c['quantity_keys'])),
        '  photometry: %d points in %d sets %s' % (c['photometry_points'], c['photometry_sets'],
                                                  dict(sorted(c['photometry_by_kind'].items()))),
        '  spectra: %d (%d pixels)   model data skipped: %s' % (
            c['spectra'], c['spectrum_pixels'],
            ', '.join('%d %s' % (v, k) for k, v in sorted(c['model_rows_skipped'].items())) or 'none'),
    ]
    if c['unmapped_top_level_keys']:
        lines.append('  unmapped top-level keys (kept in CatalogEntry.extra): %s' % ', '.join(c['unmapped_top_level_keys']))
    if plan['warnings']:
        lines.append('  warnings (%d):' % len(plan['warnings']))
        for w in plan['warnings'][:max_warnings]:
            lines.append('    - ' + w)
        if len(plan['warnings']) > max_warnings:
            lines.append('    - ... %d more' % (len(plan['warnings']) - max_warnings))
    return '\n'.join(lines)


def _fmt(value, fmt):
    return '-' if value is None else fmt % value


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description='Dry run: parse OSC entry files and report what would be imported.')
    parser.add_argument('paths', nargs='+')
    args = parser.parse_args(argv)
    for path in args.paths:
        for name, entry in load_entries(path):
            print(describe_plan(parse_entry(name, entry, origin=path)))
            print()


if __name__ == '__main__':
    main()
