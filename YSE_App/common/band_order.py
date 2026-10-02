"""Canonical order of light-curve legend entries: instrument group, then wavelength.

The photometry legend lists instruments in a fixed order and, within an
instrument, bands from bluest to reddest:

1. Instrument groups: PS1/2 (GPC1, GPC2, Pan-STARRS), DECam, Swope, LSST
   (LSSTCam / Rubin), ZTF, ATLAS, Swift (UVOT), then every other instrument
   alphabetically by its legend label (``telescope_display_name``).  Each
   legend label (``PS1``, ``PS2``, ``Thacher`` ...) is one legend column.
2. Bands: by effective wavelength from ``FILTER_EFFECTIVE_WAVELENGTH_AA``
   (exact alias, then filter family, then the part before ``-``).  Bands
   with no known wavelength follow the known ones, alphabetically.

The keys depend only on the names, never on which series have data or on
database ids, so the same band always sits in the same place (#91).
"""

from __future__ import annotations

from .filter_display import (
    FILTER_EFFECTIVE_WAVELENGTH_AA,
    normalize_filter_name,
    telescope_display_name,
)

# Legend order of the instrument groups; anything else goes after them.
INSTRUMENT_GROUP_ORDER = ('PS1/2', 'DECam', 'Swope', 'LSST', 'ZTF', 'ATLAS', 'Swift')
OTHER_GROUP = 'other'

# Legend label (telescope_display_name) -> instrument group.
LABEL_GROUPS = {
    'PS1': 'PS1/2',
    'PS2': 'PS1/2',
    'DECam': 'DECam',
    'Swope': 'Swope',
    'LSST': 'LSST',
    'ZTF': 'ZTF',
    'ATLAS': 'ATLAS',
    'Swift': 'Swift',
}


def instrument_group(instrument_name: str | None = None, telescope_name: str | None = None) -> str:
    """Instrument group name for a series (``'other'`` when it matches no known family)."""
    label = telescope_display_name(instrument_name, telescope_name)
    return LABEL_GROUPS.get(label, OTHER_GROUP)


def instrument_group_rank(group: str) -> int:
    """Position of ``group`` in the legend; unknown groups sort after the known ones."""
    try:
        return INSTRUMENT_GROUP_ORDER.index(group)
    except ValueError:
        return len(INSTRUMENT_GROUP_ORDER)


def band_wavelength(band_name: str | None) -> float | None:
    """Effective wavelength (Angstrom) of a band name, or ``None`` when it is not known."""
    if not band_name:
        return None
    lower = str(band_name).strip().lower()
    if lower in FILTER_EFFECTIVE_WAVELENGTH_AA:
        return FILTER_EFFECTIVE_WAVELENGTH_AA[lower]
    canonical = normalize_filter_name(band_name)
    if canonical in FILTER_EFFECTIVE_WAVELENGTH_AA:
        return FILTER_EFFECTIVE_WAVELENGTH_AA[canonical]
    base = lower.split('-')[0]
    if base in FILTER_EFFECTIVE_WAVELENGTH_AA:
        return FILTER_EFFECTIVE_WAVELENGTH_AA[base]
    return None


def band_sort_key(band_name: str | None) -> tuple:
    """Blue-to-red sort key: known bands by wavelength, then unknown bands by name."""
    name = (band_name or '').strip()
    wavelength = band_wavelength(name)
    if wavelength is None:
        return (1, 0.0, name.lower(), name)
    return (0, wavelength, name.lower(), name)


def legend_column_key(
    instrument_name: str | None = None,
    telescope_name: str | None = None,
) -> tuple:
    """Key of the legend column a series belongs to: (group rank, label).

    One column per legend label, so ``PS1`` and ``PS2`` are adjacent columns
    inside the PS1/2 group and every unmatched instrument gets its own
    column, alphabetically, after the known groups.
    """
    label = telescope_display_name(instrument_name, telescope_name)
    return (instrument_group_rank(LABEL_GROUPS.get(label, OTHER_GROUP)), label.lower(), label)


def legend_sort_key(
    band_name: str | None,
    *,
    instrument_name: str | None = None,
    telescope_name: str | None = None,
) -> tuple:
    """Order light-curve series by instrument group, legend label, then wavelength."""
    return legend_column_key(instrument_name, telescope_name) + band_sort_key(band_name)


def group_consecutive(items, key):
    """Group already-ordered ``items`` into ``[(key, [items...]), ...]`` runs of equal ``key``."""
    groups: list[tuple] = []
    for item in items:
        item_key = key(item)
        if groups and groups[-1][0] == item_key:
            groups[-1][1].append(item)
        else:
            groups.append((item_key, [item]))
    return groups
