"""
TNS's bulk file of all public objects (#905), for matching without one request per object.

TNS publishes a daily CSV of every public object, ``tns_public_objects.csv.zip``, to bot accounts:
one row per object with its name, coordinates, type, discovery date and the surveys' internal names
(e.g. ``PTF11kly, PSN J14030581+5416254``). :class:`PublicObjects` indexes one copy of that file by
TNS name, internal name and position. The same file serves the test and the production runs, so the
TNS check costs one download in all.

The first line of the file may be a timestamp; the header row is found by its column names. Column
names follow the TNS file (``name``, ``name_prefix``, ``ra``, ``declination``, ``type``,
``internal_names``, ``discoverydate``, ``redshift``); coordinates may be degrees or sexagesimal.
"""

from __future__ import annotations

import csv
import io
import math
import zipfile
from typing import Dict, Iterable, List, Optional

PUBLIC_OBJECTS_PATH = '/system/files/tns_public_objects/tns_public_objects.csv.zip'
_BIN_DEG = 0.1


def _norm(name: Optional[str]) -> str:
    """Names compared without spaces or case (``SN 2011fe`` = ``sn2011fe``)."""
    return ''.join((name or '').split()).lower()


def _degrees(value: str, is_ra: bool) -> Optional[float]:
    value = (value or '').strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        from YSE_App.catalog_import.osc import sexagesimal_to_degrees

        return sexagesimal_to_degrees(value, 'hours' if is_ra else 'degrees', is_ra)


def _separation_arcsec(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 3600.0


def _text_lines(path: str) -> Iterable[str]:
    if path.endswith('.zip'):
        with zipfile.ZipFile(path) as archive:
            member = next(n for n in archive.namelist() if n.endswith('.csv'))
            with archive.open(member) as handle:
                yield from io.TextIOWrapper(handle, encoding='utf-8', errors='replace')
    else:
        with open(path, encoding='utf-8', errors='replace') as handle:
            yield from handle


class PublicObjects:
    """TNS public objects indexed by name, internal name and position."""

    def __init__(self, rows: Iterable[dict]):
        self.by_name: Dict[str, dict] = {}
        self._internal: Dict[str, List[dict]] = {}
        self._bins: Dict[int, List[dict]] = {}
        for row in rows:
            name = (row.get('name') or '').strip()
            if not name:
                continue
            obj = {
                'objname': name,
                'name_prefix': (row.get('name_prefix') or '').strip(),
                'radeg': _degrees(row.get('ra', ''), True),
                'decdeg': _degrees(row.get('declination', row.get('dec', '')), False),
                'object_type': {'name': (row.get('type') or '').strip() or None},
                'discoverydate': (row.get('discoverydate') or '').strip(),
                'redshift': (row.get('redshift') or '').strip(),
                'internal_names': (row.get('internal_names') or '').strip(),
            }
            self.by_name[_norm(name)] = obj
            for internal in obj['internal_names'].split(','):
                if internal.strip():
                    self._internal.setdefault(_norm(internal), []).append(obj)
            if obj['decdeg'] is not None and obj['radeg'] is not None:
                self._bins.setdefault(int(math.floor(obj['decdeg'] / _BIN_DEG)), []).append(obj)

    def __len__(self):
        return len(self.by_name)

    @classmethod
    def load(cls, path: str) -> 'PublicObjects':
        lines = iter(_text_lines(path))
        header = None
        for line in lines:
            cells = next(csv.reader([line]))
            lowered = [c.strip().lower() for c in cells]
            if 'name' in lowered and 'ra' in lowered:
                header = lowered
                break
        if header is None:
            raise ValueError('%s: no header row with name and ra columns' % path)
        return cls(dict(zip(header, cells)) for cells in csv.reader(lines) if cells)

    def name(self, name: str) -> Optional[dict]:
        """By TNS name, with or without the SN/AT prefix."""
        key = _norm(name)
        for prefix in ('sn', 'at'):
            if key.startswith(prefix) and key[2:3].isdigit():
                key = key[2:]
        return self.by_name.get(key)

    def internal(self, name: str) -> List[dict]:
        """The objects that list ``name`` among their internal (survey) names."""
        return list(self._internal.get(_norm(name), []))

    def cone(self, ra: float, dec: float, radius_arcsec: float) -> List[dict]:
        radius_deg = radius_arcsec / 3600.0
        lo = int(math.floor((dec - radius_deg) / _BIN_DEG))
        hi = int(math.floor((dec + radius_deg) / _BIN_DEG))
        out = []
        for b in range(lo, hi + 1):
            for obj in self._bins.get(b, ()):
                if _separation_arcsec(ra, dec, obj['radeg'], obj['decdeg']) <= radius_arcsec:
                    out.append(obj)
        return out


def download(dest: str, client) -> str:
    """Fetch ``tns_public_objects.csv.zip`` with a bot's credentials (one request); returns ``dest``."""
    import requests

    host = client.base_url[:-len('/api')] if client.base_url.endswith('/api') else client.base_url
    response = requests.post(host + PUBLIC_OBJECTS_PATH, headers=client.headers,
                             data={'api_key': client.api_key}, timeout=max(client.timeout, 300), stream=True)
    response.raise_for_status()
    with open(dest + '.part', 'wb') as handle:
        for chunk in response.iter_content(1 << 20):
            handle.write(chunk)
    import os

    os.replace(dest + '.part', dest)
    return dest
