# Structured annotations and catalogue checks

SkyPortal-parity umbrella #316 (#317 model / API / detail panel / legacy shim, #318 Gaia DR3, WISE
and quasar-catalogue checks with rerun, #319 search filters and a results column by annotation key).
An **annotation** is one flat key/value document about a transient from one **origin**: a catalogue
cross-match (`gaia_dr3`, `wise`, `quasar`), a broker score (`antares`), a pipeline output, or a
user's own notes (`user:<username>`). Annotations replace the habit of adding a column to
`Transient` for every new score; the existing columns are exposed through the same read path as a
read-only `legacy` origin.

The design follows SkyPortal's annotations and annotation services (BSD-3-Clause) in spirit: an
origin, a JSON `data` document, a group audience, unique per object and origin, buttons that run a
catalogue check and store the result as an annotation. No SkyPortal code is copied.

## Concepts

| term | meaning |
|---|---|
| `TransientAnnotation` (`YSE_App/models/annotation_models.py`) | `transient`, `origin` (64 chars), `data` (flat JSON object stored as text), audience `groups` (empty = every logged-in user), `service` / `run` (the `ExternalService` and `ExternalServiceRun` that last wrote it), audit fields. Unique per `(transient, origin)`; `auditlog` tracks changes. |
| `TransientAnnotationValue` | the indexed side table: one row per key of every annotation with the value as text and, when it reads as a number or boolean, as a float. Rebuilt on every write through the service; this is what the search filters and the results column use (issue #319's "materialised side table" option, chosen because the schema stores JSON as `TEXT` and the production MySQL version is not pinned). |
| verdict | services write `verdict` (`stellar`, `AGN-like`, `clean`, `unknown`) and a one-line `summary` next to the catalogue values; `stellar` and `AGN-like` earn a badge on the Summary tab. |
| origin rules | staff and superusers write any origin; other users only `user:<their username>`; `legacy` is reserved. |
| annotation service | an `ExternalService` of kind `annotation` (#342). Its slug picks the runner; a run is an `ExternalServiceRun` with `request_payload = {"annotation": true, "ra", "dec", "radius_arcsec"}` whose result is the written document. |

## Writing annotations from code

```python
from YSE_App.services.annotations import upsert

annotation, created = upsert(transient, "antares", {"score": 0.91, "label": "SN"}, user=request.user)
upsert(transient, "antares", {"score": 0.5}, user=bot_user, merge=True)      # update keys, keep the rest
upsert(transient, "private_pipeline", {...}, user=me, groups=[yse_group])   # audience
```

`upsert` replaces the document (or merges with `merge=True`), keeps `created_by`, sets
`modified_by`, records `run` / `service` when given, and rebuilds the value rows in the same
transaction. Ingest code (`Query_LSST`, brokers) writes `origin='antares'` with this one call.
`visible_annotations(transient, user)` and `annotations_for_transients(transients, user)` read with
the group audience applied; `legacy_annotation(transient)` returns the read-only `legacy` entry.

## The Annotations tab

The transient detail page has an **Annotations** tab (fragment
`transient_detail/<id>/annotations_fragment/`, loaded when the tab opens). It lists every visible
annotation grouped by origin: the service name, the verdict badge, the one-line summary, the
key/value pairs, an expandable JSON view, who wrote it and when, plus **Rerun** (when the user may
run that service) and delete (staff, or the owner of a `user:` origin). The `legacy` row shows the
non-null `Transient` columns (`point_source_probability`, `real_bogus_score`, `mw_ebv`,
`antares_classification`, `alt_status`, `has_hst` / `has_spitzer` / `has_chandra`, `TNS_spec_class`).

The right-hand card lists the registered checks with the time of their last run (or a **failed**
badge with the error) and a **Check** / **Rerun** button. Buttons are shown to staff and superusers,
and to members of a group named on the service's `groups` (the admin's audience field; empty means
"everyone may see the service, only staff may run it"). A running check shows a spinner and the
tab polls the fragment every few seconds until it finishes. A second click while one is running
answers 409.

`transient_detail/<id>/annotations_summary.json` returns `{count, badges, active}`; the page uses it
to label the tab "Annotations (n)" and to draw the `stellar` / `AGN-like` badges next to the status
on the Summary tab (click one to open the tab). It costs no extra query on the detail page render.

## The built-in checks (#318)

Register the three services once (they are ordinary `ExternalService` rows afterwards: rename,
disable, cap or restrict them in the admin):

```bash
python manage.py register_annotation_services            # gaia_dr3, wise, quasar; enabled
python manage.py register_annotation_services --disabled
```

| slug | catalogue | stored keys | verdict |
|---|---|---|---|
| `gaia_dr3` | `gaiadr3.gaia_source` through ESA's TAP `sync` endpoint (the service `astroquery.gaia` wraps) | `source_id`, `separation_arcsec`, `parallax`, `parallax_error`, `parallax_over_error`, `pmra`, `pmdec`, `pm`, `pm_over_error`, `phot_g_mean_mag`, `bp_rp`, `ruwe`, `distance_pc` (when the parallax is significant), `n_matches` | `stellar` when the parallax or the total proper motion is significant at >= 3 sigma; `unknown` for a match without one; `clean` for no source in the cone |
| `wise` | AllWISE (`II/328/allwise`), falling back to CatWISE2020 (`II/365/catwise`) through VizieR TAP | `designation`, `separation_arcsec`, `w1`, `w2`, `w3`, `w4` and errors, `w1_w2`, `w2_w3`, `ccf`, `ext_flag`, `catalog`, `n_matches` | `AGN-like` when `W1 - W2 >= 0.8` (Stern et al. 2012); `clean` otherwise and for no match |
| `quasar` | Million Quasar Catalog v8 (`VII/294/catalog`) through VizieR TAP; it merges SDSS DR16Q, LAMOST, 2QZ ... so the SDSS QSO flag is covered | `name`, `type`, `type_label`, `separation_arcsec`, `redshift`, `rmag`, `bmag`, `qpct`, `comment`, `xray_name`, `radio_name`, `n_matches` | `AGN-like` for any match, `clean` for none |

Every document also carries `verdict`, `summary` and `radius_arcsec`. Runs go through the job
queue (#340): the detail page or the API creates a pending `ExternalServiceRun`, `run_jobs` (or the
cron pass) executes it, the runner (`YSE_App/annotation_services/`) queries the catalogue, writes
the annotation and completes the run. A catalogue that cannot be reached (HTTP error, timeout, a
VOTable error) marks the run **failed** with the message, shown on the tab; the previous annotation
is kept. A rerun overwrites the same row in place (`created_by` stays, `modified_by` / `modified_date`
move, the value rows are rebuilt). Runs left pending for `ANNOTATION_RUN_STALE_MINUTES` (a worker
that died) are marked failed the next time the tab loads.

Adding a check: write `check(ra_deg, dec_deg, radius_arcsec) -> (data, verdict, summary)` and call
`YSE_App.annotation_services.register_check("my_slug", check)` from a module named in
`JOB_HANDLER_MODULES`; register an `ExternalService` with that slug and kind `annotation`.

### Settings (`settings.ini`, all optional)

| key | default | meaning |
|---|---|---|
| `ANNOTATION_HTTP_TIMEOUT_SECONDS` | 30 | TAP request timeout |
| `ANNOTATION_SEARCH_RADIUS_ARCSEC` | 3.0 | cone radius of the checks |
| `ANNOTATION_RUN_STALE_MINUTES` | 60 | pending / running runs older than this are failed |
| `ANNOTATION_AUTORUN_SERVICES` | empty | comma-separated slugs queued for every newly created transient (`post_save`), e.g. `gaia_dr3,wise,quasar`; off by default |
| `GAIA_TAP_URL` | `https://gea.esac.esa.int/tap-server/tap/sync` | override the Gaia TAP endpoint |
| `VIZIER_TAP_URL` | `https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync` | override the VizieR TAP endpoint |

The web host needs outbound HTTPS to those two hosts; the checks use `requests` only (no
`astroquery` import at run time).

## API (`/api/transientannotations/`)

| call | meaning |
|---|---|
| `GET /api/transientannotations/?transient=<id or name>` | the transient's visible annotations plus the read-only `legacy` entry (`legacy=0` leaves it out) |
| `GET ...?origin=gaia_dr3`, `?key=parallax`, `?verdict=stellar` | filters |
| `POST /api/transientannotations/` `{"transient": "2026abc", "origin": "antares", "data": {...}, "groups": ["YSE"], "merge": false}` | upsert: 201 when created, 200 when replaced. Staff any origin; other users only their `user:<name>` origin (the default when `origin` is left out) |
| `PATCH /api/transientannotations/<id>/` `{"data": {...}}` | merge keys into the document (`PUT` replaces) |
| `DELETE /api/transientannotations/<id>/` | staff, or the owner of a `user:` origin |

Non-staff callers see annotations only on transients whose photometry or spectra they may access,
and only those whose `groups` are empty or shared with them.

## Search filters and column (#319)

`/search/` (and `/api/transients/`, which shares the `TransientSearchFilterSet`) has an
**Annotations** group:

| parameter | meaning |
|---|---|
| `annotation_origin` | has an annotation from this origin (`legacy` reads the `Transient` columns) |
| `annotation_key` | has this key (any origin unless `annotation_origin` is set) |
| `annotation_value_eq` | the key's value equals this text or number |
| `annotation_value_min`, `annotation_value_max` | numeric range on the key's value |
| `annotation_column` | `origin.key` (or `key`) shown as an extra results column, sortable (`ordering=annotation_value`); when left out the filtered `origin.key` is shown |

The acceptance case of #319, `?annotation_origin=gaia_dr3&annotation_key=parallax_over_error&annotation_value_min=3`,
is one indexed `EXISTS` on `TransientAnnotationValue (origin, key, value_num)`; the column is a
correlated subquery, so a results page stays one query. A value filter without `annotation_key` is
a form error. `legacy.<column>` filters go straight to the `Transient` column (numeric:
`point_source_probability`, `real_bogus_score`, `mw_ebv`, `has_*`).

## Tests

`YSE_App/tests/test_annotations.py`: upsert semantics and value coercion, group visibility, the
legacy shim, the three checks against recorded TAP answers (including the CatWISE fallback and TAP
errors), the job-queue path, rerun in place, autorun, the management command, the tab fragment and
its actions and permissions, the summary JSON, the DRF endpoint, and the filters / column on the
page and the API. The module runs on sqlite and MySQL.
