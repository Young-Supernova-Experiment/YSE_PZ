# Per-transient photometry statistics (`TransientPhotStat`)

Issue #268 (umbrella #267, SkyPortal parity). One row per transient stores what the dashboards, the
follow-up tables, the Bazin column and the saved Explorer queries otherwise rebuild from the raw
`YSE_App_transientphotdata` table on every request: the number of points and detections, the first,
last and peak detection, the mean and faintest magnitude, the deepest pre-detection upper limit (with its
band), the rise and decay rate and the time from the last upper limit to the first detection.

## What counts

| term | rule | same as |
|---|---|---|
| detection | `mag` and `mag_err` set, no `data_quality` flag, and either no `flux`/`flux_err` or `mag_err <= 0.36` (S/N 3) | the markers of the detail-page light-curve plot (`view_utils.lightcurveplot_detail`), the Bazin fit's `is_usable_detection` |
| upper limit | `flux` and `flux_err` set, `flux_err != 0`, `flux / flux_err < 3`, no `data_quality` flag; limit `= -2.5 log10(flux + 3 flux_err) + zp` (`zp` 27.5 when the row has none); a point that is also a detection counts as the detection; **counted only before the first detection** (every limit when there is no detection, #349) | the inverted triangles of the same plot |
| ignored | any point with a `data_quality` flag | `Transient.recent_mag()`, the Bazin fit |

Both predicates live in `services/phot_points.py` and the plot builds its masks from them (#368), so the
limits the Photometry statistics block counts are the triangles drawn before the first marker. ZTF forced
photometry uploads a `mag`/`mag_err` for every epoch, S/N < 3 included; until #368 those rows counted as
detections and a transient like 2022abom reported no pre-detection limits while the plot showed them.

`last_detected_mag` / `last_obs_date` match the dashboard "Last Mag" / "Last Obs. Date" columns
(`_recent_phot_subqueries`: latest unflagged `mag`) except when the latest unflagged row is a
forced-photometry magnitude noisier than 0.36 mag, which the statistics do not call a detection; switching
those columns to the stat table is follow-up work (#270, #331).

## Columns (`YSE_App_transientphotstat`)

| column | meaning |
|---|---|
| `num_obs_global`, `num_det_global`, `num_limits_global` | unflagged points (of any kind), detections, pre-detection upper limits |
| `last_obs_mjd` / `last_obs_date` | latest unflagged point of any kind |
| `first_detected_mjd/_date/_mag/_band_id`, `last_detected_*` | first and last detection |
| `peak_mjd/_date/_mag/_band_id` | brightest detection (ties: the earlier one) |
| `mean_mag`, `faintest_mag` | over detections |
| `deepest_limit`, `deepest_limit_mjd`, `deepest_limit_band_id` | largest limiting magnitude among the pre-detection upper limits, when and in which band |
| `last_non_detection_mjd`, `last_non_detection_band_id`, `time_to_non_detection` | last upper limit before the first detection, its band, and the gap in days |
| `rise_rate` | mag/day, positive: `(first_mag - peak_mag) / (peak_mjd - first_mjd)` in the band of the first detection; NULL when that detection is the band's peak |
| `decay_rate` | mag/day, positive: `(last_mag - peak_mag) / (last_mjd - peak_mjd)` in the band of the last detection; NULL when that detection is the peak |
| `per_band_json` | `{"<band_id>": {name, n_det, peak_mag, peak_mjd, first_mag, first_mjd, last_mag, last_mjd, n_limits, deepest_limit, deepest_limit_mjd, last_limit_mjd}}` (a band with only pre-detection limits has `n_det: 0`) |
| `phot_hash`, `last_updated` | fingerprint of the stored values (an unchanged recompute writes nothing) and when it last changed |
| `schema_version` | `services.photstat.SCHEMA_VERSION` when the row was written (2 since #349, 3 since #368). A smaller value marks a row computed under older rules: the detail page recomputes it when opened, the next signal rewrites it, `rebuild_photstats --stale-only` visits only those rows |

Indexes: `peak_mag`, `last_detected_mjd`, `last_detected_mag`, `last_obs_date`, `num_det_global`,
`first_detected_mjd`.

## How rows stay current

* `YSE_App/services/photstat.py`: `recompute(transient_id)` (one photometry query, one stat query, a write
  only when a value changed), `recompute_many(ids)` (batched, for the command) and the
  `deferred_updates()` context manager that collects requests and recomputes each transient once on exit.
* Signals (`YSE_App/signals.py`): `post_save` / `post_delete` on `TransientPhotData` and `m2m_changed` on its
  `data_quality` flags. A delete never creates a missing row (a `Transient` cascade may be in flight).
* Bulk paths wrapped in `deferred_updates()`: `data_utils.add_transient` (bulk inserts fire no signals, so it
  schedules each transient explicitly), `data_utils.add_transient_phot`, `Query_LSST.store_points`. Other
  crons write rows one at a time and are covered by the signals.
* `manage.py rebuild_photstats` for the backfill and for repairs (below); `--stale-only` after a rules change
  (`SCHEMA_VERSION` bump) reads photometry only for missing or outdated rows.

## Surfaces

* Transient detail, Summary tab, "Photometric Summary": a **Peak Mag** row (date, mag, filter) and, right
  below the table, a **Photometry statistics** table: Detections (of N unflagged points), Upper limits
  (pre-detection count; deepest with band and date), Rise rate and Decay rate (mag/day, band), Last non-detection
  before discovery (date, MJD, band) and Time to non-detection (days). A transient with **no stat row yet** gets one computed and stored on that first
  page view (`services.photstat.stat_for_transient`: one pass over that transient's photometry, at most once per
  request; a failure is logged and the block is left out, the page still renders); a row written under an older
  `schema_version` is recomputed the same way (integer compare, no photometry read for a current row). So the
  detail page does not depend on the backfill below; the dashboard column, its ordering and the API filters do.
* Dashboard tables (`TransientTable`): sortable **Peak Mag** column. The value is a `LEFT JOIN` on the stat
  table (`annotate_peak_mag`), no per-row query.
* API: `/api/transientphotstats/` (read-only; filters `transient_name`, `peak_mag_lte/gte`,
  `last_det_mag_lte/gte`, `last_det_mjd_gte/lte`, `first_det_mjd_gte/lte`, `num_det_gte`, `rise_rate_gte`,
  `decay_rate_gte`, `deepest_limit_gte`, `num_limits_gte`, `deepest_limit_band=<band name>`,
  `last_non_detection_band=<band name>`; `ordering=peak_mag,-last_detected_mjd,...`; rows carry
  `deepest_limit_band(_name)`, `last_non_detection_band(_name)` and `schema_version`). `/api/transients/` accepts the same
  `peak_mag_lte` ... filters and `ordering=peak_mag|peak_mjd|last_det_mag|last_det_mjd|first_det_mjd|num_det|rise_rate|decay_rate`.
* Explorer: join `YSE_App_transientphotstat ps ON ps.transient_id = t.id`.

## Ziggy runbook (David)

After the migration (`0011_transientphotstat`) is applied by the deploy, backfill once per database:

```
cd /data/yse_pz/YSE_PZ_test        # yse_test / yse_experimental (shared YSE_test DB)
/data/yse_pz/yse_test_virtual/bin/python manage.py rebuild_photstats
```

and on production after the promotion to `master`:

```
cd /data/yse_pz/YSE_PZ
<PROD_PYTHON> manage.py rebuild_photstats
```

Expected output: one `batch N-M of T: created ..., updated ..., unchanged ...` line per 500 transients and a
final `rebuild_photstats: processed T transient(s); created T, updated 0, unchanged 0 in S s`. The command
reads all photometry once (about 10^5 transients, 10^6-10^7 points): budget 10-30 minutes on Ziggy; it is
safe to interrupt and re-run (`--missing-only` resumes without touching finished rows). Until the backfill
has run, the Peak Mag column (and the `/api/transientphotstats/` filters and ordering) are simply empty for
transients that have not had a photometry upload since the deploy; the detail page fills its own row the first
time someone opens the transient (#345), so a page that has been visited also shows up in the column. Re-running the full command later is harmless: unchanged rows are
skipped (`updated 0, unchanged T`).

**After #349** (migration `0015_photstat_limit_bands`: limit bands, `schema_version`; pre-detection limits only) the
rows written before it are outdated. On a database where the backfill already ran, re-run it once with
`--stale-only` (only outdated or missing rows are read; idempotent, a second run reports `processed 0`):

```
cd /data/yse_pz/YSE_PZ_test && /data/yse_pz/yse_test_virtual/bin/python manage.py rebuild_photstats --stale-only
```

Until then an outdated row is refreshed the first time someone opens the transient, and the plain command above
does the same job (it rewrites every row once).

**After #368** (`SCHEMA_VERSION` 3, the plot's detection / upper-limit rules; no migration) every row is outdated
again. Nothing needs to run: a row is recomputed the first time someone opens the transient, and the next
photometry upload for the transient rewrites it. To refresh every row at once (so the API and the search
filters on `num_limits_gte` / `deepest_limit_gte` see the new values before anyone opens the page), the same
`rebuild_photstats --stale-only` command applies.

No new settings.
