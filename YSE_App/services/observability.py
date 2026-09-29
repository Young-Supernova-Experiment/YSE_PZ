"""Per-(transient, telescope, night) ephemeris for the observability page (#307).

For one night at one telescope the service samples the target's altitude,
airmass, moon separation and the moon's altitude on a fixed grid between
sunset and sunrise, and reduces those samples to the numbers the summary
table shows (dark hours, hours above airmass 2, best altitude, transit,
rise/set above 30 degrees). The answer for a night never changes, so it is
kept in the Django cache (Redis on the deployed stacks) under a versioned
key for :data:`EPHEMERIS_CACHE_TIMEOUT`; a repeated view costs one cache
read per telescope instead of the astroplan root-finds.

Nothing here touches the ORM beyond reading the rows it is handed, so the
math can be exercised on plain objects with ``pk``/``name``/``latitude``/
``longitude``/``elevation`` attributes.
"""

from __future__ import annotations

import contextlib
import datetime
import math
import warnings
from typing import Dict, List, Optional

import numpy as np
from django.core.cache import cache
from django.db.models import Q

from YSE_App.services.night_astro import observer_for

# Bump when the payload shape or the math changes so stale entries are skipped.
EPHEMERIS_CACHE_VERSION = 1
EPHEMERIS_CACHE_TIMEOUT = 3600  # one hour, per the roadmap
# Samples across the night (sunset - 30 min to sunrise + 30 min): every ~7 minutes.
N_SAMPLES = 121
# Altitude counted as "observable": airmass 2 is 30 degrees.
OBSERVABLE_ALT_DEG = 30.0
# Sun altitudes (degrees) of the twilight boundaries reported.
TWILIGHTS = (("civil", -6.0), ("nautical", -12.0), ("astronomical", -18.0))


def default_night_date(now: Optional[datetime.datetime] = None) -> datetime.date:
    """The evening date most users mean by "tonight": UT now minus 12 hours.

    Before local noon that is last night (still ending at western sites), after
    it the night about to start; it keeps every longitude on the same calendar
    night through the observing hours.
    """
    now = now or datetime.datetime.utcnow()
    return (now - datetime.timedelta(hours=12)).date()


def parse_night_date(value: Optional[str]) -> datetime.date:
    """``YYYY-MM-DD`` from the query string, or the default night; ValueError otherwise."""
    if not value:
        return default_night_date()
    return datetime.date.fromisoformat(value.strip())


def telescopes_with_coordinates(queryset=None):
    """Telescopes that can be placed on the Earth (rows with all three coordinates)."""
    from YSE_App.models import Telescope

    qs = queryset if queryset is not None else Telescope.objects.all()
    return (
        qs.filter(latitude__isnull=False, longitude__isnull=False, elevation__isnull=False)
        .select_related("observatory")
        .order_by("name")
    )


def telescope_ids_for_user(user) -> set:
    """Telescopes behind an allocation or observing resource the user's groups can see.

    Rows without groups are public, as everywhere else in the app.
    """
    from YSE_App.models import Allocation, ClassicalResource, QueuedResource, ToOResource

    if not user.is_authenticated:
        return set()
    names = list(user.groups.values_list("name", flat=True))
    visible = Q(groups__isnull=True) | Q(groups__name__in=names)
    ids = set(
        Allocation.objects.filter(is_active=True).filter(visible).values_list("telescope_id", flat=True)
    )
    for model in (ClassicalResource, ToOResource, QueuedResource):
        ids.update(model.objects.filter(visible).values_list("telescope_id", flat=True))
    return ids


def telescopes_for_user(user, mine=False):
    """All telescopes with coordinates, or only the user's (``mine``) when that leaves any."""
    telescopes = telescopes_with_coordinates()
    if mine:
        ids = telescope_ids_for_user(user)
        if ids:
            telescopes = telescopes.filter(pk__in=ids)
    return telescopes


def ephemeris_cache_key(transient, telescope, night: datetime.date) -> str:
    return "observability_v%d_%s_%.6f_%.6f_%s_%s_%s_%s_%s" % (
        EPHEMERIS_CACHE_VERSION,
        transient.pk,
        float(transient.ra),
        float(transient.dec),
        telescope.pk,
        telescope.longitude,
        telescope.latitude,
        telescope.elevation,
        night.isoformat(),
    )


def night_ephemeris(transient, telescope, night: datetime.date, use_cache: bool = True) -> Dict:
    """The cached (or freshly computed) ephemeris payload for one telescope and night."""
    key = ephemeris_cache_key(transient, telescope, night)
    if use_cache:
        payload = cache.get(key)
        if payload is not None:
            return payload
    payload = compute_night_ephemeris(transient, telescope, night)
    if use_cache:
        cache.set(key, payload, timeout=EPHEMERIS_CACHE_TIMEOUT)
    return payload


def _iso(t) -> Optional[str]:
    """``YYYY-MM-DDTHH:MM`` UT for a finite astropy Time, else None."""
    if t is None:
        return None
    try:
        if getattr(t, "mask", False) or not np.isfinite(t.jd):
            return None
    except (TypeError, ValueError):
        return None
    return t.utc.isot[:16]


def _event(observer, method, start, **kwargs):
    """One astroplan rise/set root-find, or None when the event does not happen."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            t = getattr(observer, method)(start, which="next", **kwargs)
        except Exception:
            return None
    try:
        if getattr(t, "mask", False) or not np.isfinite(t.jd):
            return None
    except (TypeError, ValueError):
        return None
    return t


@contextlib.contextmanager
def iers_quiet():
    """astropy without IERS downloads or range errors for the duration of the block.

    The Earth-rotation table only refines UT1 by milliseconds, which does not
    matter for an altitude curve, and the web workers must not wait on a download.
    """
    from astropy.utils import iers

    with contextlib.ExitStack() as stack:
        stack.enter_context(iers.conf.set_temp("auto_download", False))
        # astropy >= 5.1 raises on times outside the bundled table unless told
        # otherwise; the deployed image pins 5.0, which only warns.
        if hasattr(iers.conf, "iers_degraded_accuracy"):
            stack.enter_context(iers.conf.set_temp("iers_degraded_accuracy", "ignore"))
        yield


def compute_night_ephemeris(transient, telescope, night: datetime.date) -> Dict:
    """Sample the night at ``telescope`` for ``transient``; see the module docstring.

    The night is the one whose evening falls on ``night`` in the telescope's
    local (longitude) time: sunset after local noon of that date, sunrise after
    that sunset. Where the sun never sets or never rises (polar sites) the window
    falls back to local 18h-06h and the payload says so in ``night.note``.
    """
    import astropy.units as u
    from astroplan import moon_illumination
    from astropy.coordinates import SkyCoord, get_moon
    from astropy.time import Time

    with iers_quiet():
        return _compute_night_ephemeris(transient, telescope, night, u, moon_illumination, SkyCoord, get_moon, Time)


def _compute_night_ephemeris(transient, telescope, night, u, moon_illumination, SkyCoord, get_moon, Time):
    observer = observer_for(telescope)
    target = SkyCoord(float(transient.ra), float(transient.dec), unit=u.deg)
    lon_hours = float(telescope.longitude) / 15.0
    local_noon = Time("%sT12:00:00" % night.isoformat(), format="isot", scale="utc") - lon_hours * u.hour

    note = None
    sunset = _event(observer, "sun_set_time", local_noon)
    sunrise = _event(observer, "sun_rise_time", sunset) if sunset is not None else None
    if sunset is None or sunrise is None or (sunrise - sunset).to_value(u.hour) > 20:
        note = "the sun does not set and rise normally at this site on this date; showing local 18h to 06h"
        sunset = local_noon + 6 * u.hour
        sunrise = local_noon + 18 * u.hour

    twilight = {}
    for name, alt in TWILIGHTS:
        evening = _event(observer, "sun_set_time", local_noon, horizon=alt * u.deg)
        morning = _event(observer, "sun_rise_time", sunset, horizon=alt * u.deg)
        twilight["evening_%s" % name] = evening
        twilight["morning_%s" % name] = morning

    start = sunset - 0.5 * u.hour
    end = sunrise + 0.5 * u.hour
    times = Time(np.linspace(start.jd, end.jd, N_SAMPLES), format="jd", scale="utc")
    step_hours = float((end - start).to_value(u.hour)) / (N_SAMPLES - 1)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        altaz = observer.altaz(times, target)
        alt = np.asarray(altaz.alt.deg, dtype=float)
        az = np.asarray(altaz.az.deg, dtype=float)
        moon = get_moon(times, observer.location)
        moon_sep = np.asarray(moon.separation(target).deg, dtype=float)
        moon_alt = np.asarray(observer.altaz(times, moon).alt.deg, dtype=float)
        sun_alt = np.asarray(observer.sun_altaz(times).alt.deg, dtype=float)
        midnight = sunset + (sunrise - sunset) / 2
        illum = float(moon_illumination(midnight))

    airmass = np.full_like(alt, np.nan)
    up = alt > 0
    # Kasten & Young (1989) airmass; the plain secant blows up at the horizon.
    z = np.radians(90.0 - alt[up])
    airmass[up] = 1.0 / (np.cos(z) + 0.50572 * (96.07995 - np.degrees(z)) ** -1.6364)

    # "Dark" is between the astronomical twilights when they exist, else the
    # nautical ones, else sunset to sunrise.
    dark_start, dark_end = sunset, sunrise
    for name in ("astronomical", "nautical"):
        e, m = twilight["evening_%s" % name], twilight["morning_%s" % name]
        if e is not None and m is not None:
            dark_start, dark_end = e, m
            break
    in_dark = (times.jd >= dark_start.jd) & (times.jd <= dark_end.jd)
    observable = in_dark & (alt >= OBSERVABLE_ALT_DEG)
    hours_dark = float((dark_end - dark_start).to_value(u.hour))
    hours_observable = float(observable.sum() * step_hours)
    hours_up_dark = float((in_dark & (alt > 0)).sum() * step_hours)

    best = int(np.nanargmax(alt))
    max_alt = float(alt[best])
    summary = {
        "hours_dark": round(hours_dark, 2),
        "hours_observable": round(min(hours_observable, hours_dark), 2),
        "hours_up_dark": round(min(hours_up_dark, hours_dark), 2),
        "max_alt": round(max_alt, 1),
        "min_airmass": round(float(airmass[best]), 2) if max_alt > 0 else None,
        "transit": _iso(times[best]) if max_alt > 0 else None,
        "rise_30": None,
        "set_30": None,
        # True when the target is already above 30 deg at the first sample / still
        # above it at the last one, i.e. the crossing happens outside the window.
        "rise_30_before_window": False,
        "set_30_after_window": False,
        "moon_sep": round(float(np.nanmean(moon_sep[in_dark])) if in_dark.any() else float(moon_sep.mean()), 1),
        "moon_illum": round(illum, 2),
        "moon_up_hours": round(float((in_dark & (moon_alt > 0)).sum() * step_hours), 2),
        "never_rises": bool(max_alt <= 0),
        "always_observable": bool(observable.sum() == in_dark.sum() and in_dark.any()),
    }
    above = np.where(alt >= OBSERVABLE_ALT_DEG)[0]
    if len(above):
        summary["rise_30"] = _iso(times[above[0]])
        summary["set_30"] = _iso(times[above[-1]])
        summary["rise_30_before_window"] = bool(above[0] == 0)
        summary["set_30_after_window"] = bool(above[-1] == N_SAMPLES - 1)

    def _list(values, digits):
        return [None if not math.isfinite(v) else round(float(v), digits) for v in values]

    return {
        "version": EPHEMERIS_CACHE_VERSION,
        "telescope": {
            "id": telescope.pk,
            "name": telescope.name,
            "observatory": getattr(getattr(telescope, "observatory", None), "name", "") or "",
            "latitude": float(telescope.latitude),
            "longitude": float(telescope.longitude),
            "elevation": float(telescope.elevation),
        },
        "transient": {"id": transient.pk, "name": transient.name, "ra": float(transient.ra), "dec": float(transient.dec)},
        "night": {
            "date": night.isoformat(),
            "sunset": _iso(sunset),
            "sunrise": _iso(sunrise),
            "twilight": {k: _iso(v) for k, v in twilight.items()},
            "dark_start": _iso(dark_start),
            "dark_end": _iso(dark_end),
            "utc_offset_hours": round(lon_hours, 2),
            "note": note,
        },
        "samples": {
            "t": [t[:16] for t in times.utc.isot],
            "alt": _list(alt, 2),
            "az": _list(az, 1),
            "airmass": _list(airmass, 3),
            "moon_alt": _list(moon_alt, 2),
            "moon_sep": _list(moon_sep, 1),
            "sun_alt": _list(sun_alt, 2),
        },
        "summary": summary,
    }


def summary_rows(payloads: List[Dict]) -> List[Dict]:
    """Table rows (one per telescope) sorted by hours observable, best first."""
    rows = []
    for p in payloads:
        row = dict(p["summary"])
        row["telescope"] = p["telescope"]
        row["night"] = p["night"]
        rows.append(row)
    rows.sort(key=lambda r: (-r["hours_observable"], -(r["max_alt"] or -90), r["telescope"]["name"]))
    return rows
