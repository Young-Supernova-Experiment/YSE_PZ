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
5. **TNS check** (#889, #905) of each new transient: `osc_tns_check --tns-file $TNS_FILE`
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

**Instruments** (#894, #1003). Every telescope/instrument string pair in the catalogue gets an
alias row naming a YSE instrument, decided in this order:
1. the first matching rule in `catalog_import/osc_instruments.json`. These are Ryan's mappings
   (2026-11-16):
   - SDSS -> SDSS/SDSS; PS1 and Pan-STARRS1 -> the existing GPC1; Pan-STARRS2 -> GPC2;
   - Catalina Schmidt -> CSS/CSS; every Gaia string -> Gaia/Gaia;
   - kaitN -> KAIT/KAITN; P48 and p48 -> P48/P48; PTF48 (and P48 with CFH12k) -> P48/PTF;
   - PAIRITEL -> PAIRITEL/2MASS; nickelN -> Nickel/NickelN;
   - anything UVOT -> the existing UVOT; Hubble -> HST;
   - CfA3_KEP and FLWO/KeplerCam -> 1.2-m FLWO/KeplerCam; CfA4_KEP1 -> 1.2-m FLWO/KeplerCam1;
     CfA3_MINI -> 1.2-m FLWO/Minicam; CfA3_4SH -> 1.2-m FLWO/4Shooter (2026-11-25);
   - MDM 1.3m+2.4m+SITe -> MDM 1.3m+2.4m/SITe; Mt. Laguna 1m+Loral -> Mt. Laguna 1m/Loral (2026-11-25);
2. an existing YSE instrument of that name;
3. otherwise, automatically:
   - telescope and instrument given -> Telescope/Instrument;
   - one given -> X/X;
   - `Telescope - Instrument` in one field -> split.

Empty strings, `None` and `Other` stay on `Unknown`. `catalog_instruments --census` prints the
plan, most frequent strings first, with totals; with `--load --apply` it creates the missing
telescopes and instruments and the alias rows (commands: step 2.2 and 2.3 below). New telescopes
get latitude, longitude and elevation 0, as the TNS import does; fill them in by hand where they
matter. To change a mapping, edit the rules and run it again.

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
- Excluded entries (Ryan, 2026-11-26): entries whose name matches a pattern in
  `catalog_import/osc_exclude.json` are skipped before parsing, never matched or created, and counted
  as `excluded`. Today: test entries (`test...`, e.g. `test_25_20171001`) and the `mbr144.26.1005`
  style entries. Add a pattern there to drop more.
- Names with characters outside Latin-1 (en dashes, Greek letters, ...) are not used to look up an
  existing transient: YSE_PZ's name columns are latin1, so they cannot match, and MySQL rejects the
  query (`Illegal mix of collations`, error 1270). Matching then falls back on the other names and the
  position.
- A dry run records an entry whose preview fails as failed (stage `preview`) and goes on with the next
  one, as `--apply` does.

**Failures** (#895). With `--apply`, each entry's last download, parse and ingest outcome is in
`CatalogImportAttempt` (stage, status, attempts, error). Downloads are retried with backoff
(honouring `Retry-After`); each entry is one transaction, and a failed entry does not stop the run.
`--retry-failed` runs every failed entry again (re-downloading if needed); the run ends with the
failures by stage.

## Size

From a sample of the catalogue: SN 2011fe has 3,483 photometry points and 158 spectra
(380,000 pixels); SN 2013dy has 2.9 million pixels; SN 2017egm has 3 MOSFiT fits with 49,923
model points (not imported). Stored as arrays, a spectrum
is one row, so the full import adds tens of thousands of spectrum rows rather than about 10^8
pixel rows.

## Running it

This section is the only list of OSC commands; the Ziggy runbook points here. Commands are bash
and run on Ziggy. Every command prints the database it uses (`Using database NAME on HOST`).
Nothing is written without `--apply`, and `--apply` also needs `--database` repeating that
name (#904).

<a id="variables"></a>
### Variables

Declare these in every new shell before running any command below. Steps 1-5 use the test stack
(yse\_experimental, database `YSE_test`); step 6 changes the stack variables to production.

```bash
# Data: the OSC clones, the TNS file and every report. Pick a disk with at least 50 GB free.
export OSC_DIR=/data/osc
export REPORTS=$OSC_DIR/reports
export TNS_FILE=$OSC_DIR/tns_public_objects.csv.zip
export OSC_REPOS="sne-pre-1990 sne-1990-1999 sne-2000-2004 sne-2005-2009 sne-2010-2014 sne-2015-2019"

# Your YSE_PZ username: recorded as the creator of every row written.
export YSE_USER=<your YSE_PZ username>

# Stack: checkout, its Python, and its database (test stack for steps 1-5).
export APP=/data/yse_pz/YSE_PZ_experimental
export PY=$APP/venv/bin/python
export DB=YSE_test
cd $APP
```

Production (step 6 only), instead of the three stack lines above:

```bash
export APP=/data/yse_pz/YSE_PZ
export PY=/data/yse_pz/yse_virtual/bin/python
export DB=YSE
cd $APP
```

**Check the stack** (reads nothing but the settings):

```bash
$PY manage.py shell -c "from django.db import connection as c; print('Using database %s on %s' % (c.settings_dict['NAME'], c.settings_dict['HOST']))"
echo "DB=$DB"
```

**Expected:** `Using database <DB> on ...` with the same name as the `DB=` line (system-check
warnings printed before it are harmless). **If not:** stop; the variables point at the wrong
stack. Don't pipe these commands into `head`: closing the pipe early makes Django end with a
`BrokenPipeError` traceback.

Report files of steps 5 and 6 start with `$DB-`, so test and production reports never overwrite
each other.

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

- The code is on master and deployed to production (2026-11-25), and on yse\_test and
  yse\_experimental. The import tables (migration 0057) exist, empty.
- Production is backed up and has room for the new rows before step 6.

### Step 1: download and offline dry run (writes nothing to any database)

Status: the instrument census (1.6) ran on 2026-11-16: 55,482 entries, no failures. Ryan's
mappings went into the rules (#1003) after it, so that census is stale: it shows strings such as
`'CFHT' 'MegaCam' -> Unknown` that the current code maps (here to CFHT / MegaCam). Use the plan
from 2.2 instead. Whether 1.3-1.5 ran is not recorded.

**1.1 Disk space:**

```bash
df -h $(dirname $OSC_DIR)
```

**Expected:** at least 50G in the `Avail` column. **If not:** set `OSC_DIR` (and the variables
after it) to a directory on another disk.

**1.2 Download the OSC data** (30-90 minutes; it keeps running if the session drops):

```bash
mkdir -p $REPORTS && cd $OSC_DIR
nohup sh -c 'for r in $OSC_REPOS; do git clone --depth 1 https://github.com/astrocatalogs/$r.git; done' > $REPORTS/clone.log 2>&1 &
cd $APP
```

When `pgrep -f "git clone"` prints nothing, count the files:

```bash
for r in $OSC_REPOS; do echo "$r $(ls $OSC_DIR/$r | grep -c json)"; done
```

**Expected (about):**

| repository | files |
| --- | --- |
| `sne-pre-1990` | 5229 |
| `sne-1990-1999` | 2358 |
| `sne-2000-2004` | 1925 |
| `sne-2005-2009` | 7997 |
| `sne-2010-2014` | 10321 |
| `sne-2015-2019` | 27658 |

**If not:** send Ryan `$REPORTS/clone.log`.

**1.3 TNS public-objects file** (one request to TNS, with the TNS bot credentials of the stack):

```bash
$PY manage.py osc_tns_check --download-tns-file $TNS_FILE
```

**Expected:** `saved <TNS_FILE>; N objects`, N in the hundreds of thousands. Keep this file:
every TNS check, including production's, uses it.

**1.4 Offline dry run of every entry** (no database access; 1-3 hours):

```bash
nohup $PY manage.py import_osc $OSC_DIR/sne-* --offline --quiet --report $REPORTS/offline.jsonl > $REPORTS/offline.log 2>&1 &
```

**Expected, when `pgrep -fa import_osc` prints nothing:** the last line of
`$REPORTS/offline.log` reads `Dry run: 55xxx entries, 0 written (0 new transients), N failed, X excluded, ... s`.
N may be a few dozen: unreadable files are what this step finds.

**1.5 Summary of the offline dry run:**

```bash
$PY manage.py osc_report_summary $REPORTS/offline.jsonl --review-dir $REPORTS/offline-review > $REPORTS/offline-summary.txt
```

**Expected:** `offline-summary.txt` starts with `entries: 55xxx`; `offline-review/` holds four CSV
files.

**1.6 Instrument census** (reads the database only to name known instruments):

```bash
$PY manage.py catalog_instruments --census $OSC_DIR/sne-* --top 300 > $REPORTS/instrument-census.txt
```

**Expected:** `Using database YSE_test on ...`, then up to 300 lines of
`count kind telescope instrument -> target`.

**1.7 Send Ryan** `offline-summary.txt`, `instrument-census.txt` and `offline-review/failures.csv`
from `$REPORTS`. They hold only public catalogue data. Keep `$OSC_DIR`: every later step reads it.

### Step 2: instruments, then a read-only dry run against YSE_test

2.3 is the only command that writes (telescopes, instruments and catalogue alias rows in
`YSE_test`), and only after Ryan approves the plan from 2.2. Run 2.4 off-peak: it only reads,
but it is around a million queries, and load caused the 2026-10-21 outage.

**2.1 The deploy has the instrument rules (#1003):**

```bash
grep -c '"rules"' $APP/YSE_App/catalog_import/osc_instruments.json
grep -c 'def auto_target' $APP/YSE_App/catalog_import/instruments.py
```

**Expected:** `1` and `1`. **If either is `0`:** stop; the stack runs the code from before #1003,
whose plan sends unmapped strings (`CFHT` / `MegaCam`, ...) to `Unknown`. Deploy current code first.

**2.2 Instrument plan** (reads only; a few minutes):

```bash
$PY manage.py catalog_instruments --census $OSC_DIR/sne-* --top 0 > $REPORTS/$DB-instrument-plan.txt
tail -1 $REPORTS/$DB-instrument-plan.txt
```

**Expected:** `N strings: ... to existing instruments ..., ... to M new instruments ..., ... Unknown ...`.
**Send Ryan `$DB-instrument-plan.txt` and wait for his OK before 2.3.**

**2.3 Create the instruments** (writes; only after Ryan's OK):

```bash
$PY manage.py catalog_instruments --census $OSC_DIR/sne-* --load --apply --user $YSE_USER --database $DB > $REPORTS/$DB-instrument-load.txt
tail -1 $REPORTS/$DB-instrument-load.txt
```

**Expected:** `Created: aliases N, instruments created M, telescopes created K`, M as in 2.2.
Running it again creates nothing new. New telescopes get latitude, longitude and elevation 0; fill
them in by hand where they matter.

**2.4 Read-only dry run**, one repository at a time, in the background (likely a few hours):

```bash
nohup sh -c 'for r in $OSC_REPOS; do $PY manage.py import_osc $OSC_DIR/$r --quiet --report $REPORTS/dry-run-$r.jsonl > $REPORTS/dry-run-$r.log 2>&1; done' > /dev/null 2>&1 &
```

When `pgrep -fa import_osc` prints nothing:

```bash
tail -qn1 $REPORTS/dry-run-sne-*.log
```

**Expected:** six lines `Dry run: N entries, 0 written (0 new transients), F failed, X excluded, ... s`; the N
add up to 55,482.

**2.5 Summary for Ryan:**

```bash
cat $REPORTS/dry-run-sne-*.jsonl > $REPORTS/dry-run.jsonl
$PY manage.py osc_report_summary $REPORTS/dry-run.jsonl --review-dir $REPORTS/dry-run-review --sample 100 > $REPORTS/dry-run-summary.txt
```

**Send Ryan** `dry-run-summary.txt` and the four CSV files in `dry-run-review/`
(`sample_matches.csv`, `review_duplicates.csv`, `host_discrepancies.csv`, `failures.csv`). They hold
only public catalogue data and the names of matching YSE_PZ transients. Ryan reviews the sample of
matched entries, every `superseded_existing` and `review` case, the host discrepancies and the
instrument strings that resolve to Unknown. Anything the review finds is fixed before step 3.

### Step 3: pilot import on YSE_test, about 25 entries chosen by hand

The pilot covers every path. The first names below are fixed; add the rest from the step 2 review
files:

- rich historical SNe (SN 2011fe, 1987A, 1993J, 2013dy);
- X-ray and radio photometry (2006gy, 2014C);
- model data, which must be skipped (2017egm);
- objects YSE_PZ already has from TNS or its own data: a kept duplicate, a refereed
  `superseded_existing` and a `review` case (`review_duplicates.csv`);
- an object with no IAU name from 2013-2016 (TNS internal-name match);
- hosts nearer and farther than 20 Mpc (`host_discrepancies.csv`, `sample_matches.csv`);
- an entry with no position.

```bash
export PILOT="SN2011fe SN1987A SN1993J SN2013dy SN2006gy SN2014C SN2017egm <more entry names>"
for n in $PILOT; do ls $OSC_DIR/sne-*/$n.json* 2>/dev/null || echo "not found: $n" >&2; done > $REPORTS/pilot-files.txt
wc -l < $REPORTS/pilot-files.txt
```

**Expected:** one file per name and no `not found` lines.

**3.1 Dry run, then import:**

```bash
$PY manage.py import_osc $(cat $REPORTS/pilot-files.txt) --report $REPORTS/$DB-pilot-dry-run.jsonl
$PY manage.py import_osc $(cat $REPORTS/pilot-files.txt) --apply --user $YSE_USER --database $DB --report $REPORTS/$DB-pilot.jsonl
```

**Expected:** the last line reads `Imported: N entries, N written (K new transients), 0 failed, 0 excluded, ... s`.

**3.2 TNS check of the new transients** (dry run, then write):

```bash
$PY manage.py osc_tns_check --tns-file $TNS_FILE --max 0
$PY manage.py osc_tns_check --tns-file $TNS_FILE --max 0 --apply --database $DB
```

**3.3 Check on yse\_experimental:** transient pages, light curves, spectra, downloads, the API,
SQL Explorer and photometry statistics of the pilot transients. Then undo one entry and import
another again:

```bash
$PY manage.py import_osc --revert SN2011fe --apply --database $DB
$PY manage.py import_osc $(ls $OSC_DIR/sne-*/SN2011fe.json*) --apply --user $YSE_USER --database $DB
```

### Step 4: 1% trial on YSE_test, about 500 random entries

Measures time per entry, database growth and the failure and retry path, so the full run can be
estimated, and checks that the heaviest transient pages stay fast.

```bash
find $OSC_DIR/sne-* -path '*/.git' -prune -o \( -name '*.json' -o -name '*.json.gz' \) -print | shuf -n 500 > $REPORTS/trial-files.txt
nohup $PY manage.py import_osc $(cat $REPORTS/trial-files.txt) --apply --user $YSE_USER --database $DB --quiet --report $REPORTS/$DB-trial.jsonl > $REPORTS/$DB-trial.log 2>&1 &
```

When `pgrep -fa import_osc` prints nothing:

```bash
tail -2 $REPORTS/$DB-trial.log
$PY manage.py import_osc --retry-failed --apply --user $YSE_USER --database $DB --quiet --report $REPORTS/$DB-trial-retry.jsonl
$PY manage.py osc_tns_check --tns-file $TNS_FILE --max 0 --apply --database $DB
```

**Expected:** `Imported: 500 entries, ...` with the run time in seconds (× 110 estimates the full
run), and the retry ends with no failures. Send Ryan the two lines and the time.

### Step 5: full import on YSE_test (rehearsal)

One repository at a time, oldest first. Set `R` to each name in `$OSC_REPOS` in turn and wait for
each run to finish:

```bash
export R=sne-pre-1990
nohup $PY manage.py import_osc $OSC_DIR/$R --apply --user $YSE_USER --database $DB --quiet --report $REPORTS/$DB-import-$R.jsonl > $REPORTS/$DB-import-$R.log 2>&1 &
```

When `pgrep -fa import_osc` prints nothing:

```bash
tail -2 $REPORTS/$DB-import-$R.log
```

**Expected:** `Imported: N entries, ..., F failed, X excluded, ... s` (and the failures by stage if F > 0).
Look at a few transient pages, then go on with the next `R`.

After all six, retry until nothing fails, then the TNS check from the same file:

```bash
$PY manage.py import_osc --retry-failed --apply --user $YSE_USER --database $DB --quiet --report $REPORTS/$DB-retry.jsonl
$PY manage.py osc_tns_check --tns-file $TNS_FILE --max 0
$PY manage.py osc_tns_check --tns-file $TNS_FILE --max 0 --apply --database $DB
```

### Step 6: production

Rows are not copied from test: transient, instrument, user and group ids differ between the
databases, and duplicate matching depends on what each holds. Instead, the same importer runs on
the same local files with the same TNS file. It is deterministic and makes no outside requests.
Not a good idea: importing into a copy of production and swapping it in. Everything the crons
write between the copy and the swap would be lost.

**6.1** Declare the production stack variables ([Variables](#variables)) and check the stack.

**6.2 Prost host matching stays off** during the import and runs afterwards as its own step:

```bash
grep -i prost $APP/YSE_PZ/settings.ini; echo "env: ${YSE_PROST_HOST_MATCH:-unset}"
```

**Expected:** no `PROST_HOST_MATCH_ENABLED = True` line and `env: unset`.

**6.3 Backup** of the production database, confirmed before anything below writes.

**6.4 Instruments:** 2.1-2.3 with the production variables (Ryan approves the production plan too).

**6.5 Import**, one repository at a time, off-peak: the step 5 commands. After each repository, check
the counts and a few pages before the next.

**6.6 Retry and TNS check:** the last three step 5 commands.

## Other commands

Single entries from GitHub (cached in `--fetch-dir`, default `~/.cache/yse_osc`):

```bash
$PY manage.py import_osc --fetch SN2011fe SN1987A                                       # dry run
$PY manage.py import_osc --fetch SN2011fe --offline                                     # no database access
$PY manage.py import_osc --fetch SN2011fe --apply --user $YSE_USER --database $DB       # write
$PY -m YSE_App.catalog_import.osc $(ls $OSC_DIR/sne-*/SN2011fe.json*)                  # parse only, no Django
$PY manage.py import_osc --revert SN2011fe --apply --database $DB                       # undo one entry
```

`--fetch` looks entries up by their OSC file name (`SN2011fe`, `2011fe`, `ASASSN-14lp`), not by
alias. The index of the repositories (one GitHub API request each, about 55,000 names) is cached
in `--fetch-dir`; `--refresh-index` rebuilds it, `--boneyard` adds `sne-boneyard`. Set
`GITHUB_TOKEN` if the unauthenticated API limit (60 requests an hour) is hit.

Without `--tns-file`, `osc_tns_check` asks the TNS API one object at a time, one request a minute
(`OSC_TNS_REQUEST_INTERVAL_SECONDS`); use that only for stragglers (`--max 20`, or `--queue
--database $DB` for the background sweep job).

Other `import_osc` options: `--limit N`, `--no-spectra`, `--no-tns`, `--match-radius`,
`--phot-tolerance`, `--spec-tolerance`, `--status`.
