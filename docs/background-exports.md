# Background exports with no object cap (#855)

Downloads of photometry and spectra for many transients are capped at 300 objects
(`SEARCH_BULK_PHOTOMETRY_MAX_ROWS`). With `BACKGROUND_EXPORTS_ENABLED` on, a request over the
cap runs as a background job instead of being refused. The user gets a notification with a link
when the file is ready.

Off by default. It needs the job runner (`manage.py run_jobs`, see
[background-jobs.md](background-jobs.md)) on the stack. While it is off, the caps and messages
are exactly as before.

## Where it applies

| request | formats | over the cap, flag on |
|---|---|---|
| Search page, Download -> Photometry (preview layout) | `csv`, `snana`, `ecsv` | queued |
| Search page, Download -> Spectra (preview layout) | `csv` (zip with manifest) | queued |
| Selection page, Download -> Photometry / Spectra (preview layout) | as above | queued |
| Search page, bulk action "photometry" (ticked rows or all matches), preview layout | `csv` | queued |
| Bulk action, preview off | `csv` | refused, as before |

Formats:

- `csv`: the bulk photometry zip (one CSV per transient and a README).
- `snana`: a zip with one SNANA file per transient, the same file as the transient page's SNANA
  download (`download_photometry`), plus a README.
- `ecsv`: one combined table, the rows of the CSV with a `transient` column. Written with a header
  from astropy (`delimiter: ','`); read it with `astropy.table.Table.read(path, format="ascii.ecsv")`.
- Spectra: the spectra zip (one CSV per spectrum, `manifest.csv`, `README.txt`).
- Parquet is not offered: pyarrow is not in `requirements.txt`.

Under the cap the same formats are downloaded directly. The download options (#848: MJD and phase
ranges, bands, flagged points, instruments, S/N) apply to both.

## Access

The job stores only where the transients come from (the search's query string, the Selection's
slug, or the ticked ids), the format and the options. Nothing is read when the job is queued.

When the job runs, the requesting user (`Job.created_by`) is used for everything:

- the transients are the ones they may list now (`filter_listed_transients`, or the Selection's
  own rule, which also checks they may still see the Selection);
- points and spectra come through the same per-point and per-spectrum calls as the interactive
  download, so the file is the same as that download for the same request.

The file is downloadable only by that user, at `/jobs/exports/<job id>/download/`. Anybody else
gets a 404. `/jobs/exports/` lists the user's exports. Staff also see their own exports on `/jobs/`.

## Files and retention

Files are written with Django's default storage, under `MEDIA_ROOT/exports/<job id>-<random>/`
(`MEDIA_ROOT` is `<checkout>/media` unless overridden; the analysis-service files use the same
place). Apache must not serve this directory directly.

Files are deleted `EXPORT_RETENTION_DAYS` after they were written. Every export job runs the
cleanup first, and `manage.py prune_exports [--dry-run]` runs it by hand. The job rows
themselves go after `JOB_RETENTION_DAYS` (the daily prune of background-jobs.md). The retention
period is not decided yet (default 7 days).

## Notifications

`services.notify.notify()` with kind `job_result` ("System and jobs": in-app on, email and Slack
off unless the user turns them on), linking to `/jobs/exports/`. A request that cannot be built
(an invalid search, a Selection that is gone or no longer shared) fails at once, with a
notification that says why.

## Settings (`settings.ini` `[site_settings]`)

| key | default | meaning |
|---|---|---|
| `BACKGROUND_EXPORTS_ENABLED` | False | queue over-cap downloads as jobs (env `YSE_BACKGROUND_EXPORTS=1`) |
| `EXPORT_RETENTION_DAYS` | 7 | days a finished file is kept (at least 1; env `YSE_EXPORT_RETENTION_DAYS`) |

## Large exports

The cron pass of the job runner does not interrupt a job, so a large export can hold one pass for
minutes. A `running` job locked for longer than `JOB_RUNNER_STALE_MINUTES` (60) is queued again.
For very large exports run the systemd worker of background-jobs.md.

Server steps: [docs/ziggy-runbook.md, Background exports](ziggy-runbook.md#background-exports).
