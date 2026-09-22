# Deploy checklist → CI coverage

The pre-release checklist the team runs by hand before merging to `master`, mapped
to the Django test suite that `.github/workflows/ci.yml` runs
(`manage.py test YSE_App.tests` inside docker compose against MySQL).

Legend: **covered** = the flow is exercised end to end in CI; **partial** = the
web/DB half is exercised, an external system or an import-time form quirk is not;
**manual** = cannot run in CI, with the reason.

All new modules live in `YSE_App/tests/`:
`test_deploy_checklist_flows.py`, `test_deploy_checklist_pages.py`,
`test_cron_smoke.py`, plus the shared `deploy_checklist_helpers.py`.

## Deploy checks

| Checklist item | Status | Test(s) |
| --- | --- | --- |
| Upload data from TNS | partial | `test_tns_import_groups` (`TnsPhotometryUploadShapeTests`, `TnsImportGroupsPersistTests.test_add_transient_via_http_marks_tns_photometry_public`) covers TNS payload parsing + `/add_transient/` upload; `test_cron_smoke` runs `TNS_recent`/`TNS_updates`/`TNS_Ignore_updates`/`TNS_recent_realtime`/`TNS_emails` with the network stubbed. The live TNS API fetch itself is **manual** (needs the TNS bot key and network). |
| Upload spectra | covered | `DeployChecklistFlowTests.test_upload_spectrum_form_renders`, `.test_upload_spectrum_form_creates_spectrum_and_points` (browser form), `.test_add_transient_spec_api_basic_auth` (script/API path) |
| Sorting: summary view | covered | `DeployChecklistPageTests.test_summary_view_sorts_by_name_and_last_obs_date` |
| Sorting: dashboard (New table) | covered | `DeployChecklistPageTests.test_dashboard_new_table_sorts_by_name`, `.test_dashboard_tables_sort_by_other_columns` |
| Sorting: dashboard (other tables) | covered | `DeployChecklistPageTests.test_dashboard_other_tables_sort_by_name` (AJAX status fragments; guards the "only the New table sorts" regression) |
| Download a spectrum | covered | `DeployChecklistFlowTests.test_download_spectra_returns_zip_of_csv` |
| Upload photometry | covered | `DeployChecklistFlowTests.test_add_transient_phot_api_basic_auth`; `test_tns_import_groups.test_add_transient_phot_util_applies_public_from_tns_upload` |
| Add followup request | covered | `test_followup_requests.FollowupRequestTests` (service + form + AJAX), `DeployChecklistFlowTests.test_followup_request_appears_on_calendar_night_and_resources` |
| Followup request shows in observing calendar | covered | `DeployChecklistFlowTests.test_followup_request_appears_on_calendar_night_and_resources` (observing calendar lists the run, observing-night page lists the request, transient-detail resources table lists the night); `test_security_matrix_resources.test_observing_calendar_shows_only_authorized_runs` |
| Add to on-call schedule | partial | `DeployChecklistFlowTests.test_oncall_schedule_entry_shows_on_calendar` (model + `/yse_oncall_calendar/`). The `OncallForm.user` field is built at import time from users in the `YSE` group, so the form POST cannot be exercised on a fresh test DB (on such a DB it 500s with `KeyError: 'user'`); **manual** for the form itself. |
| Add ToO resource | covered | `DeployChecklistFlowTests.test_add_too_resource_form_and_resources_table` (form, ToO calendar, transient-detail ToO table) |
| Add classical resource | covered | `DeployChecklistFlowTests.test_add_classical_resource_form_creates_resource_and_night` |
| Change status | covered | `DeployChecklistFlowTests.test_change_transient_status_via_api_patch` (the PATCH the status buttons send) |
| Change followup status | covered | `DeployChecklistFlowTests.test_change_followup_status_via_api_patch` |
| Schedule survey obs | partial | `DeployChecklistFlowTests.test_survey_obs_schedule_and_ingest_observation_record` (model + `/yse_observing_calendar/` + `/yse_observing_night/`). `SurveyObsForm.ztf_field_id` choices are built at import time, so the form POST needs pre-existing fields at process start: **manual**. |
| Add survey field | partial | `DeployChecklistFlowTests.test_add_survey_field_api_creates_field_and_msb` (`/add_yse_survey_fields/`, the path `yse_fields_new.bash` uses). `SurveyFieldForm.instrument` is built at import time (needs a `GPC*` instrument at process start): form POST **manual**. |
| All crons individually | partial | `test_cron_smoke.CronClassInventoryTests`, `CronDoSmokeTests` (see cron table below). Real data fetches are **manual**. |
| Add/remove queries to/from personal dashboard | covered | `DeployChecklistFlowTests.test_add_and_remove_personal_dashboard_query`; `test_ui_regressions.PersonalDashboardPaginationTests` |
| Check a summary view | covered | `DeployChecklistFlowTests.test_transient_summary_for_status`, `.test_add_and_remove_personal_dashboard_query` (saved-query summary), `DeployChecklistPageTests.test_summary_view_sorts_by_name_and_last_obs_date` |
| Forced photometry | partial | `DeployChecklistFlowTests.test_forced_photometry_request_logs_and_rate_limits` (`/ztf_forced_phot/` request + 12 h rate limit, ZTF client mocked); `test_cron_smoke` runs `YSE_Forced_Phot.ForcedPhot`/`ForcedPhotUpdate`/`ZTF_Forced_Phot_Cron.ForcedPhot` with I/O stubbed. Real IPP/ZTF forced photometry: **manual** (credentials + remote jobs). |
| Flux plots | covered | `DeployChecklistFlowTests.test_flux_plot_with_photometry_returns_html`; `test_lightcurve` (`test_lightcurveplot_flux_query_count_bounded`, `test_lightcurveplot_flux_empty_returns_empty_body`) |
| Ingesting YSE observation records | covered | `DeployChecklistFlowTests.test_survey_obs_schedule_and_ingest_observation_record` (`/add_yse_survey_obs/`, the path `yse_obs.bash` → `YSE_observations.SurveyObs` posts to); the IMAP side of `SurveyObs` is stubbed in `test_cron_smoke` |
| SALT fit | partial | `DeployChecklistFlowTests.test_salt2_plot_endpoints_without_fit_return_200` (`salt2fit=0` branch). The fit (`salt2fit=1`) needs the SALT3 model download via sncosmo plus multi-band data in `bandpassdict`; **manual**. Note: the fit has no error handling, so a failed fit is a 500 — Ryan's "salt fit broken?" is plausible and a try/except around `sncosmo.fit_lc` is a follow-up. |
| Download data: photometry | covered | `DeployChecklistFlowTests.test_download_photometry_snana_text` |
| Download data: all data | partial | `DeployChecklistFlowTests.test_download_data_json_bundle_within_ceiling` (200 + JSON shape + 20 s ceiling on a 6-point object). The prod timeout is data-volume dependent; **manual** on a large object, or raise a follow-up to stream/paginate. |
| Comments | covered | `test_phase2_comments.Phase2CommentTests` (AJAX add, non-AJAX add, fragment, API list/create, Slack signature/outbound) |

## Specific pages (checked against prod by hand)

CI cannot diff against prod. It checks that each page renders 200 without template
errors and shows the seeded rows.

| Page | Test(s) |
| --- | --- |
| Transient detail | `DeployChecklistPageTests.test_transient_detail_page`; `test_ui_page_loads`, `test_performance.TransientDetailPagePerformanceTests`, `test_page_load_regression` |
| Dashboard | `DeployChecklistPageTests.test_dashboard_page`; `test_ui_page_loads.test_dashboard`, `test_performance.MainDashboardPerformanceTests` |
| Personal dashboard | `DeployChecklistPageTests.test_personal_dashboard_page`; `test_performance.PersonalDashboardPerformanceTests` |
| SQL explorer | `DeployChecklistPageTests.test_sql_explorer_page`; `test_performance.ExplorerIndexPerformanceTests` |
| YSE schedule | `DeployChecklistPageTests.test_yse_schedule_pages` (`/yse_observing_calendar/`, `/yse_observing_night/<date>/`) |
| Observing schedule | `DeployChecklistPageTests.test_observing_schedule_pages` (`/observing_calendar/`, `/too_calendar/`, `/yse_oncall_calendar/`, `/calendar/`); `test_security_matrix_resources.test_observing_calendar_shows_only_authorized_runs` |
| Observing night page | `DeployChecklistFlowTests.test_followup_request_appears_on_calendar_night_and_resources` (`/observing_night/<tel>/<date>/None`) |
| Visual diff vs prod | **manual**: no prod access from CI |

## Crons

Each crontab line on the prod host wraps `manage.py runcrons <ClassPath>` (or a
plain script). The bash wrappers are not in the repo; the class is inferred from
the script name and `settings.CRON_CLASSES` where marked *(inferred)*.

`test_cron_smoke` (a) imports every `CRON_CLASSES` entry and checks it is a
`CronJobBase` with a `Schedule`, `code` and `do()`; (b) calls `do()` with HTTP,
IMAP, SMTP, shell, `tendo` singleton and `time.sleep` stubbed, in a temp cwd, with a
per-cron alarm. Only crash-class errors from the cron's own code fail the test
(NameError/AttributeError/TypeError/ImportError/SyntaxError, raised or logged).
Network and data-shaped errors are recorded in the `-v2` output.

| Crontab script | Class (`CRON_CLASSES`) | Exercised in CI |
| --- | --- | --- |
| `tns_updates_realtime.bash` | `TNS_uploads.TNS_recent_realtime` *(inferred)* | yes (network stubbed) |
| `tags.bash` | `Apply_Tags.Tags` | yes |
| `yse_obs.bash` | `YSE_observations.SurveyObs` | yes — XFAIL: `uploaddict` unbound when IMAP fails (follow-up bug) |
| `yse_ingest.bash` | `QUB_data.YSE` *(inferred)* | yes — XFAIL until PR #165 (undefined names) |
| `qub_ingest.bash` | `QUB_data.QUB` | yes — XFAIL until PR #165 |
| `forcedphot.bash` | `YSE_Forced_Phot.ForcedPhot` | yes (IPP stubbed) |
| `tns_updates.bash` (twice daily) | `TNS_uploads.TNS_updates` | yes |
| `tns_ignore_updates.bash` (weekly) | `TNS_uploads.TNS_Ignore_updates` | yes |
| `yse_fields_new.bash` (twice daily) | not a `CRON_CLASSES` entry: `YSE_App/yse_utils/get_yse_obsfields.py`-style script posting to `/add_yse_survey_fields/` *(inferred)* | script **manual**; endpoint covered by `test_add_survey_field_api_creates_field_and_msb` |
| `check_yse_obs.bash` | not a `CRON_CLASSES` entry (script) *(inferred)* | **manual** |
| `yse_duplicates.bash` | `QUB_data.CheckDuplicates` *(inferred)* | yes |
| `tns_lastday_updates.bash` (8 h) | `TNS_uploads.TNS_recent` *(inferred)* | yes |
| `forcedphot_daily.bash` | `YSE_Forced_Phot.ForcedPhotUpdate` *(inferred)* | yes |
| `tns_latest_webform.bash` | `TNS_uploads.TNS_emails` *(inferred; TNS web-form emails)* | yes (IMAP stubbed) |
| `yse_fields_webform.bash` | script *(inferred)* | **manual** |
| `gaia_lc.bash` (hourly) | `Gaia_LC.GaiaLC` | yes — XFAIL until PR #165 |
| `ztf_forcedphot.bash` | `ZTF_Forced_Phot_Cron.ForcedPhot` | yes |
| `yse_dbbackup.bash` / `yse_crons_clean_backups.bash` (nightly) | shell scripts (mysqldump + rm) | **manual**: not django_cron, need the prod DB host and backup volume |
| `decam_ingest.bash` (commented out) | `DECam_upload.DECam` | yes (still in `CRON_CLASSES`) |
| `yse_setting_fields.bash` | script *(inferred)* | **manual** |
| `yse_updates_stack.bash` | `QUB_data.YSE_Stack` *(inferred)* | yes — XFAIL until PR #165 |
| `new_lowz.bash` | not in `CRON_CLASSES` *(inferred: a query/notification script)* | **manual** |
| — (no crontab line) | `Photo_Z.YSE`, `SDSS_Photo_Z.YSE`, `PS1_cutouts.YSE`, `host_associate.YSE`, `PS1_PhotoZ.YSE`, `Query_ZTF.AntaresZTF`, `QUB_data.YSE_Weekly`, `rapid.rapid_classify_cron`, `PhotometryUploadExample.PhotometryUploads`, `TNS_uploads.UpdateGHOST` | import + `do()` smoke; `Photo_Z`/`SDSS_Photo_Z` are skipped when `SciServer` is not installed in the web image |

Known follow-ups surfaced by the smoke test (reported in the `-v2` output, not
failures): duplicate django_cron `code` values (`QUB_data.YSE_Weekly` reuses
`QUB_data.YSE`'s; `ZTF_Forced_Phot_Cron.ForcedPhot` reuses
`YSE_Forced_Phot.ForcedPhot`'s), which makes django_cron share one run history
between two jobs.

## Manual-only summary

- Visual comparison of the seven pages against prod (no prod access from CI).
- Live TNS / ZTF / IPP forced-photometry / Gaia / ANTARES fetches (credentials and
  external services; CI stubs the network).
- SALT3 fit (`salt2fit=1`): model download + real multi-band data.
- On-call, survey-field and survey-obs *form* POSTs: their choice fields are built
  at import time from DB rows, so they only work in a process started against a
  populated DB. Making those querysets lazy would let CI cover them.
- `download_data` on a large object (prod timeout): CI keeps a 20 s ceiling on a
  small object only.
- Nightly `mysqldump` backup and backup cleanup scripts; the bash cron wrappers
  themselves (not in the repo).
