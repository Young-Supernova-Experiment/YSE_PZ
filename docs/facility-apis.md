# Allocations and facility APIs

Two SkyPortal-parity umbrellas share this module: the **Allocations page** (#303: #304 accounting, #305 page) and
**Robotic facility APIs** (#298: #299 framework, #300 queue + follow-up UI, #301 LCO / SOAR / ZTF / ATLAS, #302 GENERIC,
Swift, Gemini, MMT, LT).
It sits on the encrypted credentials and external-service runs of #342 (#264, #265), the job queue of
#340 (#263) and `notify()` of #347 (#266). The design follows SkyPortal's `Allocation` /
`FollowupRequest` / `facility_apis` (BSD-3-Clause) in spirit; no SkyPortal code is copied.

## Concepts

| term | meaning |
|---|---|
| `Allocation` | awarded time on one telescope (optionally one instrument): name, PI, audience `groups` (empty = everyone), `proposal_id`, `hours_allocated` / `hours_used`, `start_date` / `end_date`, `facility` slug, `credential` FK (`EncryptedCredential`), `endpoint_url`, `default_request_params` JSON, `is_active`, `notes`. `YSE_App/models/allocation_models.py` |
| facility adapter | a `FacilityAPI` subclass registered under a slug (`YSE_App/facilities/`): `fields()` (request form), `validate()`, `estimate_hours()`, `build_payload()`, `submit()`, and where the facility offers it `get_status()` / `delete()`; optionally `fetch_instrument_log()` (capability `instrument_log`, see docs/instrument-logs-weather.md) |
| `FacilityRequest` | one request for one transient against one allocation: validated `payload`, `kind` (`observation` or `photometry`), `state` (`draft` → `queued` → `submitted` → `accepted` → `running` → `complete`, or `failed` / `cancelled`), `external_id` / `external_url`, `submitted_by`, `submitted_at`, `attempts`, `last_polled`, `hours_charged` / `charged_at`, `results_ingested_at` / `n_results` (forced photometry), chronological `log`, optional `followup` (a `TransientFollowup`) and `run` (the latest `ExternalServiceRun`) |
| run | every allocation owns an `ExternalService` (`allocation-<id>`, kind `facility`, created on demand by `services.allocations.ensure_service`); a submission is an `ExternalServiceRun` on it, executed by the `external_service.run` job through the runner registry |

The legacy `ToOResource` / `QueuedResource` / `ClassicalResource` rows keep working as today (issue #299's
acceptance criterion). Since #304 each of them has an optional `allocation` FK: the allocation holds the
facility API slug, the encrypted credential and the default request parameters for that resource
(`resource.facility_api`, `resource.has_credential`, `resource.default_request_params`), and the legacy
`used_*` counters are maintained from follow-up statuses (see *Usage accounting* below).

## Submitting a request

Follow-up tab of a transient → **Submit to facility**: choose an allocation (only active, current
allocations with a facility API open to you are listed), fill the fields the adapter declares (they are
pre-filled from the allocation's `default_request_params`) and submit. The request is validated
synchronously (per-field errors come back), recorded as `queued`, and the job runner sends it
(`manage.py run_jobs`, the cron pass, or immediately with `JOB_RUNNER_INLINE`). The table below the form
shows every request for the transient with its state, external id (linked when the facility has a
portal), the run (staff) and actions: **Log** (the chronological log), **Refresh** (adapters with a status
endpoint), **Modify** (adapters that can update: the adapter's fields pre-filled with the current payload;
the change is queued like a submission), **Mark complete** (adapters without a status endpoint),
**Cancel**, and **Fetch photometry** on a complete forced-photometry request whose points were not
ingested yet. Only the requester or staff may act on a request. The follow-up boxes above carry a badge
per facility request (`lco: Accepted 2213890`), and **Facility Requests** in the sidebar lists every
request the user may see with state / facility / allocation / transient / mine filters.

Programmatically:

```python
from YSE_App.services.facility_requests import (submit_request, update_request, poll_request, cancel_request,
                                                mark_request, retrieve_results)
req = submit_request(allocation, transient, request.user, {"exposure_time": 600, "filters": ["gp", "rp"]})
req.state            # "queued" until the job runs, then "submitted" / "accepted" / "failed"
update_request(req, request.user, {"exposure_time": 900})   # adapters with "update"; queued like a submission
poll_request(req)    # facilities with a status endpoint
mark_request(req, "complete", request.user)   # GENERIC: a person records the outcome
cancel_request(req, request.user)
retrieve_results(req)   # photometry adapters: ingest the light curve of a complete request
```

Endpoints: `POST /transient_detail/<id>/facility_submit/` (`{"allocation": id, "parameters": {...}}`),
`GET /transient_detail/<id>/facility_requests_fragment/`, `POST /facility_requests/<id>/action/`
(`{"action": "poll"|"cancel"|"update"|"results"|"complete"|"failed", "parameters": {...}}`),
`GET /facility_requests/<id>/log/`, `GET /facility_requests/` (the list page). REST: `/api/allocations/`
(list/create/update; writes staff-only; never exposes the credential, only `has_credential`,
`hours_remaining`, `percent_used`, `facility_name`), `/api/facilityrequests/` (read-only; `?transient=`,
`?allocation=`, `?state=`, `?kind=`) and `/api/facilities/` (the registered adapters with capabilities,
credential keys and request form schema).

### What happens on submission

1. `submit_request` checks `allocation.usable_by(user)`, validates with the adapter, estimates the
   hours and refuses when `hours_allocated` is set and the request would exceed the remaining time.
2. A `TransientFollowup` (status `Requested`) is created or reused through
   `services.followup_requests.create_or_attach_request` when that status exists, so the request also
   appears in the follow-up boxes and on the Follow-up page.
3. The `FacilityRequest` is saved, an `ExternalServiceRun` is started on the allocation's service
   (`request_payload = {"facility_request_id", "parameters"}`, `target` = the request) and an
   `external_service.run` job is enqueued.
4. The job handler (`execute_run`) finds the runner registered for `kind:facility`
   (`services.facility_requests.run_facility_submission`), which calls `adapter.submit(request)` (or
   `adapter.update(request)` for a modification run). The adapter's `SubmitResult` sets the state,
   external id / URL and detail; the run records the result. A **transport error before anything reached
   the facility** (`FacilityUnreachable`: connection refused, timeout, ATLAS `429` queue full) is retried
   by the job queue with exponential backoff (`FACILITY_SUBMIT_BACKOFF_SECONDS`, up to
   `FACILITY_SUBMIT_MAX_ATTEMPTS`, never more than the job's own `max_attempts`); the request stays
   `queued`, `attempts` counts up and the log records each retry. Any other error (the facility rejected
   the request) marks the request and the run `failed` and is **not retried**, since a second attempt
   could submit twice; the requester gets a `followup_status` notification and can resubmit.
5. State changes map onto the follow-up: `submitted`/`accepted` → `Requested`, `running` →
   `InProcess`, `complete` → `Successful`, `failed`/`cancelled` → `Failed` (only when a
   `FollowupStatus` row of that name exists). Reaching `complete` charges `hours_charged` to
   `hours_used` once (`charged_at`); a later cancellation refunds it. The requester is notified on
   `accepted` and on the final state (`FACILITY_NOTIFY_GROUPS: true` also notifies the allocation's
   audience groups on the final state).
6. A `photometry`-kind request (ZTF, ATLAS) reaching `complete` queues `facility.retrieve_data`: the
   adapter's `fetch_results()` returns the light curve, `services.facility_requests.ingest_points()` files
   it under the adapter's instrument / observation group through `data_utils.add_transient_phot_util`
   (forced, difference-imaging points; band tokens normalised by `common.tns_photometry_map`; PhotStat
   updated by the usual signals), and `results_ingested_at` / `n_results` are stamped. The job retries
   (10 min backoff) when the file is not ready yet.

### Polling

The `FacilityPoll` cron class (`YSE_App/data_ingest/Facility_Queue.py`, in `CRON_CLASSES`) queues one
`facility.poll` job every `FACILITY_POLL_CRON_MINUTES` (10) once `FACILITY_POLL_CRON_ENABLED` is on; the
job polls every open request whose adapter has a status endpoint (LCO / SOAR request groups, ZTF and ATLAS
tasks, GENERIC with a `status_url`) and applies the state. An adapter's `poll_interval_minutes` spaces the
polls of one request (ZTF / ATLAS: 10 min). `python manage.py poll_facility_requests` does the same by
hand (`--enqueue` queues the job instead); GENERIC without a `status_url` has nothing to poll.

## Adapters

### `generic` — HTTP POST, email or Slack webhook (#302)

Configuration on the allocation:

| key | where | meaning |
|---|---|---|
| `notification_type` | `default_request_params` | `api`, `email` or `slack`; default `api` when an endpoint is set, else `email` |
| endpoint | `endpoint_url`, or `endpoint` in the credential | URL that receives the JSON payload (`Authorization: token <api_token>` when the credential holds `api_token`) |
| `recipients` | `default_request_params` | list or comma-separated email addresses (email mode) |
| `slack_webhook_url` | credential | incoming-webhook URL (Slack mode) |
| `payload_template` | `default_request_params` | optional JSON object; string values are filled from `{transient_name}`, `{ra}`, `{dec}`, `{transient_slug}`, `{requester}`, `{proposal_id}`, `{telescope}`, `{instrument}`, `{allocation_name}`, `{request_id}` and `{param_<field>}` |

Without a template the payload is `{"transient": {name, ra, dec}, "allocation": {...}, "parameters": {...},
"requester", "request_id"}`. Fields: `exposure_time`, `exposure_count`, `filters`, `priority`, `start`,
`end`, `comment`. Status is manual (`Mark complete`); a sent request is `submitted`. The email body
and the Slack text are archived in the request log / run result.

### `lco` — Las Cumbres Observatory (#301)

Builds the request-group document with the request-building code YSE-PZ already had
(`YSE_App/util/lcogt.py`: `make_requests`, `make_configurations`, ...), so the payload is the one
`AddAutomatedSpectrumRequestFormView` posted before, and submits it to
`https://observe.lco.global/api/requestgroups/`. Fields: `strategy` (`default` = 1m SINISTRO imaging in
`filters`, `spectroscopy` = FLOYDS on a Faulkes telescope, `instrument` = the instrument chosen in the
`instrument` field: SINISTRO, Spectral, MuSCAT (four channels at once), 0.4m QHY600 or FLOYDS; builders in
`facilities/lco_requests.py`), `exposure_time`, `exposure_count`, `filters`, `start` / `end` (the
request-group window), `max_airmass`, `min_lunar_distance`, `ipp_value`, `observation_type`, `proposal`
(defaults to the allocation's `proposal_id`). Credential: `{"api_token": "..."}` (preferred) or
`{"username": "...", "password": "..."}` (a token is fetched from `api-token-auth/`). Status: `GET
requestgroups/<id>/` → `PENDING`→`accepted`, `COMPLETED`→`complete`, `CANCELED`→`cancelled`,
`WINDOW_EXPIRED` / `FAILURE_LIMIT_REACHED`→`failed`; cancel: `POST requestgroups/<id>/cancel/`; modify:
the portal has no edit endpoint, so the pending group is cancelled and the changed one submitted (the
request then carries the new group id). `estimate_hours` is a rough per-filter (exposure + 90 s) or
spectroscopy (exposure + 400 s) figure.

The **Automated spectrum request** form on the follow-up tab (FLOYDS-N / FLOYDS-S / Goodman) now goes
through this adapter whenever the matching `ToOResource` / `ClassicalResource` is bound to an `lco` /
`soar` allocation (its `allocation` FK), or an `lco` / `soar` allocation on that telescope is open to the
user: the request is recorded, polled and cancellable and linked to the follow-up it creates. Without
such an allocation the form still calls `lcogt.main` with the `LCOGTUSER` / `LCOGTPASS` settings, so
current users see no change until an allocation exists. While adapting the existing code a bug in
`lcogt.make_configurations` was fixed: the calibration exposure times (LAMP_FLAT 50 s, ARC 60 s / 0.5 s)
overwrote the science exposure time.

### `soar` — SOAR through the LCO portal (#302)

The LCO adapter restricted to the SOAR instruments (`SOAR_GHTS_REDCAM` Goodman red-camera spectroscopy,
`SOAR_GHTS_REDCAM_IMAGER`, `SOAR_TRIPLESPEC`); `proposal_id` = the NOIRLab / LCO proposal code,
credential = an LCO portal token. Same status / cancel / modify behaviour as `lco`.

### `ztf` — ZTF forced photometry (#301, kind `photometry`)

Replaces the wget + IMAP e-mail dance of `data_ingest/ZTF_Forced_Phot.py` with the service's HTTP
interface. Submit: `GET requestForcedPhotometry.cgi?ra&dec&jdstart&jdend&email&userpass` (HTTP basic
auth with the service's shared login); status: `GET getForcedPhotometryRequests.cgi?…&option=All&action=Query`
lists the account's jobs and the one matching our coordinates and window gives `reqid` (external id),
`started` / `ended` / `exitcode` and, when done, the `lightcurve` path; results: the light-curve table is
parsed (`jd`, `filter`, `forcediffimflux`, `forcediffimfluxunc`, `zpdiff`, `procstatus`; rows with a
bad `procstatus` dropped) into `g` / `r` / `i` points on the `ZTF-Cam` instrument (observation group
`ZTF`, zero point 27.5, `mag` empty below 3 sigma). Fields: `days` (60), `jdstart`, `jdend`. Credential:
`{"email": "...", "userpass": "..."}` (the account registered with the service; optional
`http_user` / `http_password`). `estimate_hours` is 0; no follow-up is created for photometry requests.

The **ZTF forced photometry** button on the detail page submits through the first `ztf` allocation open
to the user; an unfinished request on that allocation replaces the old 12 h `Log` throttle ("already
accepted (submitted …)"). Without a `ztf` allocation the button behaves exactly as before
(`ZTF_Forced_Phot.run_ztf_fp` + the `ZTF_Forced_Phot_Cron.ForcedPhot` cron), so the legacy flow keeps
working with no config change.

### `atlas` — ATLAS forced photometry (#301, kind `photometry`)

`POST https://fallingstar-data.com/forcedphot/queue/` with `Authorization: Token …` and `ra`, `dec`,
`mjd_min`, `mjd_max`, `use_reduced` → the task URL (external id; `429` = queue full, retried by the
runner); status `GET <task url>` (`starttimestamp` → running, `finishtimestamp` + `result_url` →
complete); results: the `###MJD m dm uJy duJy F …` file parsed into `c` / `o` points on the `ATLAS`
instrument (fluxes in microJansky, zero point 23.9, `mag` empty below 3 sigma). Fields: `days` (200),
`mjd_min`, `mjd_max`, `use_reduced`. Credential: `{"api_token": "..."}` or `{"username", "password"}`
(token fetched from `api-token-auth/`).

### `swift` — Swift ToO (#302)

Speaks the Swift TOO API protocol directly (what the `swifttools` package does, without the dependency):
the JSON document `{"api_name": "Swift_TOO", "api_version": "1.2", "api_data": {...}}` is signed as an
HS256 JWT with the user's shared secret and posted as `jwt=<token>` to
`https://www.swift.psu.edu/toop/submit_json.php`; the answer's `status` (`Accepted` / `Rejected`),
`too_id`, `errors` and `warnings` are recorded. Fields: `urgency` (1–4), `obs_type`, `source_type`,
`exposure` (s per visit), `num_of_visits`, `monitoring_freq`, `xrt_mode`, `uvot_mode`, `poserr`, the three
justification texts and `debug`. **Requests are dry runs** (`debug: true`, the TOO API validates without
triggering; the request is recorded `complete` with "dry run accepted") until the allocation's
`default_request_params` set `"debug": false`. Credential: `{"username": "...", "shared_secret": "..."}`.
No status endpoint (manual). XRT product requests (light curves / spectra) are not wired yet.

### `gemini` — Gemini North / South URL ToO trigger (#302)

`POST https://gnodb.gemini.edu:8443/too` (or `gsodb` for the south; self-signed certificate) with the
program id (`proposal_id`), the program `user_key`, `email`, the template `obsnum`, target name and
coordinates, optional `mags` (`18.20/r/AB`), `exptime`, `posangle`, `group`, `note`, `ready`; the answer
is the new observation id. Fields: `site`, `obsnum`, `exptime`, `mag`, `band`, `posangle`, `group`,
`note`, `ready`. Credential: `{"user_key": "...", "email": "..."}`. No status / cancel API (manual).

### `mmt` — MMT Binospec / MMIRS (#302)

`POST https://scheduler.mmto.arizona.edu/APIv2/catalogTarget/` (token in the form data) adds a catalog
target (`instrumentid` 16 Binospec / 15 MMIRS, `observationtype` imaging / longslit / mask, exposure,
`numberexposures`, `visits`, `filter`, `grating`, `centralwavelength`, `slitwidth`, `magnitude`,
`priority`, `pa`, `photometric`, `targetofopportunity`, `notes`); `PUT catalogTarget/<id>/` modifies it
and `DELETE catalogTarget/<id>/` removes it. Credential: `{"api_token": "..."}`; `proposal_id` = the MMT
program id. No observing status in the API (manual).

### `lt` — Liverpool Telescope RTML (#302)

Builds an RTML 3.1a `request` document (project id = `proposal_id`, phase-2 `username` / `password`,
target, one `Schedule` per IO:O filter or one for IO:I (H) or SPRAT (`grating` red / blue), exposure,
window, airmass / seeing / sky constraints, priority) and sends it over TCP to
`telescope.livjm.ac.uk:8080` (`LT_RTML_HOST` / `LT_RTML_PORT`); a `confirm` reply carries the `uid`
(external id), a `reject` reply fails the request. Cancel sends an `abort` document with the same uid.
Fields: `instrument`, `exposure_time`, `exposure_count`, `filters`, `grating`, `start`, `end`,
`max_airmass`, `max_seeing`, `priority`. No status query (manual).

### Adapter matrix

| slug | kind | submit | modify | cancel | status poll | results | credential keys |
|---|---|---|---|---|---|---|---|
| `generic` | observation | API / email / Slack | re-send flagged `update` | re-send flagged `cancel` | only with `status_url` | – | `api_token`, `endpoint`, `slack_webhook_url`, `status_url`, `cancel_url` |
| `lco` | observation | request group | cancel + resubmit | portal cancel | portal | – | `api_token` or `username`/`password` |
| `soar` | observation | request group (SOAR instruments) | cancel + resubmit | portal cancel | portal | – | as `lco` |
| `ztf` | photometry | forced-photometry job | – | – | job list | light curve → `ZTF-Cam` | `email`, `userpass` |
| `atlas` | photometry | queue task | – | – | task URL | result file → `ATLAS` | `api_token` or `username`/`password` |
| `swift` | observation | TOO API (dry run by default) | – | – | – (manual) | – | `username`, `shared_secret` |
| `gemini` | observation | URL trigger | – | – | – (manual) | – | `user_key`, `email` |
| `mmt` | observation | catalog target | PUT | DELETE | – (manual) | – | `api_token` |
| `lt` | observation | RTML request | – | RTML abort | – (manual) | – | `username`, `password` |

### Writing an adapter

```python
from YSE_App.facilities import FacilityAPI, Field, SubmitResult, StatusResult, register

@register
class MyFacility(FacilityAPI):
    slug = "myfac"
    name = "My facility"
    capabilities = frozenset({"submit", "status"})
    credential_keys = ["api_token"]
    manual_status = False

    def fields(self, allocation=None):
        return [Field("exposure_time", "number", required=True, minimum=1)]

    def submit(self, request):            # request: FacilityRequest
        secret = request.allocation.secret(touch=True)
        ...                               # requests.post(..., timeout=http_timeout())
        return SubmitResult("accepted", external_id=str(body["id"]), response=body)

    def get_status(self, request):
        return StatusResult("complete", detail="done", response=body)
```

Put the module on the import path and list it in `settings.ini` → `[site_settings]
FACILITY_API_MODULES: mypkg.myfac` (comma-separated). Raise `FacilityError` for anything the facility
rejects and `FacilityUnreachable` (or use `http_request()`, which raises it on transport errors) when
nothing reached the facility, so the queue retries; `validate_extra()` is the hook for cross-field checks
(raise `FacilityValidationError({field: message})`). A forced-photometry service sets `kind =
KIND_PHOTOMETRY`, adds `"results"` to its capabilities and implements `fetch_results(request)` returning
points (`mjd` / `obs_date`, `band`, `mag`, `mag_err`, `flux`, `flux_err`, `flux_zero_point`, `forced`)
plus `results_instrument` / `results_obs_group`; `poll_interval_minutes` spaces the status polls.

## Usage accounting (#304)

* **Allocations**: a `FacilityRequest` reaching `complete` charges its `hours_charged` (the adapter's
  `estimate_hours`) to `Allocation.hours_used` once (`charged_at`); cancelling a completed request refunds.
  `submit_request` and `update_request` refuse a request that would exceed `hours_remaining` when
  `hours_allocated` is set.
* **Legacy resources**: a `TransientFollowup` whose status becomes `Successful` charges its resource once
  (`usage_charged_at`, a `post_save` signal): a `ToOResource` gets `used_too_triggers += 1` and
  `used_too_hours += hours`, a `QueuedResource` gets `used_hours += hours`, where `hours` is the follow-up's
  `usage_hours` (settable in the admin) or, when zero, the hours of the facility requests linked to it.
  Repeated saves charge nothing more; a follow-up set back to another status refunds. `remaining_hours`
  (and `remaining_triggers` for ToO) are on the models and in the REST serializers (`/api/tooresources/`,
  `/api/queuedresources/`, `/api/classicalresources/` also expose `allocation`, `facility_api`,
  `has_credential`, `default_request_params`). Facility requests never carry a legacy resource, so nothing
  is counted twice.
* **Backfill**: `python manage.py backfill_resource_usage` lists, per ToO / queued resource, the
  successful follow-ups that were never charged; `--apply` charges them (`--hours H` sets the hours per
  follow-up when they carry none). Idempotent.

## Allocations page

`/allocations/` (staff, "Allocations" under Tools): every allocation with telescope / instrument, PI,
semester, audience, a usage bar (`hours_used / hours_allocated`), the facility API, whether a
credential with a secret is bound and the request counts; filters for current / all / inactive and by
telescope. **New allocation** and **Edit** open a Django form (`YSE_App/allocation_forms.py`): pick an
existing encrypted credential, or paste a JSON secret into *New credential secret* to create and bind
a new `EncryptedCredential` (kind `facility`, service = the facility slug) without opening the admin;
the value is never displayed again. Saving syncs the allocation's `ExternalService` (credential,
endpoint, defaults, enabled). Both models are also in the Django admin.

## Settings

| key (`[site_settings]`) | default | meaning |
|---|---|---|
| `FACILITY_API_MODULES` | empty | extra adapter modules to import |
| `FACILITY_HTTP_TIMEOUT_SECONDS` | 30 | timeout for facility HTTP calls (and the LT socket) |
| `FACILITY_SUBMIT_MAX_ATTEMPTS` | 3 | attempts for a submission whose facility could not be reached (never more than the job's `max_attempts`) |
| `FACILITY_SUBMIT_BACKOFF_SECONDS` | 120 | base of the exponential backoff between those attempts |
| `FACILITY_POLL_CRON_ENABLED` | False | `FacilityPoll` cron queues a `facility.poll` job (also `YSE_FACILITY_POLL_CRON=1`) |
| `FACILITY_POLL_CRON_MINUTES` | 10 | its interval |
| `FACILITY_POLL_LIMIT` | 200 | open requests polled per job |
| `FACILITY_NOTIFY_GROUPS` | False | also notify the allocation's audience groups when a request finishes |
| `FACILITY_RESULTS_MJD_MATCH_DAYS` | 0.001 | tolerance for matching an ingested forced-photometry point to an existing one |
| `LT_RTML_HOST` / `LT_RTML_PORT` | `telescope.livjm.ac.uk` / 8080 | Liverpool Telescope RTML socket |

Facility HTTP calls run in the job runner, so the web process never waits on a facility.

## Deploy notes

- Migrations `0016_allocations_facility_requests` (two tables, two indexes; depends on
  `0015_photstat_limit_bands`) and `0025_facility_queue_accounting` (columns only: `FacilityRequest.kind` /
  `attempts` / `results_ingested_at` / `n_results`, `TransientFollowup.usage_hours` / `usage_charged_at`,
  `allocation` FK on `ToOResource` / `QueuedResource` / `ClassicalResource`). Deploy Stack runs `migrate`.
- Nothing to install: `requests` and `cryptography` are already pinned; the Swift adapter signs its JWT
  with the standard library.
- `CREDENTIALS_KEY` must be configured on non-debug stacks (see
  `docs/credentials-and-external-services.md`); allocation credentials are `EncryptedCredential` rows.
- Job runner: requests are sent by `manage.py run_jobs` (cron pass or worker, `docs/background-jobs.md`).
- Polling: set `FACILITY_POLL_CRON_ENABLED: True` under `[site_settings]` once an LCO / SOAR / ZTF / ATLAS
  allocation exists (the cron is a no-op otherwise), or run `manage.py poll_facility_requests` from the
  crontab.
- To bind LCO: create an allocation with facility `lco`, `proposal_id` = the LCO proposal, and a
  credential `{"api_token": "<portal API token>"}`; put the users allowed to trigger in its groups; to route
  the automated spectrum form through it, set the `allocation` on the matching ToO / classical resource in
  the admin.
- To bind ZTF forced photometry: an allocation with facility `ztf` and credential `{"email": ..., "userpass":
  ...}`; the detail-page button then uses it. ATLAS: facility `atlas`, credential `{"api_token": ...}`.
- Optional: `manage.py backfill_resource_usage --apply` once, to seed `used_too_triggers` / `used_hours`
  from historical successful follow-ups.
- Tests: `YSE_App/tests/test_facility_apis.py` (framework, GENERIC, LCO, submit flow, pages, REST, admin)
  and `YSE_App/tests/test_facility_queue.py` (retries and modifications, poll cron and spacing, ZTF and
  ATLAS submit / poll / ingest with recorded responses, Swift JWT and dry run, Gemini, MMT, LT RTML over a
  fake socket, SOAR, legacy resource accounting and backfill, resource serializers, list page, modify /
  log actions, automated spectrum form through an `lco` allocation, ZTF button through a `ztf` allocation).

## Not in this slice (rest of #298 / #303)

- Swift XRT product requests; Gemini / MMT / LT observing status (none of them has an API for it).
- JSON-schema rendering of adapter fields inside the classic follow-up form (the facility panel has its
  own form); telescope map on the allocations page.
