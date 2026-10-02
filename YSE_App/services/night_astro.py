"""Per-(telescope, UT date) astronomy that the observing pages used to recompute on every request.

The answers here (twilight times, the moon's position at 0h UT, the rise/set
time of a fixed target) do not change once a night is fixed, so they are kept
in the Django cache and only solved on a miss. Every helper formats its result
exactly as the views and tables did when they called astroplan inline.
"""

import astropy.units as u
from astroplan import Observer, moon_illumination
from astropy.coordinates import EarthLocation, get_moon
from astropy.time import Time
from django.core.cache import cache

from YSE_App.common.utilities import date_to_mjd

# A night's geometry never changes; the timeout only bounds cache growth.
NIGHT_CACHE_TIMEOUT = 7 * 24 * 3600


def observer_for(telescope):
    """astroplan Observer at the telescope's site (UTC)."""
    location = EarthLocation.from_geodetic(
        telescope.longitude * u.deg, telescope.latitude * u.deg, telescope.elevation * u.m
    )
    return Observer(location=location, timezone="UTC")


def _site_key(telescope):
    # Include the coordinates so an edited telescope row never serves stale times.
    return '%s_%s_%s_%s' % (telescope.pk, telescope.longitude, telescope.latitude, telescope.elevation)


def _hms(t):
    # The views' historical expression: 'HH:MM' from 'YYYY-MM-DDTHH:MM:SS.sss'.
    return t.isot.split('T')[-1][:-7]


def twilight_times(telescope, ut_obs_date, which="next"):
    """
    Sunset, twilights, sunrise and moon illumination for the night around ``ut_obs_date``.

    ``ut_obs_date`` is the ISO string the views build (``'YYYY-MM-DD 00:00:00'`` with
    ``which="next"`` for the YSE pages, the night's full timestamp with
    ``which="previous"`` for a classical observing night). Returns a dict of the
    formatted strings the templates show plus ``sunset_mjd`` / ``sunrise_mjd`` (the
    ``date_to_mjd`` values the survey-observation windows use). Six astroplan
    root-finds on a miss, a cache read afterwards.
    """
    key = 'twilight_%s_%s_%s' % (_site_key(telescope), ut_obs_date.replace(' ', 'T'), which)
    result = cache.get(key)
    if result is None:
        time = Time(ut_obs_date, format='iso')
        tel = observer_for(telescope)
        sunset = tel.sun_set_time(time, which=which)
        sunrise = tel.sun_rise_time(time, which=which)
        result = {
            'sunset': _hms(sunset),
            'night_start_12': _hms(tel.twilight_evening_nautical(time, which=which)),
            'night_start_18': _hms(tel.twilight_evening_astronomical(time, which=which)),
            'night_end_18': _hms(tel.twilight_morning_astronomical(time, which=which)),
            'night_end_12': _hms(tel.twilight_morning_nautical(time, which=which)),
            'sunrise': _hms(sunrise),
            'moon_illum': '%.3f' % moon_illumination(time),
            'sunset_mjd': float(date_to_mjd(sunset)),
            'sunrise_mjd': float(date_to_mjd(sunrise)),
        }
        cache.set(key, result, timeout=NIGHT_CACHE_TIMEOUT)
    return result


def moon_position(tme):
    """Geocentric moon SkyCoord at ``tme`` (an astropy Time), cached per instant."""
    key = 'moon_%s' % tme.isot
    moon = cache.get(key)
    if moon is None:
        moon = get_moon(tme)
        cache.set(key, moon, timeout=NIGHT_CACHE_TIMEOUT)
    return moon


def rise_set_cache_key(telescope, date_str, horizon_deg):
    return 'rise_set_%s_%s_%s' % (_site_key(telescope), date_str, horizon_deg)


def cached_rise_set(key):
    """{(ra_str, dec_str): (rise, set, moon_angle)} solved earlier for this telescope and night."""
    return dict(cache.get(key) or {})


def store_rise_set(key, rise_set):
    cache.set(key, dict(rise_set), timeout=NIGHT_CACHE_TIMEOUT)
