"""NED host redshifts through the current NED::API (#436).

NED's legacy interfaces (including ``astroquery.ned``) cannot see data ingested after
January 2026, so this client talks to ``https://ned.ipac.caltech.edu/NED::API/`` with
plain HTTP GET requests. Responses are VOTable 1.5 (TABLEDATA), parsed here with the
standard library so no new package is needed.

* :class:`NEDClient` -- ``ConeSearchByPosition`` (spec-z galaxies near a position) and
  ``RedshiftsOfObject`` (the uncertainty of the fiducial redshift), with a timeout and
  a polite interval between requests (``[redshifts] NED_REQUEST_INTERVAL_SECONDS``).
* :func:`host_match` -- the NED galaxy at a known host position (nearest spec-z galaxy
  within ``NED_HOST_MATCH_ARCSEC``); used by the host-redshift job.
* :func:`select_host` -- the old ``processTNS.getNEDData`` choice for a transient with
  no host (galaxies with a spec-z, smallest separation x z, within 100 kpc); used by the
  GW candidate script.

Coordinates are sent as decimal degrees with a ``d`` suffix: without it NED reads an
equatorial longitude as hours.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from django.conf import settings

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://ned.ipac.caltech.edu/NED::API"


class NEDError(Exception):
    """NED could not be queried (transport error, HTTP error, or a QUERY_STATUS of ERROR)."""


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


# --- VOTable -------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_votable(text) -> List[Dict[str, Optional[str]]]:
    """Rows of the first TABLE of a TABLEDATA VOTable, keyed by FIELD ``ID`` (and ``name``).

    Raises :class:`NEDError` when the response reports ``QUERY_STATUS`` ERROR.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NEDError("unreadable VOTable: %s" % exc) from exc
    for el in root.iter():
        if _local(el.tag) in ("INFO", "PARAM") and el.get("name") == "QUERY_STATUS":
            if (el.get("value") or "").upper() == "ERROR":
                raise NEDError(((el.text or "") + " " + (el.get("content") or "")).strip() or "QUERY_STATUS ERROR")
    table = next((el for el in root.iter() if _local(el.tag) == "TABLE"), None)
    if table is None:
        return []
    fields = [el for el in table if _local(el.tag) == "FIELD"]
    keys = [(f.get("ID") or f.get("name") or "", f.get("name") or f.get("ID") or "") for f in fields]
    rows = []
    for tr in table.iter():
        if _local(tr.tag) != "TR":
            continue
        cells = [td.text for td in tr if _local(td.tag) == "TD"]
        row = {}
        for (key, name), value in zip(keys, cells):
            value = value.strip() if isinstance(value, str) else None
            row[key] = value if value != "" else None
            row.setdefault(name, row[key])
        rows.append(row)
    return rows


def _float(value) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# --- objects -------------------------------------------------------------------

@dataclass
class NEDObject:
    name: str
    ra: Optional[float]
    dec: Optional[float]
    z: Optional[float]
    zflag: str
    ptype: str
    sep_arcmin: Optional[float]

    @property
    def is_galaxy(self) -> bool:
        return self.ptype.strip().upper() == "G"

    @property
    def is_spectroscopic(self) -> bool:
        """``z flag``: technique S (spectroscopic), and no "?" (uncertain) quality mark."""
        flag = self.zflag or ""
        return flag[:1].upper() == "S" and "?" not in flag

    @property
    def usable(self) -> bool:
        return self.is_galaxy and self.is_spectroscopic and self.z is not None and self.z > 0

    @classmethod
    def from_row(cls, row: dict) -> "NEDObject":
        return cls(
            name=(row.get("prefname") or row.get("Object Name") or "").strip(),
            ra=_float(row.get("ra") or row.get("RA")),
            dec=_float(row.get("dec") or row.get("Dec")),
            z=_float(row.get("z") or row.get("Redshift (z)")),
            zflag=(row.get("zflag") or row.get("z flag") or "").strip(),
            ptype=(row.get("ptype") or row.get("Phys Type") or "").strip(),
            sep_arcmin=_float(row.get("sep") or row.get("Separation")),
        )


@dataclass
class NEDMatch:
    name: str
    ra: Optional[float]
    dec: Optional[float]
    z: float
    z_err: Optional[float]
    sep_arcsec: Optional[float]

    def as_dict(self) -> dict:
        return {"name": self.name, "ra": self.ra, "dec": self.dec, "z": self.z, "z_err": self.z_err,
                "sep_arcsec": self.sep_arcsec}


# --- client --------------------------------------------------------------------

class NEDClient:
    """A small NED::API client (GET, VOTable). Pass ``session`` to stub HTTP in tests."""

    def __init__(self, base_url: Optional[str] = None, *, timeout: Optional[float] = None,
                 interval: Optional[float] = None, session=None):
        self.base_url = (base_url or _setting("NED_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.timeout = float(timeout if timeout is not None else _setting("NED_TIMEOUT_SECONDS", 30))
        self.interval = max(0.0, float(interval if interval is not None
                                       else _setting("NED_REQUEST_INTERVAL_SECONDS", 1.0)))
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

    def _get(self, service: str, params: dict) -> List[dict]:
        self._wait()
        url = "%s/%s" % (self.base_url, service)
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NEDError("%s: %s" % (service, exc)) from exc
        if response.status_code != 200:
            raise NEDError("%s: HTTP %s" % (service, response.status_code))
        return parse_votable(response.content)

    def cone_search(self, ra: float, dec: float, radius_arcmin: float, *, maxrec: int = 200) -> List[NEDObject]:
        """Objects with a redshift within ``radius_arcmin`` of (ra, dec), in decimal degrees."""
        params = {"LON": "%.7fd" % ra, "LAT": "%.7fd" % dec, "CSYS": "Equatorial", "EQUINOX": "J2000.0",
                  "RADIUS": "%.6f" % radius_arcmin, "Z_CONSTRAINT": "Available", "MAXREC": int(maxrec)}
        return [NEDObject.from_row(r) for r in self._get("ConeSearchByPosition", params)]

    def redshifts_of_object(self, name: str, *, maxrec: int = 100) -> List[dict]:
        return self._get("RedshiftsOfObject", {"TARGET": name, "MAXREC": int(maxrec)})

    def redshift_error(self, name: str, z: float) -> Optional[float]:
        """The uncertainty of the fiducial redshift of ``name`` (or of the row matching ``z``)."""
        rows = self.redshifts_of_object(name)
        fiducial = [r for r in rows if (r.get("fiducial") or r.get("Fiducial") or "").lower() in ("true", "t", "1")]
        match = [r for r in rows if _float(r.get("z") or r.get("Redshift (z)")) is not None
                 and abs(_float(r.get("z") or r.get("Redshift (z)")) - z) < 1e-6]
        for row in fiducial + match:
            err = _float(row.get("unc_z") or row.get("Redshift(z) Unc"))
            if err is not None:
                return err
        return None


def default_client() -> NEDClient:
    return NEDClient()


# --- selection -----------------------------------------------------------------

def host_match(ra: float, dec: float, *, client: Optional[NEDClient] = None,
               radius_arcsec: Optional[float] = None, with_error: bool = True) -> Optional[NEDMatch]:
    """The nearest NED galaxy with a reliable spec-z within ``radius_arcsec`` of a host position."""
    client = client or default_client()
    radius = float(radius_arcsec if radius_arcsec is not None else _setting("NED_HOST_MATCH_ARCSEC", 3.0))
    objects = [o for o in client.cone_search(ra, dec, radius / 60.0) if o.usable]
    if not objects:
        return None
    best = min(objects, key=lambda o: o.sep_arcmin if o.sep_arcmin is not None else 1e9)
    err = None
    if with_error and best.name:
        try:
            err = client.redshift_error(best.name, best.z)
        except NEDError as exc:
            log.info("NED: no redshift uncertainty for %s (%s)", best.name, exc)
    sep = best.sep_arcmin * 60.0 if best.sep_arcmin is not None else None
    return NEDMatch(best.name, best.ra, best.dec, best.z, err, sep)


def select_host(objects: List[NEDObject], *, max_kpc: float = 100.0) -> Optional[NEDObject]:
    """``getNEDData``'s choice: the spec-z galaxy with the smallest separation x z, if it lies
    within ``max_kpc`` (projected, flat LCDM H0=70, Om=0.3) of the search centre."""
    galaxies = [o for o in objects if o.usable and o.sep_arcmin is not None]
    if not galaxies:
        return None
    best = min(galaxies, key=lambda o: o.sep_arcmin * abs(o.z))
    from astropy.cosmology import FlatLambdaCDM

    kpc = best.sep_arcmin * 60.0 / FlatLambdaCDM(70, 0.3).arcsec_per_kpc_proper(best.z).value
    return best if kpc < max_kpc else None


def transient_host(ra: float, dec: float, *, client: Optional[NEDClient] = None,
                   radius_arcmin: float = 5.0) -> Optional[NEDObject]:
    """The likely NED host of a transient at (ra, dec) by :func:`select_host`, or None."""
    client = client or default_client()
    return select_host(client.cone_search(ra, dec, radius_arcmin))
