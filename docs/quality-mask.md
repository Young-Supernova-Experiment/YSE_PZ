# quality_mask: data-quality bits for photometry and spectra

The spec is final (issue #79, v6, approved 2026-10-11). The code is in
`YSE_App/common/quality_mask.py`, and `YSE_App/tests/test_quality_mask_defs.py`
checks that the tables below match it.

`quality_mask` is an unsigned integer on each photometry point and each spectrum.
Each set bit records one deficiency. A flag is set only when it applies, so
**0 is the best possible data**.

## Rules

- **Storage.** `quality_mask` is a `PositiveBigIntegerField`, which is MySQL
  `BIGINT UNSIGNED NOT NULL DEFAULT 0`. The highest bit used is 51, so every value
  stays below 2^53 and is exact in JavaScript and Bokeh.
- **Order.** Higher bits are more serious. A plain integer comparison therefore
  triages quality: `mask < 2**k` means that nothing at bit k or above is set.
- **Spacing.** Flags sit on even bits, and the odd bits are reserved. A new flag
  can go between two existing ones without renumbering any stored mask.
- **Fixed positions in every set:**
  - `REJECTED` = 51.
  - `OTHER` = 50, a catch-all that means "there is an issue; someone should look".
    It is always just below REJECTED.
  - `NOT_PUBLISHED` = 0.
- **Shared tables.** One photometry table covers transient and host photometry,
  and one spectra table covers transient and host spectra. A flag shared by both
  kinds has the same bit in both, and each kind uses a subset.

## Photometry: `TransientPhotData` and `HostPhotData`

Least to most serious. The code is `PhotFlag`. The bits that each model may use
are `TRANSIENT_PHOT_ALLOWED` and `HOST_PHOT_ALLOWED`.

| Bit | Flag | Applies to |
|---|---|---|
| 0 | `NOT_PUBLISHED` | both |
| 2 | `NON_PHOTOMETRIC` | transient |
| 4 | `HIGH_BACKGROUND` | both |
| 6 | `APERTURE` (aperture, not PSF) | transient |
| 8 | `NOT_FORCED` | transient |
| 10 | `NOT_FULLY_REDUCED` | both |
| 12 | `NO_SUPERCAL` | transient |
| 14 | `NOT_DIFFERENCED` | transient |
| 16 | `NOT_BKG_SUBTRACTED` | transient |
| 18 | `POOR_SEEING` (or bad PSF fit) | both |
| 20 | `LOW_SN` | host |
| 22 | `TEMPLATE_SUSPECT` | transient |
| 24 | `QUICKLOOK` | both |
| 26 | `BLENDED` | both |
| 28 | `SHREDDED` (split by the deblender) | host |
| 30 | `TRANSIENT_CONTAMINATED` | host |
| 32 | `NEAR_EDGE` | both |
| 34 | `NOT_CALIBRATED` | both |
| 36 | `CR` | both |
| 38 | `TRAIL_GHOST` (satellite, asteroid, reflection) | both |
| 40 | `BAD_PIXEL` | both |
| 42 | `SATURATED` (or non-linear) | both |
| 50 | `OTHER` | both |
| 51 | `REJECTED` | both |

`HostPhotData.phot_type` records the kind of host magnitude. It is one of
`total`, `r25`, `kron`, `petrosian`, `aperture`, `model`, `psf` or `other`
(`PhotType`), and it may be NULL.

## Spectra: `TransientSpectrum` and `HostSpectrum`

Least to most serious. The code is `SpecFlag` for transient spectra and
`HostSpecFlag` for host spectra.

| Bit | Flag |
|---|---|
| 0 | `NOT_PUBLISHED` |
| 2 | `NOT_ABS_CALIBRATED` (not scaled to photometry) |
| 4 | `STITCHED` |
| 6 | `BOXCAR_EXTRACTION` (good = optimal extraction) |
| 8 | `DEREDSHIFTED` (good = observed frame) |
| 10 | `DEREDDENED` (good = not dereddened) |
| 12 | `HIGH_AIRMASS` |
| 14 | `LOW_SN` |
| 16 | `NOT_FULLY_REDUCED` |
| 18 | `HOST_CONTAMINATED` (transient spectra) |
| 20 | `QUICKLOOK` |
| 22 | `NOT_PARALLACTIC` |
| 24 | `WAVELENGTH_SUSPECT` |
| 26 | `SECOND_ORDER` |
| 28 | `NOT_TELLURIC_CORRECTED` |
| 30 | `WAVELENGTH_MEDIUM_UNKNOWN` (derived from `wavelength_medium`) |
| 32 | `NOT_FLUX_CALIBRATED` |
| 50 | `OTHER` |
| 51 | `REJECTED` |

On `HostSpectrum` every bit is the same except bit 18:

| Bit | Flag |
|---|---|
| 18 | `TRANSIENT_CONTAMINATED` (host spectra) |

The metadata fields beside the mask record what was done. They are not rankings,
and they may be NULL so that the shared database keeps working.

- `wavelength_medium` (both spectrum tables) is `vacuum`, `air` or `unknown`
  (`WavelengthMedium`). Bit 30 is derived from it whenever the spectrum is saved
  (`apply_wavelength_medium`), so the field and the bit cannot disagree. A NULL
  medium counts as unknown. Air and vacuum are both fine.
- `host_subtraction` (transient spectra only) is `local`, `archival`,
  `template`, `none` or `unknown` (`HostSubtraction`).

## Pixel mask: `TransientSpecData` and `HostSpecData`

`pixel_mask` is a `PositiveSmallIntegerField` with default 0. Any set bit
excludes that pixel. The code is `PixelFlag`.

| Bit | Flag |
|---|---|
| 0 | `BAD_PIXEL` (or chip gap) |
| 1 | `CR` |
| 2 | `SKY_RESIDUAL` |
| 3 | `FRINGING` |

## Triage with `<`

Use `quality_lt(threshold)`, which returns `Q(quality_mask__lt=threshold)`, or
compare the integers directly. Each cut keeps the rows that have nothing at the
threshold's bit or above:

| Constant | Value | `mask < constant` keeps |
|---|---|---|
| `USABLE` | 2^51 | everything not REJECTED |
| `NO_OTHER` | 2^50 | also no OTHER |
| `PHOT_CLEAN` | 2^32 | photometry: also no image artefact or calibration problem (bits 32-42) |
| `PHOT_SCIENCE` | 2^24 | photometry: also not quicklook, blended, shredded or contaminated (bits 24-30) |
| `PHOT_REDUCED` | 2^10 | photometry: also no reduction-stage issue (bits 10-22) |
| `PHOT_BEST` | 2^2 | photometry: also no measurement-method caveat (bits 2-8) |
| `SPEC_CALIBRATED` | 2^22 | spectra: also no calibration problem (bits 22-32) |
| `SPEC_SCIENCE` | 2^20 | spectra: also not quicklook |
| `SPEC_GOOD_CONDITIONS` | 2^12 | spectra: also no airmass, S/N, reduction or contamination issue (bits 12-18) |
| `SPEC_BEST` | 2^2 | spectra: also no processing choice flagged (bits 2-10) |

```python
from YSE_App.common.quality_mask import USABLE, PHOT_SCIENCE, quality_lt

TransientPhotData.objects.filter(quality_lt(USABLE))        # the "good data" filter
TransientPhotData.objects.filter(quality_lt(PHOT_SCIENCE))  # stricter science cut
```

`flags_from_mask(mask, PhotFlag)` lists the flag names in a mask, least serious
first. A reserved bit shows as `BIT_<n>`, so it is never dropped silently.
`mask_from_flags(["QUICKLOOK", "BLENDED"], PhotFlag)` goes the other way.

## Why NOT_PUBLISHED is a bit test

Good data can simply be unpublished. NOT_PUBLISHED therefore sits at bit 0, below
every real deficiency, and **no `<` cut ever excludes it**: `mask < X` keeps
unpublished rows for every X above. To ask about publication, test the bit:
`is_published(mask)`, which is `mask & 1 == 0`. To compare quality while ignoring
publication entirely, compare `mask >> 1`.

## Reserved bits

The odd bits 1-49 are reserved, as are the even bits 44-48 in photometry and
34-48 in spectra. Nothing writes them. If one appears, `flags_from_mask` reports
it as `BIT_<n>`.

## Adding a flag

1. Choose the bit by seriousness. Find the two existing flags it falls between,
   and use the free bit between them (the odd bit, if both neighbours are on
   adjacent even bits). Never shift existing flags: every stored mask would have
   to be recomputed.
2. Add the member to `PhotFlag`, or to both `SpecFlag` and `HostSpecFlag`. For
   photometry, add it to `TRANSIENT_PHOT_ALLOWED`, `HOST_PHOT_ALLOWED` or both.
3. Add the row to the table above and to `V6_*` in
   `YSE_App/tests/test_quality_mask_defs.py`. The docs test fails until the two
   agree.
4. If the flag changes what a named threshold means, say so in the PR. Callers
   rely on those cuts.
5. If a `DataQuality` name should map to the new flag, update the mapping
   (#81) and re-run `backfill_quality_mask` (see the runbook).
