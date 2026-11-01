"""
Canonical filter colors (Swope / PS1 uBVgrizy) and telescope symbol shapes for LC plots.

Filter names like r-ZTF, r-WFT, and Swope r map to the same color. Telescope families
(ZTF, PS1, Swope, …) share a Bokeh marker shape regardless of filter.  The order
of legend entries lives in ``YSE_App/common/band_order.py``.
"""

from __future__ import annotations

import re

# Swope / PS1 uBVgrizy palette (matches YSE_App_photometricband seed values).
FILTER_COLORS = {
    'u': '#ff7f0e',
    'B': '#1f77b4',
    'V': '#8FBC8F',
    'g': '#008000',
    'r': '#DC143C',
    'i': '#8c465b',
    'z': '#800080',
    'y': '#C9A227',
    'w': '#EBBE0C',
}

# Lowercase alias -> canonical filter key in FILTER_COLORS.
FILTER_ALIASES = {
    'u': 'u',
    'up': 'u',
    'u-johnson': 'u',
    'u-ps1': 'u',
    'b': 'B',
    'b-johnson': 'B',
    'v': 'V',
    'v-johnson': 'V',
    'v-crts': 'V',
    'v-crts-crts': 'V',
    'g': 'g',
    'gp': 'g',
    'g-ztf': 'g',
    'g-wft': 'g',
    'g-sloan': 'g',
    'g-ptf': 'g',
    'g-sm': 'g',
    'g-sm-skymapper': 'g',
    'cyan': 'g',
    'cyan-atlas': 'g',
    'r': 'r',
    'rp': 'r',
    'r-ztf': 'r',
    'r-wft': 'r',
    'r-sloan': 'r',
    'r-ptf': 'r',
    'r-cousins': 'r',
    'r-sm-skymapper': 'r',
    'orange': 'r',
    'orange-atlas': 'r',
    'i': 'i',
    'ip': 'i',
    'i-ztf': 'i',
    'i-sloan': 'i',
    'i-ptf': 'i',
    'i-cousins': 'i',
    'z': 'z',
    'zp': 'z',
    'z-sloan': 'z',
    'y': 'y',
    'y-ps1': 'y',
    'w': 'w',
    'w-ps1': 'w',
    # Rubin / LSST (Rubin ingest adds bands named like g-LSST).
    'u-lsst': 'u',
    'g-lsst': 'g',
    'r-lsst': 'r',
    'i-lsst': 'i',
    'z-lsst': 'z',
    'y-lsst': 'y',
    'lsstu': 'u',
    'lsstg': 'g',
    'lsstr': 'r',
    'lssti': 'i',
    'lsstz': 'z',
    'lssty': 'y',
}

_BASE_FILTER_KEYS = frozenset({'u', 'b', 'v', 'g', 'r', 'i', 'z', 'y', 'w', 'up', 'gp', 'rp', 'ip', 'zp'})

# Effective wavelengths (Angstrom) for the joint Bazin fit (#225 follow-up):
# the wavelength-correlated prior ties the light-curve shape of bands that
# are close in log wavelength.  Keys are lowercase band names as stored in
# PhotometricBand.name (with the same aliases normalize_filter_name knows);
# the canonical uBVgrizy families give the fallback for any alias not listed.
FILTER_EFFECTIVE_WAVELENGTH_AA = {
    # canonical families (fallback per normalize_filter_name key)
    'u': 3560.0, 'B': 4380.0, 'V': 5450.0, 'g': 4770.0, 'r': 6230.0, 'i': 7630.0, 'z': 9050.0, 'y': 9620.0,
    'w': 6080.0,
    # Johnson / Cousins
    'u-johnson': 3660.0, 'b-johnson': 4380.0, 'v-johnson': 5450.0, 'r-cousins': 6410.0, 'i-cousins': 7980.0,
    # SDSS / Sloan-like (LCOGT up/gp/rp/ip/zp)
    'up': 3560.0, 'gp': 4770.0, 'rp': 6230.0, 'ip': 7630.0, 'zp': 9130.0,
    'g-sloan': 4770.0, 'r-sloan': 6230.0, 'i-sloan': 7630.0, 'z-sloan': 9130.0,
    # Pan-STARRS1 (GPC1)
    'g-ps1': 4870.0, 'r-ps1': 6220.0, 'i-ps1': 7550.0, 'z-ps1': 8680.0, 'y-ps1': 9630.0, 'w-ps1': 6080.0,
    # ZTF
    'g-ztf': 4810.0, 'r-ztf': 6440.0, 'i-ztf': 7980.0,
    # ATLAS cyan / orange
    'cyan': 5330.0, 'cyan-atlas': 5330.0, 'orange': 6790.0, 'orange-atlas': 6790.0,
    # Rubin / LSST
    'u-lsst': 3670.0, 'g-lsst': 4830.0, 'r-lsst': 6220.0, 'i-lsst': 7550.0, 'z-lsst': 8690.0, 'y-lsst': 9710.0,
    'lsstu': 3670.0, 'lsstg': 4830.0, 'lsstr': 6220.0, 'lssti': 7550.0, 'lsstz': 8690.0, 'lssty': 9710.0,
    # Swift UVOT
    'uvw2': 2030.0, 'uvm2': 2230.0, 'uvw1': 2590.0, 'u-uvot': 3470.0, 'b-uvot': 4390.0, 'v-uvot': 5470.0,
    'uvw2-uvot': 2030.0, 'uvm2-uvot': 2230.0, 'uvw1-uvot': 2590.0,
    # Gaia (G is broad, ~6700 A; without this the g- prefix would file it with SDSS g)
    'g-gaia': 6730.0, 'bp-gaia': 5320.0, 'rp-gaia': 7970.0,
    # near-IR
    'j': 12350.0, 'h': 16620.0, 'k': 21590.0, 'ks': 21590.0,
}
# Used when neither the band name nor its family is known (mid-optical).
DEFAULT_EFFECTIVE_WAVELENGTH_AA = 6000.0

# Telescope family -> short legend label (e.g. ZTF-Cam -> ZTF), matched on whole words of the
# names (#788, #793). A rule's words must appear as consecutive words of the name; a trailing
# camera number is allowed (ACAM1 matches acam). Generic camera names used at many
# observatories (Direct, PIXIS, ACP) are deliberately absent: such data keep their own
# telescope name (Nickel, Lulin), never another observatory's label.
#
# Rules checked against the TELESCOPE name (the database row is authoritative).
TELESCOPE_NAME_RULES = (
    (('ztf',), 'ZTF'),
    (('swift',), 'Swift'),
    # PS2 before PS1: 'pan starrs' would otherwise claim Pan-STARRS2 rows.
    (('ps2', 'pan starrs2', 'panstarrs2'), 'PS2'),
    (('ps1', 'pan starrs1', 'panstarrs1', 'pan starrs', 'panstarrs'), 'PS1'),
    (('swope',), 'Swope'),
    (('atlas',), 'ATLAS'),
    (('p200', 'hale', 'palomar 200'), 'P200'),
    (('lco', 'lcogt', 'las cumbres'), 'LCO'),
    (('blanco', 'decam'), 'DECam'),
    (('soar',), 'SOAR'),
    (('ptf',), 'PTF'),
    (('hst', 'hubble'), 'HST'),
    (('lsst', 'rubin', 'simonyi'), 'LSST'),
)
# Rules checked against the INSTRUMENT name: only cameras unique to one survey or telescope.
INSTRUMENT_NAME_RULES = (
    (('ztf',), 'ZTF'),
    (('uvot',), 'Swift'),
    (('gpc2',), 'PS2'),
    (('gpc1',), 'PS1'),
    (('acam',), 'ATLAS'),
    (('sinistro',), 'LCO'),
    (('decam',), 'DECam'),
    (('sta1600',), 'SOAR'),
    (('wfc3', 'acs'), 'HST'),
    (('lsstcam',), 'LSST'),
)
# Kept for callers that list the families (labels only).
TELESCOPE_SHORT_LABELS = TELESCOPE_NAME_RULES + INSTRUMENT_NAME_RULES

# Family label -> Bokeh glyph.
FAMILY_SYMBOLS = {
    'ZTF': 'diamond',
    'Swift': 'diamond',
    'PS1': 'square',
    'PS2': 'square',
    'HST': 'square',
    'Swope': 'circle',
    'ATLAS': 'asterisk',
    'P200': 'hex',
    'LCO': 'dash',
    'DECam': 'star',
    'SOAR': 'triangle',
    'PTF': 'cross',
    'LSST': 'plus',
}
# Kept for callers that list (patterns, symbol) pairs.
TELESCOPE_SYMBOL_RULES = tuple(
    (patterns, FAMILY_SYMBOLS[label]) for patterns, label in TELESCOPE_SHORT_LABELS
)


def _words(name: str | None) -> list[str]:
    return re.findall(r'[a-z0-9]+', (name or '').lower())


def _word_match(words: list[str], pattern: str) -> bool:
    """True when the words of ``pattern`` appear consecutively in ``words``.

    The last pattern word may be followed by digits in the name (``acam`` matches ``ACAM1``).
    """
    pwords = _words(pattern)
    n = len(pwords)
    if not n:
        return False
    for start in range(len(words) - n + 1):
        window = words[start:start + n]
        if window[:-1] != pwords[:-1]:
            continue
        last, want = window[-1], pwords[-1]
        if last == want or (last.startswith(want) and last[len(want):].isdigit()):
            return True
    return False


def telescope_family(instrument_name: str | None = None, telescope_name: str | None = None) -> str | None:
    """Known telescope family label of a series, or None (telescope name first, then a unique camera)."""
    tel_words = _words(telescope_name)
    for patterns, label in TELESCOPE_NAME_RULES:
        if any(_word_match(tel_words, pat) for pat in patterns):
            return label
    inst_words = _words(instrument_name)
    for patterns, label in INSTRUMENT_NAME_RULES:
        if any(_word_match(inst_words, pat) for pat in patterns):
            return label
    return None


_FALLBACK_COLORS = ('#8dd3c7', '#bebada', '#fb8072', '#80b1d3', '#fdb462', '#b3de69', '#fccde5', '#d9d9d9')


def normalize_filter_name(band_name: str | None) -> str | None:
    """Map a band name (e.g. r-ZTF, r-WFT, g-Sloan) to a canonical uBVgrizy key."""
    if not band_name:
        return None
    lower = band_name.strip().lower()
    if lower in FILTER_ALIASES:
        return FILTER_ALIASES[lower]
    if 'cyan' in lower:
        return 'g'
    if 'orange' in lower:
        return 'r'
    base = lower.split('-')[0]
    if base in _BASE_FILTER_KEYS:
        return FILTER_ALIASES.get(base, base)
    if base == 'b':
        return 'B'
    if base == 'v':
        return 'V'
    return None


def band_effective_wavelength(band_name: str | None) -> float:
    """Effective wavelength in Angstrom for a band name, by exact alias, then filter family, else the default."""
    if band_name:
        lower = band_name.strip().lower()
        if lower in FILTER_EFFECTIVE_WAVELENGTH_AA:
            return FILTER_EFFECTIVE_WAVELENGTH_AA[lower]
        canonical = normalize_filter_name(band_name)
        if canonical in FILTER_EFFECTIVE_WAVELENGTH_AA:
            return FILTER_EFFECTIVE_WAVELENGTH_AA[canonical]
        base = lower.split('-')[0]
        if base in FILTER_EFFECTIVE_WAVELENGTH_AA:
            return FILTER_EFFECTIVE_WAVELENGTH_AA[base]
    return DEFAULT_EFFECTIVE_WAVELENGTH_AA


def band_display_color(
    band_name: str | None,
    disp_color: str | None = None,
    *,
    fallback_index: int = 0,
) -> str:
    """Resolve plot color: canonical filter family first, then DB value, then palette."""
    canonical = normalize_filter_name(band_name)
    if canonical and canonical in FILTER_COLORS:
        return FILTER_COLORS[canonical]
    if disp_color and disp_color != 'None':
        return disp_color
    if band_name:
        # Hash the name so an unknown filter keeps its colour across
        # transients instead of taking whatever index it was plotted at (#91).
        fallback_index = sum(ord(ch) for ch in str(band_name).strip().lower())
    return _FALLBACK_COLORS[fallback_index % len(_FALLBACK_COLORS)]


def telescope_display_symbol(
    instrument_name: str | None,
    disp_symbol: str | None = None,
    telescope_name: str | None = None,
) -> str:
    """Bokeh marker of the series' telescope family (``telescope_family``); else the band's own
    ``disp_symbol``; else a triangle."""
    family = telescope_family(instrument_name, telescope_name)
    if family in FAMILY_SYMBOLS:
        return FAMILY_SYMBOLS[family]
    if disp_symbol and disp_symbol not in (None, 'None', 'inverted_triangle'):
        return disp_symbol
    return 'triangle'


def display_filter_label(band_name: str | None) -> str:
    """Short plot label (e.g. r-ZTF -> r). DB band name unchanged."""
    canonical = normalize_filter_name(band_name)
    if canonical:
        return canonical
    if band_name:
        return band_name.strip()
    return '?'


def telescope_display_name(
    instrument_name: str | None = None,
    telescope_name: str | None = None,
) -> str:
    """Short telescope family label for plot legends (e.g. ZTF-Cam -> ZTF).

    A known family (``telescope_family``: the telescope name first, then a camera unique to
    one survey) gives its short label, so ``ZTF-Cam`` on a telescope row named
    ``Palomar 48`` still reads ``ZTF`` (#250). Otherwise the telescope's own name, else the
    instrument's (#788, #793: Nickel and Lulin data are never relabelled P200 or LCO).
    """
    family = telescope_family(instrument_name, telescope_name)
    if family:
        return family
    for candidate in (telescope_name, instrument_name):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return 'Unknown'


def plot_legend_label(
    band_name: str | None,
    *,
    instrument_name: str | None = None,
    telescope_name: str | None = None,
) -> str:
    """Legend text: telescope + short filter (e.g. 'ZTF r')."""
    tel = telescope_display_name(instrument_name, telescope_name)
    filt = display_filter_label(band_name)
    return f'{tel} {filt}'


def filter_color_groups_for_display() -> list[dict]:
    """Human-readable color groups for docs / chat (filter family -> example names)."""
    groups = []
    examples = {
        'u': ['u', 'u-PS1', 'up (Sinistro)'],
        'B': ['B', 'B-Johnson'],
        'V': ['V', 'V-Johnson', 'V-crts'],
        'g': ['g', 'g-ZTF', 'g-WFT', 'g-Sloan', 'g-PTF', 'g-LSST', 'cyan-ATLAS'],
        'r': ['r', 'r-ZTF', 'r-WFT', 'r-Sloan', 'R-Cousins', 'r-LSST', 'orange-ATLAS'],
        'i': ['i', 'i-ZTF', 'i-Sloan', 'I-Cousins', 'ip'],
        'z': ['z', 'z-Sloan', 'zp'],
        'y': ['y', 'y-PS1'],
        'w': ['w', 'w-PS1'],
    }
    for key, color in FILTER_COLORS.items():
        groups.append({
            'filter': key,
            'color': color,
            'examples': examples.get(key, [key]),
        })
    return groups


def telescope_symbol_groups_for_display() -> list[dict]:
    """Human-readable telescope -> symbol groups."""
    seen: dict = {}
    for label, symbol in FAMILY_SYMBOLS.items():
        seen.setdefault(symbol, []).append(label)
    seen = {sym: ', '.join(labels) for sym, labels in seen.items()}
    seen.setdefault('triangle', 'other telescopes (default)')
    return [{'symbol': sym, 'telescopes': desc} for sym, desc in seen.items()]
