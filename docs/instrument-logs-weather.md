# Instrument logs, weather widget and SkyCam

SkyPortal-parity umbrella #309: **instrument logs** (#310) and the **weather widget + SkyCam display**
per telescope (#311). Both sit on the facility adapters of #351 (#299), the encrypted credentials and
external-service runs of #342 (#264, #265) and the job queue of #340 (#263). The design follows
SkyPortal's `InstrumentLog` and weather handlers (BSD-3-Clause) in spirit; no SkyPortal code is copied.

Everything is optional and off by default: without a configured telescope or allocation the pages
render as today, the widget is omitted, and the two new crons are no-ops.

## Instrument logs (#310)

| term | meaning |
|---|---|
| `InstrumentLog` | one dated block of operational messages for an `Instrument`: `start` / `end` (UTC; `end` blank = a point in time), `message` (free text), `log` (`{"logs": [{"timestamp", "message", "level"}, ...]}`), `source` (`manual` or `api`), `source_name` (facility slug), `fingerprint` (sha256 of instrument, start, message and entries), optional `run` (the `ExternalServiceRun` of the pull). Index on `(instrument, start)`. `YSE_App/models/instrument_log_models.py` |
| service | `YSE_App/services/instrument_logs.py`: `add_log()` (idempotent by fingerprint), `logs_between()` (overlap filter), `recent_logs_for_telescope()`, `pull_instrument_logs()`, the `instrument_logs.pull` job, `can_add_logs()` |
| page | `/instruments/<id>/logs/`: date range (bootstrap-datepicker), source filter, rendered table (message, structured entries with level badges, source, author), **Add a log entry** form (staff or accounts with `YSE_App.add_instrumentlog`) and, for staff, **Pull from the facility** for the shown range |
| observing-night box | `observing_night.html` and `yse_observing_night.html` list the telescope's logs of the last 7 days next to the twilight table (`partials/observing_night_widgets.html`) |
| telescope page | `/telescopes/<id>/`: coordinates, instruments with log counts, the last week's logs, active allocations, and the weather / SkyCam widget |
| API | `/api/instrumentlogs/`: list / retrieve for every authenticated user with `?instrument=<id>`, `?telescope=<id>`, `?start_after=`, `?end_before=` (ISO date or date-time), `?source=manual|api`; `POST` for staff or accounts holding `add_instrumentlog`; `Authorization: Token <key>` (DRF `authtoken`) is accepted on this endpoint so a facility bot can post. A repeated identical block answers `200` with the existing row instead of `201` |
| admin | `/admin/YSE_App/instrumentlog/` (filters by source and telescope; the fingerprint is recomputed on save) |

### Facility pull

`FacilityAPI.fetch_instrument_log(allocation, instrument, start, end)` is an optional adapter method
(capability `"instrument_log"`; the base class raises `NotImplementedError`). It returns a list of
entries (dicts with `message`, optional `timestamp` and `level`). The **GENERIC** adapter implements
it: set `default_request_params["instrument_log_url"]` on the allocation (or `instrument_log_endpoint`
in its credential). The URL may hold `{start}`, `{end}` (ISO, UTC), `{instrument}`, `{telescope}`,
`{instrument_id}`, `{proposal_id}` placeholders; without placeholders the window and instrument go into
the query string (`?start=&end=&instrument=&telescope=`). The credential's `api_token` is sent as
`Authorization: token ...`. The answer may be a JSON list or an object holding one under `logs`,
`data`, `results` or `entries`; each entry's message may be under `message` / `msg` / `text`, its
time under `timestamp` / `time` / `date` / `created_at` (ISO, unix seconds or MJD), its level under
`level` / `severity`.

```python
from YSE_App.services.instrument_logs import pull_instrument_logs, enqueue_pull
pull_instrument_logs(instrument, start, end, user=request.user)   # synchronous, returns a summary dict
enqueue_pull(instrument, hours=24)                                  # queue an instrument_logs.pull job
enqueue_pull()                                                      # every instrument with such an allocation
```

Each pull is an `ExternalServiceRun` on the allocation's service (`allocation-<id>`, target = the
instrument, `request_payload.purpose = "instrument_log"`), so it shows on `/service_runs/`. The entries
of one pull become one `InstrumentLog` block (`source=api`); the fingerprint makes a repeated pull of
the same window a no-op, and overlapping windows only add blocks whose content differs.

The `InstrumentLogPull` cron (`YSE_App.data_ingest.Instrument_Logs`) queues one `instrument_logs.pull`
job for the last `INSTRUMENT_LOG_PULL_HOURS` every `INSTRUMENT_LOG_PULL_CRON_MINUTES`, once
`INSTRUMENT_LOG_PULL_CRON_ENABLED` is on. Which instruments are pulled: every instrument with an active,
current allocation (on the instrument, or on its telescope with no instrument) whose facility adapter
has the `instrument_log` capability.

## Weather widget and SkyCam (#311)

Three optional fields on `Telescope` (admin → Telescopes → *Weather widget and SkyCam*):

| field | meaning |
|---|---|
| `weather_url` | JSON endpoint for current conditions. `{lat}`, `{lon}`, `{elevation}` placeholders are filled from the telescope. Understood shapes: **Open-Meteo** (`https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,precipitation,weather_code,surface_pressure,dew_point_2m&wind_speed_unit=ms`, no key needed), **OpenWeatherMap** (`https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid=<key>`), or any flat JSON object with familiar keys (`temperature`, `humidity`, `wind_speed`, `cloud_cover`, `description`, ...) |
| `weather_link` | the site's own weather page, linked from the widget header |
| `skycam_url` | an all-sky camera image; the widget reloads it every `SKYCAM_REFRESH_SECONDS` with a cache-busting query string and links to the full image |

`YSE_App/services/weather.py`: `get_weather(telescope)` serves the cached snapshot in
`Telescope.weather` (with `weather_fetched_at`) when it is younger than `WEATHER_CACHE_MINUTES`,
otherwise `fetch_weather()` GETs the endpoint, normalises it (`temperature_c`, `humidity_pct`,
`wind_speed_ms`, `wind_direction_deg`, `wind_gust_ms`, `cloud_cover_pct`, `precipitation_mm`,
`pressure_hpa`, `dew_point_c`, `visibility_m`, `description`, `observed_at`, plus `fetched_at`,
`source`, `provider`) and stores it. Every fetch is an `ExternalServiceRun` on the `weather` service
(kind generic, created on demand; target = the telescope). A failed fetch keeps the last good
snapshot, adds `error` / `error_at` to it and marks the run failed; the widget shows the old values
with a warning.

Page renders never fetch: views pass `allow_fetch=False` and render whatever is cached. The widget
(`partials/weather_widget.html` + `static/YSE_App/weather-widget.js`, loaded from `base.html`) then
calls `/telescopes/<id>/weather_fragment/`, which fetches when the cache is stale and returns the
widget body (or the snapshot with `?format=json`); staff may force a refetch with `?refresh=1`, and the
header's refresh button does that. The body is re-read every `WEATHER_WIDGET_REFRESH_SECONDS`. The
widget appears on `/telescopes/<id>/`, on both observing-night pages and, in compact form, on the
transient detail **Resources** tab beside every classical / ToO resource whose telescope has one of the
three fields set. A telescope with none of them set renders nothing (no errors).

The `WeatherRefresh` cron queues one `weather.refresh` job every `WEATHER_REFRESH_CRON_MINUTES` once
`WEATHER_REFRESH_CRON_ENABLED` is on; the job refreshes every telescope with a `weather_url` so the
pages always have a warm cache. Without it the first viewer of a stale widget triggers the fetch.

## Settings (`settings.ini`, section `[observatory]`, all optional)

| key | default | meaning |
|---|---|---|
| `INSTRUMENT_LOG_PULL_CRON_ENABLED` | `False` (env `YSE_INSTRUMENT_LOG_PULL_CRON=1`) | queue `instrument_logs.pull` from `runcrons` |
| `INSTRUMENT_LOG_PULL_CRON_MINUTES` | 60 | cron interval |
| `INSTRUMENT_LOG_PULL_HOURS` | 24 | window each pull asks for |
| `WEATHER_REFRESH_CRON_ENABLED` | `False` (env `YSE_WEATHER_REFRESH_CRON=1`) | queue `weather.refresh` from `runcrons` |
| `WEATHER_REFRESH_CRON_MINUTES` | 10 | cron interval |
| `WEATHER_CACHE_MINUTES` | 10 | a snapshot younger than this is served without a fetch |
| `WEATHER_HTTP_TIMEOUT_SECONDS` | 10 | timeout of one weather GET |
| `SKYCAM_REFRESH_SECONDS` | 300 | SkyCam image reload interval (0 = never) |
| `WEATHER_WIDGET_REFRESH_SECONDS` | 600 | widget body reload interval (0 = never) |

The facility pull uses `FACILITY_HTTP_TIMEOUT_SECONDS` (`[site_settings]`, 30) like every other
facility call. Both job handlers are registered through `DEFAULT_HANDLER_MODULES`
(`YSE_App.services.instrument_logs`, `YSE_App.services.weather`), so a `run_jobs --loop` worker knows them.

## Deploy checklist

1. `python manage.py migrate YSE_App` — `0022_instrument_logs_weather` (the `InstrumentLog` table and
   five nullable / defaulted `Telescope` columns; no data migration).
2. `python manage.py collectstatic` — `YSE_App/weather-widget.js` is new.
3. Admin → Telescopes: set `weather_url` / `weather_link` / `skycam_url` where the site provides them.
4. Allocations that publish logs: add `"instrument_log_url": "https://..."` to the allocation's default
   request parameters (GENERIC adapter) and, when needed, `api_token` to its credential.
5. Optional: `[observatory] WEATHER_REFRESH_CRON_ENABLED = True` and
   `INSTRUMENT_LOG_PULL_CRON_ENABLED = True` once the endpoints are known to work; the job runner
   (`RunQueuedJobs` or `run_jobs --loop`) executes the queued jobs.
6. Facility bots posting logs: create a user, grant `YSE_App | instrument log | Can add instrument log`,
   create a DRF token (`python manage.py drf_create_token <user>`), and `POST /api/instrumentlogs/`
   with `Authorization: Token <key>`.

## Tests

`YSE_App/tests/test_instrument_logs_weather.py`: entry normalisation and fingerprints, add / list
overlap, the GENERIC `fetch_instrument_log` (placeholders, query string, token, errors), idempotent
pulls recorded as runs, the `instrument_logs.pull` job and cron, the API (filters, permissions, token
auth, idempotent POST), the logs page (window, forms, pull button), the telescope page, weather
normalisation (Open-Meteo, OpenWeatherMap, flat), caching and failure handling, the `weather.refresh`
job and cron, the fragment endpoint, and the observing-night / Resources-tab widgets. All HTTP is
mocked (`requests.get`).
