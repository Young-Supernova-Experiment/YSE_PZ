# NGSF spectral classification (analysis service)

Part of #315 / #312. **NGSF** (Next Generation SuperFit, Goldwasser et al. 2022,
<https://github.com/oyaron/NGSF>) matches an observed spectrum against a bank of supernova
templates combined with host-galaxy templates over a grid of redshift, extinction and phase, and
ranks the combinations by chi2/dof. YSE-PZ ships it as a **built-in analysis service**
(`YSE_App/analysis/ngsf.py`) that shells out to an installed copy of NGSF; nothing of NGSF is
vendored into this repository, and its template bank (hundreds of MB) is not downloaded by the
web stack.

## What the user sees

* Summary tab, under the spectrum plot: **NGSF classification:** the best match of the last
  successful run (`Ia` badge, template name, phase, z, chi2/dof, how many of the top matches share
  each type, which spectrum, when and by whom) with a **Run NGSF** button and a **details** link to the
  Analysis tab. While a run is queued or running the line shows a spinner and polls; a failed run
  shows the error. When NGSF is not installed the line says so (staff also see the reason and a
  pointer to this page) and there is no button; when the service is not registered the line says
  "not available on this server" (staff see the registration command).
* Analysis tab: the run with its result table (`best_type`, `best_template`, `best_phase`, `best_z`,
  `best_av`, `best_chi2_dof`, `best_galaxy`, `best_frac_sn`, `type_votes`, `matches`, the spectrum used,
  the redshift window), NGSF's comparison plots (`ngsf_match_<n>.png`) and the downloads
  `ngsf_results.csv`, `parameters.json` (exactly what NGSF was given) and `ngsf_log.txt` (its stdout /
  stderr tail).
* Parameters (the Analysis tab's form; the Summary button uses the defaults): `spectrum_id`
  (blank = the most recent spectrum with data), `z_window` (half-width around the known transient or
  host redshift, default 0.02; `0` pins the exact z), `z_max` (upper limit of the grid when no
  redshift is known, default 0.3), `n` (matches to keep, 5), `resolution` (10 Angstrom),
  `mask_galaxy_lines`, `mask_telluric`, `how_many_plots` (3).

## Installing NGSF on a stack (operator steps, optional)

Nothing in the UI needs NGSF to be present: without it the Summary line reads "NGSF is not
installed on this server" and runs fail with the same message. To enable it:

1. Install NGSF where the **job runner** runs (`manage.py run_jobs` / the `RunQueuedJobs` cron; with
   `JOB_RUNNER_INLINE` that is the web process). Follow the NGSF README: clone the repository,
   create its Python environment (it needs `numpy`, `scipy`, `astropy`, `pandas`, `matplotlib`,
   `PyAstronomy`, `extinction`), and download the template bank into the repository's `bank/` directory
   as the README describes. A separate virtualenv is fine; YSE-PZ only needs a command it can run.
2. Put a base `parameters.json` in the NGSF directory with the settings you want every run to
   share (`temp_sn_tr`, `temp_gal_tr`, `lower_lam`, `upper_lam`, `z_int`, `error_spectrum`,
   `minimum_overlap`, `epoch_*`, `Alam_*` ...). The runner keeps everything in that file and
   overrides only `object_to_fit`, `saving_results_path`, `z_start` / `z_end` / `use_exact_z`
   (and `z_exact`), `n`, `resolution`, `show_plot` (0), `how_many_plots`, `mask_galaxy_lines`,
   `mask_telluric`.
3. Configure `settings.ini` (`[site_settings]`):

   | key | default | meaning |
   |---|---|---|
   | `NGSF_COMMAND` | `ngsf` | how to run NGSF: a program on `PATH`, a path, or a full command such as `/opt/ngsf-venv/bin/python /opt/NGSF/run.py`. The path of the run's `parameters.json` is appended, or put where `{params}` appears. Empty = not installed. |
   | `NGSF_HOME` | (empty) | the directory NGSF runs in: its template bank and the base `parameters.json`. Must exist when set. |
   | `NGSF_SUBPROCESS_TIMEOUT` | `1500` | seconds one NGSF process may take before the run fails. |

   `NGSF_COMMAND` must resolve to an existing program (`shutil.which`, or an existing file when given
   with a directory); the Summary tab checks this on every load, so a typo shows up as "NGSF is not
   installed: NGSF command ... was not found".
4. Register the service once per stack (idempotent):

   ```bash
   python manage.py register_analysis_service --builtin ngsf --cap 10 --timeout 1800
   ```

   `--timeout` is the analysis-run timeout (the job is abandoned after it; keep it above
   `NGSF_SUBPROCESS_TIMEOUT`). `--group "Spectra people"` restricts who may run it.
5. Try it from a transient with a spectrum: Summary tab -> **Run NGSF**. The run's `ngsf_log.txt` on
   the Analysis tab shows the exact command and NGSF's output when something is off.

Everything above is needed on **yse_experimental** only if you want NGSF results there; the
Summary-tab line and the Refit machinery of #315 work without it.

## How the runner works (`YSE_App/analysis/ngsf.py`)

1. `availability()` resolves `NGSF_COMMAND` and checks `NGSF_HOME`; the fragment and the runner both
   use it, so "not installed" is one message everywhere.
2. `choose_spectrum(payload, spectrum_id)` picks the requested spectrum, else the most recent one
   with data (the payload holds only spectra the requesting user may see, see
   `docs/analysis-services.md`). `spectrum_text` writes it as two- or three-column ASCII
   (`wavelength flux [flux_err]`, finite rows only; at least 50 points).
3. `parameters_for(...)` merges the base `parameters.json` with the run's values; the redshift window is
   `z +/- z_window` when the transient (or its host) has a redshift, else `0 .. z_max`.
4. `run_ngsf(params_path, cwd, timeout)` is the only place a process is spawned:
   `subprocess.run(argv, cwd=NGSF_HOME, capture_output=True, timeout=...)` with `MPLBACKEND=Agg`.
   A non-zero exit fails the run with the tail of stderr; a timeout with "NGSF did not finish
   within N s"; a missing program with "could not start NGSF".
5. `find_results` takes the newest `*.csv` and every `*.png` under the run's results directory;
   `parse_results_csv` reads NGSF's columns (`SPECTRUM, GALAXY, CONST_SN, CONST_GAL, Z, A_v, Phase,
   Band, Frac(SN), Frac(gal), CHI2/dof, CHI2/dof2`; names are matched case- and space-insensitively),
   splits `.../sne/<type>/<template>/<file>` into type and template, and ranks by chi2/dof.
   `summarise` builds the `best_*` fields and `type_votes` over the top `n`.
6. The scratch directory is removed after every run; the kept files are stored as
   `AnalysisResultFile` rows under `MEDIA_ROOT/service_runs/<run uuid>/`.

If a future NGSF release changes its CSV columns or its invocation, `COLUMN_ALIASES`, `build_argv`
and `parameters_for` are the three places to adjust.

## Tests

`YSE_App/tests/test_summary_refit.py` (`NgsfRunnerTests`, `NgsfFragmentAndQueueTests`): availability
states, argv building, the not-installed / no-spectrum / bad-id / too-few-points errors, parameter
merging with a base file, the redshift window, CSV ranking and template splitting, the subprocess
boundary replaced by a fake that writes a recorded CSV and PNGs (non-zero exit, timeout and missing
CSV included), the built-in registration, the Summary fragment in every state and a run end to end
through the inline job queue. No NGSF process is ever started in the suite.
