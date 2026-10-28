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
| `spectra` | `TransientSpectrum` + `TransientSpecData` (wavelengths in Angstrom); OSC fields with no column in `TransientSpectrumCatalogInfo` |
| `models` (MOSFiT fits) | `CatalogModelFit`; each realization's parameters and model light curve (photometry rows with `model`) in `CatalogModelRealization` |

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
5. **TNS check** (#889) of each new transient, by `manage.py osc_tns_check` or the `osc.tns_check`
   job queued after an `--apply` run. It looks the object up by its IAU-style names, then for
   discoveries from 2013 on by a 3-arcsec cone search (a single hit only). If found, the transient
   takes the TNS name (the old name becomes an alternate; if another transient already has it,
   nothing changes and the entry is marked `conflict`), the TNS position and `TNS_spec_class`, and
   the TNS class as `best_spec_class`. Each field changes only while it still has the value the
   import gave it. Requests are throttled to one per `OSC_TNS_REQUEST_INTERVAL_SECONDS` (10 s);
   rate limits re-queue the job. `CatalogEntry.tns_status` records the outcome.

**Host** (#893). The distance comes from the OSC luminosity distance, else cz/H0 (H0 = 70):
- beyond 20 Mpc the OSC host is never linked; Prost/Blast decide (a Prost match is queued for new
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
- Models (#892): one `CatalogModelFit` per fit with its outputs (per-parameter median and
  16th/84th percentiles, and the model light curve as median and 1-sigma range per band and time).
  The realizations (MCMC chains) are not stored.
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
to and which instruments its data would use. Nothing is written.

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
python3 manage.py import_osc --fetch SN2011fe --apply --user <USERNAME>
```

The whole catalogue, from local clones (about 12 GB):

```bash
for r in sne-pre-1990 sne-1990-1999 sne-2000-2004 sne-2005-2009 sne-2010-2014 sne-2015-2019; do
  git clone --depth 1 https://github.com/astrocatalogs/$r.git <OSC_DIR>/$r
done
python3 manage.py import_osc <OSC_DIR>/sne-* --quiet --report osc-dry-run.jsonl
python3 manage.py import_osc <OSC_DIR>/sne-* --apply --user <USERNAME> --quiet --report osc-import.jsonl
```

Then:

```bash
python3 manage.py import_osc --retry-failed --apply --user <USERNAME>   # until nothing fails
python3 manage.py osc_tns_check --max 20                                  # dry run (queries TNS)
python3 manage.py osc_tns_check --queue                                   # or let the job runner do it
python3 manage.py import_osc --revert SN2011fe --apply                    # undo one entry
```

Other options: `--limit N`, `--no-spectra`, `--no-models`, `--no-tns`, `--match-radius`,
`--phot-tolerance`, `--spec-tolerance`, `--status`. Run `catalog_instruments --load --apply` before
the first import so instruments resolve.

## Size

From a sample of the catalogue: SN 2011fe has 3,483 photometry points and 158 spectra
(380,000 pixels); SN 2013dy has 2.9 million pixels; SN 2017egm has 3 MOSFiT fits whose 300
realizations and 49,923 model points become 4,858 summary points. Stored as arrays, a spectrum
is one row, so the full import adds tens of thousands of spectrum rows rather than about 10^8
pixel rows.
