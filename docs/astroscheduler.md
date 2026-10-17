# AstroScheduler night planner

YSE_PZ plans classical nights with [AstroScheduler](https://github.com/UCSC-Transients/AstroScheduler),
installed as a pinned package (`requirements.txt`, tag `v1.3.0`). AstroScheduler owns solving
and scheduling. YSE_PZ owns the targets, the saved plans and the outcomes. AstroScheduler also
runs on its own as a standalone app, and can import a YSE_PZ night with an API token.

Status (2026-10-16): the targets feed, the server-side solve, the planner page, saved plans
(versions, approval, re-opening, target-list export) and execution feedback are in place, behind
the `ASTROSCHEDULER_ENABLED` flag (see [Turning it on](#turning-it-on)).

Contents: [Observer guide](#observer-guide) · [Execution feedback](#execution-feedback) ·
[Saved plans](#saved-plans) · [The planner page](#the-planner-page) ·
[Turning it on](#turning-it-on) · [API](#api) · [Performance](#performance) · [Notes](#notes)

## Observer guide

**Before the night**
1. Open the classical night (Observing calendar, or "Next Telescope Nights" on the home page)
   and press **Plan this night**.
2. The planner opens inside YSE_PZ with the night's date, telescope, instrument and the
   follow-ups you may see. Warnings above it name targets without photometry or with a stale
   last detection.
3. Press **Recalculate Schedule**. Move blocks by hand if needed: lock a start time, drag to
   reorder, change exposures. The planner re-solves only when you press Recalculate.
4. **Save plan**, with an optional note. Each save is a new version under "Saved plans".
   **Open** continues a version: your edits come back and the blocks keep their places.
5. The night's PI (or staff) presses **Approve** on the version to observe. **Target list**
   downloads it for the telescope, in planned order, with UT windows, exposures and offset stars.

**At the telescope**
1. In the planner, switch to **Real-Time Mode**.
2. Pick the target, then press **Start Observation** when you start it.
3. When the target is finished, press one of:
   - **Completed** when data were taken ("Data taken");
   - **Failed** when you tried and missed it, the S/N was bad, and so on;
   - **Punted** when you went to it but got no usable data, or stopped too early.

   Failed and Punted need at least one reason; tick every one that applies. The comment box
   is the note.
4. Everything is recorded in YSE_PZ. Tonight's observing log appears below the plans.
5. Press **Recalculate Schedule Starting Now** to re-plan the rest of the night. Finished targets
   are left out, even after a page reload.

**After the night**
- **Judge** each "Data taken" row in the night log: Good, Failed or Punted, with any number of
  reasons and a comment. A good observation makes the request **Successful**.
- **Uploading** the spectrum (or photometry) for that request or night does the same,
  automatically.
- A request whose window ends with no good data becomes **Failed** (daily, once
  `ASTROSCHEDULER_EXPIRE_AFTER` is set).

## Execution feedback

The planner's Real-Time Mode buttons are recorded in YSE_PZ (migration `0040_observed_blocks`:
the draft's `ObservedBlock` and `OutcomeReason`).

Two different things are recorded.
- The **observation's outcome** belongs to one attempt on one night.
- The **request's status** covers one or more nights.

| Action | Observation outcome | Request status |
| --- | --- | --- |
| Planner: Start Observation | none yet; its time becomes the block's start | InProcess |
| Planner: Completed | `observed`, shown as "Data taken" | InProcess |
| Planner: Failed (reason required): tried and missed, bad S/N... | `failed` | unchanged |
| Planner: Punted (reason required): went to it, no usable data or stopped too early | `punted` | unchanged |
| Night log: **Good** | `success` | **Successful** |
| Night log: Failed / Punted (at least one reason) | `failed` / `punted` | unchanged |
| A spectrum or photometry set is uploaded (see below) | `success` | **Successful** |
| The request's window ends without good data (see below) | (none) | **Failed** |

The draft's `skipped` outcome is kept for a planned block that was never attempted.

**Uploaded data:** while the planner is enabled, a new spectrum or photometry set proves the
observation.
- A spectrum or photometry set that names a follow-up request (its `followup` field) matches
  that request's data-taken observations.
- A spectrum that names no request matches the same transient's data-taken observations from
  the night of its `obs_date`.

Matching observations become `success`, and their classical requests (and the request the
upload names, even with no observation recorded) become Successful while still open.

**End of the request period:** the daily `ExpireFollowupRequests` cron, or
`manage.py expire_followup_requests [--apply]`, closes classical requests that are still
Requested or InProcess after their window (`valid_stop`) has ended.
- No observations, or only failed/punted/skipped ones: the request becomes Failed.
- A good observation: it becomes Successful.
- Data taken but not yet judged: left alone for someone to judge.

Only requests whose window ended on or after `ASTROSCHEDULER_EXPIRE_AFTER` (a date) are
touched. It is empty by default, which turns expiry off, so requests from before the planner
was in use are never failed. ToO and queue requests are never touched.

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
| `ASTROSCHEDULER_EXPIRE_AFTER` | empty (off) | `YYYY-MM-DD`: classical requests whose window ends on or after this date and that get no good data are closed as Failed by the daily cron. |

**Site, coordinates and time zone.** The site's coordinates always come from the YSE_PZ
`Telescope`, and its time zone from the `Observatory`'s `tz_name`. The site map only picks
AstroScheduler's pointing limits and exposure model. A telescope that matches no rule is
planned as a custom site, with generic limits and the Kast exposure model, and the response
warns about it.

## API

All endpoints are DRF views. They need an authenticated user, by session or by
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
{"result": {"blocks": [...], "solver_version": "1.3.0"}, "targets": [...],
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

### `POST /api/observed_blocks/<id>/assess/`

Judges a recorded observation:
`{"outcome": "success" | "failed" | "punted", "reasons": ["<code>", ...], "note": "..."}`.
On the planner page this is the **Judge** form on each "Data taken" row of the night log:
the outcome, a checklist of reasons (several may apply) and a comment.
`success` makes the request Successful. `failed` and `punted` need a reason and leave the request
alone.

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

## Performance

**Solves run inside the web request.** Solve time is measured on Python 3.8 with YSE_PZ's
pins, at Lick, with random synthetic targets, on one laptop (Ziggy may differ):

| Follow-ups | 10 | 20 | 40 | 60 | 100 |
| --- | --- | --- | --- | --- | --- |
| AstroScheduler 1.2.0, s | 3.3 | 4.9 | 21 | 45 | 107 |
| AstroScheduler 1.3.0 (pinned; [#110](https://github.com/UCSC-Transients/AstroScheduler/issues/110)), s | 3.2 | 3.6 | 7.2 | 13 | 23 |

1.3.0 gives the same schedules as 1.2.0, only faster. About 1 s of each solve is the night's twilight
calculation, which is cached per site and night for later solves in the same process. A
typical classical night of 10-40 follow-ups stays well inside a request's time budget, so
the job queue isn't used. Each response reports `yse_pz.solve_seconds`, which shows when that
assumption needs revisiting.

**Queries:** the planner page and frame, the targets feed, the plans list, the export and
the observing log each make a fixed number of queries, however many follow-ups, plans or
observations there are. `YSE_App/tests/test_planner_smoke.py` checks this.

## Notes

- `astroscheduler` is imported only when a planner endpoint runs. Since 1.0.1 importing it
  changes no astropy setting: its offline IERS options apply only inside each solve.
- Tests (synthetic data; Bazin fits mocked; the installed solver runs without HTTP):
  - `test_night_planner.py`: feed and solve.
  - `test_night_planner_page.py`: page, frame and nights list.
  - `test_night_plans.py`: saved plans.
  - `test_night_observations.py`: execution feedback.
  - `test_planner_smoke.py`: the end-to-end smoke test (#35) and query counts.
