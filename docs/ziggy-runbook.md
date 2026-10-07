# YSE\_PZ: commands for David to run on Ziggy

> **Canonical copy.** This file replaces the claude.ai runbook artifact (last artifact version v23, then v24 as Markdown), moved into the repo on 2026-10-02 (#400). Update it in the same PR as any change that needs a step on Ziggy. Steps use placeholders (`<PROD_PYTHON>`, `<PROD_DB>`, `<PROD_DB_USER>`, `<PROD_DB_HOST>`, `<CRON_PYTHON>`); never put secrets, credentials or personal data here.


A living checklist for David Jones, in the order things should happen. It replaces the scattered deploy notes in [PR #143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143), [PR #335](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/335) and the [2026-09-29 runbook](https://claude.ai/artifact/Epp8VK8jZm2VqNBDBLUj7c). Every command is copy-pasteable with real paths; anything we could not read from the repo is a <PLACEHOLDER> to fill in once.

**Done:** [#335](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/335) merged to `develop` (5da66de) and deployed green to yse\_test at 18:01 UTC; migrations `0010_dashboard_indexes` (18 min) through 0029 (#341 PhotStat 18:43 UTC to #386 broker streams) already applied on the shared `YSE_test` database, nothing on yse\_test to run for them; [#143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143) (develop to master) is conflict-free with 8/8 checks green and its body carries all 66 `Fixes` lines.

**REQUIRED NOW (2026-10-02): `/yse_test/` did not deploy [#362](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/362).** Ryan merged #362 into `develop` at 07:40 UTC, but Deploy Stack [run 36979824014](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36979824014) stopped at `git fetch` with `error: cannot open .git/FETCH_HEAD: Permission denied`. Some files in the yse_test checkout are no longer owned by `foley`, the runner user, probably from the hotfix commit made there by hand. `/yse_test/` is still on 82b2580; nothing was migrated or restarted.

```bash
cd /data/yse_pz/YSE_PZ_test
find . -not -user foley -printf '%u %p\n' | head -20
```
**Expected:** a few lines naming another user, e.g. `.git/FETCH_HEAD` or `.git/ORIG_HEAD`.

```bash
sudo chown -R foley:foley /data/yse_pz/YSE_PZ_test
find /data/yse_pz/YSE_PZ_test -not -user foley | wc -l
```
**Expected:** `0`. **Check:** then tell Ryan, or re-run the failed deploy (GitHub > Actions > Deploy Stack > run 36979824014 > Re-run failed jobs). It should end green with `migrate` printing `No migrations to apply.`, because `YSE_test` already has 0011-0030. No `pip install` is needed: the new packages (`django-redis`, `redis`, `async-timeout`) are imported only when `REDIS_URL` is set.

To avoid this next time, make hand edits in that checkout as `foley` (`sudo -u foley git ...`), or re-run the `chown` afterwards.

**2026-10-02, your 10/1 list:** fixes are in `develop` via [#362](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/362). They reach `/yse_test/` once the deploy above succeeds. Nothing else to run.

- **Follow-up request** ([#395](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/395), [#404](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/404)): the 500 on yse_test was `IntegrityError 1364: Field 'usage_hours' doesn't have a default value`. Migration `0030_shared_db_column_defaults` (applied to `YSE_test` by the experimental deploy, 07:13 UTC) gives the new NOT NULL columns a MySQL default, so **yse_test works already**. Please re-test there.
- **Tab offset** ([#390](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/390)): your develop hotfix 82b2580; backported in [#392](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/392).
- **Add ToO / classical resource** ([#396](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/396), [#397](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/397), [#405](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/405)): both saved fine. The dashboard lists hid them: all ToO allocations ever, alphabetical; classical skipped Swope and anything more than 5 days out. The ToO date picker also defaulted to a single, already-past day.
- **`select_yse_fields`** ([#394](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/394), [#402](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/402)): ndarray `.exists()` fixed.
- **Change All Statuses** ([#398](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/398), [#406](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/406)): one UPDATE instead of saving each transient (Auto Ignore, 2939 transients, now about 23 s); the result is reported.
- **Forced phot** ([#399](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/399), [#403](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/403)): errors are reported; a comment names the requester. If the legacy path still fails, the alert names the exception and the Apache error log has the traceback: check the `[ztf]` keys in `settings.ini`.

**REQUIRED (2026-10-06): ZTF forced phot on yse_test fails with `FileNotFoundError`** ([#411](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/411)). The request writes wget's log to `<ztfforcedtmpdir>/forced_phot_out/` and, as the Apache user, could not create the file there. After [#412](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/412) reaches yse_test, the alert names wget's actual error. These checks find and fix the cause now. Run them for the yse_test checkout; repeat with `/data/yse_pz/YSE_PZ` for production.

```bash
grep -E '^ztfforcedtmpdir' /data/yse_pz/YSE_PZ_test/YSE_PZ/settings.ini
```
**Expected:** one line, e.g. `ztfforcedtmpdir=/data/yse_pz/tmp`. Call that path `<ZTFTMPDIR>`. With no line, the `[ztf]` value from `public_settings.ini` (`/data/yse_pz/tmp`) applies.

```bash
ps -o user= -C apache2 | sort -u
```
**Expected:** `root` plus one other user, e.g. `www-data`. Call the other one `<APACHE_USER>`.

```bash
ls -ld <ZTFTMPDIR> <ZTFTMPDIR>/forced_phot_out
sudo -u <APACHE_USER> test -w <ZTFTMPDIR>/forced_phot_out && echo writable
sudo -u <APACHE_USER> sh -c 'command -v wget'
```
**Expected:** both directories listed, `writable`, and a path such as `/usr/bin/wget`. **If not:**
- If `forced_phot_out` is missing, create it with `sudo mkdir -p <ZTFTMPDIR>/forced_phot_out`.
- If it is not writable, give the Apache user write access without taking it from the cron user that reads it (`foley`): `sudo chgrp <APACHE_USER> <ZTFTMPDIR>/forced_phot_out && sudo chmod 2775 <ZTFTMPDIR>/forced_phot_out`.
- If wget is missing, install it: `sudo apt-get install wget`.

**Check:** "Request ZTF Forced Phot" on a transient on `/yse_test/` alerts `success: ZTF forced photometry requested...` and adds a comment.

**Pending for David:**  items in [section 5](#pending). **Section 1 on yse\_experimental** (1.2 PhotStat backfill, 1.5 TNS sharing service, 1.6 analysis services, 1.7 annotation services) was reported done by David at 23:44 UTC, not independently verified. 1.10 `register_summary_service` (#373) was reported done at 23:50 UTC. **Nothing required is left on yse\_experimental.** **Then:** merge #143, then the production deploy in [section 3](#prod) (backup, pull, pip, settings.ini keys incl. the new required `[secrets] credentials_key`, EXPLAIN, migrate 0004-0029 with 0010 and 0027 as the slow steps, saved-query rewrite, collectstatic, Apache, LSST cron), the #260 spectra check, and #187.

Last updated 2026-09-30 02:08 UTC · nothing on production has changed yet

1. [0Paths](#paths)
2. [1Now, on yse\_test](#now)
3. [2Before merging #143](#merge)
4. [3Production deploy](#prod)
5. [4Data checks](#data)
6. [5Pending items](#pending)
7. [6Changelog](#changelog)

<a id="paths"></a>

## 0 · Paths used below

From `.github/workflows/deploy.yml` and `docs/ziggy-static-aliases.md` on `develop`. The two non-production stacks share the `YSE_test` MySQL database.

| Stack | Checkout | Python | Static alias |
| --- | --- | --- | --- |
| production (master) | /data/yse\_pz/YSE\_PZ | <PROD\_PYTHON> (see step 3.1) | /static/ |
| yse\_test (develop) | /data/yse\_pz/YSE\_PZ\_test | /data/yse\_pz/yse\_test\_virtual/bin/python | /test\_static/ |
| yse\_experimental | /data/yse\_pz/YSE\_PZ\_experimental | /data/yse\_pz/YSE\_PZ\_experimental/venv/bin/python per deploy.yml; Apache may still use yse\_test\_virtual ([#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187)) | /experimental\_static/ |

Placeholders: `<PROD_PYTHON>`, `<PROD_DB>`, `<PROD_DB_USER>`, `<PROD_DB_HOST>`, `<CRON_PYTHON>` (the interpreter your existing `runcrons` crontab line uses). Step 3.1 finds the first four.

<a id="now"></a>

## 1 · Now, on yse\_test and yse\_experimental all required steps reported done by David (23:44 and 23:50 UTC)

The develop deploy of #335 (run [36608870047](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36608870047)) already ran `check`, `migrate` (`No migrations to apply.`), `collectstatic` and the Apache restart, and the login page returned 200. Steps 1.1, 1.3 and 1.4 are verification only; **1.2, 1.5, 1.6 and 1.7 were required** on yse\_experimental (their migrations 0011, 0018, 0020 and 0021 are on the shared database); David reported them done at 23:44 UTC ("finished #1"; not independently verified, so the commands stay here for reference and for yse\_test after the next promotion). Sections 2 and 3 wait for the develop-to-master merge, as David noted. 1.10 (#373) was added afterwards and David reported it done at 23:50 UTC, also not independently verified. **Nothing required is left on yse\_experimental** unless a later merge adds a step.

1.1

Confirm the checkout and the index migration.

Copy

```
git -C /data/yse_pz/YSE_PZ_test rev-parse --short HEAD
cd /data/yse_pz/YSE_PZ_test
/data/yse_pz/yse_test_virtual/bin/python manage.py showmigrations YSE_App | tail -22
```

**Expected** `5da66de`, then `[X] 0009_remove_transienttag_associated_users`, `[X] 0010_dashboard_indexes` and, since the 18:43 UTC experimental deploy, the experimental migrations `[X] 0011` PhotStat ([#341](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/341)), 0012 job queue ([#340](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/340)) 0013 credentials ([#342](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/342)) 0014 notification preferences ([#347](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/347)) 0015 PhotStat limit bands ([#350](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/350)) 0016 allocations ([#351](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/351)) 0017 broker candidates ([#352](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/352)) 0018 sharing services ([#354](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/354)) 0019 interests / data access ([#357](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/357)) 0020 analysis services ([#360](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/360)) 0021 annotations ([#366](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/366)) 0022 instrument logs / weather ([#367](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/367)) 0023 feeds ([#374](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/374)) 0024 AI summaries ([#373](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/373)) 0025 favourites / Slack DM ([#375](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/375)) 0026 facility queue / accounting ([#376](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/376)) 0027 galactic coordinates ([#384](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/384)) 0028 has\_jwst ([#385](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/385)) and 0029 broker streams ([#386](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/386)) (yse\_test lists them once the next promotion lands; the table already exists in the shared database).

1.2

reported done 23:44 UTC Backfill the per-transient photometry statistics ([#341](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/341), issue [#268](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/268)). The new `PhotStat` table is kept up to date on every photometry upload, but existing transients have no row until this command runs; until then the "Peak Mag" column and the detail-page statistics block are empty for transients without new photometry. Run it from the experimental checkout: it has the command, yse\_test does not until the next promotion, and both stacks share the `YSE_test` database, so one run fills both.

Copy

```
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py rebuild_photstats
# if interrupted, resume with only the transients that still have no row:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py rebuild_photstats --missing-only
# if the backfill ALREADY ran before #350 (19:51 UTC, adds the limit bands): refresh outdated rows once
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py rebuild_photstats --stale-only
```

**Which one** If `rebuild_photstats` has not run yet, the plain command covers everything, including the per-band upper limits from [#350](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/350) (issue [#349](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/349); migration `0015_photstat_limit_bands` is already on the shared database). If it already ran, run `--stale-only` once: idempotent, touches only outdated or missing rows, and a second run prints `processed 0`.

**Expected** one progress line per 500 transients, then `processed T transient(s); created T, updated 0, unchanged 0`; 10-30 minutes for about 105 transients. Safe to interrupt and re-run. No settings keys. If that interpreter cannot import Django (see [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187)), use `/data/yse_pz/yse_test_virtual/bin/python` from the same directory. Runbook: [docs/photstat.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/photstat.md) (experimental).

1.3

Dry-run the saved-query rewrite against the shared test database. This is the same command you will run on production in step 3.12, so it is worth seeing its output here first.

Copy

```
cd /data/yse_pz/YSE_PZ_test
/data/yse_pz/yse_test_virtual/bin/python manage.py rewrite_dashboard_queries --dry-run
```

**Expected** a diff per matched title (Magnitude-Limited, Fast & Young, New Transients Last Two Days, Interesting New Targets) and `REVIEW` lines printing the current text of YSE Forced Phot Only, Auto Ignore and Volume-Limited. If it says the rows are already rewritten, the experimental checkout applied it. Optional: `--apply` here too (it writes a JSON backup first; `--revert <backup.json> --apply` undoes it). Log of every rewrite: [docs/sql-rewrites-2026-09-29.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/sql-rewrites-2026-09-29.md).

1.4

Browser check on <https://ziggy.ucolick.org/yse_test/>.

* `/yse_test/personaldashboard/`: all seven sections fill (the two that used to be blank now render, [#258](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/258)); note how long the slowest box takes on a cold cache.
* `/yse_test/transient_detail/2025aarm/`: light-curve legend is a multi-column grid; "Show Bazin Fit" sits next to "Show SALT3 Fit" and draws a joint fit ([#337](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/337)); the Spectra tab shows a Points column ([#262](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/262)).
* A classical observing night: the table has a "Bazin Mag @ Night" column ([#334](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/334)).

1.5

reported done 23:44 UTC Create the TNS sharing service and its encrypted credential on yse\_experimental ([#354](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/354), merged to experimental 21:04 UTC; migration `0018_sharing_services` applied by Deploy Stack run [36630779769](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36630779769)). TNS reports now go through a `SharingService` row whose bot credentials are stored encrypted; nothing can be reported until one exists. yse\_experimental runs with `IS_DEBUG: True`, so no `credentials_key` is needed there (production needs the `[secrets]` key from 3.2 first). The command reads `tns_bot_id`, `tns_bot_name` and `tnsapikey` from `settings.ini [main]`. Runbook: [docs/tns-sharing.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/tns-sharing.md).

Copy

```
# YSE bot (fill in the TNS reporting group id; extend the co-author list)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py create_tns_sharing_service --slug yse-tns --name "YSE TNS bot" --group-id <TNS reporting group id> --group-name YSE --from-settings --coauthors "R. J. Foley (UCSC), D. O. Jones (Hawaii), ..."

# DECam bot (also backs the legacy "Submit to TNS" button)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py create_tns_sharing_service --slug decam --name "DECam TNS bot" --group-id <DECam TNS group id> --decam
```

**Alternative in the admin** Encrypted credentials > Add (service `tns`, kind `TNS`, secret JSON `{"tns_bot_id": "...", "tns_bot_name": "...", "tns_api_key": "..."}`), then Sharing services > Add and link the credential.

**Required before any real report:** every new service starts in **sandbox** mode and posts to `sandbox.wis-tns.org`. Send one report from the submission page, check it on the sandbox, then switch the service to production; `--sandbox` reverts. The report dialog labels production services "(PRODUCTION)" and asks for confirmation.

Copy

```
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py create_tns_sharing_service --slug yse-tns --production      # or untick "testing" on the service in the admin
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py create_tns_sharing_service --slug decam --production
```

**Optional, on the service** `allowed_instruments` / `allowed_obs_groups` / `groups` to restrict who can report what; `config.instrument_ids` and `config.filter_ids` for instruments outside the defaults (GPC1/GPC2, ZTF-Cam, DECam, ATLAS, Sloan/Johnson); an `AutoPublisher` rule for automatic reports. **Optional settings.ini `[sharing]`** (all default off or safe; no crontab change, `TNSRetrieval` and `AutoPublishSweep` are in `CRON_CLASSES` and do nothing until enabled; reports run through the job queue; no pip step):

Copy

```
# [sharing]
TNS_RETRIEVAL_CRON_ENABLED: True    # hourly: match internally named transients to TNS names
AUTOPUBLISH_CRON_ENABLED: True      # run AutoPublisher rules
RENAME_ON_ACCEPT: True              # rename the transient to its TNS name when the report is accepted
POLL_DELAY_SECONDS: 60
POLL_MAX_ATTEMPTS: 20
TNS_RETRIEVAL_STATUSES: New,Following,Watch,FollowupRequested,Interesting
TNS_RETRIEVAL_SINCE_DAYS: 30
```

1.6

reported done 23:44 UTC Register the analysis services on yse\_experimental ([#360](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/360), analysis-service framework, merged to experimental 21:35 UTC as 45c051d; migration `0020_analysis_services` applied by Deploy Stack run [36634361955](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36634361955)). External fitters (SALT3 via sncosmo, Bazin) now run as `AnalysisService` runs through the job queue with their plots and files stored under `MEDIA_ROOT/service_runs/`. Nothing to pip install: sncosmo 2.8.0, matplotlib, scipy and requests are already in the venv (confirmed in the deploy log). Runbook: [docs/analysis-services.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/analysis-services.md).

Copy

```
# 1. register the two built-in services (idempotent; audience groups and caps editable later in admin under External services / Analysis services)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_analysis_service --builtin sncosmo_fit --cap 20
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_analysis_service --builtin bazin_fit

# 2. SALT3 model cache: sncosmo downloads SALT3 on first use into ~/.astropy/cache/sncosmo of the web / worker user,
#    so that user needs a writable home and outbound HTTPS from Ziggy; or pre-seed the cache as that user:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python -c "import sncosmo; sncosmo.get_source('salt3')"

# 3. MEDIA_ROOT/service_runs/ must be writable by the web process AND by whatever runs manage.py run_jobs / the cron pass
#    (MEDIA_ROOT = <checkout>/media unless overridden; files are served by an access-checked view, not MEDIA_URL)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python -c "from django.conf import settings; print(settings.MEDIA_ROOT)" 2>/dev/null || true
mkdir -p <MEDIA_ROOT>/service_runs && sudo chgrp www-data <MEDIA_ROOT>/service_runs && sudo chmod 2775 <MEDIA_ROOT>/service_runs

# optional cleanup, e.g. monthly from the crontab:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py expire_service_runs --days 90
```

**Expected** step 1 prints the created or already-present service twice; step 2 ends silently after the download (without it a SALT3 run fails with a clear message; the Bazin service needs nothing); step 3 uses the group Apache runs as (`www-data` on Debian/Ubuntu; check with `ps -o user= -C apache2 | sort -u`) and the setgid bit so files written by the cron user stay readable by the web process. The first SALT3 run from the detail page then finishes with a plot instead of a permissions error. Since [#385](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/385) (migration `0028_transient_has_jwst`, schema-only) the Summary tab's SALT3 line also uses this `sncosmo_fit` service: it shows "No stored SALT3 fit yet: use Refit" until a fit has succeeded, and clicking Refit queues one for the job runner.

**Optional `[site_settings]` keys** `ANALYSIS_HTTP_TIMEOUT_SECONDS` (30), `ANALYSIS_MAX_ATTACHMENT_BYTES` (25 MB), `ANALYSIS_MAX_FILES_PER_RUN` (20). For webhook services set `YSE_PUBLIC_BASE_URL` (the callback URL the service posts back to) and store the service token in an `EncryptedCredential` (`{"api_token": "..."}`).

1.7

reported done 23:44 UTC Register the annotation services on yse\_experimental ([#366](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/366), structured annotations + Gaia / WISE / quasar checks, merged to experimental 22:09 UTC as adc52f8; migration `0021_annotations` applied by Deploy Stack run [36637888807](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36637888807)). Each check is an `ExternalService` of kind `annotation` run by the job runner (`run_jobs` via the existing `runcrons` pass, 3.18), so the runner must be running for checks to execute. No new pip dependencies. Runbook: [docs/annotations.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/annotations.md).

Copy

```
# 1. register the three annotation services (idempotent): ExternalService rows gaia_dr3, wise, quasar of kind annotation, enabled
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_annotation_services
#    flags: --disabled (create them switched off), --user <username> (owner). Restrict, cap or disable later in admin under External services;
#    adding a Group to a service lets that group's members run it, otherwise staff only.

# 2. network: the web and job-runner host needs outbound HTTPS to both TAP endpoints
curl -sS -o /dev/null -w 'gaia   %{http_code}\n' https://gea.esac.esa.int/tap-server/tap/capabilities
curl -sS -o /dev/null -w 'vizier %{http_code}\n' https://tapvizier.cds.unistra.fr/TAPVizieR/tap/capabilities
```

**Expected** step 1 prints the three services as created (or already present); both curls print `200`. A non-200 or a timeout means Ziggy's egress blocks `gea.esac.esa.int` (ESA Gaia TAP) or `tapvizier.cds.unistra.fr` (CDS VizieR TAP) and the checks will fail until it is opened. Then run one check from a transient's Annotations panel and confirm a Gaia / WISE / quasar row appears after the next runcrons pass.

**Optional `[site_settings]` keys** `ANNOTATION_HTTP_TIMEOUT_SECONDS` (30), `ANNOTATION_SEARCH_RADIUS_ARCSEC` (3.0), `ANNOTATION_RUN_STALE_MINUTES` (60), `ANNOTATION_AUTORUN_SERVICES` (empty = off; `gaia_dr3,wise,quasar` auto-checks every new transient), `GAIA_TAP_URL`, `VIZIER_TAP_URL`.

1.8

### Instrument logs, weather widget and SkyCam (optional) nothing required

[#367](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/367) (merged to experimental 22:19 UTC as 408be43; migration `0022_instrument_logs_weather` applied by Deploy Stack run [36638875730](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36638875730)) adds per-telescope instrument logs, a weather widget and a SkyCam frame on the observing pages. Nothing runs until a telescope has a `weather_url` / `skycam_url` or an allocation has an `instrument_log_url`; both crons are in `CRON_CLASSES`, run from the existing `runcrons` pass through the job runner, and do nothing without configuration. No pip step. Runbook: [docs/instrument-logs-weather.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/instrument-logs-weather.md).

Copy

```
# 1. Admin > Telescopes > edit a telescope > "Weather widget and SkyCam" fieldset (no key needed for Open-Meteo):
#    weather_url:  https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,precipitation,weather_code,surface_pressure,dew_point_2m&wind_speed_unit=ms
#    or OpenWeatherMap: https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid=<OWM key>
#    weather_link: the observatory's own weather page      skycam_url: the all-sky camera image URL

# 2. facility instrument logs: on a GENERIC-adapter allocation (3.19) add to Default request parameters
#    {"instrument_log_url": "https://facility.example.edu/logs?from={start}&to={end}&inst={instrument}&tel={telescope}"}
#    (api_token in its credential if the facility needs one), then "Pull from the facility" on the page, or wait for the cron.

# 3. facility bots that push logs: a user with "Can add instrument log", a DRF token, then POST /api/instrumentlogs/
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py drf_create_token <bot username>
curl -sS -X POST https://ziggy.ucolick.org/yse_experimental/api/instrumentlogs/ -H "Authorization: Token <token>" -H "Content-Type: application/json" -d '{"telescope": <id>, "instrument": <id>, "start": "2026-09-29T03:00:00Z", "end": "2026-09-29T12:00:00Z", "body": "test entry"}'

# 4. egress: the web / job-runner host needs outbound HTTPS to the weather endpoint and the SkyCam host
curl -sS -o /dev/null -w 'open-meteo %{http_code}\n' "https://api.open-meteo.com/v1/forecast?latitude=36.97&longitude=-122.03&current=temperature_2m"
```

Copy

```
# [observatory]  (all optional; defaults shown; env overrides YSE_WEATHER_REFRESH_CRON=1 / YSE_INSTRUMENT_LOG_PULL_CRON=1)
WEATHER_REFRESH_CRON_ENABLED: True
WEATHER_REFRESH_CRON_MINUTES: 10
INSTRUMENT_LOG_PULL_CRON_ENABLED: True
INSTRUMENT_LOG_PULL_CRON_MINUTES: 60
INSTRUMENT_LOG_PULL_HOURS: 24            # window pulled on each pass
WEATHER_CACHE_MINUTES: 10
WEATHER_HTTP_TIMEOUT_SECONDS: 10
SKYCAM_REFRESH_SECONDS: 300
WEATHER_WIDGET_REFRESH_SECONDS: 600
```

**Expected** the weather widget on the telescope's observing-night page fills within `WEATHER_CACHE_MINUTES`; the SkyCam frame refreshes every `SKYCAM_REFRESH_SECONDS`; a POSTed log returns `201` and appears in the telescope's log list; a pulled log appears after the next hourly pass.

1.9

### Other feeds: Hermes, Einstein Probe, JPL Scout (optional) nothing required

[#374](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/374) (yse\_experimental; migration `0023_feeds` already applied by the deploy) adds a `FeedSource` model with Hermes, Einstein Probe and JPL Scout kinds, a `/feeds/` page, Hermes publishing on the sharing service and minor-planet screening. Everything is off until a source or the `[feeds]` keys exist; polls run through the job runner from the existing `runcrons` pass. Docs: [docs/feeds-hermes.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-hermes.md), [docs/feeds-einstein-probe.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-einstein-probe.md), [docs/feeds-jpl-scout.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-jpl-scout.md).

Copy

```
# (a) Hermes feed (SCiMMA). Admin > Encrypted credentials > Add: kind Hermes, service hermes, secret {"hermes_token": "<token>"}
#     Admin > Feed sources (or /feeds/ > Add source): kind Hermes, topic e.g. hermes.discovery; note the slug, then:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py feeds --poll <slug> --dry-run
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py feeds --poll <slug>

# (b) Hermes publishing: on the Hermes SharingService (1.5) set hermes_topic and the token credential;
#     leave "testing" on (publishes to hermes.test) until a message looks right, then untick it.

# (c) Einstein Probe. Either a Feed source of kind Einstein Probe with config {"url": "<notice mirror JSON>"} (polled like Hermes),
#     or the GCN Kafka consumer: pip, a credential with client_id / client_secret, and a consumer loop
/data/yse_pz/YSE_PZ_experimental/venv/bin/pip install gcn-kafka
while true; do cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py feeds --consume ep --timeout 300; sleep 5; done

# (d) JPL Scout + minor-planet screening. Feed source kind JPL Scout, config e.g. {"min_neo_score": 50}; then in YSE_PZ/settings.ini:
#     [feeds]
#     POLL_CRON_ENABLED = True
#     MPC_SCREEN_ON_CREATE = True
#     MPC_SCREEN_CRON_ENABLED = True
#     MPC_SCREEN_OBS_CODE = F51
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py feeds --screen <transient name>     # one-off check; Ziggy must reach ssd-api.jpl.nasa.gov
sudo systemctl restart apache2

# (e) only if the Hopskotch Kafka consumer is wanted instead of HTTP polling:
/data/yse_pz/YSE_PZ_experimental/venv/bin/pip install hop-client
```

**Expected** the dry run lists the messages a poll would ingest without writing; the real poll creates candidates or transients per the source's rules; `--screen` prints the MPC / Scout match (or none) for the transient; with `MPC_SCREEN_ON_CREATE` every new transient gets that check. Egress needed: `hermes.lco.global`, the EP notice mirror or `kafka.gcn.nasa.gov`, `ssd-api.jpl.nasa.gov`.

1.10

reported done 23:50 UTC Register the AI summary service on yse\_experimental ([#373](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/373), AI summaries; migration `0024_ai_summaries` applied by Deploy Stack run [36646344827](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36646344827)). Until it runs the Summary card shows text / edit / history only and staff see a hint. The default template provider needs no key and no network; a real LLM is opt-in. Both stacks share the database, so one run covers yse\_experimental and yse\_test. Runbook: [docs/ai-summaries.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/ai-summaries.md).

Copy

```
# REQUIRED once per database: creates the ai_summary service with the built-in template provider (no key, no network)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_summary_service
#    variants: --group YSE (only that group may generate), --cap 20 (runs per user per day), --disabled

# OPTIONAL, real LLM: admin > Encrypted credentials > Add: kind Generic, service llm, name e.g. "Anthropic key", secret {"api_key": "..."}; then EITHER
#    [llm]
#    LLM_PROVIDER: anthropic          # or openai, with LLM_MODEL and LLM_API_BASE
#    in settings.ini and  sudo systemctl restart apache2,  OR bind it on the service:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_summary_service --provider anthropic --credential "Anthropic key"

# OPTIONAL: nightly batch (needs the job runner / RunQueuedJobs cron, 3.18):  [llm] SUMMARY_BATCH_CRON_ENABLED: True
# after changing the embedder:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_summary_service --rebuild-embeddings
```

**Expected** the command prints `ready` (or `NOT ready: <reason>`, which names the missing piece); a "Generate" control then appears on the Summary card for everyone who opts in.

1.11

### Favourites and Slack DM notifications (optional) nothing required

[#375](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/375) (migration `0025_favorites_slack_dm` applied by Deploy Stack run [36647276426](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36647276426)) lets users favourite transients and receive activity notifications, optionally as Slack DMs. Both settings are optional: without `SLACK_BOT_TOKEN` the "Look up from my email" button on the profile page is disabled and no DM is attempted; in-app notifications work regardless. Docs: [docs/favorites-and-notifications.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/favorites-and-notifications.md).

Copy

```
# (a) batching window for favourite-activity notifications, in /data/yse_pz/YSE_PZ_experimental/YSE_PZ/settings.ini
#     [site_settings]
FAVORITE_ACTIVITY_BATCH_MINUTES: 60     # 0 = one notification per event
sudo systemctl restart apache2

# (b) Slack DMs: in the YSE-PZ Slack app (api.slack.com > Your apps) add the bot scopes
#     users:read.email   chat:write   im:write
#     then "Reinstall to workspace", and make sure the web environment exports the bot token:
#     /etc/apache2/envvars (or that checkout's YSE_PZ/wsgi.py):
export SLACK_BOT_TOKEN=<xoxb-...>
sudo systemctl restart apache2
```

**Expected** after (b) a user's profile page shows the Slack lookup button enabled; "Look up from my email" fills their Slack id and a test DM arrives on the next favourite activity (or within the batching window).

1.12

### Facility queue, adapters and accounting (optional) nothing required

[#376](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/376) (migration `0026_facility_queue_accounting` applied by Deploy Stack runs [36651810314](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36651810314) / [36651810458](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36651810458)) adds retrying submission, a poll job, per-resource usage accounting and SOAR / ZTF / ATLAS / Swift / LT adapters on top of the allocations from 3.19. Nothing runs until an allocation is bound to a facility; polling is off by default. Docs: [docs/facility-apis.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/facility-apis.md).

Copy

```
# sanity check: the migration is on this database
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py showmigrations YSE_App | tail -3        # ends with [X] 0026_facility_queue_accounting

# (1) polling of submitted requests, once allocations exist: in /data/yse_pz/YSE_PZ_experimental/YSE_PZ/settings.ini
#     [site_settings]
FACILITY_POLL_CRON_ENABLED: True        # queues a facility.poll job every FACILITY_POLL_CRON_MINUTES (10), run by manage.py run_jobs (3.18)
#     or by hand:
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py poll_facility_requests

# (2) seed the per-resource usage counters from existing follow-ups (idempotent)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py backfill_resource_usage            # preview
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py backfill_resource_usage --apply

# (3) bind facilities as staff at /allocations/ (or admin > Allocations), then set "allocation" on the matching ToO / classical resource:
#     LCO    facility lco,   proposal_id,  credential {"api_token": "<portal token>"}
#     SOAR   facility soar,  an LCO token as above
#     ZTF    facility ztf   (forced photometry), credential {"email": "...", "userpass": "..."}
#     ATLAS  facility atlas, credential {"api_token": "..."}
#     Swift  credential {"username": "...", "shared_secret": "..."}; requests stay dry-run until default_request_params has {"debug": false}

# defaults, all optional under [site_settings]:
#     FACILITY_SUBMIT_MAX_ATTEMPTS 3   FACILITY_SUBMIT_BACKOFF_SECONDS 120   FACILITY_POLL_CRON_MINUTES 10   FACILITY_POLL_LIMIT 200
#     FACILITY_NOTIFY_GROUPS False     FACILITY_HTTP_TIMEOUT_SECONDS 30      LT_RTML_HOST / LT_RTML_PORT (Liverpool Telescope)
```

**Expected** the sanity check ends with `[X] 0026_facility_queue_accounting`; the backfill preview lists resources and hours, `--apply` writes them and a second run changes nothing; with polling on, submitted LCO / SOAR requests move to accepted / complete without anyone pressing Refresh.

1.13

### Galactic coordinates (optional check) nothing required

[#384](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/384) (search extras, fixes [#286](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/286); migration `0027_transient_galactic_coords` applied by Deploy Stack run [36655302742](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36655302742), whose migrate step took 6 min 11 s because the migration backfills `gal_l` / `gal_b` for every transient in one UPDATE) adds galactic coordinates to `Transient` and to the search page. `Transient.save()` keeps the columns current, so nothing is required. Docs: [docs/transient-search.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/transient-search.md).

Copy

```
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py backfill_galactic_coords          # expected: 0 rows to fill
# recompute everything (only after a change to the conversion):
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py backfill_galactic_coords --all
```

**Expected** the plain run reports `0` rows to fill; a non-zero count means transients were created by a path that bypassed `save()`, and the same command fills them.

1.14

### NGSF classification (optional) nothing required

[#385](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/385) (Summary-tab refit, NGSF, `has_jwst`; fixes [#315](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/315) / [#383](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/383); merged to experimental 01:50 UTC as 918d73f; migration `0028_transient_has_jwst` applied by Deploy Stack run [36656986210](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36656986210)) adds an NGSF spectral-classification line to the Summary tab. It stays inert until NGSF is installed on the host that runs the job runner and registered as an analysis service. Full steps: [docs/ngsf.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/ngsf.md).

Copy

```
# 1. install NGSF and its template bank where the job runner runs (paths are examples; see docs/ngsf.md)
#    e.g. git clone https://github.com/oyaron/NGSF /opt/NGSF ; python3 -m venv /opt/ngsf-venv ; /opt/ngsf-venv/bin/pip install -r /opt/NGSF/requirements.txt ; template bank into /opt/NGSF/bank
#    and put a base parameters.json in that directory

# 2. in /data/yse_pz/YSE_PZ_experimental/YSE_PZ/settings.ini
#    [site_settings]
NGSF_COMMAND: /opt/ngsf-venv/bin/python /opt/NGSF/run.py
NGSF_HOME: /opt/NGSF
NGSF_SUBPROCESS_TIMEOUT: 1800          # optional, seconds

# 3. register the service (idempotent)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py register_analysis_service --builtin ngsf --cap 10 --timeout 1800
```

**Expected** the Summary tab's NGSF line offers "Run NGSF" instead of "NGSF is not installed on this server"; a run finishes with the top template matches after the next job-runner pass.

1.15

### Broker alert streams (optional, off by default) nothing required

[#386](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/386) (broker streams; fixes #273-#275, #277-#279; merged to experimental 02:04 UTC as 41a5226; migration `0029_broker_streams` applied by Deploy Stack run [36658108677](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36658108677), schema-only) adds Kafka / ANTARES stream consumers behind `BrokerConnection` rows, ingest heartbeats and a stale-stream banner. Nothing consumes until a connection is enabled and a consumer process runs. Full steps: [docs/broker-streams.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/broker-streams.md).

Copy

```
# 1. packages in the venv the ingest runs from (confluent-kafka is already pinned)
/data/yse_pz/YSE_PZ_experimental/venv/bin/pip install fastavro            # Fink Avro topics
/data/yse_pz/YSE_PZ_experimental/venv/bin/pip install antares-client      # only for ANTARES streams

# 2. admin > Encrypted credentials, only if the broker needs one:
#    Lasair REST   service lasair, secret {"token": "..."}
#    SASL Kafka    secret {"username": "...", "password": "..."}
#    ANTARES       secret {"api_key": "...", "api_secret": "..."}

# 3. admin > Broker connections > Add: broker, kind kafka or antares, bootstrap servers
#    (e.g. kafka-ztf.fink-broker.org:24499 or kafka.lsst.ac.uk:9092), topics as a JSON list, group id, format json or avro;
#    leave "enabled" OFF for now. Then create BrokerFilters (optionally topics, default_tags, notify_group).

# 4. smoke test without writing anything
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py broker_ingest --connection <slug> --force --dry-run --max-messages 50 --timeout 60

# 5. enable the connection in the admin, then run the consumer under the systemd unit from docs/broker-streams.md
#    (ExecStart runs this; or: docker compose --profile brokers up)
cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py broker_ingest --connection <slug> --workers 2
```

Copy

```
# [brokers]  (optional; defaults shown)
STREAM_STALE_MINUTES: 15          # dashboard banner when a heartbeat is older than this
STREAM_BATCH_SIZE: 100
DETAIL_RADIUS_ARCSEC: 5
# LASAIR_API_URL: ...
```

**Expected** the smoke test prints `N messages decoded, 0 saved` (dry run) with no traceback; once the consumer runs, admin > Ingest heartbeats shows a fresh row for the connection and the dashboard shows no stale-stream banner.

<a id="merge"></a>

## 2 · Before merging #143 to master David

[PR #143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143) promotes `develop` (5da66de, 270 commits, 341 files) to `master` (dc70630). Only David merges it. Nothing deploys automatically from that merge: `deploy.yml` covers `experimental` and `develop` only.

2.1

Read the PR body: "Production deploy steps", "Added by the 2026-09-29 promotions" and "Closes on merge" (66 `Fixes #N` lines; those issues close when master gets the merge).

**Checks as of 18:07 UTC** `mergeable_state: clean`, 8/8 checks green (lint, docker build, docker-test, promotion guard, deploy). If GitHub shows anything red, stop and say so in the thread.

2.2

Do the production pre-flight in [3.1](#prod) and [3.2](#prod) (find paths, fix `settings.ini`) before merging, so the merge and the deploy can happen in one sitting.

2.3

Merge with **Create a merge commit** (not squash, not rebase) so master history matches develop. Do not delete `develop`.

**Expected** master's new head has 5da66de as its second parent. Afterwards, back-merge master into develop and experimental is not needed (the merge commit is the only new commit on master).

<a id="prod"></a>

## 3 · Production deploy after #143 David

Checkout `/data/yse_pz/YSE_PZ`, branch `master`. Pick a quiet window of about an hour: migration 0010 alone took 18 minutes on the test database, and between step 3.9 and the Apache restart in 3.14 the old code runs against the new schema (0008 drops two follow-up columns the old code reads).

3.1

Find the production interpreter and database settings once.

Copy

```
grep -h python-home /etc/apache2/sites-enabled/yse.conf          # -> <PROD_PYTHON> = that path + /bin/python
grep -A3 '^\[database\]' /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini  # -> <PROD_DB>, <PROD_DB_USER>, <PROD_DB_HOST>
grep -n IS_DEBUG /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini          # must read False or 0 (strict boolean since #160)
crontab -l | grep -n runcrons                                      # -> <CRON_PYTHON> and where the crons run today
```

**Expected** one `WSGIDaemonProcess ... python-home=/data/yse_pz/<something>` line; `IS_DEBUG: False`. If IS\_DEBUG is blank or `None`, set it to `False` now or `manage.py check` fails with `ValueError: Not a boolean`.

3.2

Edit `/data/yse_pz/YSE_PZ/YSE_PZ/settings.ini`. One key is required, the rest are optional and default to today's behaviour. Template: [YSE\_PZ/public\_settings.ini](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/YSE_PZ/public_settings.ini).

**Required ([#253](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/253)):** with `IS_DEBUG: False`, `[site_settings] SECRET_KEY` must be a real value (the `<...>` placeholder counts as unset), or `DJANGO_SECRET_KEY` must be in Apache's environment. Otherwise `manage.py check` fails in step 3.7 and the site does not start after the restart. Generate one with the command below and paste it in; changing the key logs every user out once.

Copy

```
<PROD_PYTHON> -c "import secrets; print(secrets.token_urlsafe(50))"
```

Copy

```
# [site_settings]
SECRET_KEY: <paste the generated value>
ALLOWED_HOSTS: ziggy.ucolick.org,localhost,127.0.0.1   # optional; absent = * (today's behaviour)
EXPLORER_QUERY_MAX_EXECUTION_MS: 0     # optional; keep 0 (off) until the saved queries are rewritten and measured; 20000 afterwards
EXPLORER_QUERY_CACHE_SECONDS: 3600     # optional; saved-query result TTL
DASHBOARD_CACHE_WARM_ENABLED: False    # optional; turn on after step 3.12 to let a cron refresh the dashboard cache
DASHBOARD_CACHE_WARM_MINUTES: 60       # optional; keep <= EXPLORER_QUERY_CACHE_SECONDS / 60
DASHBOARD_QUERY_BACKUP_DIR: /data/yse_pz/backups/saved_queries   # optional; where --apply writes its JSON backup

# [database]
SSL_DISABLED: True                     # optional; True is today's behaviour

# [antares]  (optional; the LSST cron runs with none of these set)
lsst_survey_id=4
lsst_cone_radius_arcsec=2.0
lsst_max_days=30
lsst_statuses=New,Following,Watch,FollowupRequested,Interesting
lsst_max_transients=500
lsst_max_dec=32.0
lsst_mjd_match_min=0.0005
lsst_min_reliability=0.0
lsst_use_alert_flags=True
```

**`REDIS_URL` is safe on Django 3.2 since [#339](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/339)** (merged to experimental 18:35 UTC, fixes [#338](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/338)): `settings.py` now uses `django_redis.cache.RedisCache` when the package is installed and falls back to LocMem with a warning when it is not. It reaches yse\_test and production only with the next promotion, and the pip step in 3.17 is required before `REDIS_URL` is set on any stack. Until then keep it unset (the one-time check in 4.3). Key meanings: [docs/dashboard-performance.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/dashboard-performance.md), [docs/rubin-antares-ingest.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/rubin-antares-ingest.md).

**Required on production ([#342](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/342), issues [#264](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/264) / [#265](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/265); merged to experimental 19:29 UTC, migration `0013_credentials_external_services` already on the shared database):** any stack with `IS_DEBUG: False` needs a `[secrets] credentials_key` in `settings.ini`, or the site refuses to start, exactly like a missing `SECRET_KEY`. It encrypts per-resource credentials (facility API tokens and the like) at rest. yse\_experimental and yse\_test run with `IS_DEBUG: True` and derive the key from `SECRET_KEY`, so nothing is needed there. No pip step (`cryptography` is already pinned). Applies to production with the promotion that carries #342; do it in the same settings.ini pass, before `migrate`. Runbook: [docs/credentials-and-external-services.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/credentials-and-external-services.md) (experimental).

Copy

```
# 1. generate a key (prints the key and the ini snippet)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py generate_credentials_key

# 2. add to /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini (one key per stack; back it up outside the database,
#    e.g. in the same place as the SECRET_KEY: without it stored credentials are unreadable)
[secrets]
credentials_key: <printed key>
#    alternative: export YSE_CREDENTIALS_KEY=<printed key> in the Apache environment (/etc/apache2/envvars) AND the crontab

# 3. after the deploy: restart Apache and any cron / job-runner process that imports Django
sudo systemctl restart apache2

# later rotation, when needed:
<PROD_PYTHON> manage.py rotate_credentials_key --old=<OLD> --new=<NEW> --dry-run
<PROD_PYTHON> manage.py rotate_credentials_key --old=<OLD> --new=<NEW>
#    or without flags: export YSE_ROTATE_OLD_KEYS=<OLD[,OLDER]> YSE_ROTATE_NEW_KEY=<NEW>  then  manage.py rotate_credentials_key [--dry-run]
#    then switch credentials_key in settings.ini to <NEW> and restart Apache
```

**Expected** `generate_credentials_key` prints one key line plus the `[secrets]` snippet; `manage.py check` in 3.7 passes once it is in place. Use the `--old=` / `--new=` form when rotating ([#361](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/361), fixes [#359](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/359)): a generated Fernet key starts with `-` about 1 time in 32, and with a space argparse would read it as a flag; the `YSE_ROTATE_OLD_KEYS` (comma-separated) and `YSE_ROTATE_NEW_KEY` environment variables avoid the flags altogether. Optional sanity check on experimental now: `cd /data/yse_pz/YSE_PZ_experimental && /data/yse_pz/YSE_PZ_experimental/venv/bin/python manage.py showmigrations YSE_App | tail -3` should end with `[X] 0013_credentials_external_services`.

3.3

Run the production EXPLAIN commands **before** migrating ([#248](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/248) asked for this before any index migration). Items 1 and 2 are read-only and safe any time; item 4 executes a saved query, so run one at a time.

Copy

```
DB=<PROD_DB>; USER=<PROD_DB_USER>; HOST=<PROD_DB_HOST>
# 1. sizes, engine, buffer pool, existing indexes
mysql -u $USER -p -h $HOST $DB -e "SHOW TABLE STATUS WHERE Name IN ('YSE_App_transient','YSE_App_transientphotometry','YSE_App_transientphotdata','YSE_App_transientphotdata_data_quality','YSE_App_transient_tags','YSE_App_host','YSE_App_transientdiffimage')\G" > explain_1_tables.txt
mysql -u $USER -p -h $HOST $DB -e "SELECT COUNT(*) FROM YSE_App_transientphotdata; SELECT COUNT(*) FROM YSE_App_transientphotdata WHERE obs_date >= NOW() - INTERVAL 2 DAY; SELECT COUNT(*) FROM YSE_App_transient; SELECT status_id, COUNT(*) FROM YSE_App_transient GROUP BY status_id;" >> explain_1_tables.txt
mysql -u $USER -p -h $HOST -e "SELECT @@version, @@innodb_buffer_pool_size/1024/1024/1024 AS bp_gb, @@max_execution_time; SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';" >> explain_1_tables.txt
mysql -u $USER -p -h $HOST $DB -e "SHOW INDEX FROM YSE_App_transientphotdata; SHOW INDEX FROM YSE_App_transient;" >> explain_1_tables.txt

# 2. the exact SQL of Ryan's seven dashboard sections
mysql -u $USER -p -h $HOST $DB -e "SELECT uq.id AS section, q.id, q.title, q.sql FROM YSE_App_userquery uq JOIN explorer_query q ON q.id = uq.query_id WHERE uq.id IN (468,469,470,472,480,481,483)\G" > ryan_queries.txt

# 3. plan without executing (paste one query from ryan_queries.txt; MySQL 8.0.16+)
mysql -u $USER -p -h $HOST $DB -e "EXPLAIN FORMAT=TREE <paste one query>\G"
# 4. real timing: EXPLAIN ANALYZE executes the query. One at a time, off-peak, capped at 10 min:
mysql -u $USER -p -h $HOST $DB -e "SET SESSION max_execution_time=600000; EXPLAIN ANALYZE <paste one query>\G"
```

**Then** post `explain_1_tables.txt` and `ryan_queries.txt` (or their key lines) in the Slack thread. Repeat item 4 after step 3.9 for a before/after; an index MySQL never picks can be dropped in a follow-up migration. Full text and the slow-log variant: [docs/dashboard-performance.md, Ziggy runbook](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/dashboard-performance.md#ziggy-runbook-david).

3.4

Back up the production database.

Copy

```
mkdir -p /data/yse_pz/backups
mysqldump -u <PROD_DB_USER> -p -h <PROD_DB_HOST> --single-transaction --routines <PROD_DB> | gzip > /data/yse_pz/backups/<PROD_DB>_pre-0004-0010_$(date +%F).sql.gz
ls -lh /data/yse_pz/backups/<PROD_DB>_pre-0004-0010_*.sql.gz
```

**Expected** a file of plausible size (hundreds of MB or more). Today's nightly `yse_dbbackup.bash` dump is fine too, if it predates step 3.9.

3.5

Note the live commit for rollback.

Copy

```
cd /data/yse_pz/YSE_PZ && git rev-parse --short HEAD && git status --short | head
```

**Expected** `dc70630` and no modified tracked files (`settings.ini` is untracked and survives the pull).

3.6

Get the code and install the pinned requirements. `requirements.txt` is new on master, so this pip step is needed; it also brings `antares-client==1.2.0` into the web venv (harmless there; the cron venv gets it in 3.15).

Copy

```
cd /data/yse_pz/YSE_PZ
git fetch origin && git checkout master && git pull --ff-only origin master && git rev-parse --short HEAD

<PROD_PYTHON> -m pip install wheel
<PROD_PYTHON> -m pip install --no-build-isolation "extinction==0.4.2"
<PROD_PYTHON> -m pip install -r requirements.txt
```

**Expected** HEAD is the #143 merge commit; each pip line ends `Successfully installed` or `Requirement already satisfied`. `requirements.txt` is resolver-consistent since [#223](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/223), so no `--no-deps`; it pins `Django>=3.2,<4.0` and `protobuf==3.20.3`. If pip is not writable as your user, prefix with `sudo -u <VENV_OWNER>` (`stat -c %U $(dirname $(dirname <PROD_PYTHON>))`). ML extras for TensorFlow crons live in `requirements-ingest.txt` and are for the cron venv only.

3.7

Configuration check. Fails before anything touches the database.

Copy

```
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py check
```

**Expected** `System check identified no issues (0 silenced).` An `ImproperlyConfigured` error naming `SECRET_KEY` or `credentials_key` means step 3.2 is incomplete.

3.8

See what migrate will do.

Copy

```
<PROD_PYTHON> manage.py showmigrations YSE_App | tail -15
```

**Expected** `[X]` for 0001-0003, `[ ]` for 0004\_slackthreadlink through 0009\_remove\_transienttag\_associated\_users and 0010\_dashboard\_indexes.

If 0002 or 0003 show `[ ]`, stop and tell Ryan. If any of 0005-0008 already show `[X]` (a hand-populated or restored `django_migrations`), do not migrate: run `<PROD_PYTHON> manage.py repair_migration_state` (dry run) and follow [docs/ziggy-migration-repair.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/ziggy-migration-repair.md).

3.9

Migrate.

Copy

```
<PROD_PYTHON> manage.py migrate --noinput
<PROD_PYTHON> manage.py showmigrations YSE_App | tail -7
```

**Expected** `Applying YSE_App.0004... OK` through `0010_dashboard_indexes... OK`, then all `[X]`.

**0010 builds 8 indexes** (`Transient(name)`, `(disc_date)`, `(status, disc_date)`, `(modified_date)`, `(ra)`, `(dec)`; `TransientPhotData(obs_date)`, `(photometry, obs_date)`). On the test database that one step took 18 minutes ([run 36604581592](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36604581592)); production photometry is larger, so expect longer. InnoDB `CREATE INDEX` is online DDL (`ALGORITHM=INPLACE, LOCK=NONE`): reads and photometry ingest continue while it runs, and the site stays up. Do not interrupt it. 0008 backfills every follow-up row and then drops `spec_priority`/`phot_priority`; its reverse is a no-op, so from here rollback means restoring the 3.4 dump.

**0027 is the only other data step** ([#384](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/384)): it backfills `gal_l` / `gal_b` for every transient in one UPDATE and took 6 min 11 s on the YSE\_test database, so budget at least that on top of 0010 and run the whole migrate in a maintenance window. The other experimental migrations (0011-0026, 0028, 0029) are schema-only and fast.

3.10

PhotStat backfill ([#341](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/341), [#268](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/268)), once the promotion that carries #341 and its migration 0011 (and [#350](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/350) with 0015) is on master and 3.9 has applied them. Same command as 1.2, in the production checkout; a first run needs no flag, a re-run after a later PhotStat migration uses `--stale-only`.

Copy

```
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py rebuild_photstats
# resume after an interruption:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py rebuild_photstats --missing-only
# only if a backfill already ran on production before a later PhotStat migration (e.g. 0015 limit bands): refresh outdated rows
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py rebuild_photstats --stale-only
```

**Expected** one line per 500 transients, final `processed T transient(s); created T, updated 0, unchanged 0`; 10-30 min for ~105 transients; safe to interrupt and re-run. Until it finishes, Peak Mag and the detail-page statistics block stay empty for transients without new photometry. Can run while Apache is still serving the old code; nothing else waits on it.

3.11

Backfills from the security stack ([#143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143) step 8). Each prints counts; the verify command must end `All checks passed`.

Copy

```
<PROD_PYTHON> manage.py ensure_users_in_public_group --dry-run
<PROD_PYTHON> manage.py ensure_users_in_public_group
<PROD_PYTHON> manage.py mark_tns_imports_public --dry-run
<PROD_PYTHON> manage.py mark_tns_imports_public
<PROD_PYTHON> manage.py verify_production_group_access
<PROD_PYTHON> manage.py dedupe_dashboard_queries --dry-run
<PROD_PYTHON> manage.py dedupe_dashboard_queries
```

**Expected** `Added 'Public' to N user(s)`, tagged counts, `All checks passed`, `Deleted N duplicate row(s)` or `No duplicates found`. A `FAIL` line from the verify command goes to Ryan before the Apache restart.

3.12

Rewrite the four slow saved dashboard queries ([#333](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/333); [#331](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/331), [#332](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/332)). Dry run first, read the diffs, then apply. The command changes a row only when its text is exactly the original the rewrite was proven against; anything else is printed and left alone.

Copy

```
<PROD_PYTHON> manage.py rewrite_dashboard_queries --dry-run
<PROD_PYTHON> manage.py rewrite_dashboard_queries --apply      # prints the JSON backup path; keep it

# undo, if a dashboard box comes back wrong:
<PROD_PYTHON> manage.py rewrite_dashboard_queries --revert <backup.json> --apply
```

**Expected** four `REWRITE` diffs and `REVIEW` lines for Forced Phot Only / Auto Ignore / Volume-Limited. If "YSE Magnitude-Limited Sample (min mag < 18.6)" is reported `SKIP ... not the text this rewrite was proven against`, paste the diff in the thread instead of hand-editing; the old hand edit in #143 step 8 (`ISNULL(pd2.data_quality_id)` to `NOT EXISTS (...)`) is what this command carries in automatically. Every rewrite, its plan and its revert: [docs/sql-rewrites-2026-09-29.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/sql-rewrites-2026-09-29.md).

3.13

Static files.

Copy

```
<PROD_PYTHON> manage.py collectstatic --noinput
```

**Expected** `N static files copied to '/data/yse_pz/YSE_PZ/YSE_PZ/static/'`. With DEBUG really off, Django no longer serves `/static/`; the Apache `Alias /static/ /data/yse_pz/YSE_PZ/YSE_PZ/static/` must exist (`grep -n 'Alias /static/' /etc/apache2/sites-enabled/yse.conf`).

3.14

Restart Apache and smoke-check.

Copy

```
sudo apache2ctl configtest && sudo systemctl restart apache2
curl -fsS -o /dev/null -w '%{http_code}\n' https://ziggy.ucolick.org/yse/login/
curl -s https://ziggy.ucolick.org/static/YSE_App/yse-theme.css | grep -c fc-today
curl -s https://ziggy.ucolick.org/yse/this-page-does-not-exist/ | grep -c 'DEBUG = True'
```

**Expected** `Syntax OK`, `200`, `2`, `0`. Then in a browser, logged in: [/yse/personaldashboard/](https://ziggy.ucolick.org/yse/personaldashboard/) (every section fills; first load is a cold cache), [/yse/transient\_detail/2025aarm/](https://ziggy.ucolick.org/yse/transient_detail/2025aarm/) (Summary tab active, spectrum plot, light curve with the multi-column legend, "Show Bazin Fit"), `/yse/observing_calendar/`; as a non-staff user `/yse/explorer/` is refused. Any 500: `sudo tail -50 /var/log/apache2/error.log`.

3.15

Rubin/LSST ingest ([#252](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/252), [#224](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/224)): the hourly `AntaresLSST` cron needs `antares-client` in the venv your crontab uses, and a crontab line.

Copy

```
<CRON_PYTHON> -m pip install "antares-client==1.2.0"       # or: -r requirements.txt / requirements-ingest.txt
cd /data/yse_pz/YSE_PZ && <CRON_PYTHON> manage.py runcrons YSE_App.data_ingest.Query_LSST.AntaresLSST --force
```

**Expected** the forced run ends without a traceback (with no package it prints `antares_client is not installed in this environment; nothing to do` and exits cleanly). Then add to the same user's crontab (`crontab -e`); if an existing line already runs a bare `manage.py runcrons` at least hourly, the new cron is covered because it is in `CRON_CLASSES`:

Copy

```
7 * * * * cd /data/yse_pz/YSE_PZ && <CRON_PYTHON> manage.py runcrons YSE_App.data_ingest.Query_LSST.AntaresLSST >> /data/yse_pz/logs/antares_lsst.log 2>&1
```

**Check after an hour** LSST points appear as `LSSTCam` ugrizy on the light curves of active transients south of dec +32; the `django_cron_cronjoblog` rows for `YSE_App.data_ingest.Query_LSST.AntaresLSST` show success. Three cron codes were renamed in #194 (`QUB_data.YSE_Weekly`, `SDSS_Photo_Z.YSE`, `ZTF_Forced_Phot_Cron.ForcedPhot`), so each runs once at the first `runcrons` after the deploy. Docs: [docs/rubin-antares-ingest.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/rubin-antares-ingest.md).

3.16

Optional, once 3.12 is applied and the dashboard is confirmed correct: turn the cache warmer on so no browser ever triggers a cold run. In `settings.ini` set `DASHBOARD_CACHE_WARM_ENABLED: True` and `EXPLORER_QUERY_CACHE_SECONDS: 7200`, then `sudo systemctl restart apache2`. The warmer runs from the existing `runcrons` schedule. Without `REDIS_URL` (3.17) it warms only the cron process's own cache.

3.17

### Enable the shared cache (optional, per stack)

With [#339](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/339) one saved-query result serves every Apache process and the cron, and survives restarts. Order matters: install the packages first, or the app logs a warning and silently keeps LocMem. Available on yse\_test and production only after the next promotion reaches them.

Copy

```
# 1. packages, as the venv owner (yse_test / experimental share this venv)
sudo -u $(stat -c %U /data/yse_pz/yse_test_virtual) /data/yse_pz/yse_test_virtual/bin/pip install django-redis==5.4.0 redis==5.0.8
#    production, once master has #339:
<PROD_PYTHON dir>/bin/pip install django-redis==5.4.0 redis==5.0.8

# 2. Redis itself (Debian/Ubuntu package binds 127.0.0.1 by default)
sudo apt install redis-server
redis-cli ping     # PONG

# 3. export REDIS_URL where mod_wsgi and the cron see it, one database per stack:
#    experimental redis://127.0.0.1:6379/1   test redis://127.0.0.1:6379/2   production redis://127.0.0.1:6379/3
#    e.g. in /etc/apache2/envvars:
export REDIS_URL=redis://127.0.0.1:6379/2
#    or per checkout, in that stack's YSE_PZ/wsgi.py before the application is created:
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6379/2')
#    and in the crontab that runs runcrons (crontab -e), so the warmer writes to the same cache:
REDIS_URL=redis://127.0.0.1:6379/2

# 4. verify the backend the app picks (yse_test shown)
cd /data/yse_pz/YSE_PZ_test && REDIS_URL=redis://127.0.0.1:6379/2 /data/yse_pz/yse_test_virtual/bin/python manage.py shell -c "from django.conf import settings; print(settings.CACHES['default']['BACKEND'])"

# 5. pick up the environment
sudo systemctl reload apache2
```

**Expected** step 4 prints `django_redis.cache.RedisCache`. `LocMemCache` plus the warning `REDIS_URL is set but no Redis cache backend is importable` means step 1 is missing for that venv. Use a different database number per stack so yse\_test and yse\_experimental never share cached results.

3.18

### Job queue and notifications (optional, per stack) nothing required

[#340](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/340) (job queue [#263](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/263), notification queue [#266](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/266); merged to experimental 18:55 UTC, migration `0012_job_queue_notifications` already on the shared database) adds a database-backed job queue and an in-app notification bell; [#347](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/347) (notification center, merged to experimental 19:38 UTC, migration `0014_notification_kind_preferences`, one nullable TEXT column, already on the shared database) adds per-kind preferences, a notification page and a daily `PruneNotifications` cron. On production `migrate` in 3.9 applies 0012 to 0030 once the promotion carrying them is on master. The existing `runcrons` run drains the queue every minute and runs the daily prune, so no crontab change is needed for any of it to work. Everything below is optional and applies to yse\_test and production only after the promotion that carries #340. Runbook: [docs/background-jobs.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/background-jobs.md) (experimental).

Copy

```
# verify: queue counts by state and the registered handler kinds (production shown; yse_test: /data/yse_pz/YSE_PZ_test + yse_test_virtual)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py run_jobs --status
```

Dedicated worker, if the once-a-minute cron drain is not enough. Pick one of the two, then set `JOB_RUNNER_CRON_ENABLED: False` under `[site_settings]` so the cron stops competing with it:

Copy

```
# a) crontab line (crontab -e), up to 50 jobs per minute
sudo mkdir -p /var/log/yse_pz && sudo chown $(id -un) /var/log/yse_pz
* * * * *  cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py run_jobs --budget 50 >> /var/log/yse_pz/run_jobs.log 2>&1

# b) or the systemd unit from docs/background-jobs.md (ExecStart runs the loop, Restart=always):
#    ExecStart=<PROD_PYTHON> /data/yse_pz/YSE_PZ/manage.py run_jobs --loop --sleep 5
sudo systemctl daemon-reload && sudo systemctl enable --now yse-run-jobs.service && systemctl status yse-run-jobs.service
```

Copy

```
# [site_settings]  (all optional; JOB_RUNNER_* defaults are listed in YSE_PZ/public_settings.ini)
JOB_RUNNER_CRON_ENABLED: False      # only when a dedicated worker (a or b) owns the queue; default True = the runcrons drain
NOTIFICATION_EMAIL_ENABLED: False   # since #347 the default is ON whenever [SMTP_provider] has real credentials; set False (or env YSE_NOTIFICATION_EMAIL=0) on a stack that must stay silent, then sudo systemctl reload apache2
NOTIFICATION_RETENTION_DAYS: 90     # read notifications older than this are pruned
NOTIFICATION_UNREAD_RETENTION_DAYS: 365
JOB_RETENTION_DAYS: 30              # finished job rows kept this long
NOTIFICATION_PRUNE_CRON_ENABLED: True
NOTIFICATION_PRUNE_CRON_MINUTES: 1440   # daily, from the existing runcrons pass
NOTIFICATION_BASE_URL: https://ziggy.ucolick.org/yse/            # production; on yse_experimental use https://ziggy.ucolick.org/yse_experimental/ so email links resolve
```

**Email default changed in #347.** `NOTIFICATION_EMAIL_ENABLED` is now on by default whenever `[SMTP_provider]` has real credentials, matching the old unconditional senders. Any stack that must stay silent (yse\_test with production-like SMTP settings, for example) needs `NOTIFICATION_EMAIL_ENABLED: False` or `YSE_NOTIFICATION_EMAIL=0` in its environment. Emails are sent by the job runner (the `runcrons` pass), so a stack without one only queues them.

Copy

```
# preview what the daily prune would delete (production shown)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py prune_notifications --dry-run
```

Smoke test from the Django shell: send yourself one notification, then reload any page and the navbar bell shows 1.

Copy

```
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py shell -c "from YSE_App.services.notify import notify; from django.contrib.auth.models import User; notify([User.objects.get(username='djones')], 'Job queue is live', '/jobs/', 'system')"
```

**Expected** `run_jobs --status` prints per-state counts and the handler kinds; the bell shows 1 after the shell call; with the crontab worker, `/var/log/yse_pz/run_jobs.log` gains a line a minute.

3.19

### Allocations and facility APIs (optional, per stack) nothing required

[#351](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/351) (allocations page [#303](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/303), facility API framework [#298](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/298); merged to experimental 20:21 UTC, migration `0016_allocations_facility_requests`, two tables and two indexes, already on the shared database) adds a staff **Allocations** page under Tools and a "Submit to facility" panel on the follow-up tab, with `generic` (HTTP POST, email or Slack webhook) and `lco` adapters. Nothing to install (`requests` and `cryptography` are pinned already). Facility calls run in the job runner, so the existing `runcrons` pass sends them. The `[secrets] credentials_key` requirement from 3.2 is unchanged: allocation credentials are encrypted rows, and on yse\_test / yse\_experimental (IS\_DEBUG True) nothing extra is needed. Runbook: [docs/facility-apis.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/facility-apis.md) (experimental).

Copy

```
# [site_settings]  (both optional; defaults shown; documented in public_settings.ini)
FACILITY_API_MODULES:                  # comma-separated extra adapter modules, e.g. mypkg.myfac; empty = built-in generic + lco
FACILITY_HTTP_TIMEOUT_SECONDS: 30      # timeout of facility HTTP calls made by the job runner
```

Only if LCO allocations are used: poll the open request groups every 10 minutes (the GENERIC adapter has nothing to poll).

Copy

```
# crontab -e, same user as the runcrons line (production shown)
*/10 * * * * cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py poll_facility_requests >> /var/log/yse_pz/poll_facility_requests.log 2>&1
```

Binding LCO, in the browser at `/allocations/new/` (staff): facility `lco`; `proposal_id` = the LCO proposal; **New credential secret** = `{"api_token": "<observe.lco.global API token>"}` (stored encrypted, never shown again); audience group `LCOGT` (the users allowed to trigger); optional hours allocated and semester dates. Requests then appear as `FacilityRequest` rows with Refresh / Cancel actions and charge hours on completion.

GENERIC allocations (facility `generic`), one of three modes chosen by `notification_type` in **Default request parameters**; status is recorded by hand with "Mark complete":

Copy

```
# api: HTTP POST of a JSON payload to endpoint_url (Authorization: token <api_token> when the secret has one)
#   Endpoint URL: https://facility.example.edu/api/requests/
#   New credential secret:       {"api_token": "<facility token>"}
#   Default request parameters:  {"notification_type": "api", "payload_template": {"target": "{transient_name}", "ra": "{ra}", "dec": "{dec}", "pi": "{requester}", "exptime": "{param_exposure_time}"}}

# email: the payload goes to a recipient list
#   Default request parameters:  {"notification_type": "email", "recipients": ["too@observatory.edu", "pi@ucsc.edu"]}

# slack: an incoming webhook
#   New credential secret:       {"slack_webhook_url": "https://hooks.slack.com/services/<...>"}
#   Default request parameters:  {"notification_type": "slack"}
```

**Expected** a submitted GENERIC request shows state `submitted` with the email body or Slack text archived in its log; an LCO request moves `queued` to `submitted`/`accepted` with a portal link after the next runcrons pass, and to `complete` after a poll. Template placeholders: `{transient_name}`, `{ra}`, `{dec}`, `{requester}`, `{proposal_id}`, `{telescope}`, `{instrument}`, `{allocation_name}`, `{request_id}`, `{param_<field>}`.

3.20

### Broker candidate ingest (optional, per stack) nothing required

[#352](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/352) (broker plugins and a candidates scanning page; merged to experimental 20:32 UTC, migration `0017_broker_candidates` already on the shared database, no pip step) adds Fink, ALeRCE and ANTARES providers behind one `BrokerFilter` model. Nothing runs until a filter exists and the ingest cron is switched on; Fink, ALeRCE and ANTARES public search need no credentials (ANTARES shows as unavailable where `antares-client` is missing from the venv, see 3.15). Runbook: [docs/brokers.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/brokers.md) (experimental).

Copy

```
# [brokers]  (all optional; defaults shown)
enabled: antares,fink,alerce
INGEST_CRON_ENABLED: False          # True = the existing runcrons pass enqueues broker polls (env: YSE_BROKER_INGEST_CRON=1)
INGEST_CRON_MINUTES: 60
HTTP_TIMEOUT_SECONDS: 30
MATCH_RADIUS_ARCSEC: 2.0            # candidate-to-transient cross-match radius
AUTO_SAVE_USERNAME: admin           # owner of transients auto-saved from a filter
CANDIDATES_PAGE_SIZE: 50
# FINK_API_URL: ...              # ALERCE_API_URL: ...              # PROVIDER_MODULES: mypkg.mybroker
```

To start polling, in order:

Copy

```
# 1. create a BrokerFilter in the Django admin (/admin/), for example:
#    broker:   fink
#    query:    {"classes": ["SN candidate", "Early SN Ia candidate"], "days": 2}
#    criteria: {"mag_max": 19.5, "drb_min": 0.9, "gal_lat_min": 10, "age_max_days": 5, "positive_only": true}

# 2. run it once by hand (production shown; yse_test: /data/yse_pz/YSE_PZ_test + yse_test_virtual)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py brokers                                   # lists providers and availability
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py broker_poll --broker fink --dry-run
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py broker_poll --broker fink

# 3. then set  [brokers] INGEST_CRON_ENABLED: True  in settings.ini and  sudo systemctl reload apache2
```

**Expected** `brokers` lists antares / fink / alerce with availability; the dry run prints the candidates the filter would create without writing; the real run fills the Candidates page (Tools menu) and cross-matches within `MATCH_RADIUS_ARCSEC`. With the cron on, polls are enqueued every `INGEST_CRON_MINUTES` from the existing `runcrons` line and run in the job runner.

3.21

### TNS sharing service required after the #354 promotion

Same as 1.5, in the production checkout, once the promotion carrying [#354](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/354) and migration 0018 is on master and 3.9 has applied it. Prerequisite: the `[secrets] credentials_key` from 3.2 (production runs with IS\_DEBUG False). Each bot once; start in sandbox, verify one report, then switch to production. Optional `[sharing]` keys as in 1.5. Runbook: [docs/tns-sharing.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/tns-sharing.md).

Copy

```
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py create_tns_sharing_service --slug yse-tns --name "YSE TNS bot" --group-id <TNS reporting group id> --group-name YSE --from-settings --coauthors "R. J. Foley (UCSC), D. O. Jones (Hawaii), ..."
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py create_tns_sharing_service --slug decam --name "DECam TNS bot" --group-id <DECam TNS group id> --decam
# after a checked sandbox report from the submission page:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py create_tns_sharing_service --slug yse-tns --production
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py create_tns_sharing_service --slug decam --production
```

**Expected** each command prints the created or updated service and credential; the Submit to TNS dialog then lists the bots, marked "(PRODUCTION)" once switched. Until this runs, TNS reporting is unavailable on production.

3.22

### Analysis services required after the #360 promotion

Same as 1.6, in the production checkout, once the promotion carrying [#360](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/360) and migration 0020 is on master and 3.9 has applied it. The web user's home must be writable (or pre-seed the SALT3 cache as that user) and `MEDIA_ROOT/service_runs/` writable by Apache and the cron user. Optional keys as in 1.6. Runbook: [docs/analysis-services.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/analysis-services.md).

Copy

```
# 1. register the two built-in services (idempotent; audience groups and caps editable later in admin under External services / Analysis services)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_analysis_service --builtin sncosmo_fit --cap 20
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_analysis_service --builtin bazin_fit

# 2. SALT3 model cache: sncosmo downloads SALT3 on first use into ~/.astropy/cache/sncosmo of the web / worker user,
#    so that user needs a writable home and outbound HTTPS from Ziggy; or pre-seed the cache as that user:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> -c "import sncosmo; sncosmo.get_source('salt3')"

# 3. MEDIA_ROOT/service_runs/ must be writable by the web process AND by whatever runs manage.py run_jobs / the cron pass
#    (MEDIA_ROOT = <checkout>/media unless overridden; files are served by an access-checked view, not MEDIA_URL)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> -c "from django.conf import settings; print(settings.MEDIA_ROOT)" 2>/dev/null || true
mkdir -p <MEDIA_ROOT>/service_runs && sudo chgrp www-data <MEDIA_ROOT>/service_runs && sudo chmod 2775 <MEDIA_ROOT>/service_runs

# optional cleanup, e.g. monthly from the crontab:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py expire_service_runs --days 90
```

**Expected** as in 1.6. Until step 1 runs, the analysis panel on the detail page lists no services and the Summary tab's SALT3 line cannot Refit. `has_jwst` (0028) fills in as detail pages are opened; no backfill.

3.23

### Annotation services required after the #366 promotion

Same as 1.7, in the production checkout, once the promotion carrying [#366](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/366) and migration 0021 is on master and 3.9 has applied it. The job runner (3.18) must be running and the host needs outbound HTTPS to the Gaia and VizieR TAP endpoints. Optional keys as in 1.7. Runbook: [docs/annotations.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/annotations.md).

Copy

```
# 1. register the three annotation services (idempotent): ExternalService rows gaia_dr3, wise, quasar of kind annotation, enabled
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_annotation_services
#    flags: --disabled (create them switched off), --user <username> (owner). Restrict, cap or disable later in admin under External services;
#    adding a Group to a service lets that group's members run it, otherwise staff only.

# 2. network: the web and job-runner host needs outbound HTTPS to both TAP endpoints
curl -sS -o /dev/null -w 'gaia   %{http_code}\n' https://gea.esac.esa.int/tap-server/tap/capabilities
curl -sS -o /dev/null -w 'vizier %{http_code}\n' https://tapvizier.cds.unistra.fr/TAPVizieR/tap/capabilities
```

**Expected** as in 1.7. Until step 1 runs, the Annotations panel offers no checks.

3.24

### Instrument logs, weather widget and SkyCam (optional, per stack) nothing required

Same as 1.8, on production once the promotion carrying [#367](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/367) and migration 0022 is on master and 3.9 has applied it. Configuration lives on the telescope and allocation rows (shared database on the test stacks, separate on production, so repeat the admin edits there). Runbook: [docs/instrument-logs-weather.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/instrument-logs-weather.md).

Copy

```
# 1. Admin > Telescopes > edit a telescope > "Weather widget and SkyCam" fieldset (no key needed for Open-Meteo):
#    weather_url:  https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,precipitation,weather_code,surface_pressure,dew_point_2m&wind_speed_unit=ms
#    or OpenWeatherMap: https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid=<OWM key>
#    weather_link: the observatory's own weather page      skycam_url: the all-sky camera image URL

# 2. facility instrument logs: on a GENERIC-adapter allocation (3.19) add to Default request parameters
#    {"instrument_log_url": "https://facility.example.edu/logs?from={start}&to={end}&inst={instrument}&tel={telescope}"}
#    (api_token in its credential if the facility needs one), then "Pull from the facility" on the page, or wait for the cron.

# 3. facility bots that push logs: a user with "Can add instrument log", a DRF token, then POST /api/instrumentlogs/
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py drf_create_token <bot username>
curl -sS -X POST https://ziggy.ucolick.org/yse/api/instrumentlogs/ -H "Authorization: Token <token>" -H "Content-Type: application/json" -d '{"telescope": <id>, "instrument": <id>, "start": "2026-09-29T03:00:00Z", "end": "2026-09-29T12:00:00Z", "body": "test entry"}'

# 4. egress: the web / job-runner host needs outbound HTTPS to the weather endpoint and the SkyCam host
curl -sS -o /dev/null -w 'open-meteo %{http_code}\n' "https://api.open-meteo.com/v1/forecast?latitude=36.97&longitude=-122.03&current=temperature_2m"
```

Copy

```
# [observatory]  (all optional; defaults shown; env overrides YSE_WEATHER_REFRESH_CRON=1 / YSE_INSTRUMENT_LOG_PULL_CRON=1)
WEATHER_REFRESH_CRON_ENABLED: True
WEATHER_REFRESH_CRON_MINUTES: 10
INSTRUMENT_LOG_PULL_CRON_ENABLED: True
INSTRUMENT_LOG_PULL_CRON_MINUTES: 60
INSTRUMENT_LOG_PULL_HOURS: 24            # window pulled on each pass
WEATHER_CACHE_MINUTES: 10
WEATHER_HTTP_TIMEOUT_SECONDS: 10
SKYCAM_REFRESH_SECONDS: 300
WEATHER_WIDGET_REFRESH_SECONDS: 600
```

3.25

### Other feeds: Hermes, Einstein Probe, JPL Scout (optional, per stack) nothing required

Same as 1.9, on production once the promotion carrying [#374](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/374) and migration 0023 is on master and 3.9 has applied it. Feed sources and credentials live in the database, so repeat the admin steps there; the `[feeds]` keys go in production `settings.ini`. Docs: [docs/feeds-hermes.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-hermes.md), [docs/feeds-einstein-probe.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-einstein-probe.md), [docs/feeds-jpl-scout.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-jpl-scout.md).

Copy

```
# (a) Hermes feed (SCiMMA). Admin > Encrypted credentials > Add: kind Hermes, service hermes, secret {"hermes_token": "<token>"}
#     Admin > Feed sources (or /feeds/ > Add source): kind Hermes, topic e.g. hermes.discovery; note the slug, then:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py feeds --poll <slug> --dry-run
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py feeds --poll <slug>

# (b) Hermes publishing: on the Hermes SharingService (1.5) set hermes_topic and the token credential;
#     leave "testing" on (publishes to hermes.test) until a message looks right, then untick it.

# (c) Einstein Probe. Either a Feed source of kind Einstein Probe with config {"url": "<notice mirror JSON>"} (polled like Hermes),
#     or the GCN Kafka consumer: pip, a credential with client_id / client_secret, and a consumer loop
<PROD_PYTHON dir>/bin/pip install gcn-kafka
while true; do cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py feeds --consume ep --timeout 300; sleep 5; done

# (d) JPL Scout + minor-planet screening. Feed source kind JPL Scout, config e.g. {"min_neo_score": 50}; then in YSE_PZ/settings.ini:
#     [feeds]
#     POLL_CRON_ENABLED = True
#     MPC_SCREEN_ON_CREATE = True
#     MPC_SCREEN_CRON_ENABLED = True
#     MPC_SCREEN_OBS_CODE = F51
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py feeds --screen <transient name>     # one-off check; Ziggy must reach ssd-api.jpl.nasa.gov
sudo systemctl restart apache2

# (e) only if the Hopskotch Kafka consumer is wanted instead of HTTP polling:
<PROD_PYTHON dir>/bin/pip install hop-client
```

3.26

### AI summary service required after the #373 promotion

Same as 1.10, in the production checkout, once the promotion carrying [#373](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/373) and migration 0024 is on master and 3.9 has applied it. An LLM credential on production needs the `[secrets] credentials_key` from 3.2. Runbook: [docs/ai-summaries.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/ai-summaries.md).

Copy

```
# REQUIRED once per database: creates the ai_summary service with the built-in template provider (no key, no network)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_summary_service
#    variants: --group YSE (only that group may generate), --cap 20 (runs per user per day), --disabled

# OPTIONAL, real LLM: admin > Encrypted credentials > Add: kind Generic, service llm, name e.g. "Anthropic key", secret {"api_key": "..."}; then EITHER
#    [llm]
#    LLM_PROVIDER: anthropic          # or openai, with LLM_MODEL and LLM_API_BASE
#    in settings.ini and  sudo systemctl restart apache2,  OR bind it on the service:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_summary_service --provider anthropic --credential "Anthropic key"

# OPTIONAL: nightly batch (needs the job runner / RunQueuedJobs cron, 3.18):  [llm] SUMMARY_BATCH_CRON_ENABLED: True
# after changing the embedder:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_summary_service --rebuild-embeddings
```

**Expected** `ready`. Until it runs, the Summary card offers no Generate control on production.

3.27

### Favourites and Slack DM notifications (optional, per stack) nothing required

Same as 1.11, on production once the promotion carrying [#375](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/375) and migration 0025 is on master and 3.9 has applied it. Docs: [docs/favorites-and-notifications.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/favorites-and-notifications.md).

Copy

```
# (a) batching window for favourite-activity notifications, in /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini
#     [site_settings]
FAVORITE_ACTIVITY_BATCH_MINUTES: 60     # 0 = one notification per event
sudo systemctl restart apache2

# (b) Slack DMs: in the YSE-PZ Slack app (api.slack.com > Your apps) add the bot scopes
#     users:read.email   chat:write   im:write
#     then "Reinstall to workspace", and make sure the web environment exports the bot token:
#     /etc/apache2/envvars (or that checkout's YSE_PZ/wsgi.py):
export SLACK_BOT_TOKEN=<xoxb-...>
sudo systemctl restart apache2
```

3.28

### Facility queue, adapters and accounting (optional, per stack) nothing required

Same as 1.12, on production once the promotion carrying [#376](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/376) and migration 0026 is on master and 3.9 has applied it. Facility credentials on production need the `[secrets] credentials_key` from 3.2. Docs: [docs/facility-apis.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/facility-apis.md).

Copy

```
# sanity check: the migration is on this database
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py showmigrations YSE_App | tail -3        # ends with [X] 0026_facility_queue_accounting

# (1) polling of submitted requests, once allocations exist: in /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini
#     [site_settings]
FACILITY_POLL_CRON_ENABLED: True        # queues a facility.poll job every FACILITY_POLL_CRON_MINUTES (10), run by manage.py run_jobs (3.18)
#     or by hand:
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py poll_facility_requests

# (2) seed the per-resource usage counters from existing follow-ups (idempotent)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py backfill_resource_usage            # preview
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py backfill_resource_usage --apply

# (3) bind facilities as staff at /allocations/ (or admin > Allocations), then set "allocation" on the matching ToO / classical resource:
#     LCO    facility lco,   proposal_id,  credential {"api_token": "<portal token>"}
#     SOAR   facility soar,  an LCO token as above
#     ZTF    facility ztf   (forced photometry), credential {"email": "...", "userpass": "..."}
#     ATLAS  facility atlas, credential {"api_token": "..."}
#     Swift  credential {"username": "...", "shared_secret": "..."}; requests stay dry-run until default_request_params has {"debug": false}

# defaults, all optional under [site_settings]:
#     FACILITY_SUBMIT_MAX_ATTEMPTS 3   FACILITY_SUBMIT_BACKOFF_SECONDS 120   FACILITY_POLL_CRON_MINUTES 10   FACILITY_POLL_LIMIT 200
#     FACILITY_NOTIFY_GROUPS False     FACILITY_HTTP_TIMEOUT_SECONDS 30      LT_RTML_HOST / LT_RTML_PORT (Liverpool Telescope)
```

3.29

### Galactic coordinates (optional post-migrate check) nothing required

After 3.9 has applied 0027 ([#384](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/384)): confirm the backfill left nothing behind, and optionally EXPLAIN the acceptance search from [docs/transient-search.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/transient-search.md) to check the new indexes are used.

Copy

```
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py backfill_galactic_coords          # expected: 0 rows to fill
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py backfill_galactic_coords --all    # only to recompute everything
# optional: EXPLAIN the acceptance search (paste the SQL from docs/transient-search.md)
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> <PROD_DB> -e "EXPLAIN FORMAT=TREE <acceptance search SQL>\G"
```

3.30

### NGSF classification (optional, per stack) nothing required

Same as 1.14, on production once the promotion carrying [#385](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/385) and migration 0028 is on master and 3.9 has applied it. NGSF must be installed on the host that runs production's job runner. Docs: [docs/ngsf.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/ngsf.md).

Copy

```
# 1. install NGSF and its template bank where the job runner runs (paths are examples; see docs/ngsf.md)
#    e.g. git clone https://github.com/oyaron/NGSF /opt/NGSF ; python3 -m venv /opt/ngsf-venv ; /opt/ngsf-venv/bin/pip install -r /opt/NGSF/requirements.txt ; template bank into /opt/NGSF/bank
#    and put a base parameters.json in that directory

# 2. in /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini
#    [site_settings]
NGSF_COMMAND: /opt/ngsf-venv/bin/python /opt/NGSF/run.py
NGSF_HOME: /opt/NGSF
NGSF_SUBPROCESS_TIMEOUT: 1800          # optional, seconds

# 3. register the service (idempotent)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py register_analysis_service --builtin ngsf --cap 10 --timeout 1800
```

3.31

### Broker alert streams (optional, per stack) nothing required

Same as 1.15, on production once the promotion carrying [#386](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/386) and migration 0029 is on master and 3.9 has applied it. Use the venv the production consumer will run from (the cron venv is shown); broker credentials need the `[secrets] credentials_key` from 3.2. Docs: [docs/broker-streams.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/broker-streams.md).

Copy

```
# 1. packages in the venv the ingest runs from (confluent-kafka is already pinned)
<CRON_PYTHON> -m pip install fastavro            # Fink Avro topics
<CRON_PYTHON> -m pip install antares-client      # only for ANTARES streams

# 2. admin > Encrypted credentials, only if the broker needs one:
#    Lasair REST   service lasair, secret {"token": "..."}
#    SASL Kafka    secret {"username": "...", "password": "..."}
#    ANTARES       secret {"api_key": "...", "api_secret": "..."}

# 3. admin > Broker connections > Add: broker, kind kafka or antares, bootstrap servers
#    (e.g. kafka-ztf.fink-broker.org:24499 or kafka.lsst.ac.uk:9092), topics as a JSON list, group id, format json or avro;
#    leave "enabled" OFF for now. Then create BrokerFilters (optionally topics, default_tags, notify_group).

# 4. smoke test without writing anything
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py broker_ingest --connection <slug> --force --dry-run --max-messages 50 --timeout 60

# 5. enable the connection in the admin, then run the consumer under the systemd unit from docs/broker-streams.md
#    (ExecStart runs this; or: docker compose --profile brokers up)
cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py broker_ingest --connection <slug> --workers 2
```

Copy

```
# [brokers]  (optional; defaults shown)
STREAM_STALE_MINUTES: 15          # dashboard banner when a heartbeat is older than this
STREAM_BATCH_SIZE: 100
DETAIL_RADIUS_ARCSEC: 5
# LASAIR_API_URL: ...
```

R

Rollback (code first; the database restore is required once 3.9 has run).

Copy

```
cd /data/yse_pz/YSE_PZ && git checkout dc70630 && <PROD_PYTHON> manage.py collectstatic --noinput && sudo systemctl restart apache2
gunzip -c /data/yse_pz/backups/<PROD_DB>_pre-0004-0010_<DATE>.sql.gz | mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> <PROD_DB>
```

<a id="data"></a>

## 4 · Data checks David

Read-only queries against the production database. Post the output in the Slack thread.

4.1

Spectra with no points ([#260](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/260)). On the shared test database, 2026fov (1 spectrum) and 2024pxl (17 spectra) have `TransientSpectrum` rows with zero `TransientSpecData` rows, so the plot pane says "no wavelength/flux points". The question is whether production has the points.

Copy

```
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> <PROD_DB> -e "
SELECT s.id, s.obs_date, i.name, COUNT(d.id) AS n_points
FROM YSE_App_transientspectrum s
JOIN YSE_App_instrument i ON i.id = s.instrument_id
LEFT JOIN YSE_App_transientspecdata d ON d.spectrum_id = s.id
WHERE s.transient_id IN (SELECT id FROM YSE_App_transient WHERE name IN ('2026fov','2024pxl'))
GROUP BY s.id, s.obs_date, i.name;"
```

**Reading it** `n_points > 0` on production means the test database was loaded without the spec-data table (a copy problem, not a code bug); `n_points = 0` means the spectra were uploaded without data and the new "Points" column in the Spectra tab is telling the truth. The transient ids on the test database were 217461 and 168401; the query above looks them up by name in case production ids differ.

4.2

Audit-log and cron-log table sizes, so a retention policy can be set (cron logs are pruned after 30 days since [#222](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/222); audit-log retention is still Ryan's call).

Copy

```
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> <PROD_DB> -e "SELECT table_name, table_rows, ROUND((data_length+index_length)/1024/1024) AS mb FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name IN ('auditlog_logentry','django_cron_cronjoblog');"
```

4.3

Server-side checks carried over from the runbook Part D: `REDIS_URL` must be unset on every stack until 3.17 is done there, and the experimental static alias must exist ([#149](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/149)).

Copy

```
grep -rn REDIS_URL /etc/apache2/ /data/yse_pz/*/YSE_PZ/wsgi.py 2>/dev/null; echo "exit=$?"
grep -n 'Alias /experimental_static/' /etc/apache2/sites-enabled/yse-experimental.conf
grep -h python-home /etc/apache2/sites-enabled/yse-experimental.conf
```

**Expected** no REDIS\_URL match (`exit=1`); one Alias line pointing at `/data/yse_pz/YSE_PZ_experimental/YSE_PZ/static/`; and, for [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187), the third line tells us which venv the experimental vhost really runs (see 5).

<a id="pending"></a>

## 5 · Pending items

Everything still waiting on David, with its source. Rows change status here as the thread reports progress.

| Item | Why | Source | Status |
| --- | --- | --- | --- |
| Merge #143 (develop to master) with a merge commit | Closes 66 issues and puts the September work on master; only David merges it | [PR #143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143) | pending |
| Production `settings.ini`: real `SECRET_KEY` (required), optional keys, `[antares]` | Startup fails with IS\_DEBUG False and a placeholder key | [#253](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/253) · [3.2](#prod) | pending |
| Production EXPLAIN before migrating; post the output | Go/no-go evidence for the 8 indexes in 0010; asked for in #248 | [#248](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/248) · [docs/dashboard-performance.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/dashboard-performance.md) · [3.3](#prod) | pending |
| Production deploy: backup, pull, pip, check, migrate 0004-0010, backfills, collectstatic, Apache | Hand deploy; nothing fires from the master merge | [PR #143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143) · [3.4-3.14](#prod) | pending |
| `rewrite_dashboard_queries --dry-run` then `--apply` on production | The 3-5 minute dashboard sections; result-identical rewrites with a revert path | [#333](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/333) · [docs/sql-rewrites-2026-09-29.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/sql-rewrites-2026-09-29.md) · [3.12](#prod) | pending |
| **Required before production deploy:** `[secrets] credentials_key` in production settings.ini (`generate_credentials_key`), one key per stack, backed up outside the DB | With IS\_DEBUG False the site refuses to start without it and stored credentials are unreadable; not needed on yse\_test / yse\_experimental (IS\_DEBUG True) | [#342](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/342) · [#264](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/264) · [docs/credentials-and-external-services.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/credentials-and-external-services.md) · [3.2](#prod) | pending |
| **yse\_experimental:** `rebuild_photstats` from the experimental checkout (fills the shared YSE\_test DB; `--stale-only` if it already ran before #350); later on production after the promotion carrying #341/#350 | PhotStat rows exist only for transients with new photometry until the backfill runs; Peak Mag and the detail-page block are empty until then. **reported done by David 2026-09-29 23:44 UTC (not independently verified)**; the production mirror stays pending | [#341](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/341) · [#268](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/268) · [docs/photstat.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/photstat.md) · [1.2](#now) / [3.10](#prod) | reported done 23:44 UTC |
| **yse\_experimental, once per bot:** `create_tns_sharing_service` (YSE and DECam bots), sandbox check, then `--production`; later on production after the #354 promotion (needs credentials\_key) | TNS reports now go through an encrypted SharingService; without one nothing can be reported, and a new service posts to the sandbox until switched. **reported done by David 2026-09-29 23:44 UTC (not independently verified)**; the production mirror stays pending | [#354](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/354) · [docs/tns-sharing.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/tns-sharing.md) · [1.5](#now) / [3.21](#prod) | reported done 23:44 UTC |
| **yse\_experimental, once per stack:** `register_analysis_service --builtin sncosmo_fit --cap 20` and `--builtin bazin_fit`, SALT3 cache for the web user, writable `MEDIA_ROOT/service_runs/`; yse\_test after promotion, production after master | External fitters run as analysis services through the job queue; without registration the panel is empty, without the cache SALT3 fails, without the directory runs cannot store files. **reported done by David 2026-09-29 23:44 UTC (not independently verified)**; the production mirror stays pending | [#360](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/360) · [docs/analysis-services.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/analysis-services.md) · [1.6](#now) / [3.22](#prod) | reported done 23:44 UTC |
| **yse\_experimental, once per stack:** `register_annotation_services` (gaia\_dr3, wise, quasar) and outbound HTTPS to gea.esac.esa.int and tapvizier.cds.unistra.fr; yse\_test after promotion, production after master | Gaia / WISE / quasar checks are external services run by the job runner; without registration the Annotations panel offers no checks, without egress they fail. **reported done by David 2026-09-29 23:44 UTC (not independently verified)**; the production mirror stays pending | [#366](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/366) · [docs/annotations.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/annotations.md) · [1.7](#now) / [3.23](#prod) | reported done 23:44 UTC |
| **yse\_experimental, once per database:** `register_summary_service` (template provider, no key); later on production after the #373 promotion | Until it runs the Summary card has no Generate control; optional LLM credential and `[llm]` keys are opt-in. **reported done by David 2026-09-29 23:50 UTC (not independently verified)**; the production mirror stays pending | [#373](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/373) · [docs/ai-summaries.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/ai-summaries.md) · [1.10](#now) / [3.26](#prod) | reported done 23:50 UTC |
| `antares-client` in the cron venv + hourly `AntaresLSST` crontab line | Without it the Rubin/LSST import silently does nothing | [#252](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/252) · [docs/rubin-antares-ingest.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/develop/docs/rubin-antares-ingest.md) · [3.15](#prod) | pending |
| #260 spectra SQL against production (2026fov, 2024pxl) | Decides whether the empty spectrum panes are a data-copy problem or real | [#260](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/260) · [4.1](#data) | pending |
| #187: experimental vhost `python-home` vs the venv deploy.yml installs into | deploy.yml pip-installs into `YSE_PZ_experimental/venv`, but the 2026-09-22 error page showed Apache running `yse_test_virtual`; requirement changes never reach the running app. Fix: point `yse-experimental.conf` at `/data/yse_pz/YSE_PZ_experimental/venv` (then `sudo apache2ctl configtest && sudo systemctl reload apache2`) or tell us which venv to target | [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187) · [4.3](#data) | pending |
| Runbook Part D: REDIS\_URL not set yet, `/experimental_static/` Alias, log-table sizes | One-time server checks never reported back | [runbook](https://claude.ai/artifact/Epp8VK8jZm2VqNBDBLUj7c) · [#149](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/149) · [4.2-4.3](#data) | pending |
| Cache warmer on (`DASHBOARD_CACHE_WARM_ENABLED`), `EXPLORER_QUERY_MAX_EXECUTION_MS: 20000` | Only after 3.12 is applied and measured | [#233](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/233) · [3.16](#prod) | after deploy |
| `REDIS_URL` shared cache: django-redis + redis-server, one DB per stack | #339 makes it safe on Django 3.2; pip step first, then REDIS\_URL; reaches yse\_test/production with the next promotion | [#339](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/339) · [#338](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/338) · [3.17](#prod) | after promotion |
| #210: plot endpoints now require login (on experimental) | Nothing to run; verify after promotion that light-curve and spectrum plots still load when logged in and that a logged-out request is redirected to login | [#210](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/210) | verify after promotion |
| #340 / #347 job queue and notification center: optional worker line, email and retention settings (after promotion) | The runcrons drain already works the queue each minute; a dedicated worker, `NOTIFICATION_EMAIL_ENABLED` and `NOTIFICATION_BASE_URL` are opt-in; email now defaults ON when [SMTP\_provider] is real | [#340](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/340) · [#347](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/347) · [docs/background-jobs.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/background-jobs.md) · [3.18](#prod) | optional |
| #351 allocations and facility APIs: optional settings, LCO poll crontab line, binding LCO / GENERIC allocations (after promotion) | Nothing to install; facility calls run in the job runner; only needed once an allocation is created | [#351](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/351) · [docs/facility-apis.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/facility-apis.md) · [3.19](#prod) | optional |
| #352 broker candidate ingest: `[brokers]` keys, a BrokerFilter, `broker_poll`, then `INGEST_CRON_ENABLED: True` (after promotion) | Off by default; no pip step, no credentials for Fink / ALeRCE / ANTARES public search | [#352](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/352) · [docs/brokers.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/brokers.md) · [3.20](#prod) | optional |
| #367 instrument logs, weather widget, SkyCam: per-telescope URLs in the admin, `instrument_log_url` on GENERIC allocations, `[observatory]` keys, bot tokens (after promotion) | Off until a telescope or allocation is configured; crons run from the existing runcrons pass; needs egress to the weather and SkyCam hosts | [#367](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/367) · [docs/instrument-logs-weather.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/instrument-logs-weather.md) · [1.8](#now) / [3.24](#prod) | optional |
| #374 other feeds: Hermes credential + source, Hermes publishing topic, Einstein Probe source or gcn-kafka consumer, JPL Scout source + `[feeds]` screening keys (after promotion) | Off until a source or the keys exist; optional pip (gcn-kafka, hop-client) only for the Kafka consumers; needs egress to hermes.lco.global / GCN / ssd-api.jpl.nasa.gov | [#374](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/374) · [docs/feeds-hermes.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-hermes.md), [docs/feeds-einstein-probe.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-einstein-probe.md), [docs/feeds-jpl-scout.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/feeds-jpl-scout.md) · [1.9](#now) / [3.25](#prod) | optional |
| #375 favourites + Slack DM notifications: `FAVORITE_ACTIVITY_BATCH_MINUTES`, Slack app scopes + `SLACK_BOT_TOKEN` (after promotion) | In-app notifications work without it; DMs need the scopes and token | [#375](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/375) · [docs/favorites-and-notifications.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/favorites-and-notifications.md) · [1.11](#now) / [3.27](#prod) | optional |
| #376 facility queue / adapters / accounting: `FACILITY_POLL_CRON_ENABLED`, `backfill_resource_usage --apply`, binding LCO / SOAR / ZTF / ATLAS / Swift allocations (after promotion) | Off until an allocation is bound to a facility; polling and accounting are opt-in | [#376](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/376) · [docs/facility-apis.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/facility-apis.md) · [1.12](#now) / [3.28](#prod) | optional |
| #384 galactic coordinates: `backfill_galactic_coords` check (expect 0), optional EXPLAIN of the acceptance search (after promotion) | The migration itself backfills every transient (~6 min on YSE\_test); save() keeps the columns current | [#384](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/384) · [docs/transient-search.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/transient-search.md) · [1.13](#now) / [3.29](#prod) | optional |
| #385 NGSF classification: install NGSF + template bank on the job-runner host, `NGSF_COMMAND` / `NGSF_HOME`, `register_analysis_service --builtin ngsf` (after promotion) | Summary tab says "NGSF is not installed on this server" until then; the SALT3 Refit line needs only the already-registered sncosmo\_fit service | [#385](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/385) · [docs/ngsf.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/ngsf.md) · [1.14](#now) / [3.30](#prod) | optional |
| #386 broker alert streams: fastavro / antares-client in the ingest venv, credential, Broker connection + filters, `broker_ingest --dry-run` smoke test, consumer under systemd (after promotion) | Off by default; nothing consumes until a connection is enabled and a consumer runs; stale-stream banner reads the heartbeats | [#386](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/386) · [docs/broker-streams.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/broker-streams.md) · [1.15](#now) / [3.31](#prod) | optional |
| Runbook Part A (yse\_test on develop, dedupe, chown) | Deploy green 07:34; dedupe run 07:37; Ryan confirmed 07:41 | [runbook](https://claude.ai/artifact/Epp8VK8jZm2VqNBDBLUj7c) | done |
| Migration 0010 on the shared YSE\_test database | Applied by the experimental deploy of #333, 17:23-17:42 UTC | [run 36604581592](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36604581592) | done |

<a id="changelog"></a>

## 6 · Changelog

* 2026-10-06 v26, **required**. ZTF forced phot `FileNotFoundError` on yse_test ([#411](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/411), [#412](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/412)): check that the Apache user can write `<ZTFTMPDIR>/forced_phot_out` and run `wget` (steps above section 1). #412 creates the directory if it is missing and reports wget's own error.
* 2026-10-02 08:00 UTC v25, **required** (yse_test checkout ownership). David's 10/1 list fixed on experimental: [#402](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/402), [#403](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/403), [#404](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/404) (migration `0030_shared_db_column_defaults`, applied to `YSE_test` 07:13 UTC; four `ALTER TABLE ... MODIFY`, seconds), [#405](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/405), [#406](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/406). Production gets 0030 with the other migrations at 3.9. Ryan merged #362 at 07:40 UTC; its yse_test deploy failed on checkout ownership, so a **REQUIRED** `chown` step was added above section 1.
* 2026-10-02 00:35 UTC v24 (Markdown copy only), informational. [#389](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/389) (JWST tab), [#392](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/392) (backport of develop 82b2580) and [#393](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/393) (docs) merged to experimental; no migrations, no dependencies: **nothing required**. Suggestion: the 82b2580 commit on `develop` is git-authored as "Dave Coulter"; check `git config user.name` / `user.email` in the Ziggy checkout you committed from.
* 2026-09-30 02:08 UTCv23, informational. #386 (broker streams; experimental 02:04 UTC as 41a5226; migration 0029 via Deploy Stack run 36658108677, schema-only): nothing required. Optional step 1.15 (fastavro / antares-client, credential kinds, a Broker connection left disabled, BrokerFilters, `broker_ingest --dry-run` smoke test, consumer under systemd, `[brokers]` stream keys), mirrored as production 3.31; 1.1 lists 0029; 3.18 says 0012-0029; an optional pending row.
* 2026-09-30 01:58 UTCv22, informational. #385 (Summary-tab refit + NGSF + has\_jwst; experimental 01:50 UTC as 918d73f; migration 0028 via Deploy Stack run 36656986210, schema-only): nothing required. The SALT3 Refit line uses the sncosmo\_fit service from 1.6 / 3.22 (note added there); optional step 1.14 NGSF (install, `NGSF_*` keys, `register_analysis_service --builtin ngsf --cap 10 --timeout 1800`), mirrored as production 3.30; 1.1 lists 0028; 3.18 says 0012-0028; an optional pending row.
* 2026-09-30 01:39 UTCv21, informational. #384 (search extras, fixes #286; migration 0027\_transient\_galactic\_coords via Deploy Stack run 36655302742, migrate step 6 min 11 s): nothing required. Optional step 1.13 (`backfill_galactic_coords` check, `--all` to recompute), production 3.9 gains a callout that 0027 is a ~6-minute data step to budget for in the maintenance window, optional post-migrate check 3.29 with an EXPLAIN of the acceptance search; 1.1 lists 0027; 3.18 says 0012-0027; an optional pending row.
* 2026-09-30 00:50 UTCv20, informational. #376 (facility queue, adapters and accounting; migration 0026 via Deploy Stack runs 36651810314 / 36651810458): nothing required. Optional step 1.12 (sanity check, `FACILITY_POLL_CRON_ENABLED` or `poll_facility_requests`, `backfill_resource_usage` preview then `--apply`, how to bind LCO / SOAR / ZTF / ATLAS / Swift allocations, the `FACILITY_*` defaults), mirrored as production 3.28; 1.1 lists 0026; 3.18 says 0012-0026; an optional pending row. Tidied the step pills of 1.2 / 1.5 / 1.6 / 1.7 to "reported done" to match their table rows.
* 2026-09-29 23:53 UTCv19, informational. #375 (favourites + Slack DM notifications, migration 0025 via Deploy Stack run 36647276426): nothing required. Optional step 1.11 (`FAVORITE_ACTIVITY_BATCH_MINUTES`, Slack app scopes users:read.email / chat:write / im:write, reinstall, `SLACK_BOT_TOKEN` in the web environment), mirrored as production 3.27; 1.1 lists 0025; 3.18 says 0012-0025; an optional pending row.
* 2026-09-29 23:51 UTCv18. David reported 1.10 (`register_summary_service`) done at 23:50 UTC, not independently verified; with 1.2, 1.5, 1.6 and 1.7 that leaves nothing required on yse\_experimental. Everything else waits for the develop-to-master merge.
* 2026-09-29 23:46 UTCv17. David reported section 1 finished at 23:44 UTC ("finished #1"; sections 2 and 3 wait for the master merge): 1.2, 1.5, 1.6 and 1.7 are marked reported done, not independently verified, and their production mirrors stay pending. #373 (AI summaries, migration 0024 via Deploy Stack run 36646344827): new **required** step 1.10 `register_summary_service` on yse\_experimental (optional LLM credential + `[llm]` keys, nightly batch, `--rebuild-embeddings`), mirrored as production 3.26; 1.1 lists 0024; 3.18 says 0012-0024; a required pending row.
* 2026-09-29 23:33 UTCv16, informational. #374 (other feeds: Hermes, Einstein Probe, JPL Scout; migration 0023\_feeds on the shared database): nothing required. Optional step 1.9 (Hermes credential + feed source + `feeds --poll`, Hermes publishing topic with testing on, Einstein Probe source or gcn-kafka consumer loop, JPL Scout source + `[feeds]` screening keys + `feeds --screen`, optional hop-client), mirrored as production 3.25; 1.1 lists 0023; 3.18 says 0012-0023; an optional pending row. Required list unchanged: 1.2, 1.5, 1.6, 1.7.
* 2026-09-29 22:22 UTCv15, informational. #367 (instrument logs, weather widget, SkyCam; experimental 22:19 UTC as 408be43, migration 0022 via Deploy Stack run 36638875730): nothing required. Optional step 1.8 (per-telescope `weather_url` / `weather_link` / `skycam_url` with Open-Meteo and OpenWeatherMap examples, `instrument_log_url` on GENERIC allocations, `drf_create_token` + `POST /api/instrumentlogs/` for facility bots, egress check, `[observatory]` keys), mirrored as production 3.24; 1.1 lists 0022; 3.18 says 0012-0022; an optional pending row.
* 2026-09-29 22:15 UTCv14. #366 (structured annotations + Gaia / WISE / quasar checks, experimental 22:09 UTC as adc52f8, migration 0021 via Deploy Stack run 36637888807): new **required** step 1.7 on yse\_experimental (`register_annotation_services`, egress check to gea.esac.esa.int and tapvizier.cds.unistra.fr, optional `ANNOTATION_*` / TAP URL keys), mirrored as production step 3.23; 1.1 lists 0021; 3.18 says 0012-0021; a required pending row; status line updated.
* 2026-09-29 21:40 UTCv13. #360 (analysis-service framework, experimental 21:35 UTC as 45c051d, migration 0020 via Deploy Stack run 36634361955): new **required** step 1.6 on yse\_experimental (`register_analysis_service` for sncosmo\_fit and bazin\_fit, SALT3 model cache for the web / worker user, writable `MEDIA_ROOT/service_runs/`, optional `expire_service_runs` and `ANALYSIS_*` keys), mirrored as production step 3.22; 1.1 lists 0020 and the placeholder is gone; 3.18 says 0012-0020; a required pending row; status line updated.
* 2026-09-29 21:30 UTCv12, correction. #361 (fixes #359, experimental 21:27 UTC, Deploy Stack run 36633470891 green): the key-rotation command in 3.2 is now `rotate_credentials_key --old=<OLD> --new=<NEW> [--dry-run]` because a Fernet key can begin with `-`; the `YSE_ROTATE_OLD_KEYS` / `YSE_ROTATE_NEW_KEY` environment alternative is noted. `generate_credentials_key` unchanged.
* 2026-09-29 21:26 UTCv11, informational. #357 (source interests + data access requests, experimental 21:23 UTC, migration 0019\_interests\_data\_access via Deploy Stack run 36632950689): nothing to run. 1.1 lists 0019 (tail -12), 3.18 says production migrate applies 0012-0019, status line updated; #360 (analysis services) will add 0020 shortly.
* 2026-09-29 21:10 UTCv10. #354 (TNS sharing service, experimental 21:04 UTC, migration 0018 via Deploy Stack run 36630779769): new **required** step 1.5 on yse\_experimental (`create_tns_sharing_service` for the YSE and DECam bots, admin alternative, sandbox-then-`--production` switch, optional service fields and `[sharing]` keys) mirrored as production step 3.21 after the promotion; 1.1 lists 0018; a required pending row; status line updated.
* 2026-09-29 20:36 UTC#352 (broker plugins + candidates page, experimental 20:32 UTC, migration 0017 on the shared database): nothing required. Added optional step 3.20 with the `[brokers]` keys, an example Fink `BrokerFilter`, `brokers` / `broker_poll --dry-run` and the `INGEST_CRON_ENABLED` switch; 1.1 lists 0017; an optional pending row.
* 2026-09-29 20:25 UTC#351 (allocations page + facility APIs, experimental 20:21 UTC, migration 0016 on the shared database): nothing required. Added optional step 3.19 with the `FACILITY_*` keys, the every-10-minutes `poll_facility_requests` crontab line for LCO, how to bind an LCO allocation at `/allocations/new/` and the GENERIC api / email / Slack examples; 1.1 lists 0016; an optional pending row.
* 2026-09-29 19:53 UTC#350 (PhotStat per-band upper limits, issue #349, experimental 19:51 UTC, migration 0015 on the shared database): amended the required step 1.2 and production 3.10: a first `rebuild_photstats` run needs no flag; if it already ran, one `--stale-only` pass refreshes outdated rows (idempotent, second run prints `processed 0`). 1.1 lists 0015. No new required item.
* 2026-09-29 19:41 UTC#347 (notification center, experimental 19:38 UTC, migration 0014 on the shared database): nothing required. Step 3.18 extended with the retention keys, the daily `PruneNotifications` cron (no crontab change; `prune_notifications --dry-run` preview) and a callout that `NOTIFICATION_EMAIL_ENABLED` now defaults ON when `[SMTP_provider]` has real credentials. 1.1 lists 0014.
* 2026-09-29 19:35 UTC#342 (encrypted credentials + external-service runs, experimental 19:29 UTC, migration 0013 on the shared database): production now needs `[secrets] credentials_key` in settings.ini before `migrate`; added to step 3.2 with `generate_credentials_key`, the env alternative, restart and rotation commands, plus a required pending row. Nothing to run on yse\_test / yse\_experimental (IS\_DEBUG True).
* 2026-09-29 18:58 UTC#340 (job queue + notification bell, experimental 18:55 UTC, migration 0012 on the shared database): nothing required. Added optional step 3.18 (`run_jobs --status`, a crontab or systemd worker with `JOB_RUNNER_CRON_ENABLED: False`, `NOTIFICATION_EMAIL_ENABLED` / `NOTIFICATION_BASE_URL`, a shell smoke test) and an "optional" pending row.
* 2026-09-29 18:46 UTCFirst **required** step for today: `rebuild_photstats` from the experimental checkout (#341 PhotStat table; migration 0011 already on the shared database since the 18:43 UTC experimental deploy), added as 1.2 and mirrored as production step 3.10 (former 3.10-3.16 are now 3.11-3.17). New pending row; status line updated.
* 2026-09-29 18:40 UTC#339 (experimental, 18:35 UTC) makes `REDIS_URL` safe on Django 3.2: replaced the "do not set REDIS\_URL" callout, added the optional shared-cache steps as 3.17 (pip first, redis-server, one Redis DB per stack, verify, reload), widened the 4.3 one-time check to `wsgi.py`. Added a #210 row (plot endpoints require login; verify after promotion). Nothing new to run on production yet.
* 2026-09-29 18:10 UTCCreated after Ryan merged #335 (17:59 UTC) and asked for a living command list for David. Covers the #143 merge, the production deploy incl. migration 0010 and the saved-query rewrite, the LSST cron, the #260 spectra check, #187 and the open runbook Part D items.

Sources: PR #143 and #335 bodies, issues #187 / #248 / #260, `docs/dashboard-performance.md`, `docs/sql-rewrites-2026-09-29.md`, `docs/rubin-antares-ingest.md` and `.github/workflows/deploy.yml` on `develop` at 5da66de, the 2026-09-29 runbook, and the plan artifact. Updated whenever there is something new for David to run.
