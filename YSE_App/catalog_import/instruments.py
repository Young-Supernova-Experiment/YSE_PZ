"""
Catalogue telescope / instrument strings -> YSE instruments (#894).

``osc_instruments.json`` lists YSE telescopes and instruments with the catalogue strings that mean
them. ``manage.py catalog_instruments --load`` turns it into ``CatalogInstrumentAlias`` rows,
creating missing observatories, telescopes and instruments; :class:`InstrumentResolver` then
resolves strings through those rows, then by instrument name, and falls back to ``Unknown``.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

UNKNOWN = 'Unknown'
MAPPING_PATH = os.path.join(os.path.dirname(__file__), 'osc_instruments.json')


class NoUnknownInstrument(Exception):
    pass


def load_mapping(path: str = MAPPING_PATH) -> List[dict]:
    with open(path) as handle:
        return json.load(handle)['instruments']


def _norm(value: Optional[str]) -> str:
    return (value or '').strip().lower()


def string_variants(telescope: Optional[str], instrument: Optional[str]) -> List[Tuple[str, str]]:
    """The (telescope, instrument) keys to try: as given, then a ``"Telescope - Instrument"`` field split."""
    out = [(_norm(telescope), _norm(instrument))]
    for field in (instrument, telescope):
        if field and ' - ' in field:
            tel, inst = field.split(' - ', 1)
            out.append((_norm(tel), _norm(inst)))
    return out


class InstrumentResolver:
    """Catalogue strings -> Instrument, cached. Order: alias rows, a unique instrument of that name, Unknown."""

    def __init__(self, catalog: str = 'OSC'):
        self.catalog = catalog
        self._aliases: Optional[Dict[Tuple[str, str], list]] = None
        self._cache: Dict[tuple, object] = {}
        self._unknown = None

    def unknown(self):
        from YSE_App.models import Instrument

        if self._unknown is None:
            self._unknown = Instrument.objects.filter(name=UNKNOWN).order_by('id').first()
            if self._unknown is None:
                raise NoUnknownInstrument("no instrument named 'Unknown'; it is needed for data from unknown instruments")
        return self._unknown

    def _alias_table(self):
        from YSE_App.models import CatalogInstrumentAlias

        if self._aliases is None:
            self._aliases = {}
            for row in CatalogInstrumentAlias.objects.filter(catalog=self.catalog).select_related('yse_instrument'):
                self._aliases.setdefault((_norm(row.telescope), _norm(row.instrument)), []).append(
                    (row.mjd_min, row.mjd_max, row.yse_instrument))
        return self._aliases

    def resolve(self, telescope: Optional[str], instrument: Optional[str], mjd: Optional[float] = None):
        key = (telescope or '', instrument or '', None if mjd is None else round(mjd))
        if key not in self._cache:
            self._cache[key] = self._lookup(telescope, instrument, mjd) or self.unknown()
        return self._cache[key]

    def is_unknown(self, instrument) -> bool:
        return instrument.id == self.unknown().id

    def _lookup(self, telescope, instrument, mjd):
        from YSE_App.models import Instrument

        table = self._alias_table()
        for variant in string_variants(telescope, instrument):
            if variant == ('', ''):
                continue
            for lo, hi, inst in table.get(variant, ()):
                if mjd is None or ((lo is None or mjd >= lo) and (hi is None or mjd < hi)):
                    return inst
        for _, inst_name in string_variants(telescope, instrument):
            if inst_name:
                qs = Instrument.objects.filter(name__iexact=inst_name)
                if telescope:
                    on_telescope = qs.filter(telescope__name__iexact=telescope)
                    if on_telescope.exists():
                        qs = on_telescope
                if qs.count() == 1:
                    return qs.first()
        return None


def ensure_instruments(user, mapping: Iterable[dict], *, catalog: str = 'OSC', apply: bool = False) -> List[str]:
    """Create the missing observatories, telescopes, instruments and alias rows of ``mapping``.

    Reuses an instrument with the same name when there is exactly one. Returns report lines; writes
    only with ``apply``.
    """
    from YSE_App.models import CatalogInstrumentAlias, Instrument, Observatory, Telescope

    audit = {'created_by': user, 'modified_by': user}
    lines = []
    for item in mapping:
        existing = Instrument.objects.filter(name__iexact=item['instrument'])
        on_telescope = existing.filter(telescope__name__iexact=item['telescope'])
        inst = (on_telescope.first() if on_telescope.count() == 1 else
                existing.first() if existing.count() == 1 else None)
        if inst is None:
            lines.append('create %s / %s (observatory %s)' % (item['telescope'], item['instrument'], item['observatory']))
            if apply:
                observatory, _ = Observatory.objects.get_or_create(
                    name=item['observatory'], defaults={'utc_offset': item.get('utc_offset', 0),
                                                        'tz_name': item.get('tz_name', 'UTC'), **audit})
                telescope, _ = Telescope.objects.get_or_create(
                    name=item['telescope'], defaults={'observatory': observatory,
                                                      'latitude': item.get('latitude', 0.0),
                                                      'longitude': item.get('longitude', 0.0),
                                                      'elevation': item.get('elevation', 0.0), **audit})
                inst = Instrument.objects.create(telescope=telescope, name=item['instrument'], **audit)
        else:
            lines.append('use %s / %s (instrument %d)' % (inst.telescope.name, inst.name, inst.id))
        for tel, ins in item['aliases']:
            if apply and inst is not None:
                CatalogInstrumentAlias.objects.update_or_create(
                    catalog=catalog, telescope=tel, instrument=ins, mjd_min=item.get('mjd_min'),
                    mjd_max=item.get('mjd_max'), defaults={'yse_instrument': inst, **audit})
        lines.append('  %d aliases%s' % (len(item['aliases']), '' if apply else ' (dry run)'))
    return lines


def census(entries: Iterable[Tuple[str, dict]]) -> Counter:
    """``Counter{(kind, telescope, instrument): rows}`` over OSC entries (observations only, not model rows)."""
    counts: Counter = Counter()
    for _, entry in entries:
        for row in entry.get('photometry') or []:
            if isinstance(row, dict) and 'model' not in row:
                counts[('photometry', row.get('telescope') or '', row.get('instrument') or '')] += 1
        for row in entry.get('spectra') or []:
            if isinstance(row, dict):
                counts[('spectra', row.get('telescope') or '', row.get('instrument') or '')] += 1
    return counts
