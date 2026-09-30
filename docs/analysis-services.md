# Analysis services

SkyPortal-parity umbrella #312 (#313 registry / payload / runners / callback, #314 Analysis tab,
#315 the sncosmo fitter, the Summary-tab Refit and NGSF). An **analysis service** takes a transient's data, runs a
fit or classifier and hands back a parameter table, plots and files that are kept per run and
shown on the transient detail page. Two kinds are supported:

* **in-process** runners: Python modules executed by the background job queue (#340). Three ship:
  `sncosmo_fit` (SALT3 / any sncosmo source, kept with parameters, errors, covariance and plots; the
  Summary tab's SALT3 overlay and Refit button use it, see below), `bazin_fit` (the joint Bazin fit
  of #225 / #336 with the extrapolated magnitude) and `ngsf` (Next Generation SuperFit spectral
  classification through an installed NGSF; `docs/ngsf.md`).
* **webhook** services: YSE-PZ POSTs the transient's data to the service's URL with a per-run
  callback token; the service reports status, results JSON, plots and opaque files (arviz /
  joblib blobs) on the callback endpoint of #342.

It sits on `ExternalService` / `ExternalServiceRun` (#342, `docs/credentials-and-external-services.md`),
the runner registry of #351 (`register_runner("kind:analysis", ...)`) and the job queue (#340).
The design follows SkyPortal's analysis services (BSD-3-Clause) in spirit: webhook with a per-run
token, results + plots + inference files, per-user daily caps. No SkyPortal code is copied.

## Concepts

| term | meaning |
|---|---|
| `ExternalService` (kind `analysis`) | name, `slug`, `description`, `base_url` (webhooks), `credential` (`EncryptedCredential`; `api_token` / `token` is sent as a bearer), `enabled`, `default_params`, `max_runs_per_user_per_day` (0 = unlimited; staff exempt), audience `groups` (empty = everyone) |
| `AnalysisService` | one-to-one profile of that row (`YSE_App/models/analysis_models.py`): `runner_kind` (`inprocess` / `webhook`), `runner_path` (built-in name or dotted module path), `input_spec` (`photometry`, `spectra`, `redshift`, `host`, or `all`), `output_spec` (`results`, `plots`, `files`), `param_schema` (the parameter form), `timeout_seconds`, `display_order`, `summary_keys` (result keys shown in the runs table) |
| run | an `ExternalServiceRun` with `request_payload = {"analysis": true, "params": {...}, "input_spec": [...]}`, `status` pending -> running -> succeeded / failed / cancelled, `result` JSON (the parameter table; `_summary`, `_files` are ours), `error` |
| `AnalysisResultFile` | a file the run produced: `kind` (`plot`, `inference`, `data`, `other`), `name`, `file` under `MEDIA_ROOT/service_runs/<run uuid>/`, `content_type`, `size`, `meta` |

## The Analysis tab

Transient detail page -> **Analysis** (lazily loaded fragment, `transient_detail/<id>/analysis_fragment/`).
Left: **Run analysis** with the services open to you, the parameter form generated from
`param_schema` (number / integer / text / boolean / choice fields with the service's defaults) and the
daily-cap state ("2 of 5 left today"; the button is disabled at 0, staff are exempt). Right: the runs
table (service, status, who, started, duration, result summary or the error text, Cancel / Delete for
your own runs, the staff run page). Below: one card per finished run with the plots inline (click for
full size), the flat result table, the parameters and download links for every file. Pending / running
runs are polled (`analysis_runs/<uuid>/status.json`) until final, then the tab reloads.

Users who may not see the transient's photometry or spectra see only their own runs and a note; a
run they start is built from the data *they* may access (usually none, so it fails with a clear
message). Result files are served by `analysis_runs/<uuid>/files/<id>/<name>` (requester, staff, or
anyone with data access), never from `MEDIA_URL`.

## The Summary tab: stored SALT3 fit, Refit, NGSF (#315)

The Summary tab no longer fits SALT2 inside the plot request. **Show SALT3 Fit** (`salt2plot/<id>/1`,
`salt2fluxplot/<id>/1`; the URLs are unchanged) overlays the **last successful `sncosmo_fit` run** on
the transient: its `model_curves.json` gives one model line per band and its result gives the labels
(phase today, z, t0, mB, x1, c, and "salt3 fit <date> by <user>"). Without a stored run the plot
says "No stored SALT3 fit yet: use Refit on the Summary tab" and never blocks. The helpers live in
`YSE_App/services/fit_status.py` (`stored_salt_fit`, `model_curves` (cached 10 min), `salt_fit_labels`)
and `view_utils._overlay_stored_salt_fit`; the cached detail plot's key carries the run id.

Under the photometry buttons a **SALT3 fit:** line (fragment `transient_detail/<id>/salt_fit_fragment/`,
template `transient_detail/salt_fit_status.html`) shows the stored fit's `t0`, `z`, `x1`, `c`, `mB`,
chi2/dof, date and requester, a **Refit** button and a **details** link to the Analysis tab. Refit POSTs
`{service: <slug>, params: {}}` to the Analysis tab's run endpoint, so the daily cap, group audience and
"enabled" state apply; the line then polls `analysis_runs/<uuid>/status.json` and reloads itself, and
if the SALT3 overlay is showing the light curve is re-fetched with the new curves. States: not
registered ("not available on this server", staff see the registration command), disabled, refit
queued / refitting (button disabled), last refit failed (error text, button back), daily limit reached,
"your groups may not run it". The service used is the first enabled in-process service whose
`runner_path` is `sncosmo_fit` that the user may run.

Under the spectrum plot the **NGSF classification:** line (`transient_detail/<id>/ngsf_fragment/`,
`transient_detail/ngsf_status.html`) does the same for the `ngsf` runner, with the extra "NGSF is not
installed on this server" state from `YSE_App.analysis.ngsf.availability()`; see `docs/ngsf.md`.

`lightcurveplot_summary(salt2=True)` (not URL-reachable with the flag) is the only synchronous sncosmo
fit left and keeps the #189 guard.

## Registering a service

```bash
# the shipped in-process runners (idempotent; re-running updates the row)
python manage.py register_analysis_service --builtin sncosmo_fit --cap 20
python manage.py register_analysis_service --builtin bazin_fit
python manage.py register_analysis_service --builtin ngsf --cap 10 --timeout 1800   # needs NGSF installed, docs/ngsf.md

# a webhook service, restricted to one group, with a bearer token from an EncryptedCredential
python manage.py register_analysis_service ngsf "NGSF spectral matching" \
    --url https://ngsf.example.org/run --input spectra redshift --output results plots files \
    --credential "NGSF token" --timeout 1800 --cap 10 --group "Spectra people" \
    --param-schema '{"n_templates": {"type": "integer", "label": "Templates", "default": 5}}'

# any importable module exposing run(payload, params)
python manage.py register_analysis_service mymodel "My model" --runner mypkg.yse_runner
```

or in code, `YSE_App.services.analysis_services.register_service(slug, name, runner=... | base_url=..., ...)`.
The Django admin (Analysis services) edits every field; `ExternalService` holds the audience, cap,
credential and `default_params`.

`param_schema` is `{"name": {"type": "number|integer|text|boolean|choice", "label", "default",
"choices", "help", "min", "max", "required"}}`. `ExternalService.default_params` override the schema
defaults and are always accepted (so a deployment can pin `source` to a locally registered sncosmo
model). Unknown keys a client sends are passed through as strings.

## Writing an in-process runner

```python
# mypkg/yse_runner.py
from YSE_App.analysis.base import AnalysisError, AnalysisResult, figure_png, new_figure
from YSE_App.services.analysis_payload import detections

NAME = "My model"                      # copied to the service row by register_analysis_service
DESCRIPTION = "..."
INPUT_SPEC = ["photometry", "redshift"]
OUTPUT_SPEC = ["results", "plots", "files"]
PARAM_SCHEMA = {"iterations": {"type": "integer", "default": 100}}
SUMMARY_KEYS = ["chi2"]

def run(payload, params):
    rows = detections(payload)         # magnitude + error, no upper limits, no flagged points
    if len(rows) < 5:
        raise AnalysisError("need at least 5 detections")   # -> run failed with this text
    ...
    out = AnalysisResult(results={"chi2": 1.2, "chi2_err": 0.1}, summary="chi2 = 1.2")
    fig = new_figure(); ...; out.add_plot("fit.png", figure_png(fig))
    out.add_json_file("posterior.json", {...}, kind="inference")
    return out
```

`payload` is the document below; `params` the validated parameters. The job runner calls `run` in a
worker thread and gives up after `timeout_seconds` (0 = no limit); a run that times out is marked
failed and the thread is abandoned. Anything raised fails the run (`AnalysisError` with its message,
other exceptions with `Type: text`).

### The payload (`YSE_App/services/analysis_payload.py`)

```json
{
  "transient": {"id": 7, "name": "2026abc", "ra": 35.2, "dec": 12.4, "redshift": 0.03, "redshift_source": "transient|host|",
                "mw_ebv": 0.02, "disc_date": "...", "status": "Following", "spec_class": "SN Ia", "host": {...} | null},
  "redshift": 0.03,
  "photometry": [{"mjd": 61000.1, "instrument": "GPC1", "band": "r", "sncosmo_band": "sdssr", "mag": 18.2, "mag_err": 0.05,
                  "flux": 5248.0, "flux_err": 241.7, "zp": 27.5, "zpsys": "ab", "mag_sys": "AB", "upper_limit": false, "flagged": false}],
  "spectra": [{"id": 3, "mjd": 61005.3, "instrument": "LRIS", "obs_group": "YSE", "redshift": null, "n_points": 4096,
               "wavelength": [...], "flux": [...], "flux_err": [...] | null}],
  "photstat": {...} | null,
  "params": {"iterations": 100}
}
```

Only data the requesting user may see is included (`PhotometryService` / `SpectraService`, the
detail-page authorization) and only the sections in `input_spec`. Fluxes are on zero point 27.5
(derived from the magnitude when a row has none); rows with a flux but no magnitude and
`flux / flux_err < 3` are upper limits. `sncosmo_band` comes from `common/bandpassdict.py`
(`"Band: <instrument> - <band>"` -> sncosmo name) and is `null` for unknown bands, which the sncosmo
runner reports by name when it has too few points.

## Webhook services

The runner POSTs (JSON, `ANALYSIS_HTTP_TIMEOUT_SECONDS`) to `base_url`:

```json
{ ...payload..., "run_id": "<uuid>", "service": "ngsf", "params": {...}, "output_spec": ["results", "plots", "files"],
  "callback_url": "https://ziggy.ucolick.org/yse_pz/api/service_runs/<uuid>/callback/",
  "callback_token": "<per-run token>", "callback_method": "POST" }
```

with headers `X-Run-Token: <token>` and, when the service has a credential with `api_token` /
`token` / `bearer` / `api_key`, `Authorization: Bearer <value>`. The token is minted at dispatch
(only its sha256 is stored). The service answers 2xx to accept (`{"id": "job-7"}` sets `external_id`);
a 2xx body that already carries a final `status` completes the run at once (synchronous services);
4xx/5xx or a transport error fails it with the response text. Runs still `running` after
`timeout_seconds` are marked failed when the tab or status endpoint is next read.

When done, the service calls the callback (token as `Authorization: Bearer`, `X-Run-Token` or
`"token"` in the body) either as JSON

```json
{"status": "succeeded", "result": {"chi2": 1.5, "best_template": "sn1994D"},
 "plots": [{"name": "fit.png", "content_type": "image/png", "data": "<base64>"}],
 "files": [{"name": "posterior.nc", "kind": "inference", "content_type": "application/x-netcdf", "data": "<base64>"}]}
{"status": "failed", "error": "diverged"}
{"status": "running", "external_id": "job-7"}
```

or as `multipart/form-data` with fields `status`, `result` (JSON string), optional `kinds`
(`{"<field>": "plot|inference|data"}`) and the files as uploads (the field name is the file name
unless the upload carries one). Files are capped by `ANALYSIS_MAX_ATTACHMENT_BYTES` (25 MB) and
`ANALYSIS_MAX_FILES_PER_RUN` (20); images become plots, `.nc` / `.joblib` / `.pkl` / `.h5` inference
data. Responses: 200 `{"uuid", "status", "files": [...]}` (plus `attachment_errors` for skipped
entries), 400 bad body, 403 wrong token, 404 unknown run, 409 already finished.

## API

* `GET /api/analysisservices/` - services you may run: `slug`, `name`, `runner_kind`, `input_spec`,
  `output_spec`, `param_schema`, `param_fields` (the rendered form), `cap` (`limit`, `used`, `remaining`, `exempt`), `groups`.
* `GET /api/analysisruns/?transient=<id or name>&service=<slug>&status=succeeded` - runs on transients
  you may see (plus your own); `GET /api/analysisruns/<uuid>/` - status, params, result, files with URLs.
* `POST /api/analysisruns/ {"service": "bazin_fit", "transient": "2026abc", "params": {...}}` -> 201 the run
  (400 bad params / unknown service, 404 unknown or invisible transient, 429 daily cap reached);
  `DELETE /api/analysisruns/<uuid>/` your own run (staff: any).
* Page endpoints: `POST transient_detail/<id>/analysis_run/`, `GET analysis_runs/<uuid>/status.json`,
  `GET analysis_runs/<uuid>/files/<id>/<name>`, `POST analysis_runs/<uuid>/cancel/` / `delete/`.
* `POST /api/service_runs/<uuid>/callback/` (above).

## Settings (`settings.ini`, `[site_settings]`, all optional)

| key | default | meaning |
|---|---|---|
| `ANALYSIS_HTTP_TIMEOUT_SECONDS` | 30 | webhook POST timeout |
| `ANALYSIS_MAX_ATTACHMENT_BYTES` | 26214400 | per result file |
| `ANALYSIS_MAX_FILES_PER_RUN` | 20 | files kept per run |
| `NGSF_COMMAND`, `NGSF_HOME`, `NGSF_SUBPROCESS_TIMEOUT` | `ngsf`, empty, 1500 | the NGSF runner (`docs/ngsf.md`) |
| `JOB_RUNNER_INLINE` | False | run jobs in the web process (development) |

Result files live under `MEDIA_ROOT/service_runs/<run uuid>/` (`MEDIA_ROOT` is `<repo>/media` unless
overridden); `manage.py expire_service_runs --days 90` deletes old runs with their files.

## Deploy notes

- Migration `0020_analysis_services` (two tables; depends on `0019_interests_data_access`). Deploy Stack runs `migrate`.
- No new packages: `sncosmo`, `matplotlib`, `scipy`, `requests` are pinned already. The SALT3 model is
  downloaded by sncosmo on first use into the astropy cache (`~/.astropy/cache/sncosmo`); the web /
  worker user needs a writable home and outbound HTTPS, or pre-seed the cache. Without the model a
  SALT3 run fails with "could not load sncosmo source 'salt3'" instead of hanging the page.
- `MEDIA_ROOT/service_runs/` must be writable by the web process (callbacks) and the job runner.
- Register the shipped services once per stack:
  `python manage.py register_analysis_service --builtin sncosmo_fit --cap 20` and
  `python manage.py register_analysis_service --builtin bazin_fit`.
- Runs execute in the job runner (`manage.py run_jobs`, the `RunQueuedJobs` cron pass, or inline with
  `JOB_RUNNER_INLINE`). `YSE_App.analysis.runners` is in `DEFAULT_HANDLER_MODULES` and imported from
  `signals.py`, so every process knows the runner.
- The callback URL uses `YSE_PUBLIC_BASE_URL`; set it on stacks that run webhook services.

- Migration `0027_transient_has_jwst` also carries the `runner_path` help text naming the `ngsf` built-in.
- The Summary tab shows a stored SALT3 fit only once `sncosmo_fit` is registered and a run has
  succeeded; until then the line says so and the plot's overlay button reports "No stored SALT3 fit".
  Register the service and click **Refit** on a few transients (or `POST /api/analysisruns/`) to seed it.
- NGSF is optional; `docs/ngsf.md` has the install steps.

## Not in this slice (rest of #312)

Spectral-cube and summariser services, Bokeh-JSON plots on the tab and a periodic expiry of timed-out
webhook runs are follow-ups.

Tests: `YSE_App/tests/test_analysis_services.py`, `YSE_App/tests/test_summary_refit.py` (Summary-tab
overlay, fragments, Refit, NGSF), `YSE_App/tests/test_salt2_fit_guard.py`.
