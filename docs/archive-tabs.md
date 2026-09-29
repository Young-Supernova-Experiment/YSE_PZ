# Archive tabs on the transient detail page (HST, JWST, Chandra)

The detail page has one tab per archive that is searched at the transient position when the
page is opened: **HST Data**, **JWST Data** (#328) and **Chandra Data**. Each tab works the same
way, so a MAST outage looks the same everywhere and is never mistaken for "no data".

## What the user sees

| tab label | meaning |
|---|---|
| `HST Data`, `JWST Data (2)` | the archive answered and has data (JWST shows the count) |
| `No HST`, `No JWST`, `No Chandra` | the archive answered that there is nothing at this position |
| `HST (lookup failed)`, `JWST (lookup failed)` | the archive timed out or the query raised; the tooltip says why and opening the tab retries |

Opening a tab loads its body; while MAST answers the body shows a spinner. On a failure the body
shows the message with a **Retry** link. The JWST body lists one row per observation: date
(UT, tooltip MJD), instrument, filter / grating, program id and PI, target name, exposure time,
data product type and calibration level, a preview thumbnail when MAST has one, and links to the
observation in the MAST Portal, to the JWST program page and to the data product.

## Endpoints

| endpoint | purpose | answer |
|---|---|---|
| `get_hst_status/<id>`, `get_jwst_status/<id>`, `get_chandra_status/<id>` | tab label on page load | `{"has_data": true/false, "count": N}`; on a failure `{"has_data": null, "count": 0, "error": "timeout"\|"lookup_failed", "message": ...}` |
| `get_hst_image/<id>` | HST tab body | `{jpegurl, fitsurl, obsdate, filters, inst}` (parallel lists) |
| `get_jwst_observations/<id>` | JWST tab body | `{"count": N, "rows": [{obs_id, inst, filters, obsdate, mjd, exptime, program, pi, target, product, calib_level, previewurl, dataurl, portalurl}]}` |
| `get_chandra_image/<id>` | Chandra tab body | `{jpegurl, fitsurl, obsdate, exptime, totalexp}` |

The JWST endpoints require a login; an unknown transient is a 404. A body lookup that fails answers
HTTP 502 (`error: lookup_failed`) and one that times out answers 504 (`error: timeout`), both with
`message` and the empty payload shape, so the page can show the failure and offer Retry.

## How the lookups run (`YSE_App/view_utils.py`)

- `_archive_status_with_timeout(work, timeout_seconds)` runs the blocking archive query in a
  worker thread and returns `None` when it does not finish in time; the worker is abandoned
  (`shutdown(wait=False)`) so the response really comes back at the timeout.
- `_archive_status_payload(cache_key, lookup, archive_name)` builds the label payload for every
  archive: answers are cached for `ARCHIVE_STATUS_CACHE_SECONDS` (1 h), timeouts and errors for
  `ARCHIVE_STATUS_FAILURE_CACHE_SECONDS` (60 s) so a reload retries soon. Timeout:
  `YSE_ARCHIVE_STATUS_TIMEOUT` (default 8 s). Cache keys: `hst_status_v3_<id>`,
  `jwst_status_v1_<id>`, `chandra_status_v3_<id>`.
- `_archive_table_response(transient_id, archive_name, lookup, empty_payload, cache_key)` does
  the same for the tab bodies (HST and JWST) with the longer `YSE_ARCHIVE_TABLE_TIMEOUT`
  (default 45 s) and caches successful JWST answers for an hour (`jwst_observations_v1_<id>`).

## MAST queries (`YSE_App/common/mast_query.py`)

- `hstImages(ra, dec, obj)` is the original HST lookup: `Observations.query_region` within
  1 arcsec, then instrument / filter / exposure-time masks and HLA cut-out URLs.
- `MastObservations(ra, dec, collections, radius=1 arcsec, product_types=('image', 'spectrum'),
  intent_type='science')` is the archive-agnostic form used for JWST (`jwstObservations(ra, dec)`
  sets `collections=['JWST']`): one `Observations.query_criteria` cone search, rows returned as
  plain dicts (`rows_from_table`) with `obsdate` (ISO from `t_min`), `previewurl` / `dataurl`
  (`mast:` product URIs turned into `https://mast.stsci.edu/api/v0.1/Download/file?uri=...`
  links) and `portalurl` (a MAST Portal deep link filtered on `obs_id`). Masked cells are `None`.
  The class does not catch exceptions; the views' timeout/error handling does.

Tests: `YSE_App/tests/test_archive_status_views.py` (HST/Chandra) and
`YSE_App/tests/test_jwst_tab.py` (helper with a recorded table, status and body endpoints, cache
TTLs, timeout/error answers, both page modes). MAST is never contacted in the test suite.
