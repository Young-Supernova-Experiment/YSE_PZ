# Einstein Probe feed

Einstein Probe's Wide-field X-ray Telescope publishes fast X-ray transient
alerts as GCN notices (Kafka topic `gcn.notices.einstein_probe.wxt.alert`,
unified JSON schema). The `ep` feed (`YSE_App/feeds/einstein_probe.py`, issue
#282, umbrella #280) turns each notice into a **candidate** with the X-ray
position and error circle and annotates every known transient inside that
circle, so the optical counterpart search starts from the scanning page. The
shared feed machinery is described in docs/feeds-hermes.md (*How the feeds work*).

## What it does

* One notice -> one `Candidate` with `broker='ep'`, `alert_id='EP<id>'`, the
  position, `classification` *X-ray transient (WXT)*, and in `payload.properties`
  the error radius (arcsec), trigger time, SNR, net count rate, energy range and
  the notice text. A **second notice for the same event** (a refined position)
  updates that candidate (`n_alerts` grows, position and error radius replaced)
  instead of creating another one; a rejected candidate stays rejected.
* Every transient within the error circle (capped at 30') gets the
  `einstein_probe` annotation (docs/annotations.md): `event`,
  `separation_arcsec`, `error_radius_arcsec`, `trigger_time`, `image_snr`,
  `net_count_rate`, `instrument`, `summary`. With `config.comment: true` a
  comment is added as well. The annotation is merged on later notices.
* Saving the candidate (scanning page, `POST /api/candidates/<id>/save/`)
  creates a transient at the X-ray position with `obs_group` *Einstein Probe*
  (or links the one already there), like any broker candidate. Set
  `config.auto_save: true` to skip the scanning step.

## Configure a source

Kind *Einstein Probe alerts*; no topic. Two ways to get the notices:

1. **Polled mirror** (queued job, no extra package): `config.url` names a JSON
   endpoint returning the notices, either a list or an object with a `notices`
   / `results` / `data` list, in the GCN schema (`id`, `ra`, `dec`,
   `ra_dec_error` in degrees, `trigger_time`, `image_snr`, `net_count_rate`,
   `instrument`). This can be an institutional archive of the Kafka stream or
   a small script that dumps it. `config.since_hours` limits the window by
   trigger time (unset: everything the mirror returns; the upsert de-duplicates).
2. **GCN Kafka** (`pip install gcn-kafka`; credential with GCN `client_id` /
   `client_secret` from https://gcn.nasa.gov/quickstart): `manage.py feeds
   --consume <slug> --max 50 --timeout 60` reads the topic (`config.topic`
   overrides the default) and processes the notices. Run it from a loop or a
   systemd unit; a poll of the same source without `config.url` fails with a
   clear message.

| key | default | meaning |
|---|---|---|
| `url` | none | notice mirror to poll |
| `topic` | `gcn.notices.einstein_probe.wxt.alert` | Kafka topic for `--consume` |
| `since_hours` | unset | only notices triggered in the last N hours |
| `max_error_arcsec` | unset | skip notices with a larger error radius |
| `annotate` | `true` | write the `einstein_probe` annotation on transients in the circle |
| `comment` | `false` | also add a comment to those transients |
| `auto_save`, `save_status`, `save_obs_group`, `criteria`, `match_radius_arcsec`, `max_per_run` | shared keys, see docs/feeds-hermes.md |

## Operator steps (Ziggy)

Off by default. To use the polled mirror:

1. Admin > Feed sources > add: kind *Einstein Probe alerts*, slug `ep`,
   `config` `{"url": "https://<your mirror>/ep_notices.json", "comment": false}`.
2. `venv/bin/python manage.py feeds --poll ep --dry-run`, then without `--dry-run`.
3. Turn on the poll cron under `[feeds]` (`POLL_CRON_ENABLED = True`), the same
   switch as the Hermes feed.

To read GCN Kafka directly instead: `venv/bin/pip install gcn-kafka`, create an
Encrypted credential (kind *Generic*, service `ep`) with `client_id` /
`client_secret`, attach it to the source, and run
`venv/bin/python manage.py feeds --consume ep --timeout 300` from a loop.
