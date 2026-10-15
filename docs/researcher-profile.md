# User agreement, researcher profile and onboarding gate

Issues #627 (profile and encrypted fields), #628 (mentorship and affiliation history),
#629 (SciX metrics), #630 (agreement and onboarding gate), #631 (develop-direct promotion rule).
Server steps: `docs/ziggy-runbook.md`, section "User agreement, researcher profile and onboarding gate".

## Pages

| URL | What |
| --- | --- |
| `/agreement/` | Current agreement, one checkbox, Accept. Acceptances store only user, version and time (no IP or user agent). |
| `/profile/` | Your profile; required and encrypted fields marked; "My students" for scientific staff and faculty. |
| `/profile/edit/` | The form. Required fields depend on career level (below). |
| user menu > My profile | Link from every page. |

Staff add agreement versions in the admin (User agreements). Saving a version as current makes
every other version not current and asks every user to accept it on their next page view.
`{acknowledgements_url}` in a body becomes this stack's Acknowledgements link.

## Requirements

| Career level | Required |
| --- | --- |
| everyone | full name, institution, department, career level |
| other | the career-level description |
| undergraduate, graduate | current advisor (a YSE-PZ user from the autocomplete, or a typed name; stored as a `current_advisor` `Mentorship`) |
| postdoc, scientific staff, tenure-track, tenured | ORCID iD (format and ISO 7064 check digit) and PhD year (waived while no research key is installed) |

## Encrypted fields (country, gender, PhD year)

* Encrypted with the research **public** key: a fresh AES-256-GCM key per value, wrapped with
  RSA-OAEP-SHA256 (`YSE_App/services/research_crypto.py`). Stored in `EncryptedProfileValue`
  with the key fingerprint and date. Nothing on the server can decrypt them; admin, templates
  and history show only "(encrypted, set on <date>)". Values are validated before encryption.
* Key: env `YSE_RESEARCH_PUBLIC_KEY` (PEM text or a path) or `[research] public_key_path`.
  Without a usable key the fields are not collected, the PhD-year requirement is waived, a
  warning is logged and shown to staff on the admin index, and nothing else changes.
* Key pair: `python manage.py generate_research_keypair --out-dir <dir> [--passphrase]`, run on
  the custodian's own machine. Only the public key goes on the server.
* Offline decryption by the custodian (never on the server):

  ```python
  from cryptography.hazmat.primitives import serialization
  from YSE_App.services.research_crypto import decrypt_value
  key = serialization.load_pem_private_key(open("research_private_key.pem", "rb").read(), password=b"...")
  decrypt_value(ciphertext, field_name, key)
  ```

## History and deletion

Every change to any profile field writes a `ProfileFieldHistory` row (field, plain value or a
link to the ciphertext row, time, who). Clearing an optional field also blanks that field's
earlier plain values in the history; deleting an encrypted field deletes all its ciphertexts.

## Mentorship, affiliations, metrics

* `Mentorship`: mentor/mentee as a user or a free-form name (database constraint: at least one
  of each), kind `phd_advisor` / `current_advisor` / `research_supervisor`, start and end year.
* `AffiliationHistory`: institution, department, position, years (admin for now).
* `ResearcherMetrics` from SciX (`https://api.adsabs.harvard.edu/v1`, `Authorization: Bearer`):
  search by `orcid:<iD>`, then the metrics endpoint for h-index, citations and refereed papers;
  first-author papers are records where the ORCID is in position 0. Token: an
  `EncryptedCredential` with service `scix`, secret `{"token": "..."}`. Command
  `refresh_researcher_metrics`, monthly cron `RefreshResearcherMetrics`; both off unless
  `[research] RESEARCHER_METRICS_ENABLED: True`. Rate limits: `REQUEST_INTERVAL_SECONDS`,
  `MAX_PER_RUN`, `MIN_AGE_DAYS`; a 429 stops the run. Google Scholar is not scraped.

## Privacy

All these tables are excluded from SQL Explorer (`EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES` and
`EXPLORER_SQL_BLACKLIST` in `YSE_PZ/settings.py`), have no REST API, and are not registered with
django-auditlog. The Explorer MySQL user's grants are the hard barrier (runbook step D).

## Onboarding gate

`YSE_App/onboarding.py`, after the auth and message middleware. For signed-in browser GET
requests to HTML pages: no acceptance of the current agreement -> `/agreement/?next=...`; then an
incomplete profile -> `/profile/edit/?next=...` (`next` is checked with
`url_has_allowed_host_and_scheme`).

Never gated: `/api/` and `/api-auth/`; any request with an `Authorization` header (Basic or
Token); every view wrapped in `login_or_basic_auth_required` (marked automatically: add_transient,
add_transient_phot, add_transient_spec, add_gw_candidate, add_yse_survey_obs/fields,
add_survey_obs, upload_spectrum, the downloads, get_transient and the box searches, query_api,
change_status_for_query); `/slack/events/`; the external-service callback; `.json` status
endpoints; AJAX/fetch requests and non-GET methods; static and media files; login, logout,
password reset and change; the agreement, profile and acknowledgements pages; `/admin/` for
superusers (other staff are gated like everyone else). Anonymous requests are untouched.

A session that passed is remembered (agreement version, key state, rules version), so normal page
views add no queries. Rollback: `[site_settings] ONBOARDING_GATE_ENABLED: False` or env
`YSE_ONBOARDING_GATE=0`. The test runner (`YSE_App/test_runner.py`) turns the gate off for the
existing suite; `YSE_App/tests/test_onboarding_gate.py` turns it on.
