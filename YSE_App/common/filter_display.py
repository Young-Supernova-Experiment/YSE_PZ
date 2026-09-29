"""
Canonical filter colors (Swope / PS1 uBVgrizy) and telescope symbol shapes for LC plots.

Filter names like r-ZTF, r-WFT, and Swope r map to the same color. Telescope families
(ZTF, PS1, Swope, …) share a Bokeh marker shape regardless of filter.
"""

from __future__ import annotations

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

# Blue-to-red order of the canonical filters; legend entries are sorted by it
# so the same band always sits in the same place (#91).  ``w`` (PS1 wide,
# g+r+i) goes last.
FILTER_WAVELENGTH_ORDER = ('u', 'B', 'g', 'V', 'r', 'i', 'z', 'y', 'w')

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
    # near-IR
    'j': 12350.0, 'h': 16620.0, 'k': 21590.0, 'ks': 21590.0,
}
# Used when neither the band name nor its family is known (mid-optical).
DEFAULT_EFFECTIVE_WAVELENGTH_AA = 6000.0

# Instrument / telescope name substrings -> short legend label (e.g. ZTF-Cam -> ZTF).
TELESCOPE_SHORT_LABELS = (
    (('ztf',), 'ZTF'),
    (('swift', 'uvot'), 'Swift'),
    (('gpc1', 'ps1', 'pan-starrs', 'panstarrs'), 'PS1'),
    (('swope',), 'Swope'),
    (('acam', 'atlas'), 'ATLAS'),
    (('direct', 'p200'), 'P200'),
    (('sinistro', 'pixis', 'lco', 'lcogt'), 'LCO'),
    (('acp', 'decam'), 'DECam'),
    (('sta1600', 'soar'), 'SOAR'),
    (('ptf',), 'PTF'),
    (('hst', 'acs', 'wfc3'), 'HST'),
    (('lsst', 'rubin', 'simonyi'), 'LSST'),
)

# Instrument / telescope name substrings -> Bokeh glyph.
TELESCOPE_SYMBOL_RULES = (
    (('ztf',), 'diamond'),
    (('gpc1', 'ps1', 'pan-starrs', 'panstarrs'), 'square'),
    (('swope',), 'circle'),
    (('acam', 'atlas'), 'asterisk'),
    (('direct', 'p200'), 'hex'),
    (('sinistro', 'pixis', 'lco', 'lcogt'), 'dash'),
    (('acp', 'decam', 'decam'), 'star'),
    (('soar', 'sta1600'), 'triangle'),
    (('ptf',), 'cross'),
    (('swift', 'uvot'), 'diamond'),
    (('hst', 'wfc3', 'acs'), 'square'),
    (('lsst', 'rubin', 'simonyi'), 'plus'),
)

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
) -> str:
    """Resolve Bokeh marker from telescope family (shape groups by instrument)."""
    inst_lower = (instrument_name or '').lower()
    for patterns, symbol in TELESCOPE_SYMBOL_RULES:
        if any(pat in inst_lower for pat in patterns):
            return symbol
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

    Both names are checked against the family patterns before either is
    used verbatim, so ``ZTF-Cam`` on a telescope row named ``Palomar 48``
    still reads ``ZTF`` (#250).
    """
    candidates = [
        str(candidate).strip()
        for candidate in (telescope_name, instrument_name)
        if candidate and str(candidate).strip()
    ]
    for candidate in candidates:
        lower = candidate.lower()
        for patterns, label in TELESCOPE_SHORT_LABELS:
            if any(pat in lower for pat in patterns):
                return label
    if candidates:
        return candidates[0]
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


def filter_sort_key(band_name: str | None) -> tuple:
    """Blue-to-red sort key: canonical filters in FILTER_WAVELENGTH_ORDER, then the rest by name."""
    canonical = normalize_filter_name(band_name)
    if canonical in FILTER_WAVELENGTH_ORDER:
        return (FILTER_WAVELENGTH_ORDER.index(canonical), '')
    return (len(FILTER_WAVELENGTH_ORDER), (band_name or '').strip().lower())


def legend_sort_key(
    band_name: str | None,
    *,
    instrument_name: str | None = None,
    telescope_name: str | None = None,
) -> tuple:
    """Order light-curve series by telescope family, then filter wavelength (#91).

    Independent of which series have data on a given transient, so ``PS1 g``
    always precedes ``PS1 r`` and every ZTF entry follows every PS1 entry.
    """
    tel = telescope_display_name(instrument_name, telescope_name)
    return (tel.lower(), tel) + filter_sort_key(band_name)


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
    labels = {
        'diamond': 'ZTF, Swift/UVOT',
        'square': 'PS1 (GPC1), HST',
        'circle': 'Swope',
        'asterisk': 'ATLAS (ACAM)',
        'hex': 'P200 Direct',
        'dash': 'LCO Sinistro / PIXIS',
        'star': 'DECam (ACP)',
        'triangle': 'SOAR / STA1600 (default fallback)',
        'cross': 'PTF',
        'plus': 'LSST (Rubin LSSTCam)',
    }
    seen = {}
    for _patterns, symbol in TELESCOPE_SYMBOL_RULES:
        seen.setdefault(symbol, labels.get(symbol, symbol))
    return [{'symbol': sym, 'telescopes': desc} for sym, desc in seen.items()]
