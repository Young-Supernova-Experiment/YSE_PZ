# Selections (#841)

A **Selection** is a saved set of criteria for transients, with an owner, a sharing setting and a
slug. It is the shared object that dashboards, downloads, plots, alerts and shares point at
(#842, #843, #844, #849, #853, #855 build on it).

- A Selection stores **criteria only**: no fixed list of rows and no per-object state (nothing
  is "seen", "skipped" or "reviewed").
- Its rows are compiled **for the viewing user**, every time it is used. Data access stays per
  point, so a Selection never shows anyone a transient, point or spectrum they could not see anyway.

## Model: `YSE_App.models.Selection` (table `YSE_App_selection`, migrations 0065 and 0066)

| Field | Meaning |
|---|---|
| `name`, `slug` (unique, never changes on rename), `description` | |
| `owner` | FK user, CASCADE |
| `visibility` | `me` (owner only), `team`, `all` (every signed-in user) |
| `team` | FK group, SET NULL; used for `team` and optionally `all` |
| `kind` | `search`, `rules`, `sql` or `function` |
| `search_params` | search query string (kind `search`; page state stripped) |
| `rules` | JSON rule tree (kind `rules`, compiler comes with #843) |
| `query` | FK `explorer.Query`, SET NULL (kind `sql`); there is no SQL text column |
| `python_query` | registered Python query name (kind `function`) |
| `user_query` | FK `UserQuery`, SET NULL: the dashboard panel it was made from |
| `created_date`, `modified_date`, `modified_by` (FK user, SET NULL) | |

Every FK to a table develop knows carries the same MySQL `ON DELETE` (via
`YSE_App/common/fk_on_delete.py`, which 0062 now imports too), so develop's deletes on the shared
database keep working.

## Sharing rules

- **Viewing.** `me`: owner. `team`: owner plus the team's members, owners and deputies. `all`: every signed-in user.
- **Editing** (rename, re-share, change criteria): the owner, plus the team when a team is set and visibility is `team` or `all` (Ryan, 2026-10-25).
- **Making a Selection private, and deleting it:** the owner only.

## Service API: `YSE_App/services/selections.py`

- `selection_queryset(selection, user)` (or `selection.queryset(user)`): matching `Transient`
  rows, with the search page's annotations and order (`-disc_date, -pk`), always passed through
  `filter_listed_transients`.
  - `search`: the same base queryset and `TransientSearchFilterSet` as `/search/`. It returns the same rows, and the same order when the criteria include `ordering`.
  - `sql`: the dashboards' runner for the viewer (refused where the dashboards refuse, else scoped, #715/#723). The owner's rights never apply.
  - `function`: `run_python_query`.
  - It raises `SelectionAccessError` or `SelectionError` (the message can be shown to the user).
- **Sharing helpers:** `visible_selections(user)`, `editable_selections(user)`, `can_view`, `can_edit`, `can_delete`, `team_choices(user)`.
- **Changing Selections:**
  - `create_selection(owner, name, kind=..., visibility=..., team=..., search_params=..., query=..., python_query=..., rules=...)`;
  - `update_selection(user, selection, name=..., visibility=..., team=..., description=..., search_params=...)`;
  - `selection_from_user_query(owner, user_query)` links a dashboard panel without changing it. Existing `UserQuery` rows and panels are untouched.
- `register_compiler(kind, func)` plugs in a new criteria kind (e.g. `rules`, #843). Start from `base_queryset(user)`.
- `criteria_summary(selection, user)` returns `[(label, value)]` for display.

## Pages (UI preview only; 404 otherwise)

- **Save as Selection** on Transient Search (`POST selections/save/`). Facet choices (#842) are ordinary search parameters, so they are saved too.
- **My Selections** (`selections/`): mine, my teams', all-YSE. Rename, re-share and delete where allowed.
- **A Selection's results** (`selections/<slug>/`): the search table, with:
  - list stepping into the transient page (#850; `ls=sel`, `lt=<slug>`, `lq=` the page/sort);
  - "Observable tonight" (#854) for up to 500 matches.

## REST API (not gated; read-only, any authenticated user)

- `GET /api/selections/`: the Selections you may view (`?kind=`, `?visibility=`, `?mine=true`).
- `GET /api/selections/<slug>/`: one Selection. It never includes SQL text.
- `GET /api/selections/<slug>/transients/`: the matching transients for you (`id, name, slug, ra, dec`), with `limit` / `offset`.

## Downloads (#848, UI preview only)

A Selection's page has **Download -> Spectra / Photometry** with phase, instrument, S/N, date, band and
quality options (`docs/spectral-phase.md`). The search filters `spec_phase_min` / `spec_phase_max` /
`spec_phase_ref` work in search Selections like every other filter.

## Alerts (#844, UI preview only; sending off by default)

**Alert me** on a Selection's page sets an alert; **My alerts** (`selection-alerts/`) lists, edits and deletes them.
Model: `SelectionAlert` and `SelectionAlertSnapshot` (migrations 0071 and 0072; every foreign key carries MySQL
`ON DELETE CASCADE`). Code: `YSE_App/services/selection_alerts.py`.

- **Triggers:** a new match, a new spectrum, a class change, and/or the latest good detection (any band)
  becoming brighter than, or fading below, a magnitude.
- **Recipients:** anyone can alert themselves. Only a team's owners (`GroupProfile.owners`; not deputies, not
  staff) can alert the whole team (each member, owner and deputy, through their own preferences) or the team's
  Slack channel (a channel id, posted by the site's Slack app). The service checks this when the alert is saved
  and again on every run, so an alert stops (with a message on My alerts) when its owner stops owning the team.
  No address outside YSE-PZ is ever added, so the external-email cap is not touched.
- **Frequency:** immediately (every pass), or an hourly or daily digest: at most one message per hour or day
  per alert and recipient. Default: daily.
- **Delivery:** `notify()` with kind `selection_alert`, in the "Alerts" preference group, so each recipient's
  in-app / email / Slack switches apply. One message per recipient per pass: an object that matched several
  alerts is listed once, with the names of all of them.
- **Access:** the Selection is evaluated as each recipient (`selection_queryset`), and spectra, photometry
  points and classes are checked for that recipient, per point; a class counts only where the recipient may
  see the object's data. A Slack channel gets only what every team member may see. An event waiting for a
  digest is checked again before it is sent and dropped if the object no longer matches or the spectrum or
  point is no longer visible. A Selection a recipient cannot view sends them nothing.
- **Volume estimate:** before saving, the form shows what the alert would have found in the last 30 days, as
  you see the Selection now (objects added, visible spectra added, recorded class changes, threshold
  crossings), and the number of messages at the chosen frequency. Only "Save alert" after the estimate saves.
- **The pass:** job kind `selection_alerts.run`, queued by the `SelectionAlerts` cron every
  `SELECTION_ALERTS_CRON_MINUTES` (15) when `SELECTION_ALERTS_ENABLED` is on (default **off**: the cron and
  the job do nothing). The first pass after an alert is set only records the baseline. A second pass right
  after the first sends nothing; comparisons and deliveries lock their rows, so two runners never send an event
  twice. Selections with more than `SELECTION_ALERTS_MAX_MATCHES` (2000) matches for a recipient are skipped.
  Slack posts go through the `selection_alerts.slack` job.

Server steps: none (flag off). Turning sending on is in `docs/ziggy-runbook.md`, "Alerts on Selections",
when Ryan asks.
