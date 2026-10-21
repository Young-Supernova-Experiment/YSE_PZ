# TNS sharing services

Reporting a transient to the Transient Name Server (TNS) is a configured, queued and tracked
operation (roadmap section 9, issues #324 / #325 / #326 / #327). A **sharing service** holds the bot
credential, the reporting group, the default coauthors and which instruments and observation groups
may be reported; a **submission** is one report with the exact payload sent, the TNS report id, the
reply and the outcome; an **auto-publisher** is a per-group rule that queues reports without a click.
Hermes publishing (#281, #326) goes through `YSE_App/sharing/hermes.py` to the Hermes REST API; see
docs/feeds-hermes.md for the message layout, the token credential and the `hermes.test` sandbox topic.

The design follows SkyPortal's sharing services (BSD-3-Clause) in spirit; no code is copied.

## Pages

| page | who | what |
|---|---|---|
| `/sharing/` (sidebar "TNS Reports") | any logged-in user | submissions through the services visible to them: filters by service, report type, status and name; error column; **Retry** on failed / rejected rows |
| `/sharing/submissions/<id>/` | same | payload sent, last TNS reply, error, queue job, retry |
| `/sharing/services/` | same (staff see disabled services and the rules' **Dry run**) | the services, their reporting group, credential key names (never values), allowed instruments / groups, who may report, and the auto-publisher rules |
| transient page, **Report to TNS** button | users who may use at least one enabled TNS service | the report dialog: service, discovery or classification, spectrum / class / redshift, coauthors, remarks, **Preview payload** (the exact JSON and the endpoint it goes to), **Submit report** |
| admin: Sharing services, Sharing submissions, Auto-publisher rules | staff | configuration; submissions admin has a "Retry" action |
| `/api/sharingservices/`, `/api/sharingsubmissions/` | any logged-in user | read-only DRF views (no secrets; submissions filter `?status=&kind=&service=&transient=`) |

The old DECam-only **Submit to TNS** button (`submit_to_tns/<name>/`) is still there for `_cand`
DECAT transients when no sharing service is visible to the user; it now takes its bot credential from
the `decam` sharing service when one exists (else from `settings.ini` as before).

## Models (`YSE_App/models/sharing_models.py`)

**`SharingService`**: `name`, `slug`, `kind` (tns / hermes), `credential` FK to `EncryptedCredential`
(#264; the JSON secret holds `tns_bot_id`, `tns_bot_name`, `tns_api_key`), `tns_group_id` (the
`reporting_group_id` / `groupid` of the reports), `tns_group_name` (appended as "on behalf of ..."),
`default_coauthors`, `default_remarks`, `allowed_instruments` M2M (empty = all), `allowed_obs_groups`
M2M (streams / surveys; empty = all), `groups` M2M (who may use it; empty = every authenticated user,
staff always), `enabled`, `testing` (sandbox, default on), `hermes_topic`, `config` JSON:

```json
{"instrument_ids": {"GPC1": 155}, "filter_ids": {"GPC1:w": 26, "o": 72},
 "proprietary_period_days": 0, "rename_transient": true, "at_type": "1",
 "max_photometry_points": 3, "observer": "YSE", "exptime": "", "archival_remarks": "Other",
 "discovery_data_source_id": "83", "spec_proprietary_period_days": 0}
```

**`SharingSubmission`**: `service`, `transient`, `kind` (discovery / classification / hermes), `payload`
(the report as sent), `status` (pending, submitted, accepted, rejected, failed), `external_id` (TNS
`report_id`), `tns_name`, `response` (last reply), `error`, `attempts`, `job` FK to the queue job,
`auto_publisher`, `submitted_at`, `finished_at`; `created_by` is the requester (the system user for
rule-created rows: `SHARING_SYSTEM_USERNAME`, else the first superuser).

**`AutoPublisher`**: `service`, `group`, `name`, `kind` (discovery / classification), `criteria` JSON
(`statuses`, `classes`, `min_detections`, `instruments`, `obs_groups`, `max_age_days`, `tags`; every
present key must hold), `tns_enabled`, `hermes_enabled`, `enabled`, `coauthors`, `remarks`,
`last_run_at`. Discovery rules skip transients that already carry a `20xxabc` name; classification
rules need one and a `best_spec_class`. A rule fires once per (service, transient, kind).

`SharingService` and `AutoPublisher` are in the audit log.

## Payloads (`YSE_App/sharing/tns.py`)

`build_at_report(service, transient, coauthors=, remarks=)` returns `{"at_report": {"0": {...}}}` in the
bulk-report schema: `ra`/`dec` (sexagesimal), `reporting_group_id`, `discovery_data_source_id`,
`reporter` (coauthors + "on behalf of <group>"), `discovery_datetime` (first unflagged detection from
an allowed instrument / group; a `discovery_point` flag wins), `at_type`, `host_name`,
`host_redshift`, `internal_name` (our name), `remarks`, `proprietary_period`, `photometry` (the
discovery point and the next `max_photometry_points - 1` detections, AB magnitudes) and
`non_detection`: the latest upper limit **before** the discovery point (same rule as PhotStat: an
unflagged row without `mag` whose `flux + 3 flux_err` gives a magnitude), else the transient's
`non_detect_date` / `non_detect_limit` / `non_detect_band` when it predates the discovery, else the
archival form `{"archiveid": "0", "archival_remarks": ...}`.

`build_classification_report(service, transient, spectrum=, classification=, redshift=, ...)` returns
`{"classification_report": {"0": {...}}, "_yse": {"spectrum_id": N}}`: `name` (the TNS designation from
the transient's name or an alternate name), `classifier`, `objtypeid` (from `TNS_OBJECT_TYPE_IDS`, with
aliases for YSE spellings such as "SN Iax"), `redshift` (argument, else the spectrum's, else the
transient's), `groupid`, `remarks`, `spectra.spectra-group.0` (`obsdate`, `instrumentid`,
`specTypeid` 10, `ascii_file` filled at submit time). The `_yse` block is stripped before sending.

Instrument and filter ids are TNS's numeric ids. `DEFAULT_INSTRUMENT_IDS` / `DEFAULT_FILTER_IDS`
cover GPC1/GPC2, ZTF-Cam, DECam, ATLAS and the Sloan / Johnson bands; anything else must be added to
the service's `config.instrument_ids` / `config.filter_ids` (keys `"<instrument>:<band>"` or
`"<band>"`), or the preview reports the missing id and the report cannot be submitted. Check new ids
against the TNS values tables before reporting from a new instrument.

## Queue (`sharing.submit`, `sharing.poll`)

`create_submission(service, transient, kind, user, **options)` builds the payload, writes the pending
row and enqueues `sharing.submit` on the background job queue (#263). The job uploads the
classification spectrum (`set/file-upload`, ASCII from the `TransientSpecData` rows or the data
file), POSTs `bulk-report`, stores the `report_id` (status **submitted**) and enqueues `sharing.poll`
after `SHARING_POLL_DELAY_SECONDS`. The poll POSTs `bulk-report-reply`; while TNS answers 404
("unknown report", still processing) it retries with the queue's backoff up to
`SHARING_POLL_MAX_ATTEMPTS`; a feedback entry carrying an `objname` means **accepted**, anything
else **rejected** with the feedback messages as the error. Network errors, 5xx and 429 (honouring
`x-rate-limit-reset`) retry; other 4xx fail at once. A submission that runs out of attempts is
**failed** with the last error.

On an accepted discovery report the transient is renamed to the TNS designation, its old name is
kept as an `AlternateTransientNames` row, a `Log` entry "Submitted to TNS (...)" is written and the
requester (and the rule's group) get a `sharing_result` notification (#266). With
`config.rename_transient: false` (or `RENAME_ON_ACCEPT: False`) the designation is added as an
alternate name instead. A designation that already belongs to another transient is not merged: a
Log entry records the conflict. Classification reports never rename.

**Retry** (page button, admin action, `tns.retry_submission`) resets a failed or rejected submission
to pending with the same payload and queues it again.

## TNS retrieval (`sharing.tns_retrieval`, #327)

`YSE_App/sharing/retrieval.py`: for transients whose name and alternate names are not TNS
designations, with status in `TNS_RETRIEVAL_STATUSES` and discovered / created within
`TNS_RETRIEVAL_SINCE_DAYS`, cone-search TNS (`get/search`, `TNS_RETRIEVAL_RADIUS_ARCSEC`) with the bot
of `TNS_RETRIEVAL_SERVICE` (else any enabled TNS service with a credential, production first) and
record the designation as above; at most `TNS_RETRIEVAL_MAX_PER_RUN` transients per run, one request
per `TNS_REQUEST_INTERVAL_SECONDS`, a 429 stops the run and retries the job later. It also records
the designation of accepted submissions whose transient still lacks it. The split with the existing
crons: `TNS_updates` / `TNS_recent` / `TNS_emails` **import new TNS objects** (and their photometry,
spectra, hosts); `tns_retrieval` only **matches transients we already have**.

## Auto-publishers

`YSE_App/sharing/autopublish.py`: every `Transient` save runs the enabled rules (a cached "any rule
enabled?" check keeps this free when there are none; saving a rule in the admin resets the cache),
and the `sharing.autopublish_sweep` job runs every rule against its qualifying transients. The
services page shows a **Dry run** per rule (which transients qualify right now) for staff.

## Crons and settings

`CRON_CLASSES` gains `YSE_App.data_ingest.Sharing_Jobs.TNSRetrieval` and `AutoPublishSweep`; both
only *queue* a job (never a duplicate while one is queued or running) and are off until enabled.
Everything is under `[sharing]` in `settings.ini` (defaults shown in `public_settings.ini`):

| key | default | meaning |
|---|---|---|
| `TNS_API_URL`, `TNS_SANDBOX_API_URL` | wis-tns.org / sandbox.wis-tns.org `/api` | API roots; the service's `testing` flag picks one |
| `TNS_HTTP_TIMEOUT_SECONDS` | 60 | |
| `DEFAULT_TESTING` | True | new services start in sandbox mode |
| `RENAME_ON_ACCEPT` | True | rename on accepted discovery (per-service `config.rename_transient` overrides) |
| `POLL_DELAY_SECONDS`, `POLL_MAX_ATTEMPTS` | 10, 12 | reply polling |
| `SYSTEM_USERNAME` | (first superuser) | actor for rule / job rows |
| `AUTOPUBLISH_ON_SAVE` | True | evaluate rules on transient saves |
| `AUTOPUBLISH_CRON_ENABLED`, `AUTOPUBLISH_CRON_MINUTES` | False, 60 | hourly sweep (env `YSE_SHARING_AUTOPUBLISH_CRON=1`) |
| `TNS_RETRIEVAL_CRON_ENABLED`, `TNS_RETRIEVAL_CRON_MINUTES` | False, 60 | hourly retrieval (env `YSE_SHARING_TNS_RETRIEVAL_CRON=1`) |
| `TNS_RETRIEVAL_SERVICE` | any | slug of the service whose bot searches |
| `TNS_RETRIEVAL_SINCE_DAYS`, `TNS_RETRIEVAL_STATUSES`, `TNS_RETRIEVAL_RADIUS_ARCSEC`, `TNS_RETRIEVAL_MAX_PER_RUN`, `TNS_REQUEST_INTERVAL_SECONDS` | 30, New,Watch,Following,FollowupRequested,Interesting, 3.0, 50, 1.0 | retrieval scope and rate |

## Setting up a service

```bash
# from the existing settings.ini bot (tns_bot_id / tns_bot_name / tnsapikey), sandbox mode:
python manage.py create_tns_sharing_service --slug yse-tns --name "YSE TNS bot" --group-id 83 \
    --group-name YSE --from-settings --coauthors "R. J. Foley (UCSC), D. O. Jones (Hawaii)"
# the DECam bot (also used by the legacy Submit to TNS view):
python manage.py create_tns_sharing_service --slug decam --name "DECam TNS bot" --group-id 83 --decam
# or explicit values; switch a service to the real TNS once sandbox reports look right:
python manage.py create_tns_sharing_service --slug yse-tns --bot-id 12345 --bot-name YSE_Bot --api-key ...
python manage.py create_tns_sharing_service --slug yse-tns --production      # (--sandbox to go back)
```

Then in the admin: restrict `allowed_instruments` / `allowed_obs_groups` / `groups` as needed, add
`config.instrument_ids` for instruments outside the defaults, and (optionally) an auto-publisher
rule. Report a few objects to the sandbox (`https://sandbox.wis-tns.org/object/<name>` is linked
from the submission) before switching `testing` off. The job queue must be running
(`RunQueuedJobs` from `runcrons`, or a `run_jobs --loop` worker; docs/background-jobs.md).

## Deploy notes

- Migration `0018_sharing_services` (three tables, two indexes; after `0017_broker_candidates`).
- `CREDENTIALS_KEY` must be configured on production (#342): the bot credential is an
  `EncryptedCredential`.
- Nothing reports until a `SharingService` row exists; new rows are sandbox-only until `testing` is
  switched off. The two crons are off until `[sharing]` enables them.
- Tests: `YSE_App/tests/test_sharing_services.py` (48; payload builders, client, queue end to end with
  mocked TNS replies, retry, views, API, retrieval, rules, crons, command).
