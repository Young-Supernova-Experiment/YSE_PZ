# Encrypted credentials and external-service runs

Two shared prerequisites of the SkyPortal-parity roadmap (plan section 9): a single, audited,
encrypted place for third-party secrets (#264) and one model that records every call to an external
computation or service (#265). Nothing else is wired to them yet; the facility APIs (#298),
allocations (#303), sharing services (#324), analysis services (#312), annotation services (#316)
and AI summaries (#294) build on top. The design follows SkyPortal's encrypted allocation
`altdata` and its analysis-service run/webhook pattern (BSD-3-Clause); no SkyPortal code is copied.

## Encrypted credentials (`EncryptedCredential`)

`YSE_App/models/credential_models.py`. One row = one named JSON secret for one service:

| field | meaning |
|---|---|
| `name`, `service` | unique together; `service` is a slug such as `lco`, `tns`, `antares` |
| `kind` | facility / broker / tns / hermes / analysis / generic |
| `owner_group`, `owner_user` | who may use it (`usable_by(user)`: staff, the owner, or a member of the group) |
| `encrypted_payload` | Fernet token of the JSON object; never rendered anywhere |
| `key_fingerprint` | first 12 hex chars of sha256(key) that encrypted the row (not the key) |
| `is_active`, `last_used_at`, audit fields | `get_secret(touch=True)` stamps `last_used_at` |

API: `cred.set_secret({...})` (then `save()`), `cred.get_secret()`, `cred.masked_secret()`,
`cred.masked_display()` (keys with bullets, what the admin shows), `cred.has_secret`.
`django-auditlog` records every change except the payload. The admin form has a write-only
"New secret (JSON object)" box: blank keeps the stored secret; the list and change pages show only
the payload's keys.

### The key

`settings.CREDENTIALS_KEY`, resolved in `YSE_PZ/settings.py` exactly like `SECRET_KEY` (#253):

1. `YSE_CREDENTIALS_KEY` environment variable, else
2. `[secrets] credentials_key` in `YSE_PZ/settings.ini` (a `<...>` placeholder counts as unset), else
3. with `IS_DEBUG: True`, a key derived from `SECRET_KEY` (local docker, CI, yse_experimental), else
4. `ImproperlyConfigured` at startup, naming the env var, the ini key and the generator command.

A comma-separated list is accepted: the first key encrypts, every key decrypts (`MultiFernet`).
`cryptography` (Fernet) is already pinned in `requirements.txt` (3.4.7 on Python 3.8); no new package.

Commands (`YSE_App/management/commands/`):

```bash
python manage.py generate_credentials_key             # prints a new key and the ini snippet
python manage.py rotate_credentials_key --old OLD --new NEW [--dry-run]
```

Rotation runs with the old configuration still active, re-encrypts every row inside one
transaction (rows without a secret are skipped), refuses and rolls back if any row does not
decrypt with the `--old` key(s) (repeat `--old` for several), and then tells you to switch
`credentials_key` to the new value and restart web + cron. The staged alternative is
`credentials_key: NEW,OLD` in the ini, restart, rotate, then drop `OLD`.

## External services and runs

`YSE_App/models/external_service_models.py`.

**`ExternalService`** is the registry: `name`, `slug` (unique), `kind` (analysis / annotation /
summary / archive / facility / broker / sharing / generic), `base_url` (blank for in-process
services), `credential` FK to `EncryptedCredential`, `enabled`, `default_params` (JSON merged under
every run's payload), `max_runs_per_user_per_day` (0 = unlimited), `groups` M2M (empty = every
authenticated user; staff always).

**`ExternalServiceRun`** records one request: `uuid`, `service`, `transient` FK (the common target),
generic FK `target` for anything else, free-form `target_ref` for things with no row of ours
(`mast:jw01234`), `request_payload`, `status` (pending / running / succeeded / failed / cancelled),
`started_at`, `finished_at`, `result` JSON, `error`, `artifact_file` / `artifact_url`,
`external_id`, `attempts`, `callback_token_hash`; `created_by` is the requester. JSON columns are
TEXT (`YSE_App.models.fields.JSONTextField`, shared with the job queue) so they work on every
MySQL/MariaDB Ziggy might run.

Service layer `YSE_App/services/external_services.py`:

```python
run, token = start_run(service, user, {"iterations": 5}, transient=t)   # pending; token shown once
run.mark_running(external_id="job-7")
record_completion(run, "succeeded", result={...}, artifact_url="https://...")  # or "failed", error=...
```

`start_run` raises `ServiceDisabled` (disabled or not visible to the user) or `RunLimitExceeded`
(daily cap; map to HTTP 429 in an API). `dispatch_run(run)` enqueues an `external_service.run`
job on the background job queue (#263, `YSE_App.services.job_queue`) carrying `{"run_id": ...}`;
`execute_run(payload, job)` is the registered handler (imported from `YSE_App/signals.py` so every
process registers it) and, until #313 adds a runner registry, only logs and leaves the run
pending, finishing the job with `handled=False`. With `JOB_RUNNER_INLINE` the handler runs in
the requesting process. `expire_runs(days)` / `manage.py expire_service_runs --days 90 [--dry-run]` deletes old
finished runs and their artifact files.

### Callback endpoint

`POST /api/service_runs/<uuid>/callback/` (`external_service_run_callback`, CSRF-exempt, POST only).
Authenticate with the token returned by `start_run`: `Authorization: Bearer <token>`,
`X-Run-Token: <token>`, or `"token"` in the JSON body. Body:

```json
{"status": "succeeded", "result": {"chi2": 1.2}, "artifact_url": "https://...", "external_id": "job-7"}
{"status": "failed", "error": "diverged"}
{"status": "running", "external_id": "job-7"}
```

Responses: 200 `{"uuid", "status"}`; 400 bad JSON / unknown status / non-object result;
403 missing or wrong token; 404 unknown run; 405 not POST; 409 the run is already finished.

### Pages

Staff-only (`staff_member_required`): `/service_runs/` (newest 100, filter by service and status;
"Service Runs" under Tools in the sidebar for staff) and `/service_runs/<uuid>/` (summary, request,
result, error, callback URL; never the token or any credential value). Services, credentials and
runs are also in the Django admin.

## Deploy notes

- Migration `0013_credentials_external_services` (three tables, two indexes). `yse_experimental`
  and `yse_test` share `YSE_test`; Deploy Stack runs `migrate`.
- `IS_DEBUG: True` stacks (experimental) need nothing. Before the work reaches `yse_test` /
  production (`IS_DEBUG: False`) add to `YSE_PZ/settings.ini`:
  ```ini
  [secrets]
  credentials_key: <output of `python manage.py generate_credentials_key`>
  ```
  or export `YSE_CREDENTIALS_KEY`; without it the stack refuses to start, like a missing `SECRET_KEY`.
  Use a different key per stack. Back the key up outside the database: the rows are unreadable without it.
- No pip step: `cryptography` is already installed.
- Tests: `YSE_App/tests/test_credentials_services.py` (45; round trip, masking, key resolution,
  rotation, run lifecycle, cap, callback auth, staff pages).
