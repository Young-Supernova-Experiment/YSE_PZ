# Computed spectral phase (#848)

Every transient spectrum gets a phase under each reference epoch. Find (the transient search) can filter on it,
and Download -> Spectra puts it in a manifest. There is no separate spectra search page.

## References (Ryan, 2026-10-25 and 2026-11-01)

| Reference | Epoch | Frame |
|---|---|---|
| `default` | the Bazin peak: `bazin_rest`, or `bazin_obs` when the transient has no redshift | rest, else observer |
| `bazin_rest` | peak MJD of the reference band of the newest non-subset Bazin run (`BazinFit`, #846) | rest: days / (1 + z), z = best redshift (`redshift_precedence.best_redshift`) |
| `bazin_obs` | the same peak | observer |
| `salt_t0` | `t0` of the newest non-subset SALT run (`SaltFit`) | rest, with the fit's own z, else the best redshift; observer when neither is known |
| `discovery` | `Transient.disc_date` | observer (days since discovery) |

**Validity rule.** A Bazin phase exists only when the peak is no earlier than the last non-detection
(`BazinFit.peak_after_last_non_detection` is not False; unknown counts as valid). Otherwise the spectrum has no
`bazin_*` row and no default phase.

## Storage: `YSE_App.models.SpectrumPhase` (table `YSE_App_spectrumphase`, migrations 0069 and 0070)

One row per spectrum and reference: `phase` (days), `frame`, `redshift` (used for the rest frame), `reference_mjd`,
`spectrum_mjd`, `is_default`. Both foreign keys (`spectrum`, `transient`) carry MySQL `ON DELETE CASCADE`
(0070, through `YSE_App/common/fk_on_delete.py`), so develop's deletes on the shared database still work.

Rows are rewritten (`YSE_App/services/spectral_phase.py`):

- for every spectrum of a transient when a Bazin or SALT run writes its fit rows (`fit_tables.on_run_saved`);
- for one spectrum when it is saved (a `post_save` hook);
- by `manage.py backfill_spectral_phases` (dry run; `--apply` writes; `--limit N`). It also picks up changed
  redshifts and discovery dates, which do not trigger a recompute. Runbook: "when Ryan asks".

Hooks never raise into the save that triggered them.

## Find: "has a spectrum in phase range"

Search parameters (group "Spectra" on the search form, shown with the UI preview on; the API takes them always):

- `spec_phase_min`, `spec_phase_max`: days;
- `spec_phase_ref`: `bazin_rest`, `bazin_obs`, `salt_t0`, `discovery`; empty means the default rule.

A transient matches when a spectrum **the viewer may see** has a phase in the range under the reference. The
reference alone filters nothing. The filters live in `TransientSearchFilterSet`, so saved search Selections
(`services/selections.py`) use them too, compiled for each viewer.

## Download -> Spectra / Photometry (UI preview only)

A **Download** menu on the search results and on a Selection's page. GET forms, signed-in users, preview on
(404 otherwise):

- `search/download/spectra/?params=<search query string>&...` and `search/download/photometry/?params=...`
- `selections/<slug>/download/spectra/?...` and `selections/<slug>/download/photometry/?...`

At most `SEARCH_BULK_PHOTOMETRY_MAX_ROWS` (300) transients, the cap of the existing bulk photometry download; more
sends you back with a message (#855 will lift caps through jobs). Spectra and points are those the viewer may
download today, through the same per-spectrum and per-point calls; options only narrow them.

**Spectra** options: `phase_ref`, `phase_min`, `phase_max`, `instrument` (repeat), `snr_min` (median flux / error
over pixels with an error; spectra without errors are left out when set). The zip is streamed
(`download_stream.zip_stream`): one CSV per spectrum (the transient page's format), `manifest.csv` and `README.txt`.
Manifest columns: `file, transient, obs_date_utc, spectrum_mjd, instrument, telescope, phase_days, phase_reference,
phase_frame, redshift_used, transient_redshift, median_snr, note`. `phase_reference` names the reference actually
used (`bazin_rest` or `bazin_obs` for the default).

Fit-based phases are derived values: they may select spectra, but the manifest leaves them blank (with a note) for a
viewer from whom some of the transient's photometry is hidden, as for the stored SALT fit. Discovery phases are
always shown.

**Photometry** options: `mjd_min`, `mjd_max`, `phase_ref` + `phase_min` / `phase_max` (the transient's reference
epoch, same rules as above), `band` (comma-separated FLT names), `exclude_flagged`. Same CSV format as the bulk
photometry zip; `README.txt` lists the options and how many points they left out. Without options the zip is as before.

## Not done here

- Redshift or discovery-date edits do not recompute phases by themselves (the backfill does).
- To test with users: whether the default reference makes sense for non-Ia classes.
