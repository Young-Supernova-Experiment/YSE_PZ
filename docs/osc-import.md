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

## Rules

- **Which transient.** The one the entry was imported to before; else a transient whose name is
  the entry name or an alias (`SN2011fe` also matches `2011fe`), or has one as an alternate name;
  else one within `--match-radius` (2 arcsec); else a new transient named like TNS (`SN2011fe`
  becomes `2011fe`, `ASASSN-13av` stays), with status `--status` (default `Ignore`, as the TNS
  import uses for old objects) and observation group `OSC`. An entry with no match and no position
  is reported and skipped.
- **Existing transients.** Only empty fields are filled (discovery date only at day precision;
  classification only when it matches a `TransientClass` name, `Ia` -> `SN Ia`). The redshift
  follows the redshift precedence list (docs/redshift-precedence.md) with source `OSC`, which ranks
  as another catalogue: it replaces a blank-source or TNS value, never NED, DESI, our own or an
  override. Redshift, classification and host-link changes are in the change log (service
  `osc-import`).
- **Peak.** The visual-band peak (`maxvisual*`) when the OSC has one; `maxabsmag` is the brightest
  point in any band, often UV.
- **Photometry.** A magnitude and its error go to `mag` / `mag_err`. Upper limits keep their
  magnitude but no `mag_err` (the light curve counts magnitude plus error as a detection; the
  error is kept in the info row). Optical flux densities or count rates with a zero point fill
  `flux` / `flux_err` / `flux_zero_point`, so the light curve draws those limits. X-ray, radio and
  luminosity points have no magnitude: their values are in the info row and their band is named
  by energy or frequency (`0.5-8 keV`, `8.5 GHz`). `[start, stop]` times use the midpoint.
  `includeshost` sets the NOT_DIFFERENCED quality bit.
- **Spectra.** `deredshifted`, `dereddened`, `Uncalibrated` fluxes and `includeshost` set the
  matching quality bits; `vacuumwavelengths` sets the wavelength medium. A spectrum with no time
  gets the discovery (else peak) date, flagged `time_missing`. Non-numeric pixels are dropped.
- **Instruments.** Matched to a YSE instrument by name (case-insensitive, on the named telescope
  if that helps); otherwise the `Unknown` instrument, with the band created there by name. The
  OSC's telescope and instrument strings are in the info rows.
- **Access.** OSC data are published: photometry sets and spectra get the Public group and the
  `OSC` observation group. The new tables are not yet in the SQL Explorer row rules, so Explorer
  shows them to staff only.
- **Re-import.** Importing an entry again deletes what its last import wrote (photometry sets,
  spectra, quantities, model fits) and writes it anew. Transient fields filled earlier stay.
- **Historical import.** No automatic TNS publishing, minor-planet screen or annotation runs for
  transients created by the import; spectra are saved without the new-observation signals
  (favourite notices, follow-up success, program links); photometry statistics are recomputed
  once per transient.
- Dates before year 1000 (SN 185, SN 386) cannot be stored in MySQL `DATETIME`; they stay in the
  quantities only.

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

Each entry is one transaction; a failed entry is reported (and in `--report`) and the run goes on.
Other options: `--limit N`, `--no-spectra`, `--no-models`, `--match-radius`, `--status`.

## Size

From a sample of the catalogue: SN 2011fe alone has 3,483 photometry points, 158 spectra and
380,000 spectrum pixels; SN 2013dy has 2.9 million pixels; SN 2017egm has 3 MOSFiT fits with 300
realizations and 49,923 model points. Spectrum pixels (one `TransientSpecData` row each) are by far
the largest part of a full import: expect on the order of 10^8 rows. Plan disk space and run the
full import off-peak; `--no-spectra` imports everything else first.
