"""AstroScheduler night planner: a classical night's targets and server-side solves.

AstroScheduler (https://github.com/UCSC-Transients/AstroScheduler) is installed as a pinned
package (#653). This module turns YSE_PZ's models into its inputs:

* ``night_targets(night, user)`` -- the night's follow-ups that ``user`` may see, as
  AstroScheduler ``TargetSpec`` dicts plus the night's site, instrument and warnings (#655).
* ``solve_for_night(night, payload)`` -- one solve of the planner's ``/api/schedule``
  payload, with the date, site, instrument and window taken from the night (#656).

``astroscheduler`` is imported only inside the functions that solve: importing it sets
astropy's IERS options (no auto-download), so pages that never plan are unaffected.
Everything is behind ``settings.ASTROSCHEDULER_ENABLED`` (#654).
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import pytz
from django.conf import settings

logger = logging.getLogger(__name__)

# Bumped when a field of the targets response is removed or changes meaning.
TARGETS_SCHEMA_VERSION = 1

# Cap on targets per solve request, which bounds the solver's work per request.
MAX_SOLVE_TARGETS = 300

# ClassicalNightType names (lower-cased, without spaces) for half nights.
FIRST_HALF = {"firsthalf", "first half"}
SECOND_HALF = {"secondhalf", "second half"}

# Priority for a follow-up with no requests and no stored priority: the request default.
DEFAULT_PRIORITY = 4.0


def enabled() -> bool:
    """Whether the planner's pages and endpoints are on (``ASTROSCHEDULER_ENABLED``)."""
    return bool(getattr(settings, "ASTROSCHEDULER_ENABLED", False))


# ------------------------------------------------------------------ site and instrument


def _site_rules() -> List[Tuple[str, str, str]]:
    rules = []
    for item in (getattr(settings, "ASTROSCHEDULER_SITE_MAP", "") or "").split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 3 and all(parts):
            rules.append((parts[0].lower(), parts[1], parts[2]))
    return rules


def site_for_telescope(telescope) -> Tuple[str, str, bool]:
    """``(site_id, instrument, mapped)`` for a YSE_PZ telescope.

    The first ``ASTROSCHEDULER_SITE_MAP`` pattern found in the lower-cased telescope name
    wins. An unmatched telescope becomes a custom AstroScheduler site
    (``mapped=False``) with generic limits and the Kast exposure model.
    """
    name = " ".join((telescope.name or "").lower().split())
    for pattern, site_id, instrument in _site_rules():
        if pattern in name:
            return site_id, instrument, True
    return "yse-telescope-%d" % telescope.pk, "kast", False


def _timezone_name(observatory) -> Optional[str]:
    tz_name = ((observatory.tz_name if observatory else "") or "").strip()
    if not tz_name:
        return None
    try:
        pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        return None
    return tz_name


def observatory_payload(telescope) -> Dict[str, Any]:
    """AstroScheduler ``observatory`` dict: coordinates and time zone from YSE_PZ's models."""
    site_id, _instrument, mapped = site_for_telescope(telescope)
    payload = {
        "id": site_id,
        "name": telescope.name,
        "lat": float(telescope.latitude),
        "lon": float(telescope.longitude),
        "elevation": float(telescope.elevation),
    }
    tz_name = _timezone_name(telescope.observatory)
    if tz_name:
        payload["timezone"] = tz_name
    if not mapped:
        payload["telescope"] = {"name": telescope.name}
    return payload


def night_date(night) -> datetime.date:
    """The night's local evening date (``obs_date``'s calendar date, as the night pages use)."""
    obs_date = night.obs_date
    return obs_date.date() if isinstance(obs_date, datetime.datetime) else obs_date


def night_window(night, observatory: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(start, end)`` ISO UTC overrides for a half night, else ``(None, None)``.

    Half nights split at the midpoint between evening and morning 18-degree twilight, from
    the same twilight calculation the solver uses.
    """
    kind = " ".join((night.night_type.name or "").lower().split()) if night.night_type_id else ""
    if kind not in FIRST_HALF | SECOND_HALF:
        return None, None
    from astroscheduler.core import Observatory, fast_astrometry

    site = Observatory(observatory["name"], observatory["lat"], observatory["lon"],
                       observatory["elevation"], timezone=observatory.get("timezone") or "UTC")
    with fast_astrometry():
        solar = site.get_night_parameters(night_date(night))["solar_times"]
    evening, morning = solar["twilight_evening_18"], solar["twilight_morning_18"]
    middle = (evening + (morning - evening) / 2).replace(second=0, microsecond=0)
    if kind in FIRST_HALF:
        return None, middle.isoformat()
    return middle.isoformat(), None


# ------------------------------------------------------------------ targets


def sexagesimal(ra_deg: float, dec_deg: float) -> Tuple[str, str]:
    """``("HH:MM:SS.ss", "+DD:MM:SS.s")``. AstroScheduler reads a bare number below 24 as
    hours, so degrees are never sent as plain numbers."""
    total = round((float(ra_deg) % 360.0) / 15.0 * 3600.0, 2) % 86400.0
    hours, rest = divmod(total, 3600.0)
    minutes, seconds = divmod(rest, 60.0)
    ra = "%02d:%02d:%05.2f" % (hours, minutes, seconds)

    sign = "-" if dec_deg < 0 else "+"
    total = round(abs(float(dec_deg)) * 3600.0, 1)
    degrees, rest = divmod(total, 3600.0)
    minutes, seconds = divmod(rest, 60.0)
    dec = "%s%02d:%02d:%04.1f" % (sign, degrees, minutes, seconds)
    return ra, dec


def night_magnitudes(transient_ids, night) -> Dict[int, Dict[str, Any]]:
    """Magnitude per transient for the night (decision 4, 2026-10-15).

    The Bazin extrapolation to local midnight of the night when there is a usable fit,
    else the last detection. ``age_days`` is the time from the last detection to that
    midnight. Transients with neither are absent.
    """
    from YSE_App.models.phot_stat_models import TransientPhotStat
    from YSE_App.services.bazin import extrapolated_mags, local_midnight_mjd

    ids = sorted({int(t) for t in transient_ids})
    if not ids:
        return {}
    observatory = night.resource.telescope.observatory
    at_mjd = local_midnight_mjd(night.obs_date, observatory.utc_offset if observatory else 0)
    bazin = extrapolated_mags(ids, at_mjd)
    stats = {
        s.transient_id: s
        for s in TransientPhotStat.objects.filter(transient_id__in=ids).select_related("last_detected_band")
    }
    out: Dict[int, Dict[str, Any]] = {}
    for tid in ids:
        stat = stats.get(tid)
        last_mjd = stat.last_detected_mjd if stat else None
        age = round(at_mjd - last_mjd, 2) if last_mjd is not None else None
        if tid in bazin:
            mag, band = bazin[tid]
            out[tid] = {"magnitude": round(float(mag), 2), "band": band, "source": "bazin", "age_days": age}
        elif stat is not None and stat.last_detected_mag is not None:
            band = stat.last_detected_band.name if stat.last_detected_band_id else None
            out[tid] = {"magnitude": round(float(stat.last_detected_mag), 2), "band": band,
                        "source": "last_detection", "age_days": age}
    return out


def _offset_star(followup) -> Optional[Dict[str, float]]:
    if followup.offset_star_ra is None or followup.offset_star_dec is None:
        return None
    ra, dec = sexagesimal(followup.offset_star_ra, followup.offset_star_dec)
    return {
        "ra_deg": followup.offset_star_ra,
        "dec_deg": followup.offset_star_dec,
        "ra": ra,
        "dec": dec,
        "offset_north_arcsec": followup.offset_north,
        "offset_east_arcsec": followup.offset_east,
    }


def night_targets(night, user, url_for=None) -> Dict[str, Any]:
    """The night's follow-ups that ``user`` may see, in AstroScheduler's target schema.

    ``url_for(transient)`` gives each target's ``url`` (its YSE_PZ page, which the planner
    links target names to); without it, ``url`` is None.
    """
    from YSE_App.services.classical_nights import night_followups
    from YSE_App.services.followup_requests import effective_priority, format_comments, requests_prefetch

    telescope = night.resource.telescope
    observatory = observatory_payload(telescope)
    _site_id, instrument, mapped = site_for_telescope(telescope)
    followups = list(
        night_followups(night, user)
        .select_related("transient", "status")
        .prefetch_related(requests_prefetch())
    )
    mags = night_magnitudes([f.transient_id for f in followups], night)
    default_mag = float(getattr(settings, "ASTROSCHEDULER_DEFAULT_MAG", 19.0))
    stale_days = float(getattr(settings, "ASTROSCHEDULER_STALE_MAG_DAYS", 7))

    warnings: List[str] = []
    if not mapped:
        warnings.append(
            "Telescope %r is not in ASTROSCHEDULER_SITE_MAP: planned with generic pointing "
            "limits and the Kast exposure model." % telescope.name
        )

    targets = []
    for fu in followups:
        transient = fu.transient
        ra, dec = sexagesimal(transient.ra, transient.dec)
        mag = mags.get(transient.id)
        target_warnings = []
        if mag is None:
            mag = {"magnitude": default_mag, "band": None, "source": "none", "age_days": None}
            target_warnings.append("no photometry; magnitude %.1f assumed" % default_mag)
        elif mag["age_days"] is not None and mag["age_days"] > stale_days:
            target_warnings.append("last detection %.0f days before the night" % mag["age_days"])
        priority = effective_priority(fu)
        targets.append({
            "name": transient.name,
            "ra": ra,
            "dec": dec,
            "ra_deg": transient.ra,
            "dec_deg": transient.dec,
            "magnitude": mag["magnitude"],
            "mag_band": mag["band"],
            "mag_source": mag["source"],
            "mag_age_days": mag["age_days"],
            "priority": float(priority) if priority is not None else DEFAULT_PRIORITY,
            "comment": format_comments(fu),
            "external_id": str(fu.id),
            "followup_id": fu.id,
            "transient_id": transient.id,
            "followup_status": fu.status.name if fu.status_id else None,
            "valid_start": fu.valid_start.isoformat() if fu.valid_start else None,
            "valid_stop": fu.valid_stop.isoformat() if fu.valid_stop else None,
            "offset_star": _offset_star(fu),
            "url": url_for(transient) if url_for else None,
            "warnings": target_warnings,
        })
        warnings.extend("%s: %s" % (transient.name, w) for w in target_warnings)

    targets.sort(key=lambda t: (t["priority"], t["name"]))
    return {
        "schema_version": TARGETS_SCHEMA_VERSION,
        "night": {
            "id": night.id,
            "date": night_date(night).isoformat(),
            "telescope": telescope.name,
            "night_type": night.night_type.name if night.night_type_id else None,
        },
        "observatory": observatory,
        "instrument": instrument,
        "targets": targets,
        "warnings": warnings,
    }


def planner_nights(user, days_back: int = 7, days_ahead: int = 60, now=None) -> List[Dict[str, Any]]:
    """Classical nights ``user`` is authorised for, from ``days_back`` days ago to ``days_ahead``
    days ahead (#658): the same set as the home page's upcoming nights."""
    from django.utils import timezone

    from YSE_App.data import ObservingResourceService

    now = now or timezone.now()
    today = datetime.datetime.combine(now.date(), datetime.time(0, 0), tzinfo=datetime.timezone.utc)
    nights = (
        ObservingResourceService.GetAuthorizedClassicalObservingDate_ByUser(user)
        .filter(obs_date__gte=today - datetime.timedelta(days=days_back),
                obs_date__lt=today + datetime.timedelta(days=days_ahead + 1))
        .select_related("resource__telescope", "night_type")
        .order_by("obs_date", "resource__telescope__name", "pk")
    )
    out = []
    for night in nights:
        site_id, instrument, _mapped = site_for_telescope(night.resource.telescope)
        out.append({
            "id": night.id,
            "date": night_date(night).isoformat(),
            "telescope": night.resource.telescope.name,
            "night_type": night.night_type.name if night.night_type_id else None,
            "site": site_id,
            "instrument": instrument,
        })
    return out


def frame_config(night, user, *, api_base: str, csrf_token: str, url_for=None) -> Dict[str, Any]:
    """``window.ASTROSCHEDULER_CONFIG`` for the embedded planner of ``night`` (#659)."""
    data = night_targets(night, user, url_for=url_for)
    return {
        "mode": "embedded",
        "apiBase": api_base,
        "headers": {"X-CSRFToken": csrf_token},
        "site": data["observatory"],
        "initial": {
            "date": data["night"]["date"],
            "instrument": data["instrument"],
            "targets": data["targets"],
            "result": None,
        },
    }


# ------------------------------------------------------------------ solving


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a or not b:
        return a or b
    return max(a, b, key=lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00")))


def _earlier(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a or not b:
        return a or b
    return min(a, b, key=lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00")))


def build_solve_request(night, payload: Any):
    """An ``astroscheduler.api.SolveRequest`` from the planner's payload and the night.

    The night fixes the date, site, instrument and (for half nights) the window; from the
    payload only targets, standards options, real-time constraints, ``previous_schedule``,
    telescope-limit overrides and a narrower night window are used. Raises ValueError.
    """
    from astroscheduler.api import SolveRequest

    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    data = dict(payload)
    targets = data.get("targets") or []
    if not isinstance(targets, list):
        raise ValueError("'targets' must be a list")
    if len(targets) > MAX_SOLVE_TARGETS:
        raise ValueError("Too many targets (at most %d)" % MAX_SOLVE_TARGETS)

    telescope = night.resource.telescope
    observatory = observatory_payload(telescope)
    client_site = data.get("observatory") or {}
    overrides = client_site.get("telescope") if isinstance(client_site, dict) else None
    if isinstance(overrides, dict) and overrides:
        observatory["telescope"] = {**observatory.get("telescope", {}), **overrides}
    data["observatory"] = observatory
    data["date"] = night_date(night).isoformat()
    data["instrument"] = site_for_telescope(telescope)[1]

    start, end = night_window(night, observatory)
    data["night_start_override"] = _later(data.get("night_start_override") or None, start)
    data["night_end_override"] = _earlier(data.get("night_end_override") or None, end)
    return SolveRequest.from_dict(data)


def solve_for_night(night, payload: Any) -> Dict[str, Any]:
    """Solve the planner's payload for ``night``; the AstroScheduler result as JSON."""
    from astroscheduler.api import solve

    try:
        request = build_solve_request(night, payload)
    except (TypeError, KeyError) as exc:
        raise ValueError("Invalid planner request: %s" % exc) from exc
    started = time.monotonic()
    result = solve(request).to_dict()
    seconds = round(time.monotonic() - started, 2)
    logger.info("AstroScheduler solve: night %s, %d targets, %.2f s", night.id, len(request.targets), seconds)
    result["yse_pz"] = {"night_id": night.id, "solve_seconds": seconds}
    return result
