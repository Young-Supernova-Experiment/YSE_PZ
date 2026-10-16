# AstroScheduler night planner

YSE_PZ plans classical nights with [AstroScheduler](https://github.com/UCSC-Transients/AstroScheduler),
installed as a pinned package (`requirements.txt`, tag `v1.2.0`). AstroScheduler owns solving
and scheduling. YSE_PZ owns the targets, and later the saved plans and outcomes.
AstroScheduler also runs on its own as a standalone app.

Status (2026-10-16): the targets feed, the server-side solve, the planner page, saved plans
(versions, approval, re-opening, target-list export) and execution feedback are in place.

## Execution feedback

The planner's Real-Time Mode buttons are recorded in YSE_PZ (migration `0040_observed_blocks`:
the draft's `ObservedBlock` and `OutcomeReason`).

| Planner action | Follow-up status | Observed block |
| --- | --- | --- |
| Start Observation | InProcess | none; its time becomes the block's start |
| Completed | Successful | outcome `success` |
| Failed (reason required) | Failed | outcome `failed` |
| Punted (reason required) | unchanged | outcome `skipped` |

**What a block records:** the note, the planned and actual times, and who recorded it.

**Linking to a plan:**
- A block links to the matching planned block, either of the plan that is open or of the
  night's approved plan.
- The first outcome moves the approved plan to `executing`.

**Who may record:** staff, the night's PI, or users authorised for the night's resource. In
every case the follow-up must be one they can see. If YSE_PZ refuses, the action is cancelled
in the planner too.

**Reasons:** they are a lookup table (Admin > Outcome reasons). The seed codes match the
planner's reason menu.

**After a reload:** targets finished tonight carry the status the solver skips (`Observed`,
`Failed` or `Punted`). "Recalculate Schedule Starting Now" therefore re-plans only what's left.
The planner page shows tonight's observing log.

## Saved plans

Plans use the `NightPlan` and `PlannedBlock` tables from the decision-trail schema draft
(migration `0039_night_plans`).

**Saving:**
- **Save plan** on the planner page stores the planner's current schedule, including the
  observer's manual moves and edits, as a new version of the night's plan. The new version is
  in state `draft`, with the saver as author.
- `solver_inputs` keeps the targets with their edits, the site, the instrument and a SHA-256
  hash.
- Blocks link to their follow-up only when the saver can see that follow-up.
- Standards get their coordinates from AstroScheduler's standards list.

**Opening and approving:**
- **Open** re-opens a version: the night's current follow-ups get the saved edits back, and
  the first solve starts from the version's blocks. A save from there records the opened
  version as its `parent`.
- **Approve** is for the night resource's PI (through the PI's linked account) or staff. The
  approved version becomes `approved`, and the previously approved one `superseded`.

**Target list:** **Target list** downloads a version in the `download_target_list` star-list
format, in planned order.
- Each comment carries the order, the UT window, the exposure sequence (`B 1x1237 R 2x600`)
  and the requestors' comments.
- A follow-up's own offset star follows its target, with `raoffset`/`decoffset`.
- `?finders=1` adds finder-chart offsets for targets without one. This is slow, because it
  builds the finder charts.

**Visibility:** a plan holds the blocks its author could see. Other users see a science block
only if they can see its follow-up on the night page; standards are visible to everyone.

## The planner page

With the flag on, each observing-night page has a **Plan this night** button. It opens
`/night_planner/<night id>/`.

**What the page shows:**
- It sits inside the YSE_PZ shell (same login, navigation and header).
- It lists the feed's warnings.
- It embeds AstroScheduler's planner in a same-origin frame, `/night_planner/<night id>/frame/`.
  The frame is needed because AstroScheduler's stylesheet resets selectors (`body`, `.card`,
  `.btn`) that YSE_PZ's own CSS uses.

**What the planner starts with:**
- the night's date, site and instrument (fixed: the controls are disabled);
- the targets the user may see;
- target names linked to their YSE_PZ pages.

**Solving:** it solves only when the user presses Recalculate, through
`/night_planner/<night id>/api/schedule` (the same solve view as below, with the CSRF token
from the frame). Nothing is saved yet.

**Static files:** the planner's files are served from the installed package as
`<STATIC_URL>astroscheduler/`, through `STATICFILES_DIRS`, so `collectstatic` must run after
the package is installed or upgraded.

**Standalone import:** standalone AstroScheduler (1.1.0+) can import a night through
**Import from YSE_PZ**, with a user's API token. It uses the two GET endpoints below.

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

### `GET /api/observing_nights/?days_back=7&days_ahead=60`

**What it returns:** the classical nights the user is authorised for, the same set as the home
page's upcoming nights. The window runs from `days_back` days ago (at most 60) to `days_ahead`
days ahead (at most 366).

**Fields per night:** `id`, `date`, `telescope`, `night_type`, `site`, `instrument`,
`planner_targets_url` and `planner_url`.

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
- **Link:** `url`, the transient's YSE_PZ page.
- **Warnings:** `warnings`.

### `GET` / `POST /api/observing_nights/<night id>/plans/`

**GET** lists the night's versions, newest first: id, version, state, parent, author, created,
approved by and at, solver, notes, and the number of blocks the requester can see. It also
returns `can_approve`.

**POST** saves the current schedule as a new version and returns 201 with the new id and
version. The body:

```json
{"result": {"blocks": [...], "solver_version": "1.2.0"}, "targets": [...],
 "parent": null, "notes": "optional"}
```

Limits: at most 400 blocks and 300 targets.

### `GET` / `POST /api/observing_nights/<night id>/observations/`

**GET** returns tonight's observed blocks, newest first.

**POST** records one planner action (the AstroScheduler event):

```json
{"action": "complete", "external_id": "<follow-up id>", "reason": null, "note": "",
 "time_utc": "...", "started_utc": "...", "plan": <open plan id or null>}
```

`action` is `start`, `complete`, `fail` or `punt`. `reason` is an OutcomeReason code, required
for `fail` and `punt`.

### `POST /api/night_plans/<plan id>/approve/`

Approves a version; returns 403 unless the requester is the night's PI or staff.

### `GET /night_plans/<plan id>/target_list.txt`

The version as a star list, as described above (`?finders=1` for finder offsets).

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
