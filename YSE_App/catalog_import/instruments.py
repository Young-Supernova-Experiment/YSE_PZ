"""
Catalogue telescope / instrument strings -> YSE instruments (#894, #1003).

Every string pair found in the catalogue gets a ``CatalogInstrumentAlias`` row naming the YSE
instrument it means, decided in this order (:func:`plan`):

1. the first rule of ``osc_instruments.json`` that matches (:class:`Rules`; Ryan's mappings);
2. an existing YSE instrument of that name;
3. automatically, Telescope/Instrument from the strings (:func:`auto_target`): both given ->
   Telescope/Instrument, one given -> X/X, ``Telescope - Instrument`` in one field -> split.

Empty or meaningless strings (``unknown`` in the file) stay on the ``Unknown`` instrument.
``manage.py catalog_instruments --census <OSC_DIR> --load --apply`` creates the missing telescopes and
instruments and the alias rows; :class:`InstrumentResolver` then resolves strings through the alias
rows, then by instrument name, and falls back to ``Unknown``.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

UNKNOWN = 'Unknown'
MAPPING_PATH = os.path.join(os.path.dirname(__file__), 'osc_instruments.json')


class NoUnknownInstrument(Exception):
    pass


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


NAME_LENGTH = 64          # Telescope.name, Instrument.name
ALIAS_LENGTH = 128        # CatalogInstrumentAlias.telescope / instrument


def _clean(value: Optional[str]) -> str:
    return ' '.join((value or '').split())


class Target(NamedTuple):
    telescope: str
    instrument: str
    existing: Optional[str] = None   # a YSE instrument name to use if there is exactly one
    how: str = 'rule'


class Rules:
    """The rules of ``osc_instruments.json``: patterns -> target telescope / instrument."""

    def __init__(self, data: dict):
        self.unknown = {_norm(v) for v in data.get('unknown', [])}
        self.rules = []
        for rule in data.get('rules', []):
            compiled = {k: re.compile(rule[k], re.IGNORECASE) for k in ('telescope', 'instrument', 'any') if k in rule}
            self.rules.append((compiled, rule))

    @classmethod
    def load(cls, path: str = MAPPING_PATH) -> 'Rules':
        with open(path) as handle:
            return cls(json.load(handle))

    def is_unknown(self, value: str) -> bool:
        return _norm(value) in self.unknown

    def rule_target(self, telescope: Optional[str], instrument: Optional[str]) -> Optional[Target]:
        tel, inst = _clean(telescope), _clean(instrument)
        for compiled, rule in self.rules:
            groups = []
            matched = True
            for key, pattern in compiled.items():
                if key == 'any':
                    m = pattern.search(tel) or pattern.search(inst)
                else:
                    m = pattern.fullmatch(tel if key == 'telescope' else inst)
                if m is None:
                    matched = False
                    break
                groups.extend(g for g in m.groups() if g)
            if not matched:
                continue

            def fill(template):
                return re.sub(r'\\(\d)', lambda m: groups[int(m.group(1)) - 1] if len(groups) >= int(m.group(1)) else '',
                              template)[:NAME_LENGTH]

            return Target(fill(rule['target_telescope']), fill(rule['target_instrument']), rule.get('existing'), 'rule')
        return None

    def auto_target(self, telescope: Optional[str], instrument: Optional[str]) -> Optional[Target]:
        """Telescope/Instrument from the strings themselves; None when both are empty or meaningless."""
        tel, inst = _clean(telescope), _clean(instrument)
        if self.is_unknown(tel):
            tel = ''
        if self.is_unknown(inst):
            inst = ''
        if not tel and ' - ' in inst:
            tel, inst = (part.strip() for part in inst.split(' - ', 1))
        elif not inst and ' - ' in tel:
            tel, inst = (part.strip() for part in tel.split(' - ', 1))
        if self.is_unknown(tel):
            tel = ''
        if self.is_unknown(inst):
            inst = ''
        if not tel and not inst:
            return None
        return Target((tel or inst)[:NAME_LENGTH], (inst or tel)[:NAME_LENGTH], None, 'auto')


class PlanRow(NamedTuple):
    telescope: str            # as written in the catalogue
    instrument: str
    rows: int
    kinds: Tuple[str, ...]
    action: str               # use | create | unknown
    target: Optional[Target]
    yse_instrument: object    # the Instrument to use (action ``use``), else None


def _find_instrument(telescope_name: str, instrument_name: str):
    from YSE_App.models import Instrument

    qs = Instrument.objects.filter(name__iexact=instrument_name, telescope__name__iexact=telescope_name)
    return qs.first() if qs.count() == 1 else None


def _unique_instrument(name: str):
    from YSE_App.models import Instrument

    qs = Instrument.objects.filter(name__iexact=name)
    return qs.first() if qs.count() == 1 else None


def plan(counts: Counter, rules: Rules) -> List[PlanRow]:
    """What each catalogue string pair resolves to: an existing instrument, a new one, or Unknown.

    ``counts`` is :func:`census` output. Reads the database only.
    """
    pairs: Dict[Tuple[str, str], list] = {}
    for (kind, tel, inst), n in counts.items():
        entry = pairs.setdefault((tel[:ALIAS_LENGTH], inst[:ALIAS_LENGTH]), [0, set()])
        entry[0] += n
        entry[1].add(kind)
    resolver = InstrumentResolver()
    out = []
    for (tel, inst), (n, kinds) in sorted(pairs.items(), key=lambda item: -item[1][0]):
        target = rules.rule_target(tel, inst)
        found = None
        if target is not None:
            found = (_unique_instrument(target.existing) if target.existing else None) or \
                _find_instrument(target.telescope, target.instrument)
        else:
            target = rules.auto_target(tel, inst)   # None: empty or meaningless strings stay Unknown
            if target is not None:
                found = resolver._lookup(tel, inst, None) or _find_instrument(target.telescope, target.instrument)
        if found is not None:
            action = 'use'
        elif target is not None:
            action = 'create'
        else:
            action = 'unknown'
        out.append(PlanRow(tel, inst, n, tuple(sorted(kinds)), action, target, found))
    return out


def describe(row: PlanRow) -> str:
    if row.action == 'use':
        what = 'use %s / %s' % (row.yse_instrument.telescope.name, row.yse_instrument.name)
    elif row.action == 'create':
        what = 'create %s / %s (%s)' % (row.target.telescope, row.target.instrument, row.target.how)
    else:
        what = 'Unknown'
    return '%9d  %-18s %-30r %-30r -> %s' % (row.rows, '+'.join(row.kinds), row.telescope, row.instrument, what)


def apply_plan(rows: Iterable[PlanRow], user, *, catalog: str = 'OSC') -> Dict[str, int]:
    """Create the new telescopes and instruments (latitude, longitude, elevation 0, as the TNS import
    does) and one alias row per string pair. Idempotent."""
    from YSE_App.models import CatalogInstrumentAlias, Instrument, Observatory, Telescope

    audit = {'created_by': user, 'modified_by': user}
    created: Dict[Tuple[str, str], object] = {}
    counts = Counter()
    for row in rows:
        if row.action == 'unknown':
            continue
        inst = row.yse_instrument
        if inst is None:
            key = (row.target.telescope.lower(), row.target.instrument.lower())
            inst = created.get(key) or _find_instrument(row.target.telescope, row.target.instrument)
            if inst is None:
                telescope = Telescope.objects.filter(name__iexact=row.target.telescope).order_by('id').first()
                if telescope is None:
                    observatory, _ = Observatory.objects.get_or_create(
                        name=row.target.telescope, defaults={'utc_offset': 0, 'tz_name': 'UTC', **audit})
                    telescope = Telescope.objects.create(observatory=observatory, name=row.target.telescope,
                                                         latitude=0.0, longitude=0.0, elevation=0.0, **audit)
                    counts['telescopes created'] += 1
                inst = Instrument.objects.create(telescope=telescope, name=row.target.instrument, **audit)
                counts['instruments created'] += 1
            created[key] = inst
        CatalogInstrumentAlias.objects.update_or_create(
            catalog=catalog, telescope=row.telescope, instrument=row.instrument, mjd_min=None, mjd_max=None,
            defaults={'yse_instrument': inst, **audit})
        counts['aliases'] += 1
    return dict(counts)


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
