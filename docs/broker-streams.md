# Broker streams: Kafka / ANTARES consumers to candidates

`manage.py broker_ingest` (issue #278) is the streaming half of the broker
ingest described in docs/brokers.md. Where the `BrokerPoll` cron asks a broker
for recent alerts once an hour, a stream worker sits on the broker's Kafka
topic(s) and evaluates every alert as it arrives, with the same
`BrokerFilter` rules, the same `Candidate` table and page, the same auto-save,
default tags and group notifications. Nothing streams unless an operator
creates and enables a `BrokerConnection` **and** starts the worker.

## Pieces

| piece | where | what |
|---|---|---|
| `BrokerConnection` | admin *Broker connections*, `/api/brokerconnections/` (read) | one stream: broker slug, kind (`kafka` / `antares`), bootstrap servers, topics, consumer group id, message format (`json` / `avro`), credential, extra config, `enabled` (default **off**) |
| `IngestHeartbeat` | admin *Ingest heartbeats*, `/api/brokerconnections/heartbeats/` | one row per (connection, topic, worker): last seen, messages / candidates / saved / errors, last offset, lag when the client reports it, last error; `status` running / stopped / error |
| `YSE_App.brokers.streams` | code | `consume()` worker loop, `KafkaStreamClient` (`confluent_kafka`, imported lazily), `AntaresStreamClient` (`antares_client.StreamingClient`), `run_workers()` (fork N processes in one consumer group), `stale_heartbeats()` |
| `provider.parse_stream_message(topic, message)` | `brokers/fink.py`, `brokers/lasair.py`, `brokers/antares.py` | decodes one topic message into the normalised `BrokerAlert` (capability `stream`) |
| `broker_ingest` | management command | `--connection SLUG --workers N [--from-beginning | --since ISO] [--max-messages N] [--timeout S] [--dry-run] [--list]` |
| dashboard banner | `/dashboard/` | warns when a *running* heartbeat of an enabled connection is older than `STREAM_STALE_MINUTES` (15) |

## Providers with a stream

| broker | client | wire format | public entry point (defaults shown by `manage.py brokers --json`) | credential |
|---|---|---|---|---|
| `fink` | Kafka | Avro (`fastavro`) | `kafka-ztf.fink-broker.org:24499`, topics `fink_sn_candidates_ztf`, `fink_early_sn_candidates_ztf`, ... | none; the group id is issued by Fink |
| `lasair` | Kafka | JSON | `kafka.lsst.ac.uk:9092`, topics `lasair_<filter name>` | none for Kafka; REST enrichment uses the Lasair token (`EncryptedCredential(service="lasair")`, `{"token": ...}`) |
| `antares` | `antares_client.StreamingClient` | loci | topics are ANTARES stream names (e.g. `extragalactic`) | `api_key` / `api_secret` in the connection's credential |

Fink packets carry the ZTF `candidate` record plus Fink's added values; the
parser flattens them into the same `i:` / `d:` row the REST provider reads.
Lasair filter-stream messages carry `objectId` and whatever columns the Lasair
filter selected; when position or magnitude is missing and a token is
configured the parser fetches `/objects/` once. ALeRCE has no public Kafka feed.

Per message the worker: decodes (JSON or Avro) -> `parse_stream_message` ->
enabled `BrokerFilter` rows of that broker whose `topics` list is empty or
contains the topic -> `ingest.process_alert` (criteria, candidate upsert,
auto-save through the first passing filter with `auto_save`, notification to
the group for filters with `notify_group`). Offsets are committed after each
batch (`STREAM_BATCH_SIZE`, 100); with `--dry-run` nothing is written and
nothing is committed. One bad message is logged, counted in the heartbeat's
`errors` / `last_error`, and skipped; the loop continues. A missing client
package or credential stops the worker with a clear message and marks the
heartbeat `error`.

## Setting up a stream (operator)

1. Python packages in the ingest venv (the web venv needs none of them):
   `confluent-kafka` (already pinned in requirements.txt) for Kafka, `fastavro`
   for Fink's Avro topics, `antares-client` for ANTARES streams.
2. Admin > *Encrypted credentials*: a row with `service=<broker slug>` holding
   `{"username": ..., "password": ...}` (Kafka SASL) or `{"api_key": ..., "api_secret": ...}`
   (ANTARES), only when the stream needs one. Lasair's REST token
   (`{"token": ...}`) also lives here, `service=lasair`.
3. Admin > *Broker connections*: name, slug, broker, kind, bootstrap servers,
   topics (JSON list), group id (Fink hands one out; otherwise any stable
   string, shared by all workers of one connection), message format, credential.
   Optional `config`: `{"security_protocol": "SASL_SSL", "sasl_mechanism": "SCRAM-SHA-512",
   "auto_offset_reset": "latest", "batch_size": 100, "kafka": {<any confluent-kafka option>}}`.
   Leave **enabled off** until the filters are ready.
4. Create the `BrokerFilter` rows for that broker (docs/brokers.md); set `topics`
   on a filter to restrict it to some of the connection's topics.
5. Smoke test without writing: `python manage.py broker_ingest --connection <slug> --force --dry-run --max-messages 50 --timeout 60`.
6. Enable the connection and run the worker as a service:

```ini
# /etc/systemd/system/yse-pz-broker-ingest@.service   (instance = connection slug)
[Unit]
Description=YSE-PZ broker stream ingest (%i)
After=network.target mysql.service

[Service]
Type=simple
User=yse
WorkingDirectory=/data/yse_pz/YSE_PZ
Environment=DJANGO_SETTINGS_MODULE=YSE_PZ.settings
ExecStart=/path/to/ingest-venv/bin/python manage.py broker_ingest --connection %i --workers 2
Restart=always
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now yse-pz-broker-ingest@fink-sn`. With docker compose the
`yse_broker_ingest` service in `docker/docker-compose.yml` does the same and
only starts under `--profile brokers` (`BROKER_CONNECTION`, `BROKER_WORKERS`).
Workers stop cleanly on SIGTERM after the current batch (offsets committed).
The job runner (`run_jobs`, docs/background-jobs.md) must be running too: the
worker enqueues nothing itself, but saved transients use the queue for
photometry imports triggered from the page.

## Replay and workers

`--from-beginning` starts at the earliest retained offset of every partition;
`--since 2026-09-01T00:00:00Z` seeks with `offsets_for_times`. `--workers N`
forks N processes in the same consumer group, so Kafka splits the partitions
between them; a topic with one partition gains nothing from a second worker.
Candidates are keyed on (broker, object id), so the same alert set gives the
same candidate set whatever the number of workers (test
`test_two_workers_match_one_worker`). `--max-messages` / `--timeout` /
`--idle-exit` bound a run for tests and smoke checks; `--list` prints the
connections and heartbeats (`--json` for scripts).

## Settings (`settings.ini`, `[brokers]`)

| key | default | meaning |
|---|---|---|
| `STREAM_BATCH_SIZE` | `100` | messages per poll / commit |
| `STREAM_STALE_MINUTES` | `15` | heartbeat age that triggers the dashboard banner |
| `LASAIR_API_URL` | `https://lasair-ztf.lsst.ac.uk/api` | Lasair REST base |
| `DETAIL_RADIUS_ARCSEC` | `5` | cone radius of the transient-detail *Brokers* tab (#275) |

Tests: `YSE_App/tests/test_broker_streams.py` (fake stream client with
recorded messages, Avro / JSON decoding, Lasair provider with mocked HTTP,
Fink / ANTARES stream parsers, heartbeats, error tolerance, one vs two workers,
dry run, notification hook, default tags, per-group rejection, filter versions
and preview, Brokers tab, cone-search page, dashboard banner, command).
