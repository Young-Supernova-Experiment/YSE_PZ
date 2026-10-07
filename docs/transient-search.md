# Transient search filters

Issues #284 (umbrella), #285, #286, #287 (SkyPortal parity: "Sources search with ~40 filters").
One django-filter `FilterSet`, `YSE_App/filters/transient_search.py::TransientSearchFilterSet`, serves
both the search page (`/search/`, `SearchResultsView`) and the REST list (`/api/transients/`,
`TransientViewSet.filterset_class`). The same query string means the same rows on both, and the page's
URL is the whole state of a search, so it can be pasted into Slack.

## Using the page

* The header box still takes a name (`2026abc`, matched anywhere in the name) or coordinates
  (`10.5 -20.25`, `10.5, -20.25, 30`, `00:42:00 +41:16:00`, `00 42 00 +41 16 00 12`; the optional last
  number is the radius in arcseconds, default 5). The box's `q` is expanded into the form fields, so the
  page shows what the quick search meant and the URL is shareable.
* The form has six collapsible groups (Names, Position, Time, Photometry, Classification, Relations); a
  group with active filters shows a count and opens by default. Active filters are listed as chips
  with a remove link. "Order by", "Per page" (25/50/100/200), the table's column sort (`sort=`) and the
  page number all live in the query string. "Copy link" copies the URL; "API" opens the same search on
  `/api/transients/`.
* Multi-selects repeat the parameter: `?status=New&status=Watch`, `?tags=young&tags=old`.
  Booleans take `true` / `false`. Dates take `YYYY-MM-DD` or ISO 8601 (`2026-09-01T12:00:00Z`);
  a bare date is midnight UTC, so `disc_date_before=2026-09-01` is "before the start of Sept 1".

## Filters

| group | parameter | meaning |
|---|---|---|
| names | `name` | exact name |
| | `name_contains` | case-insensitive substring of the name |
| | `alias` | substring of the name or of any `AlternateTransientNames` row |
| | `has_tns_name` | name looks like a TNS name (`20YYabc`, 1 to 4 letters) |
| position | `ra`, `dec`, `radius_arcsec` | cone search; centre in decimal degrees or sexagesimal (hours for RA), radius default 5"; results get a `separation` (degrees) annotation and, with no other ordering, are sorted by it. The page shows a `Sep. (arcsec)` column |
| | `ra_gte`, `ra_lte`, `dec_gte`, `dec_lte` | coordinate box in degrees (legacy API names) |
| | `gal_b_abs_min`, `gal_b_abs_max` | absolute galactic latitude range (degrees), on the stored `gal_b` column |
| time | `disc_date_after`, `disc_date_before` | discovery date window |
| | `days_since_disc_max` | discovered within the last N days |
| | `created_after`, `created_before`, `modified_after`, `modified_before` | row created / modified windows (`created_date_gte`, `modified_date_gte` still work) |
| photometry (`TransientPhotStat`, #268) | `peak_mag_min`, `peak_mag_max` | brightest detection |
| | `latest_mag_min`, `latest_mag_max` | most recent detection |
| | `num_det_min`, `num_det_max` | number of detections |
| | `first_det_after`, `first_det_before`, `last_det_after`, `last_det_before` | first / last detection date windows |
| | `days_since_last_det_max` | last detection within the last N days |
| | `rise_rate_min/max`, `decay_rate_min/max` | mag/day, both positive numbers |
| | `deepest_limit_min`, `deepest_limit_max` | deepest upper limit |
| | `peak_mag_lte`, `peak_mag_gte`, `last_det_mag_lte/gte`, `last_det_mjd_gte/lte`, `first_det_mjd_gte/lte`, `num_det_gte`, `rise_rate_gte`, `decay_rate_gte` | the #341 API names, kept |
| classification | `spec_class`, `photo_class` | `best_spec_class` / `photo_class` name is one of the given |
| | `classification` | either the spectroscopic or the photometric class is one of the given |
| | `exclude_class` | neither class is one of the given (SkyPortal's "nonclassifications") |
| | `has_redshift`, `redshift_min`, `redshift_max` | on the transient's redshift, else the host's (`best_redshift`, what the tables show) |
| relations | `status`, `status_in` | status name(s); `status_in=New,Watch` is the legacy comma form |
| | `obs_group`, `internal_survey` | by name |
| | `tags`, `tags_all`, `tag_in` | any of / all of the tag names (`tag_in` is the legacy comma form) |
| | `has_spectrum` | a spectrum the requesting user may see (group rules of `services/visibility.py`) |
| | `has_followup`, `followup_status` | any follow-up / a follow-up in one of the given statuses |
| | `has_comment` | a transient-level comment (`Log` row without a follow-up) |
| | `has_host`, `host_redshift_min`, `host_redshift_max` | host presence and redshift |
| | `has_hst`, `has_jwst`, `has_chandra` | archive coverage flags on the transient (`true` = known to have data, `false` = known not to; never looked up matches neither). `has_jwst` is set by the detail page's JWST lookup (#383, `docs/archive-tabs.md`) |
| | `visible_to_group` | photometry or spectra shared with that collaboration group; non-staff may only ask about groups they belong to (anything else matches nothing) |
| ordering | `ordering` | `name`, `ra`, `dec`, `disc_date`, `created_date`, `modified_date`, `redshift`, `best_redshift`, `mw_ebv`, `peak_mag`, `peak_mjd`, `last_det_mag`, `last_det_mjd`, `first_det_mjd`, `num_det`, `rise_rate`, `decay_rate`, `deepest_limit`, `separation` (cone only), `gal_b`; prefix `-` for descending; a primary-key tie-breaker is always appended |

Photometry filters read the stat row, so a transient without one (no unflagged points, or the
backfill in `docs/photstat.md` not yet run) never matches them.

### Annotations (#319)

`annotation_origin`, `annotation_key`, `annotation_value_eq`, `annotation_value_min`,
`annotation_value_max` and `annotation_column` (an `origin.key` shown as an extra, sortable
results column). They read the indexed `TransientAnnotationValue` side table, or the `Transient`
column itself for `annotation_origin=legacy`. Details in `docs/annotations.md`.

## How it stays one query

* Cone: `getRADecBox` gives an RA/Dec bounding box (wrap-safe at RA 0/360, RA unconstrained when a pole
  is inside the cone) on the indexed `ra` / `dec` columns (#248), then the exact spherical-cosine
  separation is an annotation the WHERE clause compares to the radius.
* Galactic latitude: the stored, indexed `Transient.gal_b` column (#286, below). `gal_b_abs_min` /
  `gal_b_abs_max` compile to plain range predicates (`gal_b >= v OR gal_b <= -v`, `-v <= gal_b <= v`), no
  `ABS()`, so the `yse_transient_gal_b_idx` index is usable; `ordering=gal_b` sorts the column.
* Relations (spectra, follow-ups, comments, tags, group visibility, aliases) are `EXISTS` subqueries,
  so no `DISTINCT` and no row multiplication; stat columns are the `photstat` LEFT JOIN.
* `ModelMultipleChoice` filters (status, classes, tags, groups) filter on primary keys of the chosen
  rows; the choice lists cost one small query each when the page renders the form.
* The page lists rows through `annotate_dashboard_transient_fields` (the dashboard's `select_related`
  and recent-photometry subqueries, `PageFirstQuerySet` page slicing) plus `select_related('best_spec_class')`,
  so a page of results is the id query, the row query and the paginator's COUNT regardless of how many
  rows match (`test_query_count_does_not_grow_with_rows`).
* Both MySQL and sqlite are covered: the tests run on both in CI/docker; Django registers `ACOS`,
  `ASIN`, `RADIANS`, `DEGREES`, `SIN`, `COS` and `REGEXP` for sqlite.

## API notes

* `/api/transients/?ra=10.0&dec=20.0&radius_arcsec=60&ordering=separation`
* `/api/transients/?has_spectrum=true&exclude_class=SN%20Ia&days_since_last_det_max=5&ordering=-peak_mag`
* `/api/transients/?tags_all=young&tags_all=YSE&status=New&status=Watch`
* Invalid input (an unknown status name, a cone with only RA) is a 400 with the form errors; the search
  page shows the same message above the form and renders the rest of the search.
* Non-staff users of the API still go through `filter_transients_by_user_access` before the filters
  (unchanged from before this work).

## Saving a search as an SQL query (personal dashboard)

The "Save as SQL query" button on the search page (and `POST /api/transients/save_search/`) turns the
current filters into a django-sql-explorer `Query`, the same kind of saved query the personal dashboard,
Summary View and the bulk photometry download already use:

* `YSE_App/services/search_queries.py::compiled_search` compiles the FilterSet's queryset for the
  `explorer` connection (`values('name')`, ordering kept) and inlines the parameters as SQL literals
  (`sql_literal`: numbers bare, strings single-quoted, backslashes doubled only on MySQL, datetimes as the
  backend's UTC text). The statement is one `SELECT YSE_App_transient.name FROM ... WHERE ... ORDER BY ...`
  that passes `dashboard_sql_is_supported`; the tests run it on the `explorer` connection (sqlite and
  docker MySQL) and check it returns exactly the rows the ORM returns, in the same order.
* Relative-time filters (`days_since_disc_max`, `days_since_last_det_max`) compile to database time
  (`UTC_TIMESTAMP() - INTERVAL n SECOND`, `UNIX_TIMESTAMP()/86400 + 40587`; `datetime('now', ...)` and
  `julianday('now')` on sqlite), so a saved "last detected within 5 days" keeps moving. Every other value
  is frozen as typed. Filters that depend on who saves (`has_spectrum` for a non-staff user,
  `visible_to_group`) bake that user's group membership into the SQL.
* The modal asks for a title (suggested from the active filters; must be unique, because Summary View
  and the download look queries up by title), has an "Add to my personal dashboard" box (checked by
  default; creates the `UserQuery` row, once) and shows the SQL that will be saved. The description of
  the `Query` lists the filters and the shareable search URL. A search that does not validate (an
  unknown status, a cone with only RA) cannot be saved and the modal says why.
* After saving, the page confirms with the title, a link to the query in the Query Explorer (staff, who
  may edit queries there) and to the dashboard section. Anyone can pick the query in "Add Dashboard
  Query" on their own dashboard.
* API: `POST /api/transients/save_search/` with `title`, optional `add_to_dashboard` and either
  `params` (JSON object, list values for multi-selects) or `query_string`; returns `query_id`,
  `explorer_url`, `sql`, `user_query_id`, `dashboard_url`; 400 with the reason otherwise.

## Stored galactic coordinates and indexes (#286)

`Transient.gal_l` / `Transient.gal_b` (degrees, J2000, IAU 1958 galactic frame) are real columns:

* `Transient.save()` fills them from `ra` / `dec` with `YSE_App/common/galactic.py::galactic_coords`
  (a closed-form rotation; the tests check it against astropy to 1e-4 deg), also when
  `save(update_fields=[...])` names `ra` or `dec`. The fields are `editable=False`, so forms and the
  admin never show them; `/api/transients/` exposes them read-only.
* Migration `0027_transient_galactic_coords` adds the columns, the `gal_b` index and the remaining
  stat-row indexes (`TransientPhotStat.first_detected_date`, `last_detected_date`, `rise_rate`,
  `decay_rate`, `deepest_limit`; `peak_mag`, `last_detected_mag`, `last_detected_mjd`,
  `first_detected_mjd`, `num_det_global`, `last_obs_date` were indexed by #268) and fills every existing
  row in one `UPDATE` evaluated by the database (`galactic_latitude_expression()` /
  `galactic_longitude_expression()`, the same formula in SQL). On sqlite the trig functions Django
  registers are used, so tests and the migration run on both backends.
* Rows written around `save()` (`bulk_create`, raw SQL, `QuerySet.update(ra=..., dec=...)`) keep
  `NULL` and never match a `|b|` cut until `python manage.py backfill_galactic_coords` (rows with
  `gal_b IS NULL`; `--all` recomputes every row) runs: one `UPDATE`, seconds for 1e5 rows. Nothing in
  the ingest paths does that today; the command exists for the case.
* The search page shows a **Gal. b (deg)** column when a `|b|` filter or `gal_b` ordering is active.

### Acceptance benchmark

`YSE_App/perf/search_filters.py` seeds transients with a stat row each and times the acceptance search
of #286, `gal_b_abs_min=10&peak_mag_max=19&num_det_min=3&days_since_last_det_max=5`, as the
FilterSet queryset (`search_acceptance_sql`) and as `GET /search/` (`search_acceptance_page`);
`test_search_perf_regression` gates both against `perf_baselines.json` (1000 ms / 2500 ms, not scaled
with the size) at 2000 rows, and `YSE_PERF_SEARCH_TRANSIENTS=100000 YSE_PERF_SEARCH_EXPLAIN=1` runs
the production-sized tier and prints the plan. Recorded on docker MySQL 8.0.25, 100 000 transients +
stat rows (2026-09-30): queryset 106 ms, page 723 ms; the plan is an index range scan on
`yse_photstat_last_det_mjd_idx` (18 728 of 100 000 rows), the `peak_mag` / `num_det` filters on those
rows, then a primary-key lookup of the transient with the `gal_b` range predicate. With a less
selective time window MySQL can start from `yse_transient_gal_b_idx` instead; either way no full
scan. Production `EXPLAIN` after the deploy (`docs/dashboard-performance.md`, "Checks before
promoting") is still the go/no-go for keeping each new index.

## Not in this change (rest of #284)

* Bulk actions on results (status change, tag, photometry download) and CSV export (#287); saving a
  search is covered above.
* Annotation-origin filters (#319, after #317).

## Tests

`YSE_App/tests/test_transient_search.py`: one fixture (five transients differing in every filtered
property, one with private photometry and spectra), one test per filter family, a one-query
assertion per filter, quick-search parsing, page rendering (form, chips, hidden legacy parameters,
sort / per-page, error display, JS syntax check) and API parity / legacy names / 400s.
