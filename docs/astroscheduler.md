# AstroScheduler night planner

YSE_PZ plans classical nights with [AstroScheduler](https://github.com/UCSC-Transients/AstroScheduler),
installed as a pinned package (`requirements.txt`, tag `v1.0.0`). AstroScheduler owns solving
and scheduling. YSE_PZ owns the targets, and later the saved plans and outcomes.
AstroScheduler also runs on its own as a standalone app.

Status (2026-10-15): the targets feed and the server-side solve are in place. The embedded
planner page, saved plans, export and execution feedback come in later PRs.

## Turning it on

Everything is behind one flag, which is off by default. When it is off, the endpoints below
return 404. To turn it on, set this in the stack's `YSE_PZ/settings.ini`, under `[site_settings]`:

```ini
ASTROSCHEDULER_ENABLED: True
```

You can also set `YSE_ASTROSCHEDULER_ENABLED=1` in the environment instead.

Optional keys, with their defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `ASTROSCHEDULER_SITE_MAP` | `shane:lick:kast,lick:lick:kast,keck ii:keck2:kcwi,keck2:keck2:kcwi,keck i:keck1:lris,keck1:keck1:lris` | `pattern:site:instrument` rules. The first pattern found in the lower-cased telescope name wins. |
| `ASTROSCHEDULER_DEFAULT_MAG` | `19.0` | Magnitude assumed for a target with no photometry. The target gets a warning. |
| `ASTROSCHEDULER_STALE_MAG_DAYS` | `7` | A last detection older than this before the night gets a warning. |

**Site, coordinates and time zone.** The site's coordinates always come from the YSE_PZ
`Telescope`, and its time zone from the `Observatory`'s `tz_name`. The site map only picks
AstroScheduler's pointing limits and exposure model. A telescope that matches no rule is
planned as a custom site, with generic limits and the Kast exposure model, and the response
warns about it.

## API

Both endpoints are DRF views. They need an authenticated user, by session or by
`Authorization: Token <key>`. Session POSTs also need the CSRF token.

### `GET /api/observing_nights/<night id>/planner_targets/`

**What it returns:** the night's follow-ups that the requesting user may see, in
AstroScheduler's `TargetSpec` format. The rows come from `night_followups`, the same query
and visibility rules as the observing-night page.

**Response fields:**
- `night`: id, date, telescope and night type.
- `observatory`: the AstroScheduler site dict.
- `instrument`.
- `targets`.
- `warnings`.

**Each target has:**
- **Ids:** `external_id` (the follow-up id, as a string), `followup_id` and `transient_id`.
- **Name and position:** `name`; `ra` and `dec` as sexagesimal strings; `ra_deg` and `dec_deg`.
  AstroScheduler reads a bare number below 24 as hours, so send it the strings, not the degrees.
- **Magnitude:** `magnitude`, `mag_band`, `mag_source` and `mag_age_days`.
  - `mag_source` is `bazin` when the magnitude is the Bazin extrapolation to local midnight
    of the night.
  - Otherwise it is `last_detection`.
  - With no photometry it is `none`, and the default magnitude is used.
- **Priority:** `priority`, from 1 to 5 with 1 the highest. It is the minimum over the
  requestors.
- **Comment:** `comment`, the requestors' comments.
- **Status and window:** `followup_status`, `valid_start` and `valid_stop`.
- **Offset star:** `offset_star` (from the follow-up's offset-star fields), or null.
- **Warnings:** `warnings`.

### `POST /api/observing_nights/<night id>/planner_solve/`

**Request body:** AstroScheduler's `/api/schedule` payload: targets, standards options,
`realtime_constraints` and `previous_schedule`.

**What the server fixes:** the night sets the date, the site and the instrument. For
`FirstHalf` and `SecondHalf` nights it also sets the window, split midway between the
18-degree twilights.

**What it keeps from the body:** only telescope-limit overrides (`observatory.telescope`)
and a narrower night window. Everything else under "observatory" is ignored.

**Response:** AstroScheduler's result, plus `yse_pz.night_id` and `yse_pz.solve_seconds`.

**Limits and errors:**
- At most 300 targets per request.
- An invalid body returns 400 with a `detail` message.
- Nothing is saved.

A typical night solves in 1-2 s, plus about 1 s the first time that night is solved in a
process.

## Notes

- `astroscheduler` is imported only when a planner endpoint runs. Importing it turns off
  astropy's IERS auto-download for the process, so pages that never plan are unaffected.
- Tests: `YSE_App/tests/test_night_planner.py`. They use synthetic data, mock the Bazin fits,
  and solve with the installed package without HTTP.
