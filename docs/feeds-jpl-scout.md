# JPL Scout feed, minor-planet screening and NEOfixer ranks

Moving objects are the classic false positive of a young-transient search.
Issue #283 (umbrella #280) adds three pieces in `YSE_App/feeds/scout.py`; the
shared feed machinery is described in docs/feeds-hermes.md (*How the feeds work*).

## 1. Scout NEO candidates (`scout` feed)

JPL's Scout service scores the objects on the MPC's NEO Confirmation Page
(`https://ssd-api.jpl.nasa.gov/scout.api`, `FEEDS_SCOUT_API_URL`). A source of
kind *JPL Scout NEO sweep* polls that list and upserts one **candidate** per
object (`broker='scout'`, `alert_id` the temporary designation, tag `NEO` in
`payload.properties`): `classification` *NEO candidate (score N)*, `rb` the NEO
score / 100, `last_mag` V, the sky-plane uncertainty (`unc_arcmin`, also
`error_radius_arcsec`), motion rate, number of observations, arc, H, MOID and
the other Scout scores. Each poll refreshes the values (`latest_alert_id` is
Scout's `lastRun`). A transient at that position (within the match cone) gets
the `minor_planet` annotation with `possible_mpc` set to the designation and
the *moving object* verdict; the designation is recorded as an alternate name.

| key | default | meaning |
|---|---|---|
| `min_neo_score` | unset | keep objects with `neoScore >=` this (0-100) |
| `max_vmag` | unset | keep objects at least this bright |
| `max_unc_arcmin` | unset | skip objects with a larger uncertainty |
| `neofixer` | unset | `{"url": ...}` for NEOfixer ranks (section 3) |
| shared keys | | `auto_save` (default false), `criteria`, `match_radius_arcsec`, `max_per_run` |

## 2. Minor-planet screening of new transients (`minor_planet` annotation)

`YSE_App.feeds.scout.screen_transient` asks JPL's Small-Body Identification API
(`sb_ident.api`, `FEEDS_SBIDENT_API_URL`) which known asteroids were inside a
small field around the transient's position **at its discovery epoch**
(`disc_date`, else the first detection, else the row's creation time), seen
from `FEEDS_MPC_SCREEN_OBS_CODE` (MPC observatory code, default `500` =
geocentre; use the survey's code, e.g. `F51` for Pan-STARRS 1, for better
precision). The nearest body within `FEEDS_MPC_SCREEN_RADIUS_ARCSEC` (5")
writes the `minor_planet` annotation:

| key | meaning |
|---|---|
| `possible_mpc` | the designation (null for a clean result) |
| `separation_arcsec`, `vmag`, `ra_rate_arcsec_per_hr`, `dec_rate_arcsec_per_hr` | from sb_ident |
| `epoch_mjd`, `radius_arcsec`, `obs_code`, `n_bodies_in_field`, `source` | what was asked |
| `verdict` | `moving object` (badge on the Summary tab, like `stellar` / `AGN-like`) or `clean` |
| `summary` | one line for the annotations tab and the badge tooltip |

A clean result is recorded too, so the sweep does not repeat it. The
annotation is searchable like any other (`annotation_origin=minor_planet`,
`minor_planet.possible_mpc`, docs/transient-search.md).

Ways to run it (all through the `feeds.screen_minor_planets` job):

* `FEEDS_MPC_SCREEN_ON_CREATE = True`: a `Transient` `post_save` signal queues
  the check for every new transient (signal path and candidate promotion).
* `FEEDS_MPC_SCREEN_CRON_ENABLED = True`: the `MinorPlanetScreen` cron queues a
  sweep every `FEEDS_MPC_SCREEN_CRON_MINUTES` (360) over transients created in
  the last `FEEDS_MPC_SCREEN_SINCE_DAYS` (3) without the annotation, at most
  `FEEDS_MPC_SCREEN_MAX_PER_RUN` (50) per run.
* On demand: `POST /feeds/screen/<transient id>/` (any logged-in user; the
  annotations tab shows the result when the job has run) or
  `manage.py feeds --screen <name>` (inline, prints the document).

## 3. NEOfixer ranks (`neofixer` annotation)

NEOfixer (Catalina Sky Survey) ranks NEO candidates by follow-up priority; its
API needs an account and the response layout is theirs to change, so the
integration is a configurable endpoint rather than a fixed client. Put
`{"neofixer": {"url": "<endpoint returning JSON>"}}` in the Scout source
config (and, if the endpoint wants a header, `neofixer_token` in the source's
credential). The feed accepts a list of objects (or an `objects` / `results` /
`data` list, or a `{designation: rank}` mapping) with `designation` /
`objectName` / `name` and `rank` / `priority` / `score`; the rank of every
Scout object in the poll is stored on the candidate (`neofixer_rank`) and,
when the object matches a transient, as the `neofixer` annotation with key
`rank`. Verify the shape against the live endpoint when enabling it.

## Operator steps (Ziggy)

Off by default. Screening needs no source row, only the switches:

```ini
[feeds]
MPC_SCREEN_ON_CREATE = True
MPC_SCREEN_CRON_ENABLED = True
MPC_SCREEN_OBS_CODE = F51
```

Then `venv/bin/python manage.py feeds --screen <a known transient>` once to see
a document come back from `ssd-api.jpl.nasa.gov` (Ziggy must reach it). For the
Scout candidates, add a Feed source of kind *JPL Scout NEO sweep* (slug
`scout`, e.g. `{"min_neo_score": 50, "max_vmag": 21.5}`), poll it once with
`manage.py feeds --poll scout`, and enable `POLL_CRON_ENABLED` as for the
other feeds.
