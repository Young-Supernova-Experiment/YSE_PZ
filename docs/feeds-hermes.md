# Hermes / SCiMMA feed and publishing

YSE-PZ can read the transient reports other groups publish on **Hermes**
(hermes.lco.global, the web front of the SCiMMA Hopskotch Kafka broker) and
turn them into transients or candidates, and it can publish its own discovery
and classification reports there (issues #281 and the Hermes half of #326;
umbrella #280). The code lives in `YSE_App/feeds/hermes.py`; the shared feed
machinery in `YSE_App/feeds/base.py` is described in the *How the feeds work*
section below and is the same for the Einstein Probe (docs/feeds-einstein-probe.md)
and JPL Scout (docs/feeds-jpl-scout.md) feeds.

The design follows SkyPortal's Hermes / SCiMMA sync (BSD-3-Clause) in spirit:
a configured topic is read on a schedule and mapped onto sources; no code is
copied.

## What it does

| message | unknown object | known object (name, alias or within the match cone) |
|---|---|---|
| discovery (`hermes.discovery`, `tns.*` mirrors, `hermes.test`) | creates a **New** transient through the `/add_transient` path with the reporting group as `obs_group`, the message photometry imported, an `InformationSource` "Hermes" web resource pointing at the message, and a comment | records the Hermes name as an alternate name, imports any new photometry, adds the web resource and a comment |
| classification (`hermes.classification`, or a message with `spectroscopy[].classification`) | as a discovery, then classified | sets `best_spec_class` (matching a `TransientClass` by name; an unknown label goes to `TNS_spec_class`), fills a missing redshift and adds the comment *Hermes classification: SN Ia at z=... by <reporter>* |
| photometry only | ignored unless `auto_save` (then a transient) | photometry imported |

With `config.auto_save: false` a source produces **candidates** instead
(`broker='hermes'` on the Broker Candidates page); saving one goes through the
same provider, so the message photometry is imported at that point. Optional
`criteria` (the `BrokerFilter` rule keys, e.g. `{"mag_max": 19.5}`) are
evaluated on every message first.

## Configure a source

1. **Credential** (admin > Encrypted credentials): kind *Hermes / SCiMMA*,
   service `hermes`, secret JSON `{"hermes_token": "<Hermes API token>"}`.
   Public topics can be read without a token; publishing always needs one
   (Hermes > profile > API token). Add `"hop_username"` / `"hop_password"`
   (SCiMMA credentials) only for the Kafka consumer below.
2. **Feed source** (admin > Feed sources, or the *Add source* button on
   `/feeds/`): kind *Hermes / SCiMMA topic*, `topic` (e.g. `hermes.discovery`,
   `tns.new-objects`, `hermes.test`), the credential, and `config`:

| key | default | meaning |
|---|---|---|
| `auto_save` | `true` | save discoveries as transients (else candidates) |
| `save_status` | `"New"` | `TransientStatus` for created transients |
| `save_obs_group` | reporter | force one `ObservationGroup` name instead of the reporting group |
| `criteria` | none | `BrokerFilter` criteria keys evaluated on each message |
| `match_radius_arcsec` | `[brokers] MATCH_RADIUS_ARCSEC` (2") | cone for matching existing transients |
| `import_photometry` | `true` | import the message photometry |
| `since_hours` | 24 | first poll window (later polls start at the last poll time) |
| `kinds` | all | restrict to `discovery`, `classification`, `photometry` |
| `classify` | `true` | apply classifications to known transients |
| `update_redshift` | `true` | fill a missing redshift from the message |
| `max_per_run` | 200 | messages handled per poll |

3. **Run it**: `manage.py feeds --poll <slug>` once by hand (`--dry-run` to see
   the counters without writing), then switch the cron on (below).

## Polling, cron and the queue

Every enabled source is polled by the `feeds.poll` job (`YSE_App/feeds/jobs.py`)
through the background queue (#263). The `FeedPoll` django_cron class
(`YSE_App/data_ingest/Feeds.py`, in `CRON_CLASSES`) enqueues one job per source
without an active poll; it is off until `[feeds] POLL_CRON_ENABLED = True`
(`POLL_CRON_MINUTES`, default 15). Staff can also press *Poll now* on `/feeds/`
or `POST /api/feedsources/<id>/poll/`. The source row keeps `last_polled`,
`last_summary` (`fetched, passed, created, linked, candidates, skipped, errors`)
and `last_error`; a failed poll also fails the job so `/jobs/` shows it.

The poll uses the Hermes REST API (`GET /api/v0/messages/?topic=...&published_after=...`,
`FEEDS_HERMES_API_URL`). When `hop-client` is installed in the venv and the
credential carries SCiMMA credentials, `manage.py feeds --consume <slug> --max 100 --timeout 30`
reads the topic straight from Kafka (`FEEDS_HERMES_KAFKA_URL`, default
`kafka://kafka.scimma.org/`) and processes the messages the same way; run it
from a loop or a systemd unit when latency matters. `hop-client` is optional
and not pinned in `requirements.txt` (its `confluent-kafka` requirement differs
from the pinned 1.7.0); `pip install hop-client` into the venv when needed.

## Publishing (sharing services)

A `SharingService` of kind *Hermes / SCiMMA* (docs/tns-sharing.md) with a
`hermes_topic` and a credential holding `hermes_token` publishes submissions of
kind `hermes` (and every submission routed to a Hermes service) through
`POST /api/v0/submit_message/`. The message is built from the transient by
`YSE_App.feeds.hermes.build_message`: one target (name, position, discovery
info with the service's `tns_group_name` as reporting group, redshift,
aliases), the detections from the service's allowed instruments / observation
groups (newest 50, AB mag), and, for a classification, a `spectroscopy` row
with the class. A service in *testing* mode publishes to `hermes.test`. The
returned message `uuid` is stored as the submission's `external_id`; the full
message and the Hermes reply are in `response`. Missing topic or token, or an
HTTP error from Hermes, marks the submission failed with the reason so it can
be retried from `/sharing/`.

## How the feeds work (shared)

`FeedProvider` (`YSE_App/feeds/base.py`) is a `BrokerProvider` (docs/brokers.md)
whose input is a `FeedSource` row. The three providers register with the broker
registry under the slugs `hermes`, `ep` and `scout`, so feed candidates use the
existing `Candidate` table, scanning page, save / reject actions and API without
new tables. Each provider implements `poll(source)` (and `consume` for the
Kafka-backed ones) returning `FeedMessage` records; `FeedProvider.run` evaluates
the source's criteria, matches existing transients (name, aliases, cone) and
routes each message to *link*, *create* or *candidate*, with per-provider hooks
for classifications and annotations. Provenance is an `InformationSource` row
per feed plus a `TransientWebResource` link to the message.

| piece | where |
|---|---|
| model / migration | `YSE_App/models/feed_models.py` (`FeedSource`), `0023_feeds` |
| providers | `YSE_App/feeds/{hermes,einstein_probe,scout}.py` |
| jobs / cron | `YSE_App/feeds/jobs.py` (`feeds.poll`, `feeds.screen_minor_planets`), `YSE_App/data_ingest/Feeds.py` |
| page / API | `/feeds/` (`YSE_App/feed_views.py`), `/feeds/status.json`, `/api/feedsources/` |
| admin | Feed sources (config JSON validated per kind) |
| command | `manage.py feeds [--json] [--poll SLUG] [--consume SLUG] [--screen NAME] [--dry-run]` |
| settings | `[feeds]` in settings.ini (see `YSE_PZ/public_settings.ini`) |
| tests | `YSE_App/tests/test_feeds.py` (all HTTP / Kafka mocked) |

## Operator steps (Ziggy)

Everything is off by default; the migration alone changes nothing visible.

1. Create the Hermes credential and the feed source(s) in the admin, or on
   `/feeds/` > *Add source*.
2. Test once: `venv/bin/python manage.py feeds --poll <slug> --dry-run`, then
   without `--dry-run`.
3. Enable the schedule in `YSE_PZ/settings.ini`:

```ini
[feeds]
POLL_CRON_ENABLED = True
POLL_CRON_MINUTES = 15
```

   `manage.py runcrons` (already scheduled) then queues the polls, and the
   queue runner (`RunQueuedJobs` / `run_jobs --loop`) executes them.
4. For publishing, set `hermes_topic` and the token credential on the Hermes
   `SharingService`; leave *testing* on until a message on `hermes.test` looks
   right.
