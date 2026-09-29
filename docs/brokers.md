# Alert brokers: provider plugins and the candidate ingest

YSE-PZ talks to alert brokers through one interface (`YSE_App/brokers/`,
issue #272) and can poll them on a schedule, run each group's saved filters on
the incoming alerts and put the ones that pass on a scanning page as
*candidates*, separate from the dashboard, until someone saves or rejects them
(issue #276). This is the polling half of that design; Kafka stream consumers
(#278) are not part of it. The non-optical feeds (Hermes / SCiMMA, Einstein Probe,
JPL Scout; #280) are `BrokerProvider`s too and reuse the candidate table and page:
see docs/feeds-hermes.md, docs/feeds-einstein-probe.md and docs/feeds-jpl-scout.md.

## Concepts

| term | meaning |
|---|---|
| provider | a `BrokerProvider` subclass for one broker (`antares`, `fink`, `alerce`), registered by slug in `YSE_App.brokers.registry` |
| capability | what a provider implements: `query_alerts`, `get_alert`, `cutouts`, `cone_search`, `photometry`, `save_as_transient`, `filter_crud`; `provider.capabilities_list()` drives what the page and the API offer |
| `BrokerAlert` | the normalised alert every provider returns: object id, latest alert id, position, MJDs, latest mag/band, `rb`/`drb`, `ndet`, classification and class probabilities, galactic latitude, cutout links, the raw payload |
| `BrokerFilter` | a saved rule for one broker, owned by a group (or shared when the group is blank): a broker-side `query` and client-side `criteria`, plus the save policy |
| `Candidate` | a broker object that passed at least one enabled filter during a poll; status `new` / `saved` / `rejected`, linked to the `Transient` once saved; unique on (broker, object id) |
| poll | one `brokers.ingest` job run: for each enabled filter of a broker, `query_alerts` → evaluate criteria → upsert candidates → auto-save when the filter says so |

## Providers

| slug | source | credential | capabilities |
|---|---|---|---|
| `antares` | NOIRLab ANTARES via `antares-client` (already pinned; the web venv may lack it, then the provider is *unavailable*) | none for public search | query alerts, fetch, cone search, photometry (ZTF and LSST rows), save |
| `fink` | Fink public REST API (`/api/v1/latests`, `/objects`, `/conesearch`, `/cutouts`) | none | all of the above plus cutouts |
| `alerce` | ALeRCE public REST API (`/ztf/v1/objects`, `/lightcurve`, `/probabilities`, avro stamps) | none | all of the above plus cutouts |

`python manage.py brokers` prints the table for this environment (`--json`,
`--all` to include providers switched off by `enabled`). A provider whose client
package is missing is listed as unavailable with the reason instead of erroring;
the page greys its actions out. Credentials, when a broker needs one later
(Lasair), come from an active `EncryptedCredential` with `service=<slug>` (#264).

`Query_LSST.AntaresLSST` (Rubin photometry refresh, #224) and its tests keep
working: the ANTARES client import and cone search moved to
`YSE_App/brokers/antares.py`; `Query_LSST` keeps the LSST parsing/storage and
delegates the search.

### Writing a provider

```python
from YSE_App.brokers import BrokerAlert, BrokerProvider, register
from YSE_App.brokers.base import QUERY_ALERTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT

@register
class LasairProvider(BrokerProvider):
    slug = "lasair"
    name = "Lasair"
    capabilities = (QUERY_ALERTS, CONE_SEARCH, PHOTOMETRY, SAVE_AS_TRANSIENT)
    requires_credential = True          # EncryptedCredential(service="lasair")
    query_keys = ("sherlock_classes", "days")

    def query_alerts(self, query=None, *, since_mjd=None, limit=100):
        token = self.credential["token"]
        ...
        return [BrokerAlert(broker=self.slug, object_id=..., ra=..., dec=..., mag=..., ...)]
```

Put the module in `[brokers] PROVIDER_MODULES` (or in
`registry.DEFAULT_PROVIDER_MODULES` for a shipped one). Only the methods for the
declared capabilities need implementing; the base class raises
`CapabilityNotSupported` for the rest. `get_photometry` returns the point dicts
`Query_LSST.parse_locus_lightcurve` produces (`make_point` builds one), with
`limit` set for non-detections; `points_to_upload_blocks` turns them into the
`/add_transient` photometry document.

## Filters

A `BrokerFilter` (admin: *Broker filters*, or `/api/brokerfilters/`) has:

- `broker`, `name`, `group` (members see its candidates; blank = everyone), `enabled`;
- `query`: what the poll asks the broker for, provider-specific keys, validated
  against the provider's `query_keys`:
  - `fink`: `classes` (Fink class labels, default `SN candidate`, `Early SN Ia candidate`), `days`, `n`;
  - `alerce`: `classifier` (`stamp_classifier`), `classes` (default `SN`), `probability_min`, `days`, `ndet_min`, `page_size`;
  - `antares`: `tags` (ANTARES tags / streams), `days`, `rb_min`, `survey`, `ra_min`, `ra_max`, `dec_min`, `dec_max`;
- `criteria`: cuts on the normalised alert, all of which must pass
  (`YSE_App.brokers.filters.CRITERIA`, also listed on the candidates page):

| key | meaning |
|---|---|
| `mag_min`, `mag_max` | latest magnitude range |
| `bands` | latest detection band in the list |
| `rb_min`, `drb_min` | real/bogus scores |
| `ndet_min`, `ndet_max` | number of detections |
| `gal_lat_min` | \|b\| in degrees (avoid the plane) |
| `dec_min`, `dec_max` | declination |
| `age_min_days`, `age_max_days` | days since the first detection |
| `positive_only` | positive difference flux |
| `sgscore_max`, `distpsnr1_min` | nearest-PS1-source star/galaxy score and distance |
| `classes`, `exclude_classes` | broker classification label |
| `class_probabilities` | `{label: min probability}`, any one suffices |

An unknown key or a wrong type is rejected at save time (admin, API and
`BrokerFilter.clean()`); a missing value fails its cut (an alert without an
`rb` does not pass `rb_min`).

- save policy: `auto_save` (promote at poll time), `save_status` (default
  `New`), `save_obs_group` (blank: the provider's, `ZTF` or `LSST`),
  `import_photometry`, `max_alerts` per poll.

Example (Fink, young bright SN candidates away from the plane):

```json
query:    {"classes": ["SN candidate", "Early SN Ia candidate"], "days": 2}
criteria: {"mag_max": 19.5, "drb_min": 0.9, "gal_lat_min": 10, "age_max_days": 5, "positive_only": true}
```

## Running a poll

```
python manage.py broker_poll --broker fink --dry-run      # evaluate, write nothing
python manage.py broker_poll --broker fink                # write candidates now
python manage.py broker_poll --broker fink --enqueue      # put a brokers.ingest job on the queue
```

On a schedule: `YSE_App.data_ingest.Broker_Ingest.BrokerPoll` is in
`CRON_CLASSES` and enqueues one `brokers.ingest` job per broker that has an
enabled filter, every `INGEST_CRON_MINUTES` (60), **only when**
`[brokers] INGEST_CRON_ENABLED = True` (or env `YSE_BROKER_INGEST_CRON=1`). The
job runs through the background queue (`RunQueuedJobs` / `run_jobs`,
docs/background-jobs.md), so the `runcrons` run is not held by broker calls. A
broker with a poll still queued or running is skipped. Each filter records
`last_run_at` / `last_run_summary` (`fetched N, passed N, new N, saved N,
errors N`, or `failed: ...`); one failing filter does not stop the others.

## Scanning candidates

`/candidates/` (login required; sidebar *Broker Candidates*) lists the
candidates of the user's groups' filters (and shared filters; staff see all),
newest first, with status / broker / filter / magnitude / real-bogus / name
filters and sorting. Each row shows position and galactic latitude, latest
detection, magnitude, class, the filters it passed, cutout links (science /
template / difference) when the provider has that capability, and:

- **Save**: choose the `TransientStatus`; a transient within
  `MATCH_RADIUS_ARCSEC` (2") of the position, or already carrying the broker
  name, is *linked* (the broker name becomes an `AlternateTransientNames`
  alias and the light curve is imported onto it); otherwise the provider's
  `save_as_transient` creates one through the existing `/add_transient` code
  path (`data_utils.add_transient_payload`), so duplicate detection, aliases
  and photometry behave exactly like a TNS or ZTF upload. The transient's
  `obs_group` is the provider's (`ZTF`, `LSST`) unless the filter sets one;
  `disc_date` is the first broker detection; `real_bogus_score` is kept. The
  broker light curve (detections, `ZTF-Cam` bands `g`/`r`/`i` or `LSSTCam`)
  is written as `TransientPhotometry` / `TransientPhotData`; a broker error at
  that step still creates the transient. It then appears on the dashboard in
  the chosen status.
- **Reject** (with an optional note) hides it from the *new* queue; a later
  poll updates the row but never re-opens it. **Re-open** undoes a rejection.

## API

- `GET /api/brokers/` (providers, capabilities, criteria keys), `GET /api/brokers/<slug>/`,
  `GET /api/brokers/<slug>/cone_search/?ra=&dec=&radius=` (arcsec; 503 when the
  provider is unavailable, 502 on a broker error);
- `/api/brokerfilters/` CRUD, group-scoped: a user manages filters of groups
  they belong to, only staff create shared (group-less) filters; validation
  errors are 400;
- `GET /api/candidates/?broker=&status=&alert_id=` and
  `POST /api/candidates/<id>/save/` (`status`, `obs_group`, `import_photometry`),
  `.../reject/` (`note`), `.../reopen/`; `GET /brokers/status.json` is the
  same provider listing for the page.

## Settings (`settings.ini`, `[brokers]`, all optional)

| key | default | meaning |
|---|---|---|
| `enabled` | all shipped | comma-separated provider slugs to offer |
| `PROVIDER_MODULES` | | extra modules registering providers |
| `INGEST_CRON_ENABLED` | `False` | `BrokerPoll` enqueues polls from `runcrons` |
| `INGEST_CRON_MINUTES` | `60` | poll interval |
| `HTTP_TIMEOUT_SECONDS` | `30` | REST timeout |
| `MATCH_RADIUS_ARCSEC` | `2.0` | link instead of create within this radius |
| `AUTO_SAVE_USERNAME` | `admin` | user stamped on auto-saved transients |
| `CANDIDATES_PAGE_SIZE` | `50` | rows per page |
| `FINK_API_URL`, `ALERCE_API_URL` | public endpoints | overrides |

## Deploying

1. `python manage.py migrate` (adds `YSE_App_brokerfilter`, `YSE_App_candidate`
   and the M2M table).
2. No new Python packages: Fink and ALeRCE use `requests`; ANTARES uses the
   pinned `antares-client` where the ingest venv has it.
3. Create one `BrokerFilter` per group in the admin (or via the API), test it
   with `manage.py broker_poll --broker <slug> --dry-run`, then set
   `[brokers] INGEST_CRON_ENABLED = True` so the existing `runcrons` crontab
   entry polls it. The queue runner (`RunQueuedJobs` cron or `run_jobs --loop`)
   must be running, as for notifications.

Tests: `YSE_App/tests/test_brokers.py` (providers with mocked HTTP / client,
evaluator, ingest, save/reject, page, API, commands; 42 tests) and the
unchanged `test_lsst_antares_ingest.py`. SkyPortal's `broker_plugins.md` /
`broker_ingestion.md` were the design reference; no SkyPortal code is used.
