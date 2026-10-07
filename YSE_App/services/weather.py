"""Per-telescope weather snapshots and the SkyCam widget (#309: #311).

A :class:`Telescope` row may name a ``weather_url`` (a JSON endpoint; Open-Meteo
``/v1/forecast?...&current=...`` and OpenWeatherMap ``/data/2.5/weather`` shapes
are understood, anything else is kept as flat key/value pairs), a
``weather_link`` (the site's own weather page) and a ``skycam_url`` (an all-sky
camera image). :func:`get_weather` returns the cached snapshot in
``Telescope.weather`` when it is younger than ``WEATHER_CACHE_MINUTES``, and
otherwise fetches it; every fetch is an :class:`ExternalServiceRun` on the
``weather`` service. The ``weather.refresh`` job (:func:`refresh_job`) refreshes
every configured telescope; the ``WeatherRefresh`` cron queues it when
``WEATHER_REFRESH_CRON_ENABLED`` is on. No provider key is required for
Open-Meteo; an OpenWeatherMap ``appid`` goes into the URL.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from YSE_App.jobs import job
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.telescope_models import Telescope
from YSE_App.services import external_services as runs

log = logging.getLogger(__name__)

REFRESH_JOB_KIND = "weather.refresh"
SERVICE_SLUG = "weather"

# WMO weather interpretation codes (Open-Meteo `weather_code`).
WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Slight showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}

FIELDS = ("temperature_c", "humidity_pct", "wind_speed_ms", "wind_direction_deg", "wind_gust_ms",
          "cloud_cover_pct", "precipitation_mm", "pressure_hpa", "dew_point_c", "visibility_m", "description",
          "observed_at")


def cache_minutes() -> int:
    return int(getattr(settings, "WEATHER_CACHE_MINUTES", 10) or 0)


def http_timeout() -> float:
    return float(getattr(settings, "WEATHER_HTTP_TIMEOUT_SECONDS", 10) or 10)


def widget_settings() -> Dict[str, int]:
    return {
        "skycam_refresh_seconds": int(getattr(settings, "SKYCAM_REFRESH_SECONDS", 300) or 0),
        "weather_refresh_seconds": int(getattr(settings, "WEATHER_WIDGET_REFRESH_SECONDS", 600) or 0),
    }


def render_url(telescope: Telescope) -> str:
    url = telescope.weather_url or ""
    try:
        return url.format(lat=telescope.latitude, lon=telescope.longitude, elevation=telescope.elevation,
                          latitude=telescope.latitude, longitude=telescope.longitude)
    except (KeyError, IndexError, ValueError):
        return url


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(mapping: Dict[str, Any], *keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _iso(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.utcfromtimestamp(float(value)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, ValueError, OSError):
            return ""
    text = str(value).strip()
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_weather(data: Any) -> Dict[str, Any]:
    """Map an Open-Meteo, OpenWeatherMap or flat JSON answer onto the widget's fields."""
    out: Dict[str, Any] = {k: None for k in FIELDS}
    out["description"] = ""
    out["observed_at"] = ""
    if not isinstance(data, dict):
        return out
    # Open-Meteo: {"current": {...}} (or the older {"current_weather": {...}})
    current = data.get("current") or data.get("current_weather")
    if isinstance(current, dict):
        out["temperature_c"] = _num(_first(current, "temperature_2m", "temperature"))
        out["humidity_pct"] = _num(_first(current, "relative_humidity_2m", "relativehumidity_2m", "humidity"))
        out["wind_speed_ms"] = _num(_first(current, "wind_speed_10m", "windspeed_10m", "windspeed", "wind_speed"))
        out["wind_gust_ms"] = _num(_first(current, "wind_gusts_10m", "windgusts_10m"))
        out["wind_direction_deg"] = _num(_first(current, "wind_direction_10m", "winddirection_10m", "winddirection"))
        out["cloud_cover_pct"] = _num(_first(current, "cloud_cover", "cloudcover"))
        out["precipitation_mm"] = _num(_first(current, "precipitation", "rain"))
        out["pressure_hpa"] = _num(_first(current, "surface_pressure", "pressure_msl"))
        out["dew_point_c"] = _num(_first(current, "dew_point_2m"))
        out["visibility_m"] = _num(_first(current, "visibility"))
        code = _first(current, "weather_code", "weathercode")
        if code is not None:
            try:
                out["description"] = WMO_CODES.get(int(code), "WMO code %s" % code)
            except (TypeError, ValueError):
                out["description"] = str(code)
        out["observed_at"] = _iso(_first(current, "time"))
        units = data.get("current_units") or {}
        speed_unit = str(units.get("wind_speed_10m") or units.get("windspeed_10m") or "").lower()
        if speed_unit in ("km/h", "kmh") and out["wind_speed_ms"] is not None:
            out["wind_speed_ms"] = round(out["wind_speed_ms"] / 3.6, 2)
            if out["wind_gust_ms"] is not None:
                out["wind_gust_ms"] = round(out["wind_gust_ms"] / 3.6, 2)
        elif speed_unit in ("mp/h", "mph") and out["wind_speed_ms"] is not None:
            out["wind_speed_ms"] = round(out["wind_speed_ms"] * 0.44704, 2)
        return out
    # OpenWeatherMap current weather: {"main": {...}, "wind": {...}, "clouds": {...}, "weather": [...], "dt": ...}
    main = data.get("main")
    if isinstance(main, dict):
        temp = _num(main.get("temp"))
        if temp is not None and temp > 200:  # Kelvin unless units=metric was requested
            temp = round(temp - 273.15, 2)
        out["temperature_c"] = temp
        out["humidity_pct"] = _num(main.get("humidity"))
        out["pressure_hpa"] = _num(main.get("pressure"))
        wind = data.get("wind") or {}
        out["wind_speed_ms"] = _num(wind.get("speed"))
        out["wind_gust_ms"] = _num(wind.get("gust"))
        out["wind_direction_deg"] = _num(wind.get("deg"))
        out["cloud_cover_pct"] = _num((data.get("clouds") or {}).get("all"))
        rain = data.get("rain") or {}
        out["precipitation_mm"] = _num(rain.get("1h") if isinstance(rain, dict) else rain)
        out["visibility_m"] = _num(data.get("visibility"))
        weather = data.get("weather")
        if isinstance(weather, list) and weather and isinstance(weather[0], dict):
            out["description"] = str(weather[0].get("description") or weather[0].get("main") or "").capitalize()
        out["observed_at"] = _iso(data.get("dt"))
        return out
    # Flat: take whatever keys look familiar.
    out["temperature_c"] = _num(_first(data, "temperature_c", "temperature", "temp", "air_temperature"))
    out["humidity_pct"] = _num(_first(data, "humidity_pct", "humidity", "relative_humidity"))
    out["wind_speed_ms"] = _num(_first(data, "wind_speed_ms", "wind_speed", "windspeed", "wind"))
    out["wind_gust_ms"] = _num(_first(data, "wind_gust_ms", "wind_gust", "gust"))
    out["wind_direction_deg"] = _num(_first(data, "wind_direction_deg", "wind_direction", "wind_dir"))
    out["cloud_cover_pct"] = _num(_first(data, "cloud_cover_pct", "cloud_cover", "clouds", "cloudcover"))
    out["precipitation_mm"] = _num(_first(data, "precipitation_mm", "precipitation", "rain"))
    out["pressure_hpa"] = _num(_first(data, "pressure_hpa", "pressure"))
    out["dew_point_c"] = _num(_first(data, "dew_point_c", "dew_point", "dewpoint"))
    out["visibility_m"] = _num(_first(data, "visibility_m", "visibility"))
    out["description"] = str(_first(data, "description", "conditions", "summary", "sky") or "")
    out["observed_at"] = _iso(_first(data, "observed_at", "time", "timestamp", "dt"))
    return out


def is_fresh(telescope: Telescope, now=None) -> bool:
    if not telescope.weather_fetched_at or not telescope.weather:
        return False
    now = now or timezone.now()
    return telescope.weather_fetched_at >= now - datetime.timedelta(minutes=cache_minutes())


def _system_user() -> Optional[User]:
    return User.objects.filter(is_superuser=True).order_by("pk").first() or User.objects.order_by("pk").first()


def ensure_service(user: Optional[User] = None) -> ExternalService:
    actor = user or _system_user()
    service, _created = ExternalService.objects.get_or_create(
        slug=SERVICE_SLUG,
        defaults={"name": "Weather (telescope widgets)", "kind": ExternalService.KIND_GENERIC,
                  "description": "Current-conditions JSON endpoints configured per telescope (#311).",
                  "created_by": actor, "modified_by": actor},
    )
    return service


def fetch_weather(telescope: Telescope, user: Optional[User] = None) -> Dict[str, Any]:
    """GET the telescope's weather endpoint now, store the snapshot, record a run; returns the snapshot.

    On failure the previous snapshot is kept and ``error`` is set on the returned copy.
    """
    url = render_url(telescope)
    snapshot = dict(telescope.weather or {})
    if not url:
        snapshot["error"] = "no weather_url configured"
        return snapshot
    actor = user or telescope.modified_by or telescope.created_by or _system_user()
    service = ensure_service(actor)
    run = ExternalServiceRun.objects.create(
        service=service, target=telescope, target_ref=telescope.name,
        request_payload={"url": url, "telescope_id": telescope.pk}, created_by=actor, modified_by=actor,
    )
    run.mark_running()
    try:
        response = requests.get(url, headers={"Accept": "application/json"}, timeout=http_timeout())
        if response.status_code >= 300:
            raise requests.HTTPError("weather endpoint answered %s" % response.status_code)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
        snapshot["error"] = error
        snapshot["error_at"] = _iso(timezone.now())
        Telescope.objects.filter(pk=telescope.pk).update(weather=snapshot)
        telescope.weather = snapshot
        return snapshot
    now = timezone.now()
    snapshot = normalize_weather(data)
    snapshot["fetched_at"] = _iso(now)
    snapshot["source"] = urlsplit(url).netloc
    snapshot["provider"] = ("open-meteo" if isinstance(data, dict) and ("current" in data or "current_weather" in data)
                            else "openweathermap" if isinstance(data, dict) and "main" in data else "json")
    telescope.weather = snapshot
    telescope.weather_fetched_at = now
    Telescope.objects.filter(pk=telescope.pk).update(weather=snapshot, weather_fetched_at=now)
    runs.record_completion(run, ExternalServiceRun.STATUS_SUCCEEDED, result=snapshot)
    return snapshot


def get_weather(telescope: Telescope, *, user: Optional[User] = None, allow_fetch: bool = True,
                force: bool = False) -> Optional[Dict[str, Any]]:
    """The snapshot to show: the cache when fresh (or when fetching is not allowed), else a fresh fetch."""
    if not telescope.weather_url:
        return None
    if not force and (is_fresh(telescope) or not allow_fetch):
        return dict(telescope.weather or {}) or None
    return fetch_weather(telescope, user=user)


def widget_context(telescope: Telescope, *, user=None, allow_fetch: bool = False, force: bool = False,
                   compact: bool = False) -> Dict[str, Any]:
    """Template context for ``YSE_App/partials/weather_widget.html``."""
    snapshot = get_weather(telescope, user=user, allow_fetch=allow_fetch, force=force)
    ctx = {"telescope": telescope, "weather": snapshot, "compact": compact,
           "has_weather": bool(telescope.weather_url), "has_skycam": bool(telescope.skycam_url),
           "weather_link": telescope.weather_link, "stale": bool(snapshot) and not is_fresh(telescope)}
    ctx.update(widget_settings())
    return ctx


def telescopes_with_weather():
    return Telescope.objects.exclude(weather_url="").order_by("name")


def refresh_all(user: Optional[User] = None, telescope_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    qs = telescopes_with_weather()
    if telescope_ids:
        qs = qs.filter(pk__in=telescope_ids)
    results = []
    for telescope in qs:
        snapshot = fetch_weather(telescope, user=user)
        results.append({"telescope": telescope.pk, "name": telescope.name, "error": snapshot.get("error", "")})
    return {"telescopes": len(results), "errors": sum(1 for r in results if r["error"]), "results": results}


def enqueue_refresh(user: Optional[User] = None, telescope: Optional[Telescope] = None):
    from YSE_App.jobs import enqueue

    payload = {"telescope_ids": [telescope.pk]} if telescope is not None else {}
    return enqueue(REFRESH_JOB_KIND, payload, created_by=user)


@job(REFRESH_JOB_KIND, max_attempts=1)
def refresh_job(payload, job=None):
    payload = payload or {}
    return refresh_all(user=getattr(job, "created_by", None), telescope_ids=payload.get("telescope_ids"))
