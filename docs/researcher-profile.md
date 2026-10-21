# User agreement, researcher profile and onboarding gate

Issues #627 (profile and encrypted fields), #628 (mentorship and affiliation history),
#629 (SciX metrics), #630 (agreement and onboarding gate), #631 (develop-direct promotion rule),
#743 (User Agreement v2), #744 (voluntary research consent form), #746 (research export).
Server steps: `docs/ziggy-runbook.md`, section "User agreement, researcher profile and onboarding gate".

## Pages

| URL | What |
| --- | --- |
| `/agreement/` | Current agreement, one checkbox, Accept. Acceptances store only user, version and time (no IP or user agent). |
| `/research-consent/` | The voluntary research consent form: one checkbox, Consent, Decline or Not now; Withdraw once consented. Never a gate. |
| `/profile/` | Your profile; required and encrypted fields marked; "My students" for scientific staff and faculty. |
| `/profile/edit/` | The form. Required fields depend on career level (below). |
| user menu > My profile | Link from every page. |

Staff add agreement versions in the admin (User agreements). Saving a version as current makes
every other version not current and asks every user to accept it on their next page view.
`{acknowledgements_url}` in a body becomes this stack's Acknowledgements link.

Version 2 (`0038_user_agreement_v2`, #743) is v1 without item 5 ("Use of platform data for
research", now the separate consent form below) and without "(such as gender)" in the Privacy
item. v1 and its acceptances stay on record; everyone accepts v2 once through the gate.

## Requirements

Without research consent (#744) only full name and institution are required; the rows below
apply to consenting users.

| Career level | Required |
| --- | --- |
| everyone (consenting) | full name, institution, department, career level |
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

## Research consent (#744)

Voluntary and separate from the agreement. Text: `0040_seed_research_consent` (version 1, from
Ryan Foley, verbatim). Models: `ResearchConsentForm` (versions; `effective_date` is the go-live
date, i.e. the day the migration first ran on that database), `ResearchConsentRecord` (one row
per consent, decline or withdrawal, with time and form version; never deleted) and
`ResearchConsentPrompt` (when the form was offered, for reminders). A user's state is their latest
record. Service: `YSE_App/services/research_consent.py`.

* **Offer:** right after the user accepts an agreement, if they have never answered. Consent,
  Decline and Not now all continue to the page they were going to. The page is exempt from the
  gate. My profile shows the state and links to the form (consent later, or withdraw).
* **Reminders**, only for users who have not answered: a second request after 10 logins or 30
  days from the first offer (whichever comes first), then at the first login of each calendar
  year. A login sets a session flag, and the first page that passes the gate redirects once to
  the form. Users who answered either way are never reminded.
* **Essential vs research data** (Ryan, 2026-10-20). Kept for everyone (`ESSENTIAL_PROFILE_FIELDS`):
  username, full and preferred name, publication name, acknowledgment text, email, institution,
  ORCID, observing experience (it shows who can observe with which telescope and instrument), and
  group memberships and roles. Everything else is research data: `RESEARCH_PROFILE_FIELDS`
  (pronouns, department, career level, interests, website, GitHub, time zone), every encrypted field (country, PhD year, gender), advisors and
  students, affiliation history and SciX metrics. Research data are offered on `/profile/edit/`
  only to consenting users and required only from them (the onboarding gate requires only full
  name and institution otherwise). "My students" and the SciX refresh are for consenting users only.
* **Decline or withdraw:** nothing is deleted automatically. The user is excluded from research at
  once, and the administrators in `RESEARCH_OPTOUT_NOTIFY_USERNAMES` (settings.ini `[research]
  optout_notify_usernames`, default every active superuser) get a `research_optout` notification
  (in-app, plus email per their preferences). They run `manage.py purge_unconsented_demographics
  [--user NAME] [--apply]` (dry run by default). It deletes that user's research data and keeps the
  essential data and all activity. Advisor and student rows are deleted only when the opted-out
  user entered them (as mentee or mentor); rows the other person entered stay, because privacy
  covers what an individual revealed, not information about them (Ryan, 2026-10-20). Users who have not answered keep stored values (they may still
  delete encrypted ones) but are never used for research.
* **Admin:** read-only for superusers (Research consent forms, Research consent records); other
  staff cannot see them. No demographic value is ever shown.

## Research datasets and the research export (#744, #746)

Every research dataset is built with `YSE_App/services/research_dataset.py` (`ResearchCohort`,
`ActivitySource`, `build_dataset`). The rules:

* Activity **before go-live** (the first form's `effective_date`, 00:00 UTC) is exempt, because it
  was public, as on an internet forum. It is included anonymized for everyone, including users who
  declined or withdrew (Ryan, 2026-10-20).
* Activity **from go-live on** is included only for consenting users, inside their consent periods
  (consent to the next withdrawal; a user may consent again). A user whose latest decision is
  `withdrawn` has none of their post-go-live activity included.
* **Research profile data** (plain research fields, mentorships, metrics, and the latest
  ciphertext per encrypted field) only for users whose latest decision is `consented`. Ciphertexts
  are decrypted offline by the custodian.
* **Anonymized:** users appear only as random strings drawn fresh for each export. Only the fields
  a source names are copied, and identifying fields (user, username, email, names) are refused.

`manage.py export_research_database --out FILE.sqlite [--force]` (`services/research_export.py`)
writes a standalone SQLite database under these rules. It is a management command, so it runs only
on the server.

* **Activity tables:** `followups`, `followup_requests`, `comments`, `annotations`,
  `data_access_requests`, `facility_requests`, `interests`, `favorites` and `transient_changes`
  (status, class and redshift changes from the change log).
* **Public lookups:** `transients`, `transient_statuses`, `transient_classes`,
  `followup_statuses`. Transients keep their real ids and names.
* **Participants:** `participants` (research profile only for consenting users; institution as a
  random string), `participant_instruments`, `demographics` (ciphertext), `mentorships`, `metrics`
  and `metadata`.
* **Identifiers:** person, row, group and allocation ids are random strings per export, and other
  users named in a row appear only when included.
* **Free text:** every mention of a person is replaced with that person's pseudonym for this
  export (the same random string used for them everywhere in the file; people outside the
  participant set get a consistent random string too), by `NameReplacer`:
  - first, direct matches: usernames (so `@username`), emails, ORCID iDs, full, display and
    publication names;
  - then given names and surnames of any length from 2 letters (single initials are left), as
    whole words only: the character before is the start of the text, whitespace or a
    non-alphanumeric character (`-`, `@`, `(`, ...), and the character after is the end, whitespace
    or punctuation, so "Li" is replaced but "Literally" is not;
  - case-insensitive, longest match first; a name shared by several people becomes one neutral
    per-export string (`n_...`); emails and ORCID iDs of nobody known become `[email]` and
    `[orcid]`.
* **Timestamps** are exact (Ryan, 2026-10-20): fresh random ids make matching two leaked exports
  harder, not impossible.
* **Never exported:** credentials, sessions, tokens, notifications, jobs, request payloads, external
  ids and URLs, IP addresses, websites and GitHub names.

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

All these tables (including the two research consent tables) are excluded from SQL Explorer (`EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES` and
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
password reset and change; the agreement, research consent, profile and acknowledgements pages; `/admin/` for
superusers (other staff are gated like everyone else). Anonymous requests are untouched.

A session that passed is remembered (agreement version, key state, rules version), so normal page
views add no queries. Rollback: `[site_settings] ONBOARDING_GATE_ENABLED: False` or env
`YSE_ONBOARDING_GATE=0`. The test runner (`YSE_App/test_runner.py`) turns the gate off for the
existing suite; `YSE_App/tests/test_onboarding_gate.py` turns it on.
