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
| `rules` | JSON rule tree (kind `rules`, #843; see "Rule Selections" below) |
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
  - `rules`: the rule-tree compiler of `services/selection_rules.py` (#843), registered with `register_compiler`.
  - It raises `SelectionAccessError` or `SelectionError` (the message can be shown to the user).
- **Sharing helpers:** `visible_selections(user)`, `editable_selections(user)`, `can_view`, `can_edit`, `can_delete`, `team_choices(user)`.
- **Changing Selections:**
  - `create_selection(owner, name, kind=..., visibility=..., team=..., search_params=..., query=..., python_query=..., rules=...)`;
  - `update_selection(user, selection, name=..., visibility=..., team=..., description=..., search_params=..., rules=...)`;
  - `selection_from_user_query(owner, user_query)` links a dashboard panel without changing it. Existing `UserQuery` rows and panels are untouched.
- `register_compiler(kind, func)` plugs in a new criteria kind (e.g. `rules`, #843). Start from `base_queryset(user)`.
- `criteria_summary(selection, user)` returns `[(label, value)]` for display.

## Pages (UI preview only; 404 otherwise)

- **Save as Selection** on Transient Search (`POST selections/save/`). Facet choices (#842) are ordinary search parameters, so they are saved too.
- **My Selections** (`selections/`): mine, my teams', all-YSE. Rename, re-share and delete where allowed.
- **A Selection's results** (`selections/<slug>/`): the search table, with:
  - list stepping into the transient page (#850; `ls=sel`, `lt=<slug>`, `lq=` the page/sort);
  - "Observable tonight" (#854) for up to 500 matches.

## Rule Selections and the rule builder (#843, UI preview only)

A `rules` Selection stores a tree of rows (field, comparison, value) in nested AND/OR groups:

```json
{"op": "and", "rules": [
  {"field": "peak_mag", "operator": "brighter", "value": "19"},
  {"op": "or", "rules": [
    {"field": "status", "operator": "any", "value": ["New", "Watch"]},
    {"field": "has_spectrum", "operator": "is", "value": "true"}]}]}
```

- **Fields** come from `RULE_FIELDS` in `YSE_App/services/selection_rules.py`, a registry of
  human-readable names over the transient search filters (`filters/transient_search.py`), never
  database columns. Magnitudes read "brighter than" (the search's "at most") and "fainter than".
  Choice fields offer "is any of" and "is none of" (tags: has any / all / none of). The spectral
  phase filters (#848) are offered for each phase reference.
- **Compiling.** Each rule runs the search filter it stands for, for the viewer, so its value is
  validated by that filter's own form field (numbers, dates, known names) and per-point
  visibility applies as on `/search/`. Groups combine the rules with `Q` objects (`AND`, `OR`;
  "none of" is `NOT`). The result starts from `base_queryset(user)` and `selection_queryset`
  applies the listing rule again. Values reach the database only as bound parameters.
- **Limits:** groups nest at most 4 deep, and a tree has at most 40 rules. Empty nested groups are dropped;
  an empty tree selects every listed transient.
- **Pages:**
  - The builder renders in the search page's facet sidebar (`#search-rule-builder-slot`) and, for
    editors, in a rule Selection's Edit panel (where "Save rules" posts `rules` to `selections/<slug>/edit/`).
  - Rows are server-rendered; `static/YSE_App/rule-builder.js` (jQuery) adds and removes them by
    cloning `<template>` prototypes. All controls are native buttons, selects and inputs.
  - `POST selections/rule-builder/check/` (`rules`, plus `count=1` and/or `sql=1`) returns the summary,
    the viewer's match count and "View as SQL": the query Django sends for the rules (read-only, with
    values shown in place for reading). `POST selections/rule-builder/save/` is "Save as Selection".
  - Users without the preview get 404 on both, and the search page does not change for them.

## REST API (not gated; read-only, any authenticated user)

- `GET /api/selections/`: the Selections you may view (`?kind=`, `?visibility=`, `?mine=true`).
- `GET /api/selections/<slug>/`: one Selection. It never includes SQL text.
- `GET /api/selections/<slug>/transients/`: the matching transients for you (`id, name, slug, ra, dec`), with `limit` / `offset`.

## Downloads (#848, UI preview only)

A Selection's page has **Download -> Spectra / Photometry** with phase, instrument, S/N, date, band and
quality options (`docs/spectral-phase.md`). The search filters `spec_phase_min` / `spec_phase_max` /
`spec_phase_ref` work in search Selections like every other filter.

Server steps: none.
