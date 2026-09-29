# Allocations and facility APIs

Two SkyPortal-parity umbrellas share this module: the **Allocations page** (#303: #304, #305) and
**Robotic facility APIs** (#298: #299 framework, #300 queue + follow-up UI, #301 LCO, #302 GENERIC).
It sits on the encrypted credentials and external-service runs of #342 (#264, #265), the job queue of
#340 (#263) and `notify()` of #347 (#266). The design follows SkyPortal's `Allocation` /
`FollowupRequest` / `facility_apis` (BSD-3-Clause) in spirit; no SkyPortal code is copied.

## Concepts

| term | meaning |
|---|---|
| `Allocation` | awarded time on one telescope (optionally one instrument): name, PI, audience `groups` (empty = everyone), `proposal_id`, `hours_allocated` / `hours_used`, `start_date` / `end_date`, `facility` slug, `credential` FK (`EncryptedCredential`), `endpoint_url`, `default_request_params` JSON, `is_active`, `notes`. `YSE_App/models/allocation_models.py` |
| facility adapter | a `FacilityAPI` subclass registered under a slug (`YSE_App/facilities/`): `fields()` (request form), `validate()`, `estimate_hours()`, `build_payload()`, `submit()`, and where the facility offers it `get_status()` / `delete()` |
| `FacilityRequest` | one request for one transient against one allocation: validated `payload`, `state` (`draft` → `queued` → `submitted` → `accepted` → `running` → `complete`, or `failed` / `cancelled`), `external_id` / `external_url`, `submitted_by`, `submitted_at`, `last_polled`, `hours_charged` / `charged_at`, chronological `log`, optional `followup` (a `TransientFollowup`) and `run` (the `ExternalServiceRun`) |
| run | every allocation owns an `ExternalService` (`allocation-<id>`, kind `facility`, created on demand by `services.allocations.ensure_service`); a submission is an `ExternalServiceRun` on it, executed by the `external_service.run` job through the runner registry |

The legacy `ToOResource` / `QueuedResource` / `ClassicalResource` rows are untouched (issue #299's
acceptance criterion: a resource without a facility behaves exactly as today). Allocations are a new
table beside them; a later step can link the two.

## Submitting a request

Follow-up tab of a transient → **Submit to facility**: choose an allocation (only active, current
allocations with a facility API open to you are listed), fill the fields the adapter declares (they are
pre-filled from the allocation's `default_request_params`) and submit. The request is validated
synchronously (per-field errors come back), recorded as `queued`, and the job runner sends it
(`manage.py run_jobs`, the cron pass, or immediately with `JOB_RUNNER_INLINE`). The table below the form
shows every request for the transient with its state, external id (linked when the facility has a
portal), the run (staff) and actions: **Refresh** (adapters with a status endpoint), **Mark complete**
(adapters without one), **Cancel**. Only the requester or staff may act on a request.

Programmatically:

```python
from YSE_App.services.facility_requests import submit_request, poll_request, cancel_request, mark_request
req = submit_request(allocation, transient, request.user, {"exposure_time": 600, "filters": ["gp", "rp"]})
req.state            # "queued" until the job runs, then "submitted" / "accepted" / "failed"
poll_request(req)    # facilities with a status endpoint
mark_request(req, "complete", request.user)   # GENERIC: a person records the outcome
cancel_request(req, request.user)
```

Endpoints: `POST /transient_detail/<id>/facility_submit/` (`{"allocation": id, "parameters": {...}}`),
`GET /transient_detail/<id>/facility_requests_fragment/`, `POST /facility_requests/<id>/action/`
(`{"action": "poll"|"cancel"|"complete"|"failed"}`). REST: `/api/allocations/` (list/create/update;
writes staff-only; never exposes the credential, only `has_credential`, `hours_remaining`,
`percent_used`, `facility_name`) and `/api/facilityrequests/` (read-only; `?transient=`, `?allocation=`,
`?state=`).

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
   (`services.facility_requests.run_facility_submission`), which calls `adapter.submit(request)`. The
   adapter's `SubmitResult` sets the state, external id / URL and detail; the run records the result.
   Adapter errors mark the request and the run `failed` and are **not retried** (a retry could submit
   twice); the requester gets a `followup_status` notification and can resubmit.
5. State changes map onto the follow-up: `submitted`/`accepted` → `Requested`, `running` →
   `InProcess`, `complete` → `Successful`, `failed`/`cancelled` → `Failed` (only when a
   `FollowupStatus` row of that name exists). Reaching `complete` charges `hours_charged` to
   `hours_used` once (`charged_at`); a later cancellation refunds it.

### Polling

`python manage.py poll_facility_requests` polls every open request whose adapter has a status
endpoint (LCO) and applies the state; `--enqueue` queues a `facility.poll` job instead. Add it to the
crontab (every 10 minutes is plenty) or enqueue the job from `runcrons`; the GENERIC adapter has
nothing to poll.

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
`AddAutomatedSpectrumRequestFormView` posts today, and submits it to
`https://observe.lco.global/api/requestgroups/`. Fields: `strategy` (`default` = 1m SINISTRO imaging in
`filters`, `spectroscopy` = FLOYDS on a Faulkes telescope or the SOAR Goodman strategy), `exposure_time`,
`filters`, `start` / `end` (the request-group window), `max_airmass`, `min_lunar_distance`, `ipp_value`,
`observation_type`, `proposal` (defaults to the allocation's `proposal_id`). Credential: `{"api_token":
"..."}` (preferred) or `{"username": "...", "password": "..."}` (a token is fetched from
`api-token-auth/`). Status: `GET requestgroups/<id>/` → `PENDING`→`accepted`, `COMPLETED`→`complete`,
`CANCELED`→`cancelled`, `WINDOW_EXPIRED` / `FAILURE_LIMIT_REACHED`→`failed`; cancel: `POST
requestgroups/<id>/cancel/`. `estimate_hours` is a rough per-filter (exposure + 90 s) or
spectroscopy (exposure + 400 s) figure.

While adapting the existing code a bug in `lcogt.make_configurations` was fixed: the calibration
exposure times (LAMP_FLAT 50 s, ARC 60 s / 0.5 s) overwrote the science exposure time, so the SPECTRUM
element was requested with the calibration exposure. `AddAutomatedSpectrumRequestFormView` still
calls `lcogt.main` with the settings credentials; moving it onto an `lco` allocation is the next step
of #301.

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
rejects; `validate_extra()` is the hook for cross-field checks (raise `FacilityValidationError({field:
message})`).

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
| `FACILITY_HTTP_TIMEOUT_SECONDS` | 30 | timeout for facility HTTP calls |

Facility HTTP calls run in the job runner, so the web process never waits on a facility.

## Deploy notes

- Migration `0016_allocations_facility_requests` (two tables, two indexes; depends on
  `0015_photstat_limit_bands`). Deploy Stack runs `migrate`.
- Nothing to install: `requests` and `cryptography` are already pinned.
- `CREDENTIALS_KEY` must be configured on non-debug stacks (see
  `docs/credentials-and-external-services.md`); allocation credentials are `EncryptedCredential` rows.
- Job runner: requests are sent by `manage.py run_jobs` (cron pass or worker, `docs/background-jobs.md`);
  add `manage.py poll_facility_requests` to the crontab if LCO allocations are used.
- To bind LCO: create an allocation with facility `lco`, `proposal_id` = the LCO proposal, and a
  credential `{"api_token": "<portal API token>"}`; put the users allowed to trigger in its groups.
- Tests: `YSE_App/tests/test_facility_apis.py` (36: models, registry and fields, GENERIC api/email/slack
  with mocked HTTP, LCO payloads and portal calls with mocked HTTP, submit flow end to end through the
  job queue, pages, REST, admin).

## Not in this slice (rest of #298 / #303)

- #301: `AddAutomatedSpectrumRequestFormView` still submits through `lcogt.main`; ZTF and ATLAS
  forced-photometry adapters and data retrieval into photometry.
- #302: Swift ToO/XRT, Gemini, MMT, SOAR (beyond the LCO/SOAR strategy), LT adapters.
- #300: modify (`update`) of submitted requests; periodic poll from `runcrons`; JSON-schema rendering
  inside the classic follow-up form (the panel has its own form).
- #304: fields on the legacy `TelescopeResource` subclasses and usage accounting for follow-ups
  that bypass a facility; #305: telescope map, links from the Follow-up page header.
