# Per-transient photometry statistics (`TransientPhotStat`)

Issue #268 (umbrella #267, SkyPortal parity). One row per transient stores what the dashboards, the
follow-up tables, the Bazin column and the saved Explorer queries otherwise rebuild from the raw
`YSE_App_transientphotdata` table on every request: the number of points and detections, the first,
last and peak detection, the mean and faintest magnitude, the deepest upper limit, the rise and decay
rate and the time from the last upper limit to the first detection.

## What counts

| term | rule | same as |
|---|---|---|
| detection | `mag` set and no `data_quality` flag | `Transient.recent_mag()`, the `recent_mag` table columns, `_recent_phot_subqueries` |
| upper limit | no `mag`, `flux`, `flux_err`, `flux_zero_point` set and `flux + 3 flux_err > 0`; limit `= -2.5 log10(flux + 3 flux_err) + zp` | the light-curve plot (`view_utils.lightcurveplot`) |
| ignored | any point with a `data_quality` flag | everything above |

So `last_detected_mag` / `last_obs_date` are exactly the values the dashboard "Last Mag" / "Last Obs. Date"
columns compute with two subqueries per row today; those columns can be switched to the stat table in the
follow-up work (#270, #331).

## Columns (`YSE_App_transientphotstat`)

| column | meaning |
|---|---|
| `num_obs_global`, `num_det_global`, `num_limits_global` | unflagged points, detections, upper limits |
| `last_obs_mjd` / `last_obs_date` | latest unflagged point of any kind |
| `first_detected_mjd/_date/_mag/_band_id`, `last_detected_*` | first and last detection |
| `peak_mjd/_date/_mag/_band_id` | brightest detection (ties: the earlier one) |
| `mean_mag`, `faintest_mag` | over detections |
| `deepest_limit`, `deepest_limit_mjd` | largest limiting magnitude among upper limits |
| `last_non_detection_mjd`, `time_to_non_detection` | last upper limit before the first detection and the gap in days |
| `rise_rate` | mag/day, positive: `(first_mag - peak_mag) / (peak_mjd - first_mjd)` in the band of the first detection; NULL when that detection is the band's peak |
| `decay_rate` | mag/day, positive: `(last_mag - peak_mag) / (last_mjd - peak_mjd)` in the band of the last detection; NULL when that detection is the peak |
| `per_band_json` | `{"<band_id>": {name, n_det, peak_mag, peak_mjd, first_mag, first_mjd, last_mag, last_mjd}}` |
| `phot_hash`, `last_updated` | fingerprint of the stored values (an unchanged recompute writes nothing) and when it last changed |

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
* `manage.py rebuild_photstats` for the backfill and for repairs (below).

## Surfaces

* Transient detail, Summary tab, "Photometric Summary": a **Peak Mag** row (date, mag, filter) and, right
  below the table, a **Photometry statistics** table: Detections (of N unflagged points), Upper limits (deepest,
  with its date), Rise rate and Decay rate (mag/day, band), Last non-detection before discovery (date, MJD) and
  Time to non-detection (days). A transient with **no stat row yet** gets one computed and stored on that first
  page view (`services.photstat.stat_for_transient`: one pass over that transient's photometry, at most once per
  request; a failure is logged and the block is left out, the page still renders). So the detail page does not
  depend on the backfill below; the dashboard column, its ordering and the API filters do.
* Dashboard tables (`TransientTable`): sortable **Peak Mag** column. The value is a `LEFT JOIN` on the stat
  table (`annotate_peak_mag`), no per-row query.
* API: `/api/transientphotstats/` (read-only; filters `transient_name`, `peak_mag_lte/gte`,
  `last_det_mag_lte/gte`, `last_det_mjd_gte/lte`, `first_det_mjd_gte/lte`, `num_det_gte`, `rise_rate_gte`,
  `decay_rate_gte`; `ordering=peak_mag,-last_detected_mjd,...`). `/api/transients/` accepts the same
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

No new settings.
