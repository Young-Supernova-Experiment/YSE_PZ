"""DESI spectroscopic redshifts from Astro Data Lab (#437).

Astro Data Lab (NOIRLab) serves the DESI redshift catalogues anonymously through TAP
(``https://datalab.noirlab.edu/tap/sync``). We send one synchronous ADQL query per
position and read CSV back, with plain HTTP (no new package). The table is a setting
(``[redshifts] DESI_TABLE``, default ``desi_dr1.zpix``) so later releases need only a
settings change, together with ``DESI_SOURCE_LABEL`` (``DESI DR1``), the provenance
written to ``Host.redshift_source``.

Quality cuts (Redrock): ``zwarn = 0``, ``spectype = 'GALAXY'``, ``zcat_primary`` (the
primary coadd of each target), ``deltachi2 >= DESI_MIN_DELTACHI2`` (25). Data Lab's
ADQL has no ``CONTAINS``; the cone is ``q3c_radial_query`` on the mean fibre position.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import requests
from django.conf import settings

from YSE_App.services.redshift_precedence import desi_label

log = logging.getLogger(__name__)

DEFAULT_TAP_URL = "https://datalab.noirlab.edu/tap/sync"
DEFAULT_TABLE = "desi_dr1.zpix"
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


class DESIError(Exception):
    """Data Lab could not be queried, or returned an error."""


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def table_name() -> str:
    table = str(_setting("DESI_TABLE", DEFAULT_TABLE) or DEFAULT_TABLE).strip()
    if not _TABLE_RE.match(table):
        raise DESIError("DESI_TABLE %r is not a schema.table name" % table)
    return table


@dataclass
class DESIMatch:
    targetid: str
    ra: float
    dec: float
    z: float
    z_err: Optional[float]
    deltachi2: Optional[float]
    sep_arcsec: float

    def as_dict(self) -> dict:
        return {"targetid": self.targetid, "ra": self.ra, "dec": self.dec, "z": self.z, "z_err": self.z_err,
                "deltachi2": self.deltachi2, "sep_arcsec": self.sep_arcsec}


def _float(value) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _sep_arcsec(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    a = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a)))) * 3600.0


def cone_query(ra: float, dec: float, radius_arcsec: float, *, table: Optional[str] = None) -> str:
    """The ADQL for every quality-cut galaxy redshift within ``radius_arcsec``."""
    min_dchi2 = float(_setting("DESI_MIN_DELTACHI2", 25.0))
    return (
        "SELECT targetid, mean_fiber_ra, mean_fiber_dec, z, zerr, deltachi2 FROM %s "
        "WHERE q3c_radial_query(mean_fiber_ra, mean_fiber_dec, %.8f, %.8f, %.8f) = 't' "
        "AND zwarn = 0 AND spectype = 'GALAXY' AND zcat_primary = 't' AND deltachi2 >= %g"
        % (table or table_name(), float(ra), float(dec), float(radius_arcsec) / 3600.0, min_dchi2)
    )


class DESIClient:
    """Synchronous TAP queries against Astro Data Lab. Pass ``session`` to stub HTTP in tests."""

    def __init__(self, tap_url: Optional[str] = None, *, timeout: Optional[float] = None,
                 interval: Optional[float] = None, session=None):
        self.tap_url = tap_url or _setting("DESI_TAP_URL", DEFAULT_TAP_URL)
        self.timeout = float(timeout if timeout is not None else _setting("DESI_TIMEOUT_SECONDS", 60))
        self.interval = max(0.0, float(interval if interval is not None
                                       else _setting("DESI_REQUEST_INTERVAL_SECONDS", 1.0)))
        self.session = session or requests.Session()
        self.requests = 0
        self._last = None

    def _wait(self):
        if self._last is not None and self.interval:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
        self._last = time.monotonic()
        self.requests += 1

    def query(self, adql: str) -> List[dict]:
        self._wait()
        data = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql}
        try:
            response = self.session.post(self.tap_url, data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DESIError("Data Lab: %s" % exc) from exc
        if response.status_code != 200:
            raise DESIError("Data Lab: HTTP %s" % response.status_code)
        text = response.text or ""
        if text.lstrip().startswith("<"):
            # TAP reports errors as a VOTable with QUERY_STATUS ERROR
            match = re.search(r'QUERY_STATUS"\s+value="ERROR"\s*>(.*?)</INFO>', text, re.S)
            raise DESIError("Data Lab: %s" % ((match.group(1).strip() if match else text[:300]) or "error"))
        return list(csv.DictReader(io.StringIO(text)))

    def redshifts_near(self, ra: float, dec: float, radius_arcsec: float) -> List[DESIMatch]:
        matches = []
        for row in self.query(cone_query(ra, dec, radius_arcsec)):
            mra, mdec, z = _float(row.get("mean_fiber_ra")), _float(row.get("mean_fiber_dec")), _float(row.get("z"))
            if mra is None or mdec is None or z is None or z <= 0:
                continue
            matches.append(DESIMatch(str(row.get("targetid") or ""), mra, mdec, z, _float(row.get("zerr")),
                                     _float(row.get("deltachi2")), _sep_arcsec(ra, dec, mra, mdec)))
        return sorted(matches, key=lambda m: m.sep_arcsec)


def default_client() -> DESIClient:
    return DESIClient()


def desi_redshifts_near(ra: float, dec: float, radius_arcsec: float, *, client=None) -> List[DESIMatch]:
    """Quality-cut DESI galaxy redshifts within ``radius_arcsec``, nearest first."""
    return (client or default_client()).redshifts_near(ra, dec, radius_arcsec)


def host_match(ra: float, dec: float, *, client=None, radius_arcsec: Optional[float] = None) -> Optional[DESIMatch]:
    """The nearest DESI galaxy redshift at a host position (``DESI_HOST_MATCH_ARCSEC``, 1.5")."""
    radius = float(radius_arcsec if radius_arcsec is not None else _setting("DESI_HOST_MATCH_ARCSEC", 1.5))
    matches = desi_redshifts_near(ra, dec, radius, client=client)
    return matches[0] if matches else None


def source_label() -> str:
    return desi_label()
