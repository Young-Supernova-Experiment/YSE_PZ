# Spectra stored as arrays

Issues [#492](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/492) and
[#891](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/891).

## What changed

A spectrum's pixels used to be one `TransientSpecData` / `HostSpecData` row per pixel. Now they
are stored as one `TransientSpectrumArrays` / `HostSpectrumArrays` row per spectrum: wavelength,
flux and flux error as zlib-compressed float64 arrays, plus the pixel count and wavelength range.
Code: `YSE_App/common/spectrum_arrays.py`.

- **Reading** (dual read): plots, the spectra tab, the CSV and JSON downloads, the analysis
  payload, TNS classification reports, SQL Explorer and the REST API read both storages. A spectrum
  with an arrays row is read from it; one without is read from its pixel rows.
- **Writing**: the spectrum upload form, `/add_transient_spec/` and the JSON transient upload (also
  used by the TNS import) write arrays, through `spectrum_arrays.write_pixels`, which first deletes
  the spectrum's old pixels in either storage.
- **REST API**: `/api/transientspecdata/` and `/api/hostspecdata/` still list one record per pixel.
  Pixels of an array-stored spectrum get negative ids, are read-only (PUT/PATCH: 405), and take
  `created_by` / `created_date` from the spectrum; `wavelength_err` is null and `pixel_mask` 0.
  New filter: `?spectrum=<id>`.
- **Rollback switch**: `SPECTRUM_PIXEL_STORAGE: rows` in `[site_settings]` (or the env var
  `YSE_SPECTRUM_PIXEL_STORAGE=rows`) makes uploads write pixel rows again.
  Reading needs no switch.

## Converting existing spectra

`manage.py convert_spectra_to_arrays` (dry run by default) writes an arrays row for each spectrum
that has pixel rows and none yet, verifying the pixel count. Options: `--apply`, `--batch`, `--max`,
`--transient` / `--host`. It never deletes rows unless `--apply --delete-rows` is given, and then
only for spectra whose arrays row holds the same number of pixels.

## Shared test database

yse\_test (code from `develop`) and yse\_experimental share the `YSE_test` database, and `develop`
reads only pixel rows until this change is promoted. So, until then:

- a spectrum uploaded on yse\_experimental shows on yse\_experimental only;
- converting without `--delete-rows` is safe for both stacks;
- **never run `--delete-rows` on `YSE_test`**.
