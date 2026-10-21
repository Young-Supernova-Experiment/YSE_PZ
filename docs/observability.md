# Observability page (#307)

`/observability/<transient_id>/` answers "where can this transient be observed tonight?" for
every telescope in the database at once, the way SkyPortal's observability page does. It is
reached from the **Observability** button under the coordinates on the transient detail page
(Summary tab) and needs a login.

## What the user sees

* A **summary table** with one row per telescope: hours observable (dark hours above 30
  degrees, i.e. airmass 2), dark hours (astronomical twilight to astronomical twilight),
  best altitude and minimum airmass, the times the target first and last stands above
  30 degrees, transit, the mean Moon separation and the Moon's illumination, and the dark
  window in UT. Rows start sorted by hours observable; clicking a header re-sorts the table
  and the plot grid together. A badge marks targets that never rise or are up all night.
* A **grid of small plots**, one per telescope: the target's altitude against UT for the
  night, the Moon's altitude dashed, the twilight bands shaded from sunset to astronomical
  darkness (daylight in orange), the 30 degree line dotted and, when the night is in
  progress, a red dashed "now" line. Hovering gives UT, altitude, airmass and Moon distance.
  The plots are Bokeh figures like the light curves, embedded from the JSON endpoint below.
* A **night picker**: a date input with previous/next/tonight buttons, and a checkbox to
  keep only the telescopes the user's collaboration groups have an active allocation or an
  observing resource (classical, ToO, queued) on. When that leaves nothing the page shows every
  telescope instead of an empty grid.

"Night of `D`" is the night whose evening falls on calendar date `D` in the telescope's local
(longitude) time: sunset after local noon of `D`, sunrise after that sunset. The default `D` is UT
now minus twelve hours, so a page opened during the observing hours shows the night in progress
at every longitude.

## Endpoint

`GET /observability/<transient_id>/data/?date=YYYY-MM-DD&telescope=1,2&mine=1&plots=0`

| parameter | meaning |
|---|---|
| `date` | the evening date; default as above; a malformed value is a 400 |
| `telescope` | comma-separated telescope ids; default every telescope with coordinates (or the user's, with `mine=1`) |
| `mine` | `1` keeps the telescopes behind the user's groups' allocations and resources |
| `plots` | `0` leaves the Bokeh plot item out of each entry |

The answer is `{"date", "transient", "observable_alt", "results": [...], "rows": [...]}` where
each `results` entry holds the telescope, the night (sunset, sunrise, the six twilight times,
the dark window, a `note` when the sun does not set or rise normally), 121 evenly spaced
`samples` (`t` in UT, `alt`, `az`, `airmass`, `moon_alt`, `moon_sep`, `sun_alt`), the `summary`
numbers of the table row and, unless `plots=0`, a Bokeh `json_item` for
`Bokeh.embed.embed_item`. `rows` repeats the summaries sorted by hours observable.

The page asks for **one telescope per request** and fills the table and grid as answers
arrive, so a slow site never blocks the others and one request stays well under a second.

## Ephemeris service and cache

`YSE_App/services/observability.py` does the astronomy with astroplan/astropy, which the app
already depends on:

* `compute_night_ephemeris(transient, telescope, night)` samples the night (sunset - 30 min to
  sunrise + 30 min) and reduces it to the summary; the airmass is Kasten & Young (1989), so it
  stays finite near the horizon; positions are geometric (no refraction). The IERS table is
  neither downloaded nor required (`iers_quiet()`): its millisecond refinement of UT1 is
  irrelevant here and the web workers must not wait on a download.
* `night_ephemeris(...)` wraps it in the Django cache under
  `observability_v<version>_<transient>_<ra>_<dec>_<telescope>_<lon>_<lat>_<elev>_<date>` for
  one hour (`EPHEMERIS_CACHE_TIMEOUT`). The key carries the coordinates, so an edited transient or
  telescope row never serves a stale night, and `EPHEMERIS_CACHE_VERSION` is bumped whenever the
  payload shape or the math changes. On the deployed stacks the cache is Redis (`REDIS_URL`),
  so the entries are shared between the web workers; without Redis the per-process
  `LocMemCache` is used and each worker computes once.
* `telescopes_for_user(user, mine)` and `telescope_ids_for_user(user)` implement the "my
  telescopes" filter (allocations `is_active=True` and observing resources whose groups are
  empty or intersect the user's groups).

There is no new table and no migration.

## Tests

`YSE_App/tests/test_observability.py`: the service on known sites and nights (Maunakea,
Paranal, a polar site; a morning object, a zenith-transiting object, one that never rises),
the cache (hit, key contents, one hour timeout, bypass), the page and the endpoint
(login, telescope list, date handling, `mine`, `telescope`, `plots=0`, 400/404/405, cache
reuse, the Bokeh item, the inline script's bracket balance) and the link on the detail page.
