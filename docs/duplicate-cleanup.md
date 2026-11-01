# Duplicate clean-up of photometry and spectra (#890)

Some transients hold the same observation more than once: a file uploaded twice, or a point or
spectrum we reported to TNS that came back in a TNS import. `manage.py clean_duplicate_data` finds
these copies within each transient and flags all but one REJECTED. Nothing is deleted, and every
decision can be undone.

Code: `YSE_App/catalog_import/duplicates.py` (rules), `YSE_App/catalog_import/matching.py`
(`duplicate_photometry`, `duplicate_spectra`; the same tests the OSC import uses),
`YSE_App/models/duplicate_models.py` (`DuplicateDataDecision`), migration `0061_duplicate_data_decision`.

## What counts as a copy

Two copies of one observation may differ in value (another reduction), so they are matched by
origin, as in the OSC import (docs/osc-import.md):

* **photometry**: same transient, same filter (`r'` = `r`, `W1` = `UVW1`, ...), same instrument,
  times within 0.001 days (86 s; `--phot-tolerance`);
* **spectra**: same transient, same instrument, times within 0.05 days (72 min;
  `--spec-tolerance`), and wavelength ranges overlapping by at least half of the shorter one.
  Ranges come from either pixel storage (`spectrum_arrays.wavelength_ranges`).

When either copy has the `Unknown` instrument the match is *weak*: photometry then also needs
magnitudes within 0.5 mag. The time windows are narrower than the OSC import's (0.02 and 0.5 days),
because a repeat upload or a TNS round trip keeps the time, while real exposures in one band can be
minutes apart (ATLAS quads, ZTF). A copy joins a group only if it matches every member, so a fast
time series is never chained into one group.

## Which copy is kept

In order:

1. our own reduction over a copy from TNS (header `obs_group` `TNS`, or a `reference` starting `TNS`);
2. a copy with errors (`mag_err` or `flux_err`; for spectra, flux errors) over one without;
3. the earliest upload (`created_date`, then id).

The others get the REJECTED quality bit (bit 51; docs/quality-mask.md) and one
`DuplicateDataDecision` row each: the flagged row, the copy kept, the reason (`tns_copy`,
`no_errors`, `later_upload`), whether the match was weak, the time difference and the previous
`quality_mask`.

## What it never touches

* Points and spectra imported from a catalogue (a `catalog_info` row on the point, its header or the
  spectrum). The catalogue import matches those against YSE data itself (`CatalogDataMatch`).
* Rows already flagged REJECTED, and rows the clean-up flagged before and a person has cleared by
  hand since (a decision that is not reverted keeps them out of later runs).
* Anything but `quality_mask`: no point or spectrum moves between headers, and no groups or
  allocations change. Access is per point, so the report marks a flagged copy whose groups or
  allocation differ from the copy kept (`access differs`), for example a public TNS copy kept
  back in favour of our private reduction. Look at those before applying.

## Running it

Dry run (default; reads only):

```bash
python3 manage.py clean_duplicate_data --max 1000
python3 manage.py clean_duplicate_data --transient 2099abc
```

The first line names the database. Each transient with duplicates gets a line, and each copy to
flag a line with the copy kept and the reason (`--quiet`: transient lines only). The last lines
give the totals and, with `--max`, the `--after ID` that continues the run (transients are taken in
id order). `--no-photometry` / `--no-spectra` skip one kind.

Apply (one transaction per transient; needs `--database` naming the configured database, #904):

```bash
python3 manage.py clean_duplicate_data --max 1000 --after <ID> --apply --database <DB_NAME>
```

A re-run finds nothing new in transients already done, so an interrupted run can simply be
started again.

Undo (clears the REJECTED bit where the previous mask did not have it, and marks the decision
reverted; dry run without `--apply`):

```bash
python3 manage.py clean_duplicate_data --revert [--transient NAME ...] --apply --database <DB_NAME>
```

**Not to be run with `--apply` on Ziggy until Ryan asks** (runbook changelog v114).
