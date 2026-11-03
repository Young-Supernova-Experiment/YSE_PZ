""""Observable tonight" for a shortlist (#854): when each object is up from one site.

This is the last step on a list someone already narrowed (the follow-up list,
a dashboard tab, a filtered search), never a query over the whole database:
callers hand in at most :data:`MAX_OBJECTS` objects and get one answer per
object. Nothing is hidden or ranked; each row only says whether it is up.

For one site and night the sun's twilight times are solved once, the dark
window (astronomical twilight to astronomical twilight) is sampled on a fixed
grid, and the altitude of every object at every sample comes from one
vectorised astropy transform (objects x times). Per object it reports:

* the UTC window in which it is above the airmass limit (default 2) during
  astronomical night, as ``YYYYMMDD.ddd`` (``YSE_App.common.utc_dates``);
* the minimum airmass during the dark window;
* the mean moon distance during the dark window.

The night, the airmass formula (Kasten & Young 1989) and the IERS handling
(``iers_quiet``: no downloads, no range errors) are the observability page's
(#307, ``services/observability.py``). The answer for a (site, night, object
set, limit) is cached in the Django cache for :data:`CACHE_TIMEOUT`; the key
holds the coordinates, so an edited object or telescope never serves a stale
night.
"""

from __future__ import annotations

import datetime
import hashlib
import warnings
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from django.core.cache import cache

from YSE_App.common.utc_dates import mjd_string, utc_day, utc_night
from YSE_App.services.observability import _event, default_night_date, iers_quiet

# Bump when the payload shape or the math changes so stale entries are skipped.
CACHE_VERSION = 1
CACHE_TIMEOUT = 3600
# Never more objects than this per request: it is a filter for shortlists.
MAX_OBJECTS = 500
DEFAULT_AIRMASS = 2.0
MIN_AIRMASS_LIMIT = 1.0
MAX_AIRMASS_LIMIT = 4.0
# Samples across the dark window (about every 5 minutes on a 10 hour night).
N_SAMPLES = 121


class ShortlistTooLong(ValueError):
    """More objects than :data:`MAX_OBJECTS`."""


def airmass_from_alt(alt_deg):
    """Kasten & Young (1989) airmass; NaN at or below the horizon. Same formula as #307."""
    alt = np.asarray(alt_deg, dtype=float)
    out = np.full(alt.shape, np.nan)
    up = alt > 0
    z = 90.0 - alt[up]
    out[up] = 1.0 / (np.cos(np.radians(z)) + 0.50572 * (96.07995 - z) ** -1.6364)
    return out


def _angular_distance(alt1, az1, alt2, az2):
    """Great-circle distance in degrees between (alt, az) pairs (broadcasting)."""
    a1, a2 = np.radians(alt1), np.radians(alt2)
    daz = np.radians(np.asarray(az1) - np.asarray(az2))
    cos_d = np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(daz)
    return np.degrees(np.arccos(np.clip(cos_d, -1.0, 1.0)))


def cache_key(targets: Sequence[Tuple[int, float, float]], telescope, night: datetime.date,
              airmass_limit: float) -> str:
    """One key per (site, night, object set, limit); the coordinates are part of the hash."""
    digest = hashlib.sha1()
    for pk, ra, dec in sorted(targets):
        digest.update(("%s:%.6f:%.6f;" % (pk, ra, dec)).encode())
    return "obs_tonight_v%d_%s_%s_%s_%s_%s_%.2f_%s" % (
        CACHE_VERSION, telescope.pk, telescope.longitude, telescope.latitude, telescope.elevation,
        night.isoformat(), airmass_limit, digest.hexdigest(),
    )


def _dark_window(observer, telescope, night, u, Time):
    """(start, end, note) of astronomical night for the evening of ``night`` at the site.

    Falls back to nautical night, then sunset-sunrise, then local 18h-06h (polar
    sites), and says so in ``note``. The night is the observability page's: the
    evening falls on ``night`` in the site's local (longitude) time.
    """
    lon_hours = float(telescope.longitude) / 15.0
    local_noon = Time("%sT12:00:00" % night.isoformat(), format="isot", scale="utc") - lon_hours * u.hour
    for name, alt in (("astronomical", -18.0), ("nautical", -12.0), ("sunset", -0.833)):
        evening = _event(observer, "sun_set_time", local_noon, horizon=alt * u.deg)
        morning = _event(observer, "sun_rise_time", evening, horizon=alt * u.deg) if evening is not None else None
        if evening is not None and morning is not None and (morning - evening).to_value(u.hour) < 20:
            note = None if name == "astronomical" else "no astronomical night at this site on this date; using %s" % (
                "nautical twilight" if name == "nautical" else "sunset to sunrise")
            return evening, morning, note
    return (local_noon + 6 * u.hour, local_noon + 18 * u.hour,
            "the sun does not set and rise normally at this site on this date; using local 18h to 06h")


def _when(t) -> Dict[str, str]:
    dt = t.utc.to_datetime().replace(tzinfo=datetime.timezone.utc)
    return {"utc": utc_day(dt), "mjd": mjd_string(dt), "iso": t.utc.isot[:16]}


def compute_visibility(targets: Sequence[Tuple[int, float, float]], telescope, night: datetime.date,
                       airmass_limit: float = DEFAULT_AIRMASS) -> Dict:
    """Visibility of every ``(pk, ra, dec)`` in ``targets`` from ``telescope`` on ``night``.

    One altitude transform for all objects and samples; see the module docstring.
    """
    import astropy.units as u
    from astropy.coordinates import AltAz, SkyCoord, get_body
    from astropy.time import Time

    from YSE_App.services.night_astro import observer_for

    if len(targets) > MAX_OBJECTS:
        raise ShortlistTooLong("at most %d objects" % MAX_OBJECTS)

    with iers_quiet(), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        observer = observer_for(telescope)
        start, end, note = _dark_window(observer, telescope, night, u, Time)
        times = Time(np.linspace(start.jd, end.jd, N_SAMPLES), format="jd", scale="utc")
        step_hours = float((end - start).to_value(u.hour)) / (N_SAMPLES - 1)
        frame = AltAz(obstime=times, location=observer.location)
        moon = get_body("moon", times, observer.location).transform_to(frame)
        moon_alt = np.asarray(moon.alt.deg, dtype=float)
        moon_az = np.asarray(moon.az.deg, dtype=float)
        results = {}
        if targets:
            ra = np.array([float(t[1]) for t in targets])
            dec = np.array([float(t[2]) for t in targets])
            coords = SkyCoord(ra * u.deg, dec * u.deg).reshape((len(targets), 1))
            altaz = coords.transform_to(frame)  # (objects, samples)
            alt = np.asarray(altaz.alt.deg, dtype=float)
            az = np.asarray(altaz.az.deg, dtype=float)
            airmass = airmass_from_alt(alt)
            moon_sep = _angular_distance(alt, az, moon_alt[np.newaxis, :], moon_az[np.newaxis, :])
            ok = np.isfinite(airmass) & (airmass <= airmass_limit)
            for row, (pk, _, _) in enumerate(targets):
                finite = airmass[row][np.isfinite(airmass[row])]
                above = np.where(ok[row])[0]
                entry = {
                    "up": bool(len(above)),
                    "start": None,
                    "end": None,
                    "hours": round(float(len(above) * step_hours), 1) if len(above) else 0.0,
                    "min_airmass": round(float(finite.min()), 2) if finite.size else None,
                    "moon_sep": int(round(float(moon_sep[row].mean()))),
                }
                if len(above):
                    entry["start"] = _when(times[above[0]])
                    entry["end"] = _when(times[above[-1]])
                results[str(pk)] = entry
    return {
        "version": CACHE_VERSION,
        "night": utc_night(night),
        "airmass_limit": airmass_limit,
        "dark_start": _when(start),
        "dark_end": _when(end),
        "note": note,
        "telescope": {"id": telescope.pk, "name": telescope.name},
        "results": results,
    }


def shortlist_visibility(targets: Sequence[Tuple[int, float, float]], telescope,
                         night: Optional[datetime.date] = None,
                         airmass_limit: float = DEFAULT_AIRMASS, use_cache: bool = True) -> Dict:
    """Cached :func:`compute_visibility` (key: site, night, object set, limit)."""
    night = night or default_night_date()
    if len(targets) > MAX_OBJECTS:
        raise ShortlistTooLong("at most %d objects" % MAX_OBJECTS)
    key = cache_key(targets, telescope, night, airmass_limit)
    if use_cache:
        payload = cache.get(key)
        if payload is not None:
            return payload
    payload = compute_visibility(targets, telescope, night, airmass_limit)
    if use_cache:
        cache.set(key, payload, timeout=CACHE_TIMEOUT)
    return payload


def parse_ids(values: Iterable[str]) -> List[int]:
    """Transient ids from ``?ids=1,2,3`` (or repeated ``ids``); order kept, duplicates dropped."""
    seen, out = set(), []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            pk = int(part)  # ValueError for junk; the view turns it into a 400
            if pk not in seen:
                seen.add(pk)
                out.append(pk)
    return out
