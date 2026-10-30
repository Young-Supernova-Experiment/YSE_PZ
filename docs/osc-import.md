# Open Supernova Catalog import

The Open Supernova Catalog (OSC, <https://github.com/astrocatalogs/supernovae>) holds about 55,000
entries: one JSON file per object, in repositories split by discovery year (`sne-pre-1990` ...
`sne-2015-2019`; `sne-boneyard` holds entries the OSC set aside, mostly not supernovae). The
format is the Open Astronomy Catalog schema, <https://github.com/astrocatalogs/schema>.

Code: `YSE_App/catalog_import/` (`osc.py` parser, `osc_writer.py` database writer,
`osc_fetch.py` file finder), `YSE_App/models/catalog_models.py`, `manage.py import_osc`.

## Where each part of an entry goes

The OSC keeps every value with its sources (several redshifts, types, host names, ...). YSE
keeps one value per field. So every OSC value is stored as given, and the importer also fills
the YSE fields from them.

| OSC | YSE |
| --- | --- |
| `sources` | `CatalogSource` (shared by all entries that cite it; matched on bibcode, DOI, arXiv id, else name and URL), linked to the entry with the entry's alias in `CatalogEntrySource` |
| the entry itself (`name`, `schema`, file it came from) | `CatalogEntry`, linked to the transient; top-level keys with no mapping go in `CatalogEntry.extra` |
| every sourced quantity (`alias`, `ra`, `dec`, `redshift`, `claimedtype`, `host*`, `max*`, `lumdist`, `ebv`, `errors`, ... and any future key) | `TransientCatalogQuantity`: one row per value, verbatim (`value`), parsed (`value_float`), with errors, unit, kind, flags, probability, correlations and sources |
| best position, discovery date, redshift, class, MW E(B-V), peak absolute magnitude and date | `Transient` fields (see "Rules") |
| `alias` | `AlternateTransientNames` (HTML anchors in OSC aliases removed) |
| `host`, `hostra`, `hostdec`, `hostredshift` | `Host`, linked when the transient has none |
| `photometry` (observations) | `TransientPhotometry` + `TransientPhotData`, one set per telescope, instrument, observatory, survey and source list; every OSC field our point table has no column for is in `TransientPhotDataCatalogInfo` (one row per point); the set's entry and sources in `TransientPhotometryCatalogInfo` |
| `spectra` | `TransientSpectrum` + one `TransientSpectrumArrays` row (wavelengths in Angstrom, #492); OSC fields with no column in `TransientSpectrumCatalogInfo` |
| `models` (MOSFiT fits) and rows citing a model | not imported yet (#892); counted in the report |

Any field the importer does not know goes into an `extra` JSON column next to where it was
found, so nothing in an entry is dropped.

## Rules (Ryan, 2026-10-27)

TNS is the authority for names, coordinates and spectroscopic classifications, unless a value was
changed by hand. An entry is imported in steps:

1. **Find the YSE_PZ transient**: the one the entry was imported to before; else one whose name
   is the entry name, an alias or its TNS-style form (`SN2011fe` -> `2011fe`), or that carries one
   as an alternate name; else one within `--match-radius` (2 arcsec).
2. **It exists**: its coordinates, classification, redshift, host and other fields do not change.
   Only data are added (and the OSC names as alternate names, for tracking).
3. **No duplicates** (`catalog_import/matching.py`, #888). Each OSC point is compared with the
   transient's points by origin: the same filter (`r'` = `r`, `W1` = `UVW1`), times within
   `--phot-tolerance` (0.02 d) and the same instrument. Spectra: times within `--spec-tolerance`
   (0.5 d), the same instrument, and wavelength ranges overlapping by half the shorter one. Values
   may differ (another reduction). When the OSC gives no instrument YSE knows, the match is
   *weak*, and a weak photometry match also needs magnitudes within 0.5 mag. Then:
   - match, OSC source not refereed: YSE_PZ's copy is kept, the OSC copy is not imported (it is kept
     in the `CatalogDataMatch` row);
   - match, OSC source refereed (the bibcode's journal is a refereed one, `CatalogSource.refereed`):
     the OSC copy is imported and YSE_PZ's copy is flagged REJECTED, so it is drawn as flagged and
     left out of statistics and fits. `--revert` or a re-import restores it;
   - weak match from a refereed paper: both kept, marked `review`.
4. **No transient**: a new one with name, position, discovery date, class, redshift, MW E(B-V) and
   peak from the OSC; status `--status` (default `Ignore`), observation group `OSC`.
5. **TNS check** (#889, #905) of each new transient: `manage.py osc_tns_check --tns-file FILE`
   matches it against TNS's bulk file of all public objects (`tns_public_objects.csv.zip`,
   downloaded once with `--download-tns-file`; no request per object). Lookup: its IAU-style names,
   then its names and alternate names among the TNS objects' internal (survey) names (PTF11kly,
   ASASSN-14lp, ...), then, for discoveries from 2013 on, its position (3 arcsec, a single hit). If
   found, the transient takes the TNS name (the old name becomes an alternate; if another transient
   already has it, nothing changes and the entry is marked `conflict`), the TNS position and
   `TNS_spec_class`, and the TNS class as `best_spec_class`. Each field changes only while it still
   has the value the import gave it. `CatalogEntry.tns_status` records the outcome. Keep the file:
   the production run uses the same one. Without `--tns-file` the check asks the TNS API one object
   at a time, one request a minute (`OSC_TNS_REQUEST_INTERVAL_SECONDS`), for stragglers only. TNS
   coordinates for transients YSE_PZ already had are left for a later clean-up.

**Host** (#893). The distance comes from the OSC luminosity distance, else cz/H0 (H0 = 70):
- beyond 20 Mpc, or with no listed distance, the OSC host is never linked; Prost/Blast decide (a Prost match is queued for new
  transients when `PROST_HOST_MATCH_ENABLED`);
- at 20 Mpc or closer a new transient gets the OSC host (an existing host within 3 arcsec, else a
  new one); for an existing transient a host more than 10 arcsec from the OSC's (or with another
  name) is flagged `discrepant` in `CatalogEntry.host_check`, never changed.

**Instruments** (#894). `catalog_import/osc_instruments.json` maps OSC telescope/instrument strings
(`p48`, `PTF48`, `1m0-08`, `Lick 3m - KAST`, ...) to YSE telescopes and instruments, with date
ranges where one string means different cameras (P48: CFH12k before MJD 58194, ZTF-Cam after).
`manage.py catalog_instruments --load --apply --user <USERNAME>` creates what is missing (new
telescopes get latitude, longitude and elevation 0, as the TNS import does; fill them in by hand
where they matter) and the alias rows; `--census <OSC_DIR>` lists the strings in the catalogue by
frequency with what each resolves to, to extend the file. Unmapped strings go to `Unknown`.

**Other rules.**
- Peak: the visual-band peak (`maxvisual*`) when there is one.
- Photometry: limits keep their magnitude but no `mag_err` (kept in the info row); optical flux
  densities or count rates with a zero point fill `flux` / `flux_err` / `flux_zero_point`; X-ray,
  radio and luminosity values are in the info row with bands named by energy or frequency;
  `includeshost` sets NOT_DIFFERENCED.
- Spectra are stored as arrays (`TransientSpectrumArrays`, #492), not one row per pixel.
  `deredshifted`, `dereddened`, `Uncalibrated` fluxes and `includeshost` set quality bits; a
  spectrum with no time gets the discovery (else peak) date, flagged `time_missing`.
- Model data (#892) are not imported yet (Ryan, 2026-10-28; how to store several models and
  code versions is still open): the `models` list and every photometry row, spectrum and quantity
  that cites a model are skipped and only counted in the report.
- Access: OSC data are published (Public group, `OSC` observation group).
- Historical import: no automatic TNS publishing, minor-planet screen or annotation runs; no
  new-observation signals for spectra; photometry statistics recomputed once per transient.
- Dates before year 1000 stay in the quantities only (MySQL `DATETIME`).

**Failures** (#895). With `--apply`, each entry's last download, parse and ingest outcome is in
`CatalogImportAttempt` (stage, status, attempts, error). Downloads are retried with backoff
(honouring `Retry-After`); each entry is one transaction, and a failed entry does not stop the run.
`--retry-failed` runs every failed entry again (re-downloading if needed); the run ends with the
failures by stage.

## Running it

Dry run (the default) parses each entry and, with a database, says which transient it would go
to, what it would do with duplicates, what the host rule says and which instruments its data would
use. Nothing is written. Every command prints the database it uses; `--apply` also needs
`--database <DB_NAME>` naming that database (#904).

```bash
python3 manage.py import_osc --fetch SN2011fe SN1987A            # download from GitHub, dry run
python3 manage.py import_osc --fetch SN2011fe --offline          # no database access at all
python3 -m YSE_App.catalog_import.osc path/to/SN2011fe.json      # same, without Django
```

`--fetch` looks entries up by their OSC file name (`SN2011fe`, `2011fe`, `ASASSN-14lp`), not by
alias. The index of the repositories (one GitHub API request each, about 55,000 names) is cached
in `--fetch-dir` (default `~/.cache/yse_osc`); `--refresh-index` rebuilds it, `--boneyard` adds
`sne-boneyard`. Set `GITHUB_TOKEN` if the unauthenticated API limit (60 requests an hour) is hit.

Write:

```bash
python3 manage.py import_osc --fetch SN2011fe --apply --user <USERNAME> --database <DB_NAME>
```

The whole catalogue, from local clones (about 12 GB):

```bash
for r in sne-pre-1990 sne-1990-1999 sne-2000-2004 sne-2005-2009 sne-2010-2014 sne-2015-2019; do
  git clone --depth 1 https://github.com/astrocatalogs/$r.git <OSC_DIR>/$r
done
python3 manage.py import_osc <OSC_DIR>/sne-* --quiet --report osc-dry-run.jsonl
python3 manage.py osc_report_summary osc-dry-run.jsonl --review-dir osc-review     # #906
python3 manage.py import_osc <OSC_DIR>/sne-* --apply --user <USERNAME> --database <DB_NAME> --quiet --report osc-import.jsonl
```

Then:

```bash
python3 manage.py import_osc --retry-failed --apply --user <USERNAME> --database <DB_NAME>   # until nothing fails
python3 manage.py osc_tns_check --download-tns-file <TNS_FILE>                                # once; keep it
python3 manage.py osc_tns_check --tns-file <TNS_FILE> --max 0                                 # dry run
python3 manage.py osc_tns_check --tns-file <TNS_FILE> --max 0 --apply --database <DB_NAME>
python3 manage.py import_osc --revert SN2011fe --apply --database <DB_NAME>                   # undo one entry
```

Other options: `--limit N`, `--no-spectra`, `--no-tns`, `--match-radius`,
`--phot-tolerance`, `--spec-tolerance`, `--status`. Run `catalog_instruments --load --apply` before
the first import so instruments resolve.

## Size

From a sample of the catalogue: SN 2011fe has 3,483 photometry points and 158 spectra
(380,000 pixels); SN 2013dy has 2.9 million pixels; SN 2017egm has 3 MOSFiT fits with 49,923
model points (not imported). Stored as arrays, a spectrum
is one row, so the full import adds tens of thousands of spectrum rows rather than about 10^8
pixel rows.

## Plan for the full import (Ryan, 2026-10-29)

The order matters more than speed: every step is checked before the next one writes anything.

Facts that shape it:

- The test database (`YSE_test`, shared by yse\_test and yse\_experimental) is a copy of
  production from 2026-10-06, after any OSC data. So matching on test predicts matching on
  production, and a full import on test is a faithful rehearsal.
- The OSC data are about 55,500 entries in six repositories, about 13 GB as git data. Cloning them
  needs a machine with room for that plus the unpacked files (Ziggy, or a disk with at least 50 GB
  free).
- Spectra are written as arrays (#492). Converting the existing spectra
  (`convert_spectra_to_arrays`) is not needed for the import and can wait.
- Model data are not imported yet (#892).

### Step 0: before any data

1. Merge #896 (this code) into experimental; the import tables then exist, empty.
2. Check which database each stack uses (`import_osc` prints it). Every `--apply` names it
   (`--database`, #904).
3. Download TNS's public objects file once (`osc_tns_check --download-tns-file`, #905) and keep it
   for the whole project.
4. Back up production and check disk space for the new rows.

### Step 1: offline, no database

- Clone the six repositories once (see "Running it"). Every later step reads these files; nothing
  is downloaded again.
- Offline dry run of everything: `import_osc <OSC_DIR>/sne-* --offline --quiet --report offline.jsonl`,
  then `osc_report_summary offline.jsonl`. Gives parse failures, warnings, entries with no
  position, and exact row and pixel counts.
- `catalog_instruments --census <OSC_DIR>/sne-*`. Extend `osc_instruments.json` with the frequent
  unmapped strings (a code change, reviewed like any other).

### Step 2: read-only dry run against the database

- `import_osc <OSC_DIR>/sne-* --quiet --report dry-run.jsonl` against `YSE_test`, off-peak, one
  repository at a time. It only reads, but it is around a million queries, and load caused the
  2026-10-21 outage.
- `osc_report_summary dry-run.jsonl --review-dir review`. Ryan reviews: the sample of matched
  entries, every `superseded_existing` and `review` case, host discrepancies, and instrument strings
  that resolve to Unknown.
- Fix anything the review finds before step 3.

### Step 3: pilot import on YSE_test, about 25 entries chosen by hand

- `catalog_instruments --load --apply` first.
- Entries covering every path:
  - rich historical SNe (SN 2011fe, 1987A, 1993J, 2013dy);
  - X-ray and radio photometry (2006gy, 2014C);
  - model data, which must be skipped (2017egm);
  - objects YSE_PZ already has from TNS or its own data (duplicates and refereed superseding);
  - an object with no IAU name from 2013-2016 (TNS internal-name match);
  - hosts nearer and farther than 20 Mpc;
  - an entry with no position.
- Then `osc_tns_check --tns-file` for the new transients.
- Check on yse\_experimental: transient pages, light curves, spectra, downloads, the API, SQL
  Explorer, photometry statistics, `--revert` on one entry and a re-import of another.

### Step 4: 1% trial on YSE_test, about 500 random entries

Measures time per entry, database growth and the failure and retry path, so the full run can be
estimated, and checks that the heaviest transient pages stay fast.

### Step 5: full import on YSE_test (rehearsal)

- One repository at a time, oldest first, `--report` for each, `--retry-failed` until nothing
  fails, then the TNS check from the same file.
- yse\_test runs `develop` code on this database; it cannot read array-stored spectra until #492
  is promoted.

### Step 6: production

- Rows are not copied from test: transient, instrument, user and group ids differ between the
  databases, and duplicate matching depends on what each holds.
- Instead, the same importer runs on the same local files with the same TNS file. It is
  deterministic and makes no outside requests. Prost host matching stays off during the import and
  runs afterwards as its own step.
- Order: backup, instruments, one repository at a time off-peak (check counts and a few pages after
  each), then the TNS check.
- Not a good idea: importing into a copy of production and swapping it in. Everything the crons
  write between the copy and the swap would be lost.

