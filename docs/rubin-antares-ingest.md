# Rubin / LSST photometry from ANTARES

`YSE_App/data_ingest/Query_LSST.py` (`AntaresLSST`, issue #224) is the Rubin
twin of `Query_ZTF.AntaresZTF`. Once an hour it takes the transients YSE is
actively working on, runs an ANTARES cone search around each one, keeps the LSST
alerts on the matched loci and stores them as ordinary
`TransientPhotometry` / `TransientPhotData` rows. The detail-page light curve,
`recent_mag` and the scheduling tables then show Rubin points with no further
changes.

## What it stores

| YSE row | Value |
|---|---|
| `Observatory` | `Cerro Pachón` (UTC-4, `America/Santiago`) |
| `Telescope` | `Rubin Observatory / Simonyi Survey Telescope` (-30.2446, -70.7494, 2663 m) |
| `Instrument` | `LSSTCam` |
| `PhotometricBand` | `u g r i z y` on `LSSTCam` (colours from `filter_display.FILTER_COLORS`, glyph `plus`) |
| `ObservationGroup` | `LSST` |
| `TransientPhotometry` | one per transient, `reference = "ANTARES <locus_id>"`, in the `Public` collaboration group |
| `TransientPhotData` | `mag`/`mag_err` (AB), `flux`/`flux_err` at zero point 27.5 (as the ZTF ingest), `obs_date` from `ant_mjd`, `diffim = True`, `forced = False`, `discovery_point = False`; points that fail the alert quality flags carry the `Bad` `DataQuality` flag |

All of these are `get_or_create`d at the start of every run, so nothing has to be
seeded by hand. Points are de-duplicated per (instrument, band) on `obs_date`:
two points closer than `lsst_mjd_match_min` days (default 0.0005 d = 43 s, well
under Rubin's ~30 s exposure spacing, so two visits of one night in the same
band are kept apart) are the same visit.

## ANTARES field mapping

Verified against the `antares-client` repository's own LSST API fixtures
(`test/data/api_responses/lsst-loci-ANT2025uns34defs98p*.json`; the ANTARES
web docs were not reachable from the build environment):

- `locus.lightcurve` columns: `time, alert_id, ant_mjd, ant_survey, ant_ra,
  ant_dec, ant_passband, ant_mag, ant_magerr, ant_maglim`.
  `ant_survey` is `1` (ZTF candidate), `2` (ZTF upper limit), `4` (LSST alert,
  `alert_id` prefix `lsst:`). Only rows with `ant_survey == lsst_survey_id` and a
  finite `ant_mag` are ingested; upper limits are skipped like the ZTF path does.
- `locus.properties["survey"]["lsst"]["dia_object_id"]` marks an LSST locus.
- `alert.properties` (LSST alert schema, prefix `lsst_diaSource_`):
  `pixelFlags`, `isNegative`, `reliability` decide the `Bad` flag.
- `cone_search` in `antares-client` 1.2.0 (the pin in `requirements.txt`) and
  1.14.0 build the same `sky_distance` query, so the pinned client works.

Every name is a module constant or a `settings.ini` key, so a broker-side rename
is a config change.

## Configuration (`settings.ini`, `[antares]`, all optional)

| Key | Default | Meaning |
|---|---|---|
| `lsst_survey_id` | `4` | `ant_survey` value of LSST alerts |
| `lsst_cone_radius_arcsec` | `2.0` | cone-search radius around each transient |
| `lsst_max_days` | `30` | poll transients created / discovered / modified this recently |
| `lsst_statuses` | `New,Following,Watch,FollowupRequested,Interesting` | statuses polled (plus anything with an open `TransientFollowup` window) |
| `lsst_max_transients` | `500` | per run, most recently modified first |
| `lsst_max_dec` | `32.0` | skip transients north of this declination |
| `lsst_mjd_match_min` | `0.0005` | dedupe window in days |
| `lsst_min_reliability` | `0.0` | `0` = off; lower `reliability` gets the `Bad` flag |
| `lsst_use_alert_flags` | `True` | fetch per-alert flags (one extra API call per locus) |

## Running it

```
python manage.py runcrons YSE_App.data_ingest.Query_LSST.AntaresLSST --force
```

The `antares_client` import is guarded: a venv without the package imports the
module and `runcrons` fine; the cron logs
`antares_client is not installed in this environment; nothing to do` and exits.
On Ziggy the cron venv needs `antares-client` (already pinned in
`requirements.txt`, which `requirements-ingest.txt` includes). Failures inside a
run are printed and, when `[SMTP_provider]` / `[main] dbemail` are set, emailed
like the ZTF cron; one transient's broker error never stops the run.

`getLSSTPhotometry_ANTARES(ra, dec)` returns the same `/add_transient`-shaped
block as `QUB_data.getZTFPhotometry_ANTARES`, for callers that upload through the
API (for example a future TNS-ingest hook).

Tests: `YSE_App/tests/test_lsst_antares_ingest.py` (client mocked with the
fixture field names above).
