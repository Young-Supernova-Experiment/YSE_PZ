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

**REQUIRED (2026-10-07): no job runner on the test stacks, so SALT fits stay "queued"** ([#431](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/431)). `/yse_test/jobs/status.json` shows two `external_service.run` jobs queued at 22:14 UTC and **0 done ever**: nothing drains the job queue on the shared `YSE_test` database. SALT3/sncosmo, NGSF and Bazin fits, annotations, sharing, summaries and weather all run on that queue. Section 3.18 sets this up for production only. One runner covers **both** yse_test and yse_experimental, since they share the database. These steps use the yse_test checkout and interpreter. (The earlier 403 was most likely the service still disabled; `sncosmo_fit` is enabled now.)

```bash
cd /data/yse_pz/YSE_PZ_test && /data/yse_pz/yse_test_virtual/bin/python manage.py run_jobs --status
```
**Expected:** counts with `queued: 2` (or more), and `external_service.run` among the registered kinds.

```bash
cd /data/yse_pz/YSE_PZ_test && /data/yse_pz/yse_test_virtual/bin/python manage.py run_jobs --budget 300
```
**Expected:** it processes the queued jobs and exits. A SALT3 fit takes a minute or two. A `PermissionError` under `service_runs` means the step-1.6 permissions (`<MEDIA_ROOT>/service_runs`, group-writable for the web user and for `foley`) are missing on this stack.

**Check:** `run_jobs --status` shows them `done` (or `failed` with an error to send Ryan), and the transient's Summary tab shows the SALT3 fit. Then make it permanent (as `foley`, `crontab -e`), one line only for the shared database:

```bash
* * * * * cd /data/yse_pz/YSE_PZ_test && /data/yse_pz/yse_test_virtual/bin/python manage.py run_jobs --budget 50 >> /data/yse_pz/logs/run_jobs_yse_test.log 2>&1
```
**Check after a few minutes:** a new Refit on `/yse_test/` or `/yse_experimental/` changes from "queued" to the fit result within about two minutes, and `/yse_test/jobs/` lists recent `done` jobs.

**REQUIRED (2026-10-07): ZTF forced phot on yse_test is rejected by ZTF** ([#411](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/411)). Before #530 the alert read `wget exited with status 8: ... ERROR 400: Bad Request.`; it now reads `ZTF request failed with HTTP status 400. ZTF says: ...`. Either way the directory and wget are fine and ZTF refused the request. ZTF's 400 page names the reason; for an address it does not know it says `Error: e-mail address is unknown.` ([#433](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/433) shows that text in the alert once it reaches yse_test). The view submits as `<SMTP_LOGIN>@gmail.com` with the `ztfforcedphotpass` password. Compare the yse_test values with production's, without printing the passwords:

```bash
for f in /data/yse_pz/YSE_PZ/YSE_PZ/settings.ini /data/yse_pz/YSE_PZ_test/YSE_PZ/settings.ini; do echo "$f"; grep -E '^SMTP_LOGIN' "$f"; grep -E '^ztfforcedphotpass' "$f" | sha256sum; done
```
**Expected:** the same `SMTP_LOGIN` line and the same hash for both files. `SMTP_LOGIN` + `@gmail.com` must be the address registered with the ZTF forced-photometry service, and `ztfforcedphotpass` its password. **If not:** copy production's two values into yse_test's `settings.ini`, then `sudo systemctl restart apache2` (settings are read when the web process starts). **Check:** "Request ZTF Forced Phot" on `/yse_test/` alerts `success: ...` and adds a comment.

**YSE forced phot cron (2026-10-07):** fixed by [#419](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/419). It reaches yse_test with promotion [#428](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/428); then re-run `runcrons YSE_App.data_ingest.YSE_Forced_Phot.ForcedPhot --force`. Runs since pstamp began redirecting http to https submitted nothing.

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
```
**Expected:** both directories listed and `writable`. (ZTF forced phot no longer needs `wget`: it uses Python `requests`, #530.) **If not:**
- If `forced_phot_out` is missing, create it with `sudo mkdir -p <ZTFTMPDIR>/forced_phot_out`.
- If it is not writable, give the Apache user write access without taking it from the cron user that reads it (`foley`): `sudo chgrp <APACHE_USER> <ZTFTMPDIR>/forced_phot_out && sudo chmod 2775 <ZTFTMPDIR>/forced_phot_out`.

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

### Login required on every page (#496): verify, rollback switch only if needed

From this change every page needs a login except a public allowlist (`YSE_App/login_required.py`): the landing page and survey pages, login / logout, password reset, the Slack events endpoint, the external-service run callback, the Django admin, everything under `/api/` (DRF does its own Basic / token / session auth) and every view behind the Basic-auth decorator that crons and broker scripts call (`add_transient`, `add_transient_phot`, `add_transient_spec`, `upload_spectrum`, `query_api`, `box_search`, `get_host`, `add_yse_survey_*`, `add_survey_obs`, downloads). Logout is now POST-only (the menu item is a form; a GET of `/logout/` shows a confirmation button). It is on by default and needs nothing on Ziggy.

Check after the experimental deploy (run each from any shell with network access):

```
curl -s -o /dev/null -w '%{http_code}\n' https://ziggy.ucolick.org/yse_experimental/login/
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://ziggy.ucolick.org/yse_experimental/view_yse_fields/
```

**Expected** `200`, then `302 https://ziggy.ucolick.org/yse_experimental/login/?next=/yse_experimental/view_yse_fields/`. **Check** the next cron runs that post to YSE-PZ (DECam / QUB / ZTF uploads, `yse_fields_new.bash`) log no new 302 or 403 errors.

**Rollback** only if a page or cron that should be public breaks: in that stack's `YSE_PZ/settings.ini` add, under `[site_settings]`,

```
LOGIN_REQUIRED_MIDDLEWARE = False
```

(or set `YSE_LOGIN_REQUIRED_MIDDLEWARE=0` in the Apache / WSGI environment; the env var wins), then `sudo systemctl restart apache2`. **Check** the second curl above now returns `302` from the view's own login decorator, or `200` for a view without one. Views keep their own login decorators either way. Report what broke so the allowlist can be fixed, then remove the line.

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

3.32

### Spatial and time indexes, migration 0031 (optional EXPLAIN, budget time at 3.9) nothing required on yse\_test

Migration `0031_spatial_time_indexes` (perf audit 2026-10-10, [#477](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/477), [#478](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/478)) adds seven indexes and changes no columns: `(dec, ra)` on `YSE_App_transient`, `YSE_App_host` and `YSE_App_candidate`; `(dec_cen, ra_cen)` on `YSE_App_surveyfield`; `created_date` on `YSE_App_transient`; `obs_mjd` and `mjd_requested` on `YSE_App_surveyobservation`. **yse\_test / yse\_experimental:** applied automatically by the `experimental` deploy on the shared `YSE_test` database; nothing to run. **Production:** it is applied by 3.9 with the other migrations. Build time is unknown: each `CREATE INDEX` on InnoDB (MySQL 8) is an online in-place build that allows reads and writes, but it scans the whole table, and the Transient and Host builds are the long ones. Check the row counts first and budget for them in the maintenance window.

Copy

```
DB=<PROD_DB>; USER=<PROD_DB_USER>; HOST=<PROD_DB_HOST>
# 1. before 3.9: sizes and current indexes (read-only)
mysql -u $USER -p -h $HOST $DB -e "SELECT table_name, table_rows, ROUND(data_length/1024/1024) AS data_mb FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name IN ('YSE_App_transient','YSE_App_host','YSE_App_candidate','YSE_App_surveyfield','YSE_App_surveyobservation');" > idx0031_before.txt
mysql -u $USER -p -h $HOST $DB -e "SHOW INDEX FROM YSE_App_transient; SHOW INDEX FROM YSE_App_host; SHOW INDEX FROM YSE_App_candidate; SHOW INDEX FROM YSE_App_surveyfield; SHOW INDEX FROM YSE_App_surveyobservation;" >> idx0031_before.txt
# 2. a representative cone box and a night window (plan only, nothing executes; single quotes keep the shell off the `dec` backticks)
mysql -u $USER -p -h $HOST $DB -e 'EXPLAIN SELECT id FROM YSE_App_transient WHERE `dec` >= 20.0 AND `dec` <= 20.003 AND ra >= 150.0 AND ra <= 150.003; EXPLAIN SELECT id FROM YSE_App_host WHERE `dec` >= 20.0 AND `dec` <= 20.003 AND ra >= 150.0 AND ra <= 150.003; EXPLAIN SELECT id FROM YSE_App_surveyobservation WHERE (mjd_requested >= 61000 OR obs_mjd >= 61000) AND (mjd_requested <= 61001 OR obs_mjd <= 61001);' >> idx0031_before.txt
# 3. after 3.9: repeat 1 and 2 into idx0031_after.txt
```

**Expected** before: `SHOW INDEX` has no `yse_*_dec_ra_idx`, `yse_transient_created_idx` or `yse_surveyobs_*` rows; the host EXPLAIN shows `type: ALL` (full scan). After: the seven indexes are listed; the transient and host EXPLAINs show `key: yse_transient_dec_ra_idx` / `yse_host_dec_ra_idx` with `type: range`; the survey EXPLAIN shows `index_merge` on the two `yse_surveyobs_*` indexes, or a full scan if the table is small (that is fine). **Check** `cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py showmigrations YSE_App | tail -1` reads `[X] 0031_spatial_time_indexes`. The older single-column `yse_transient_dec_idx` and `yse_candidate_dec_idx` become redundant prefixes of the new composites; they stay in place for now (a later migration may drop them). Rollback of the indexes alone: `cd /data/yse_pz/YSE_PZ && <PROD_PYTHON> manage.py migrate YSE_App 0030_shared_db_column_defaults` drops just these seven while 0031 is the latest migration.

R

Rollback (code first; the database restore is required once 3.9 has run).

Copy

```
cd /data/yse_pz/YSE_PZ && git checkout dc70630 && <PROD_PYTHON> manage.py collectstatic --noinput && sudo systemctl restart apache2
gunzip -c /data/yse_pz/backups/<PROD_DB>_pre-0004-0010_<DATE>.sql.gz | mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> <PROD_DB>
```

<a id="photstat-view"></a>

### Photometry statistics view and upkeep jobs, migrations 0033-0034 (2026-10-12, #269-#271) one check recommended, per database

Migration `0033_photstat_extrapolated` adds four nullable columns to `YSE_App_transientphotstat` (`extrapolated_mag`, `extrapolated_band_id`, `extrapolated_mjd`, `extrapolated_at`; safe on the shared `YSE_test` database, `develop` never writes them). Migration `0034_transient_photstat_view` creates the SQL Explorer view `yse_transient_photstat_v` (one row per stat row, with the transient name and band names). Creating a view needs MySQL's `CREATE VIEW` privilege: if the deploy's database user lacks it, `migrate` logs `yse_transient_photstat_v not changed: the database user lacks the privilege` and carries on, and step 2 below creates it by hand. The deploy runs both migrations; nothing else is required for the site to work. Docs: [docs/photstat.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/photstat.md).

Placeholders: `<STACK>` / `<STACK_PYTHON>` from section 0 (for the shared `YSE_test` database use the yse\_experimental checkout, which has the migration first; production after the promotion), `<DB>`, `<DB_USER>`, `<DB_HOST>` the stack's database, `<EXPLORER_USER>` the `[database] EXPLORER_USER` from that stack's `settings.ini`, `<ADMIN_USER>` a MySQL user with `CREATE VIEW` on `<DB>`.

1. Check the view exists and the Explorer user can read it:

```
mysql -u <DB_USER> -p -h <DB_HOST> <DB> -e "SHOW FULL TABLES WHERE Table_type = 'VIEW'; SELECT COUNT(*) FROM yse_transient_photstat_v; SELECT COUNT(*) FROM YSE_App_transientphotstat;"
mysql -u <EXPLORER_USER> -p -h <DB_HOST> <DB> -e "SELECT transient_name, peak_mag, peak_band FROM yse_transient_photstat_v LIMIT 3;"
```

**Expected:** `yse_transient_photstat_v VIEW` is listed, the two counts are equal, and the Explorer user prints three rows. **If the view is missing**, do step 2. **If the Explorer user gets `SELECT command denied`** (its grants are per table rather than on `<DB>.*`): `mysql -u <ADMIN_USER> -p -h <DB_HOST> -e "GRANT SELECT ON <DB>.yse_transient_photstat_v TO '<EXPLORER_USER>'@'%';"` (use the host part shown by `SHOW GRANTS FOR '<EXPLORER_USER>'`), then repeat the second command.

2. Only if step 1 found no view: write the statement out of the migration and run it as `<ADMIN_USER>`:

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "import importlib; print(importlib.import_module('YSE_App.migrations.0034_transient_photstat_view').CREATE_VIEW_SQL)" > /tmp/photstat_view.sql
mysql -u <ADMIN_USER> -p -h <DB_HOST> <DB> < /tmp/photstat_view.sql
```

**Expected:** no output from `mysql`. **Check:** repeat step 1.

3. Upkeep jobs (nothing to install; they run from the existing `runcrons` pass through the job runner, 3.18 / the `run_jobs` crontab line on yse\_test). The `PhotStatRefresh` cron queues one `photstat.refresh_stale` job an hour, which recomputes at most 2000 missing or outdated stat rows (the `rebuild_photstats --stale-only` selection), so a stack never needs a manual `rebuild_photstats` after a rules change again. Check after the next hour:

```
cd <STACK> && <STACK_PYTHON> manage.py run_jobs --status
```

**Expected:** `photstat.refresh_stale` and `photstat.bazin_extrapolate` among the registered kinds, and a `done` `photstat.refresh_stale` job once an hour. Optional `[site_settings]` keys in `settings.ini` (read by the cron process at its next run; no Apache restart needed): `PHOTSTAT_REFRESH_CRON_ENABLED: False` turns the refresh off; `PHOTSTAT_REFRESH_LIMIT` (2000) and `PHOTSTAT_REFRESH_CRON_MINUTES` (60) size it; `PHOTSTAT_BAZIN_CRON_ENABLED: True` (off by default) also stores the Bazin-extrapolated magnitude for transients with an open follow-up request every run (at most `PHOTSTAT_BAZIN_LIMIT`, 500, each a Bazin fit), which fills the internal `extrapolated_*` columns (not in the view or the API until #607).

**Rollback:** `cd <STACK> && <STACK_PYTHON> manage.py migrate YSE_App 0032` drops the view and the four columns (only after the code is rolled back too).

### Performance audit 2026-10-10: server settings (optional, per stack) nothing required

Everything here is optional and off by default; code lands with the PR for [#481](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/481)-[#490](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/490). Placeholders: `<STACK_DIR>` (the checkout), `<STACK_PYTHON>` (that stack's interpreter), `<CRON_PYTHON>`, `<STATIC_DIR>` (that stack's `STATIC_ROOT`, the directory its Apache `Alias` points at), `<APACHE_SITE_CONF>`, `<MYSQL_ADMIN_USER>`.

**A. Compression (`mod_deflate`, #482).**

```
sudo a2enmod deflate headers expires
# in <APACHE_SITE_CONF>, inside the vhost:
#   AddOutputFilterByType DEFLATE text/html text/css application/javascript application/json image/svg+xml
sudo apache2ctl configtest && sudo systemctl reload apache2
# check:
curl -s -H 'Accept-Encoding: gzip' -o /dev/null -D - <SITE_URL><STATIC_URL>YSE_App/yse-theme.css | grep -i content-encoding
```

**Expected** `content-encoding: gzip`.

**B. Hashed static files and a long `Cache-Control` (#482).** The setting is OFF until `YSE_HASHED_STATIC=1` is in the environment of BOTH `collectstatic` and the web process. Order matters: collect first, then turn it on, or pages reference names that do not exist yet (files missing from the manifest fall back to the plain name, so a missed step degrades to today's behaviour rather than a 500).

```
# 1. collect with the setting on (writes name.<hash>.ext and staticfiles.json under <STATIC_DIR>)
cd <STACK_DIR> && YSE_HASHED_STATIC=1 <STACK_PYTHON> manage.py collectstatic --noinput

# 2. long cache ONLY for the hashed copies, in <APACHE_SITE_CONF> (the names change when content changes):
#   <Directory <STATIC_DIR>>
#       <FilesMatch "\.[0-9a-f]{12}\.(css|js|png|jpg|gif|svg|woff2?|ttf)$">
#           Header set Cache-Control "public, max-age=31536000, immutable"
#       </FilesMatch>
#   </Directory>

# 3. export YSE_HASHED_STATIC=1 where mod_wsgi sees it (/etc/apache2/envvars, or wsgi.py setdefault, as for REDIS_URL), then
sudo apache2ctl configtest && sudo systemctl reload apache2

# check: a page references hashed names, and they carry the header
curl -s <SITE_URL>/ | grep -o 'yse-theme\.[0-9a-f]*\.css' | head -1
curl -sI <SITE_URL><STATIC_URL>YSE_App/<hashed name from the line above> | grep -i cache-control
```

**Expected** a `yse-theme.<12 hex>.css` name and `cache-control: public, max-age=31536000, immutable`. Roll back: unset `YSE_HASHED_STATIC` and reload; the old plain files are still collected. Re-run step 1 after every deploy that changes static files.

**C. Finder charts (#484).** Charts are now written at dpi 150 through one helper (`view_utils.finder_chart_paths`), by default under `<STATIC_DIR>/YSE_App/images/findercharts`; `YSE_FINDER_CHART_DIR` can relocate them (the transient detail page only shows them while they stay under STATIC_ROOT). Old dpi-1000 files are still read as they are until pruned.

```
cd <STACK_DIR> && <STACK_PYTHON> manage.py prune_finder_charts --older-than-days 90            # dry run
cd <STACK_DIR> && <STACK_PYTHON> manage.py prune_finder_charts --older-than-days 90 --execute  # delete
# weekly crontab line (crontab -e), after a dry run looks right:
#   17 4 * * 0  cd <STACK_DIR> && <CRON_PYTHON> manage.py prune_finder_charts --older-than-days 90 --execute
```

**Expected** `would delete (dry run; add --execute) N finder chart directories, X MB`. Charts are regenerated on the next view.

**D. auditlog retention (#486).** Ingest writes an auditlog row per Transient save. There is no default: the command refuses to run without `--older-than-days`.

```
cd <STACK_DIR> && <STACK_PYTHON> manage.py prune_auditlog --older-than-days 365 --dry-run
cd <STACK_DIR> && <STACK_PYTHON> manage.py prune_auditlog --older-than-days 365 --batch-size 5000 --sleep 0.2
# optional, one model only: --model YSE_App.Transient
# monthly crontab line, once the retention period is agreed:
#   23 3 1 * *  cd <STACK_DIR> && <CRON_PYTHON> manage.py prune_auditlog --older-than-days <N>
```

**Expected** `would delete N auditlog entries older than <date>` then `deleted N auditlog entries ...`. Take a DB backup before the first real run. The shared `YSE_test` database is common to yse\_test and yse\_experimental: run it once, not per stack.

**E. Logging and sessions (#485, #488).** The app now has a console `LOGGING` config: `YSE_LOG_LEVEL` (default `INFO`; `DEBUG`, `WARNING`, ...) in the same environment as `REDIS_URL` sets the level; output goes to the Apache error log. `SESSION_ENGINE = cached_db` is selected automatically ONLY when `REDIS_URL` resolves to a Redis cache backend (never on LocMem). Set `REDIS_URL` on production only once [#339](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/339) is on master (see "Enable the shared cache" above). Check:

```
cd <STACK_DIR> && REDIS_URL=<REDIS_URL> <STACK_PYTHON> manage.py shell -c "from django.conf import settings; print(settings.SESSION_ENGINE)"
```

**Expected** `django.contrib.sessions.backends.cached_db` with Redis, `django.contrib.sessions.backends.db` without.

**F. Preload the plot stack at worker start (#489, optional).** Saves the multi-second first plot request per worker. Create `<STACK_DIR>/wsgi_preload.py` (not committed) containing:

```
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "YSE_PZ.settings")
from YSE_App.preload import preload
preload()
```

then in `<APACHE_SITE_CONF>`, next to the existing `WSGIDaemonProcess` / `WSGIScriptAlias` lines for that stack (use that stack's process group and application group):

```
WSGIImportScript <STACK_DIR>/wsgi_preload.py process-group=<PROCESS_GROUP> application-group=%{GLOBAL}
sudo apache2ctl configtest && sudo systemctl reload apache2
```

**Expected** `YSE plot stack preloaded` in the error log after the reload, and the first light-curve request no longer slower than later ones. Note that `application-group=%{GLOBAL}` must match the group the site runs in, or the preload warms a different interpreter. Roll back by removing the line.

**G. Measure before tuning (#490).** MySQL slow query log on YSE\_test (shared by yse\_test and yse\_experimental; do not leave it on for production without agreement). Changing these needs a MySQL admin account (`<MYSQL_ADMIN_USER>`), not the app user.

```
mysql -u <MYSQL_ADMIN_USER> -p -e "SET GLOBAL slow_query_log_file='<SLOW_LOG_PATH>'; SET GLOBAL long_query_time=1; SET GLOBAL log_queries_not_using_indexes=OFF; SET GLOBAL slow_query_log='ON';"
mysql -u <MYSQL_ADMIN_USER> -p -e "SHOW VARIABLES LIKE 'slow_query%'; SHOW VARIABLES LIKE 'long_query_time';"
# ... reproduce the slow pages (dashboard, transient detail), then digest:
pt-query-digest <SLOW_LOG_PATH> > <OUT_DIR>/slow_digest.txt        # percona-toolkit: sudo apt install percona-toolkit
head -60 <OUT_DIR>/slow_digest.txt
# turn it off again (these SET GLOBALs are lost at a MySQL restart anyway):
mysql -u <MYSQL_ADMIN_USER> -p -e "SET GLOBAL slow_query_log='OFF'; SET GLOBAL long_query_time=10;"
```

**Expected** `slow_query_log ON` with the chosen file; the digest ranks fingerprints by total time. The log file is written by the mysqld user: pick a path it may write, and delete it when done (it can contain query literals).

EXPLAIN a slow dashboard query that the digest names (paste it from the digest, with its literals; read-only):

```
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> <PROD_DB> -e "EXPLAIN FORMAT=TREE <paste the SELECT here>\G" > <OUT_DIR>/explain_tree.txt
```

**Expected** an operator tree with `actual time` absent (EXPLAIN does not run the query); look for `Table scan` on large tables and `Filter` above a large `Index lookup`. Use `EXPLAIN ANALYZE` only on yse\_test, since it runs the query.

Apache request time: add `%D` (microseconds) to a copy of the LogFormat so existing parsers keep working.

```
# <APACHE_SITE_CONF> (or apache2.conf):
#   LogFormat "%h %l %u %t \"%r\" %>s %b \"%{Referer}i\" \"%{User-Agent}i\" %D" combined_timed
#   CustomLog ${APACHE_LOG_DIR}/<SITE>_timed.log combined_timed
sudo apache2ctl configtest && sudo systemctl reload apache2
# slowest 20 requests (last field is %D, microseconds):
awk '{print $NF, $7}' ${APACHE_LOG_DIR}/<SITE>_timed.log | sort -rn | head -20
```

**Expected** `<microseconds> <path>` pairs. For the cron overlap guard (`flock -n` on `runcrons`) see [PR #546](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/546); it is not repeated here.

<a id="authz-flags"></a>

### Write authorization and Explorer access (optional, per stack; #520-#527) nothing required

Migration `0032_api_writer_groups` is data only. It creates two groups, `api_writers` and `explorer_users`. It gives `api_writers` the add and change permissions on the models the ingest crons and scripts write. It adds the `[main] dblogin` account to `api_writers` if that account exists. Three flags in `settings.ini` `[site_settings]` stay **off** by default, so behaviour is unchanged until you turn them on. Each flag can be turned on per stack. yse\_test and yse\_experimental share `YSE_test`, so group membership is shared, but each stack reads its own `settings.ini`.

| Flag (`[site_settings]`, or the env var `=1`) | When on |
| --- | --- |
| `API_WRITE_PERMISSIONS_ENFORCED` (`YSE_API_WRITE_PERMISSIONS_ENFORCED`) | `POST`/`PUT`/`PATCH` on the older `/api/` resources needs one of: staff, superuser, or the model permission (members of `api_writers`). Status and tag buttons, follow-up status, survey scheduling and "new tag" stay open to every signed-in user. Reads are unchanged. |
| `UPLOAD_GROUP_CHECKS_ENFORCED` (`YSE_UPLOAD_GROUP_CHECKS_ENFORCED`) | `add_transient` is limited to staff, superusers and `api_writers`. `add_transient_phot`, `add_transient_spec` and the spectrum upload form are open to those accounts as well as other collaboration-group members, with two limits: a member may share only with their own groups, and may replace or re-share only rows they created. |
| `EXPLORER_RESTRICTED_ACCESS` (`YSE_EXPLORER_RESTRICTED_ACCESS`) | `/explorer/` is limited to superusers and members of `explorer_users`. While it is off, all staff have access, as today. Dashboards are not affected. |

The group names can be changed with `API_WRITERS_GROUP` and `EXPLORER_ACCESS_GROUP` in `[site_settings]`. `<STACK>` is the checkout and `<STACK_PYTHON>` its interpreter, both from section 0.

**Step A: check the groups (after the deploy that applies 0032).**

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "from django.contrib.auth.models import Group; g=Group.objects.get(name='api_writers'); print(g.permissions.count(), g.user_set.count()); print(Group.objects.filter(name='explorer_users').exists())"
```

**Expected:** `52 1` (or `52 0` if the `[main] dblogin` account does not exist on this database), then `True`.

**Step B: add the accounts that write through the API or the upload endpoints.**

Add each of these:

- the `[main] dblogin` account the crons use;
- any account passed to `YSE_App/yse_utils/add_yse_fields.py`, `add_decam_obs.py` (`--yseuser`), `YSE_App/util/uploadTransientData.py`, `YSE_App/util/TNS_Synopsis/*.py` or `getGWevents.py`;
- people who upload with their own `add_transient` scripts.

Staff and superusers do not need to be added.

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "from django.contrib.auth.models import User, Group; User.objects.get(username='<ACCOUNT>').groups.add(Group.objects.get(name='api_writers')); print(User.objects.get(username='<ACCOUNT>').has_perm('YSE_App.change_transientphotdata'))"
```

**Expected:** `True`.

To find non-staff accounts that wrote ingest data in the last 30 days and are not in the group, run the command below. It prints to your terminal only; do not paste the usernames into GitHub.

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "
import datetime; from django.utils import timezone; from django.db.models import Count
from YSE_App.models import Transient, TransientPhotData, TransientSpectrum, SurveyObservation, SurveyField
since = timezone.now() - datetime.timedelta(days=30)
for m in (Transient, TransientPhotData, TransientSpectrum, SurveyObservation, SurveyField):
    rows = (m.objects.filter(created_date__gte=since, created_by__is_staff=False, created_by__is_superuser=False)
            .exclude(created_by__groups__name='api_writers').values('created_by__username').annotate(n=Count('id')))
    print(m.__name__, list(rows))
"
```

**Expected:** each model prints `[]` once every cron or script account is in the group. Some rows are fine to leave: accounts listed there because of browser uploads (the photometry or spectrum form) need nothing. Accounts behind a script or cron should be added with the previous command.

**Step C: turn the flags on, one at a time.** In `<STACK>/YSE_PZ/settings.ini`:

```
[site_settings]
API_WRITE_PERMISSIONS_ENFORCED: True
UPLOAD_GROUP_CHECKS_ENFORCED: True
```

Then `sudo systemctl restart apache2`. Do this for production; for yse\_test and yse\_experimental, use whatever restart those stacks use.

**Check:**
- `<STACK_PYTHON> manage.py shell -c "from django.conf import settings; print(settings.API_WRITE_PERMISSIONS_ENFORCED, settings.UPLOAD_GROUP_CHECKS_ENFORCED)"` prints `True True`.
- After the next TNS / QUB / forced-phot cron run, its log has no `403`, and no `Bulk transient ingest is limited` or `needs the matching model permission` message.
- A status change on a transient page still works for a non-staff member.

**Rollback:** set the flag to `False` and restart.

**Step D (Explorer): give `explorer_users` to the staff who should keep `/explorer/`, then turn on the restriction.**

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "from django.contrib.auth.models import User, Group; User.objects.get(username='<ACCOUNT>').groups.add(Group.objects.get(name='explorer_users'))"
```

Next, set `EXPLORER_RESTRICTED_ACCESS: True` in `[site_settings]` and restart.

**Check:** `/explorer/` opens for a superuser and for a member of `explorer_users`. A staff account outside the group gets the Explorer login page. The dashboards still load.

**Rollback:** set the flag to `False` and restart.

**Step E (Explorer database user, #526): limit its grants to science tables.** The `explorer` connection (`[database] EXPLORER_USER`) is what Explorer and the dashboard SQL run as. Do this first on `YSE_test`, check it, then on production. `<EXPLORER_DB_USER>` is the `EXPLORER_USER` value; `<EXPLORER_DB_HOST>` is the host part shown by `SHOW GRANTS`.

```
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "SELECT user, host FROM mysql.user WHERE user='<EXPLORER_DB_USER>'; SHOW GRANTS FOR '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';"
```

**Expected:** the output includes a line like `GRANT SELECT ON <DB>.* TO ...` (database-wide). Generate per-table grants that leave out accounts, sessions, tokens, audit, credential and contact tables, and save the statements to a file you can review:

```
mysql -N -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "
SELECT CONCAT('GRANT SELECT ON \`', table_schema, '\`.\`', table_name, '\` TO ''<EXPLORER_DB_USER>''@''<EXPLORER_DB_HOST>'';')
FROM information_schema.tables WHERE table_schema='<DB>'
AND table_name NOT LIKE 'auth\_%' AND table_name NOT LIKE 'authtoken\_%' AND table_name NOT LIKE 'axes\_%'
AND table_name NOT LIKE 'auditlog\_%' AND table_name NOT LIKE 'otp\_%' AND table_name NOT IN ('django_session','django_admin_log',
'YSE_App_encryptedcredential','YSE_App_profile','YSE_App_principalinvestigator','YSE_App_externalservicerun','YSE_App_feedback');" > /tmp/explorer_grants.sql
wc -l /tmp/explorer_grants.sql
```

Saved queries often join `auth_user` for names. Keep that working with a column grant. Append it to the file, then swap the grants:

```
echo "GRANT SELECT (id, username, first_name, last_name) ON \`<DB>\`.\`auth_user\` TO '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';" >> /tmp/explorer_grants.sql
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "REVOKE SELECT ON \`<DB>\`.* FROM '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';"
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> < /tmp/explorer_grants.sql
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "SHOW GRANTS FOR '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';" | head
```

**Check: every saved query still runs.** The first command below prints only failures and a count, so names of tables are not shown. The second shows the user lacks access to an excluded table:

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "
from django.db import connections; from explorer.models import Query
bad = 0
for q in Query.objects.all():
    try:
        c = connections['explorer'].cursor(); c.execute('SELECT * FROM (' + q.sql.strip().rstrip(';').replace('%','%%') + ') AS t LIMIT 1'); c.close()
    except Exception as e:
        bad += 1; print(q.id, type(e).__name__, str(e)[:120])
print('failed', bad, 'of', Query.objects.count())
"
mysql -u <EXPLORER_DB_USER> -p -h <PROD_DB_HOST> <DB> -e "SELECT COUNT(*) FROM django_session;"
```

**Expected:**
- The first command prints `failed 0 of N`, or only failures that also happened before the change. A query using a table outside the list fails with `SELECT command denied`: grant that table and re-run.
- The second command prints `ERROR 1142 ... SELECT command denied`.
- The dashboards load.

**After a later migration adds a table**, re-run the generator and apply only the new grants.

**Rollback:** `GRANT SELECT ON \`<DB>\`.* TO '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';`

`docker/db_init/YSE_create_users.sql` keeps the database-wide grant. It runs before the tables exist, and MySQL accepts table-level grants only on existing tables, so the local stacks and CI are unchanged.

<a id="dep-pins"></a>

### Dependency pin bumps (2026-10-11, #539) required on any venv that the deploy does not install, per stack

`requirements.txt` now pins newer releases of Pillow (10.4.0), cryptography (45.0.7), urllib3 (1.26.20), certifi (2026.7.22), idna (3.15), djangorestframework (3.15.1), sqlparse (0.5.5), lxml (6.1.3), Jinja2 (3.1.6), MarkupSafe (2.1.5), tornado (6.4.2) and Werkzeug (3.0.6). Every pin supports Python 3.8 and Django 3.2, and the file still resolves as a whole. The Deploy Stack job installs `requirements.txt` into the yse\_experimental venv by itself. It does NOT install into `yse_test_virtual` (yse\_test, and Apache for yse\_experimental per [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187)) or production. Run this there, once the stack's checkout has #539. No migration.

```
# <STACK_DIR> / <STACK_PYTHON> from section 0; production gets this from step 3.6 (pip install -r requirements.txt)
# <VENV_OWNER>: owner of the venv directory, e.g. stat -c %U /data/yse_pz/yse_test_virtual
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip install -r <STACK_DIR>/requirements.txt
<STACK_PYTHON> -m pip check
<STACK_PYTHON> -c "import PIL, cryptography, rest_framework, sqlparse, lxml.etree, jinja2, markupsafe, tornado, werkzeug, urllib3, certifi, idna; print(PIL.__version__, cryptography.__version__, rest_framework.VERSION, sqlparse.__version__, jinja2.__version__, urllib3.__version__)"
sudo systemctl restart apache2
```

**Expected:** `pip install` ends without a resolver error; `pip check` prints `No broken requirements found.` (any line it prints for a package not listed above was there before); the last command prints `10.4.0 45.0.7 3.15.1 0.5.5 3.1.6 1.26.20`. **Check:** log in on that stack and open a transient page and `/api/` (the DRF API root).

**Rollback:** `<STACK_PYTHON> -m pip install -r requirements.txt` from the previous commit (`git show <PREVIOUS_COMMIT>:requirements.txt > /tmp/req-old.txt`), then restart Apache.

<a id="query-api"></a>

### `/query_api/` saved-query allowlist and row visibility (2026-10-11, #524) one check recommended, per stack

From this change `/query_api/<title>/` serves a saved query only when one of these holds: the title is on the allowlist, the caller created the query, the caller has SQL Explorer access, or the caller is a service account (staff, superuser, a member of `api_writers`, or the `[main] dblogin` account). Other titles return 404. The built-in allowlist holds the four titles the crons and scripts request (`QUB_data.py`, `DECam_upload.py`, `DECam_upload_clusters.py`, `util/` and `yse_utils/get_YSE_PAs.py`). For other callers, rows are limited to transients they may view. Staff, superusers and service accounts get every row, as before. No migration.

**Check (recommended): the accounts that run `get_YSE_PAs.py` (`--yseuser`) are staff or in `api_writers`.** If they are not, they now get only the rows of transients they may view. Run this once per account; it prints to your terminal only.

```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "from django.contrib.auth.models import User; u=User.objects.get(username='<ACCOUNT>'); print(u.is_staff or u.is_superuser or u.groups.filter(name='api_writers').exists())"
```

**Expected:** `True`. If it prints `False`, add the account to `api_writers` (Step B of [Write authorization and Explorer access](#authz-flags)) and re-run.

**Optional settings** (`[site_settings]` in `settings.ini`; restart Apache after a change):
```
# extra titles /query_api/ may serve to any signed-in account, one per line (continuation lines indented) or separated by |
# QUERY_API_ALLOWED_QUERIES: <TITLE_1> | <TITLE_2>
# rollback switch for the row filter; default True
# QUERY_API_VISIBILITY_FILTER: True
```
Env equivalents: `YSE_QUERY_API_ALLOWED_QUERIES`, `YSE_QUERY_API_VISIBILITY_FILTER=0`.

**Check after deploy:** the crons' logs show no new 404 from `/query_api/`.

<a id="authn"></a>

### Login, session and transport settings (2026-10-11, sec/authn PR) one check required, the rest optional, per stack

Everything below is in `[security]` of that stack's `YSE_PZ/settings.ini` (template: `YSE_PZ/public_settings.ini`). Every key is optional and its default is today's behaviour. Below, `<STACK_DIR>` and `<STACK_PYTHON>` are a row of the table in [section 0](#paths); after any `settings.ini` edit run `sudo systemctl restart apache2`, then the **Check** of that step. Do one stack at a time: yse\_experimental first, then yse\_test, production last. Nothing here needs a migration, except `migrate axes` in (c).

**(a) REQUIRED check: Slack events.** `/slack/events/` now answers only when `SLACK_ENABLED=1` **and** `SLACK_SIGNING_SECRET` are both in the web environment (it used to accept requests when no secret was set). Only a stack whose Slack app sends events (thread replies into comments) is affected.
```bash
sudo grep -lE 'SLACK_SIGNING_SECRET|SLACK_ENABLED' /etc/apache2/envvars <STACK_DIR>/YSE_PZ/wsgi.py 2>/dev/null
```
**Expected:** nothing on a stack without Slack (nothing to do), or the file(s) holding both names. **If** only `SLACK_ENABLED` is there, add the app's signing secret (api.slack.com > Your apps > Basic Information > Signing Secret) next to it and restart Apache. **Check:** in api.slack.com > Event Subscriptions the Request URL shows "Verified" after "Retry"; a reply in a transient's Slack thread appears as a comment.

**(b) Session length.** Default two weeks; recommended 12-24 h:
```ini
SESSION_COOKIE_AGE: 86400
```
**Check:** after logging in, the browser's `sessionid` cookie expires about one day later. Existing sessions keep their old expiry.

**(c) Failed-login lockout (django-axes 5.41.1, new dependency).** Counts failed logins (login page, admin, API Basic auth) per username and client address; after 10 failures that pair is refused for 60 minutes. Install first (yse\_experimental's deploy already runs `pip install -r requirements.txt`; yse\_test and production need it by hand):
```bash
<STACK_PYTHON> -m pip install "django-axes==5.41.1" "django-ipware==5.0.2"
<STACK_PYTHON> -c "import axes; print(axes.__version__)"
```
**Expected:** `5.41.1`. Then add to `settings.ini` and create the tables (once per database; yse\_test and yse\_experimental share `YSE_test`, so once for both):
```ini
LOGIN_LOCKOUT_ENABLED: True
# optional: LOGIN_LOCKOUT_FAILURE_LIMIT: 10   LOGIN_LOCKOUT_COOLOFF_MINUTES: 60
# optional: LOGIN_LOCKOUT_MODE: user_and_ip (default) | user | ip
# optional: LOGIN_LOCKOUT_NEVER_LOCK_IPS: <comma-separated addresses the crons connect from>
```
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py migrate axes
<STACK_PYTHON> manage.py check
sudo systemctl restart apache2
```
**Expected:** `migrate` applies `axes.0001` to `axes.0008` (or `No migrations to apply.` on the second stack of a shared database); `check` prints `System check identified no issues`. **Check:** log in normally on that stack; then `<STACK_PYTHON> manage.py axes_list_attempts` prints no attempts for your login. To unlock everyone: `<STACK_PYTHON> manage.py axes_reset`. With the flag on but the package missing, the site still starts and logs a warning (lockout stays off). To undo: remove the key and restart.

**(d) Service accounts for crons and scripts.** Crons should not use a personal or admin login. Once perf #457 (API tokens) is on the stack: create one user per integration (admin > Users > Add; not staff, not superuser), give it only the model permissions that integration writes (admin > Users > that user > User permissions), create its token and use it from the script:
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py drf_create_token <SERVICE_USER>
```
**Expected:** `Generated token <40 hex characters> for user <SERVICE_USER>`. Store it where that cron reads its settings (not in this runbook) and send `Authorization: Token <token>` instead of Basic auth. Set `[brokers] AUTO_SAVE_USERNAME` (default `admin`) and `[sharing] SYSTEM_USERNAME` to such a service user. List every service username in `INACTIVE_USER_EXEMPT_USERNAMES` (step (h)).

**(e) Host names and forwarded headers (password-reset links).** First find out whether anything proxies to Django or whether Apache runs it directly with mod\_wsgi:
```bash
grep -nE 'WSGIDaemonProcess|ProxyPass|RequestHeader|X-Forwarded' /etc/apache2/sites-enabled/*.conf
grep -nE '^(ALLOWED_HOSTS|IS_DEBUG)' <STACK_DIR>/YSE_PZ/settings.ini
```
**Expected:** a `WSGIDaemonProcess` line per stack. If there is **no** `ProxyPass` to the Django process and no `RequestHeader set` for `X-Forwarded-Host` / `X-Forwarded-Proto`, Django is not behind a proxy and should not trust those headers. Then set, in `[site_settings]` and `[security]`:
```ini
# [site_settings]
ALLOWED_HOSTS: ziggy.ucolick.org,localhost,127.0.0.1
# [security]
REQUIRE_ALLOWED_HOSTS: True
USE_X_FORWARDED_HOST: False
USE_X_FORWARDED_PORT: False
PASSWORD_RESET_DOMAIN: ziggy.ucolick.org
```
If a proxy does set those headers itself (overwriting what the client sent), keep `USE_X_FORWARDED_HOST` / `PORT` at True and set only `ALLOWED_HOSTS`, `REQUIRE_ALLOWED_HOSTS` and `PASSWORD_RESET_DOMAIN`. `REQUIRE_ALLOWED_HOSTS` has an effect only with `IS_DEBUG: False` (production); yse\_test and yse\_experimental run `IS_DEBUG: 1`. **Check:** `manage.py check` passes; the login page loads; "Forgot password" for your own account sends an email whose link starts with `https://ziggy.ucolick.org/` and the right stack path.

**(f) HTTPS proxy header.** With `IS_DEBUG: False` Django trusts `X-Forwarded-Proto: https` today (`TRUST_X_FORWARDED_PROTO` defaults to on there). Under mod\_wsgi with Apache terminating TLS, Django knows the scheme without that header. If (e) showed no proxy and no `RequestHeader set X-Forwarded-Proto`, set explicitly:
```ini
TRUST_X_FORWARDED_PROTO: False
```
If a proxy sets/overwrites `X-Forwarded-Proto`, set `TRUST_X_FORWARDED_PROTO: True` explicitly instead. **Check:** the login page loads over https and logging in works (the CSRF check compares the scheme).

**(g) HTTPS-only cookies, redirect, HSTS.** First confirm the stack is served over https and plain http redirects or is closed:
```bash
curl -sI https://ziggy.ucolick.org/yse_experimental/login/ | head -1
curl -sI http://ziggy.ucolick.org/yse_experimental/login/ | head -3
```
**Expected:** `HTTP/1.1 200 OK` (or `HTTP/2 200`) for https; for http a `301`/`302` to https, or a connection error. Then:
```ini
SESSION_COOKIE_SECURE: True
CSRF_COOKIE_SECURE: True
SECURE_SSL_REDIRECT: True
SECURE_HSTS_SECONDS: 3600
# SECURE_HSTS_INCLUDE_SUBDOMAINS: False   (leave off: other services share ziggy.ucolick.org)
```
`SECURE_SSL_REDIRECT` needs Django to see https: under mod\_wsgi it does; behind a proxy it needs (f) set to True, otherwise every page redirects to itself. **Check:** `curl -sI https://ziggy.ucolick.org/yse_experimental/login/ | grep -i strict-transport` prints `strict-transport-security: max-age=3600`; logging in still works. After a week without problems raise `SECURE_HSTS_SECONDS` to `31536000`.

**(h) Deactivate long-unused accounts.** Dry run first; it prints counts only:
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py deactivate_inactive_users --days 365
```
**Expected:** `skipped: ... superusers, ... exempt service accounts, ... token holders` and `dry run: N accounts ... would be deactivated`. Superusers, accounts holding an API token and the names in `[security] INACTIVE_USER_EXEMPT_USERNAMES` (comma-separated; add every account a cron or script logs in with, since Basic-auth logins do not update the last-login date) are never touched. Then `--apply`. **Check:** `deactivated N accounts`; re-activate one in admin > Users > Active if needed.

**(i) Database TLS (optional).** Only if the MySQL server offers TLS: in `[database]` set `SSL_DISABLED: False` and, as needed, `SSL_CA`, `SSL_CERT`, `SSL_KEY` (file paths readable by the Apache user), `SSL_MODE: VERIFY_CA`, `DB_TLS_EXPLORER: True`. **Check:** `cd <STACK_DIR> && <STACK_PYTHON> manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute(\"SHOW SESSION STATUS LIKE 'Ssl_cipher'\"); print(c.fetchone())"` prints a non-empty cipher.

**(j) Content-Security-Policy, report-only (optional).** `CSP_REPORT_ONLY_ENABLED: True` adds a `Content-Security-Policy-Report-Only` header; browsers report (in the developer console, or to `CSP_REPORT_URI` if set) what the policy would block, and block nothing. **Check:** `curl -sI https://ziggy.ucolick.org/yse_experimental/login/ | grep -i content-security-policy-report-only`. Send Ryan the console reports from a few pages before anything is enforced.

**(k) API rate limits (optional, off by default).** DRF throttles for `/api/` (not `add_transient` and the other data-upload views). Suggested:
```ini
API_THROTTLE_ANON: 300/min
API_THROTTLE_USER: 1000/min
```
Before turning `API_THROTTLE_USER` on, ask Ryan which scripts read `/api/` in bulk, and give them a higher rate or leave the user throttle off. **Check:** normal pages and the transient-detail tabs still load; a script that exceeds the rate gets HTTP 429. Rollback: remove the keys and restart Apache.

**Template change only:** `public_settings.ini` now has `IS_DEBUG: False`. Ziggy's stacks read only their own `settings.ini`, never the template, so nothing changes there; local docker and CI turn DEBUG on with `YSE_IS_DEBUG=1` in `docker/.env`.

<a id="https-followup"></a>

### https follow-up (2026-10-12, #441) nothing required; a decision for David and Ryan, then optional steps, per stack

The code now builds its own links from `YSE_PUBLIC_BASE_URL` (default `https://ziggy.ucolick.org/yse/`) and uses https for the services that support it. **Nothing below is switched on by the deploy**; the HTTPS flags from step (g) of [Login, session and transport settings](#authn) keep their defaults (off).

**(1) Decision needed: who redirects http to https?** Ryan has not chosen. Both options end with the same result for a browser.

| | A. Apache redirect | B. Django `SECURE_SSL_REDIRECT` |
|---|---|---|
| Where | the port-80 `VirtualHost` in `/etc/apache2/sites-enabled/*.conf` (a `Redirect permanent / https://ziggy.ucolick.org/` or `RewriteRule`) | `[security] SECURE_SSL_REDIRECT: True` in each stack's `settings.ini` |
| Covers | every app and path on the host (other services on `ziggy.ucolick.org` too) | only requests that reach this Django stack |
| Needs | Apache edit, `apache2ctl configtest`, reload | Django must see https: fine under mod\_wsgi; behind a proxy `TRUST_X_FORWARDED_PROTO: True` first, otherwise pages redirect to themselves |
| Risk | affects other services on the host; one change for all stacks | per stack, easy to roll back |
| Rollback | revert the Apache file, reload | remove the key, restart Apache |

**Recommended order, one stack at a time (yse\_experimental, yse\_test, production last):**
1. Confirm https works for the stack and see what http does now:
   ```bash
   curl -sI https://ziggy.ucolick.org/yse_experimental/login/ | head -1
   curl -sI http://ziggy.ucolick.org/yse_experimental/login/ | head -3
   ```
   **Expected:** `200` for https; http answers with a redirect or an error.
2. Turn on the redirect (A or B, once decided). **Check:** the second `curl` above now shows `301` with `Location: https://...`, and logging in from a browser still works.
3. Only after the redirect has run for a few days: set `SECURE_HSTS_SECONDS: 3600` (one hour) in `[security]`, restart Apache. **Check:** `curl -sI https://ziggy.ucolick.org/yse_experimental/login/ | grep -i strict-transport` prints `max-age=3600`.
4. Raise it step by step (one day, one week, one year), only after a quiet period at each step; browsers that saw HSTS keep it until the max-age runs out. Leave `SECURE_HSTS_INCLUDE_SUBDOMAINS` off (other services share the host). The first HSTS value is also open: 3600 is only a suggestion.
5. Secure cookies (`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`) can go on together with step 2.

**(2) Deployment check.**
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py check --deploy
```
**Expected:** with the flags above on and `IS_DEBUG: False`, no cookie, HSTS or SSL-redirect warnings (yse\_test and yse\_experimental run with `IS_DEBUG: 1`, so DEBUG warnings are expected there). Record what remains.

**(3) Stored http URLs (optional, any time after the deploy).** Rewrites only our own host and known https-capable services; other hosts, URLs with an explicit port and comment text are never changed. Dry run first (the default):
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py https_urls
```
**Expected:** one line per field with counts, then `dry run: N URLs would be rewritten, M left on http` (and a count of comments containing http://, which is never changed). Review the "left" hosts. Then:
```bash
<STACK_PYTHON> manage.py https_urls --apply
```
**Expected:** `applied: N URLs rewritten, M left on http`; running it again prints `0 URLs rewritten`. yse\_test and yse\_experimental share one database, so run it once for both. No migration; modified dates are not touched.

**(4) Hosts that still use http (recheck from Ziggy now and then).** `skyvision.caltech.edu` (refuses https) and `schedule.ztf.uw.edu` (certificate for another name) are used by the ZTF scheduler; `datastore.ipp.ifa.hawaii.edu` by the YSE forced-photometry cron. Test each:
```bash
for h in skyvision.caltech.edu schedule.ztf.uw.edu datastore.ipp.ifa.hawaii.edu; do echo $h; curl -sS -o /dev/null -w '%{http_code}\n' "https://$h/"; done
```
**Expected:** a three-digit code without a certificate error. If a host works, set it in `settings.ini` under `[ztf]` (`obs_summary_url`, `schedule_url`; the defaults are the http URLs) or, for the datastore, the `[yse] stamp` / `detectability` keys (the template now shows https; Ziggy's own `settings.ini` is not changed by the deploy).
`plpsipp1v.stsci.edu`, `antares.noao.edu` and `api.keplerscience.org` no longer resolve; the code now points at `ps1images.stsci.edu`, the ANTARES host `api.antares.noirlab.edu` and https for Kepler (`[main] kepler_api_endpoint` overrides it). If Ziggy's `settings.ini` has `antaresapi=http://antares.noao.edu/api`, replace it with the template's `https://api.antares.noirlab.edu/v1` and test one ANTARES cron run.

<a id="staff-mfa"></a>

### Two-factor sign-in for staff (2026-10-11, #512) optional, off by default, per stack

With `[security] STAFF_MFA_REQUIRED: True`, an account with **Staff status** or **Superuser status** signs in with its password and then a 6-digit code from an authenticator app: Duo Mobile (as a third-party account) or any other TOTP app. Staff without an authenticator are sent to a setup page: a QR code (issuer `YSE_PZ`), one confirmation code, then 10 single-use backup codes, shown once. This covers every page, including admin and SQL Explorer. It does not affect non-staff accounts or requests that authenticate with an API token or HTTP Basic auth (crons, brokers, scripts). Wrong codes are rate-limited: each failure doubles the wait before the next try. New dependencies: django-otp 1.5.4 and segno 1.6.6 (pure Python, used for the QR code). The two-factor tables come from django-otp's own migrations, not a YSE\_App migration. With the flag off nothing changes, and with the flag on but the package missing the site still starts and logs a warning.

Do yse\_experimental first, then yse\_test, and production after #143. yse\_test and yse\_experimental share `YSE_test`, so step (c) runs once for both. `<STACK_DIR>` / `<STACK_PYTHON>` are from [section 0](#paths); `<VENV_OWNER>` is the venv directory's owner (`stat -c %U <venv dir>`).

**(a) Server clock.** TOTP codes depend on the time:
```bash
timedatectl | grep -i synchronized
```
**Expected:** `System clock synchronized: yes`. If it says `no`, ask the sysadmin to enable NTP first.

**(b) Install.** The yse\_experimental deploy installs `requirements.txt` into its own venv. For `yse_test_virtual` and production, run:
```bash
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip install "django-otp==1.5.4" "segno==1.6.6"
<STACK_PYTHON> -m pip show django-otp segno | grep -E '^(Name|Version)'
```
**Expected:** `Name: django-otp` / `Version: 1.5.4` and `Name: segno` / `Version: 1.6.6`.

**(c) Turn it on in grace mode and create the tables.** In `[security]` of `<STACK_DIR>/YSE_PZ/settings.ini`, set the grace end date about two weeks out. Until then, staff without an authenticator are offered setup once per session and can click "Not now". Staff who have set one up always enter a code.
```ini
STAFF_MFA_REQUIRED: True
STAFF_MFA_ENFORCE_AFTER: <YYYY-MM-DD>
```
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py migrate
<STACK_PYTHON> manage.py showmigrations otp_totp otp_static
<STACK_PYTHON> manage.py check
sudo systemctl restart apache2
```
**Expected:** `migrate` applies `otp_static.0001_initial` to `0003_add_timestamps` and `otp_totp.0001_initial` to `0003_add_timestamps`, or prints `No migrations to apply.` on the second stack of a shared database. `showmigrations` shows `[X]` on all six. `check` prints `System check identified no issues`. The env var `YSE_STAFF_MFA_REQUIRED=1` (or `0`) overrides the ini key, and `YSE_STAFF_MFA_ENFORCE_AFTER` overrides the date.

**(d) Keep the two-factor tables out of Explorer's database user.** If you did [Step E](#authz-flags) (per-table grants), the new `otp_*` tables are not granted, and the generator there now leaves them out:
```bash
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "SHOW GRANTS FOR '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';" | grep -c 'otp_'
```
**Expected:** `0`. If the Explorer user still has the database-wide `GRANT SELECT ON <DB>.*`, do Step E now. The schema browser already hides `otp_` tables.

**(e) Check.**
1. Sign in with your own staff account. **Expected:** the setup page with a QR code. In Duo Mobile, tap Add, then Use QR code, and scan it. Enter the 6-digit code. **Expected:** the backup-codes page. Store the codes, then Continue.
2. Sign out and sign in again. **Expected:** the code page. A wrong code gives "That code was not accepted"; the right code opens the page you asked for. `/yse_experimental/admin/` and `/yse_experimental/explorer/` work only after the code.
3. Sign in with a non-staff account. **Expected:** no code page.
4. Scripts are unaffected. After the next `runcrons` run, its log shows no new 401 or 403 errors. Or, with a token you already have for a script account: `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Token <TOKEN>" "https://ziggy.ucolick.org/yse_experimental/api/transients/?limit=1"` **Expected:** `200`.

**(f) Enforce.** After `STAFF_MFA_ENFORCE_AFTER` passes, setup is required automatically; no restart is needed. To enforce at once, delete the `STAFF_MFA_ENFORCE_AFTER` line and restart Apache. List staff who have not set up yet (prints a count only):
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py shell -c "from django.contrib.auth.models import User; from django.db.models import Q; from django_otp.plugins.otp_totp.models import TOTPDevice; s=User.objects.filter(Q(is_staff=True)|Q(is_superuser=True), is_active=True); print(s.exclude(id__in=TOTPDevice.objects.filter(confirmed=True).values('user_id')).count(), 'of', s.count(), 'staff not set up')"
```

**(g) Reset a user's authenticator (lost phone).** A superuser can do this in admin > Users: select the user, choose the action "Reset two-factor sign-in", then Go. Or from the shell, which also works when no superuser can sign in:
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py reset_staff_mfa <USERNAME>
```
**Expected:** `Removed N two-factor device(s); the user sets up a new authenticator at the next sign-in.` Any session that user had verified must enter a code again. After entering a code, a staff user can replace their own authenticator, or get new backup codes, at `/mfa/setup/`.

**(h) Service accounts.** Accounts that crons or scripts use should not be staff. If one must stay staff, use an API token for it (step (d) of [Login, session and transport settings](#authn)); token and Basic-auth requests do not need a code.

**Rollback:** set `STAFF_MFA_REQUIRED: False` (or delete the line) and restart Apache. Sign-in is password-only again at once. Enrolled devices stay in the tables, so turning the flag back on keeps them. To remove the tables as well, while the flag is still on: `<STACK_PYTHON> manage.py migrate otp_totp zero && <STACK_PYTHON> manage.py migrate otp_static zero`. Then set the flag off and restart Apache.

<a id="feedback"></a>

### Feedback button (2026-10-12, #443) optional, off by default, per stack

A "Feedback" button in the navbar lets a signed-in user send a bug report, idea or question. Feedback is **only stored** in the new table `YSE_App_feedback` (migration `0035_feedback`). Staff read it at `/feedback/review/` (or admin > Feedback), edit a draft, and press "Create GitHub issue" to post it to the public repository. Nothing is ever posted automatically, and the sender's name or email is never put in the issue. Until the flag is on, nothing changes: no button, and `POST /feedback/` returns 404. `<STACK_DIR>` / `<STACK_PYTHON>` are from [section 0](#paths).

**(a) Migration.** On yse\_experimental the deploy applies it; yse\_test shares `YSE_test`, so it is already there. On production it runs with the normal `migrate` in [section 3](#prod).
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py showmigrations YSE_App | grep 0035
```
**Expected:** `[X] 0035_feedback`. It creates one new table and changes no existing one.

**(b) GitHub token (Ryan, as repository owner).** On GitHub: Settings > Developer settings > Fine-grained tokens > Generate new token. Resource owner `Young-Supernova-Experiment`, repository access "Only select repositories" > `YSE_PZ`, permission **Issues: Read and write** (nothing else), an expiry date. Create the label `feedback` on the repository if it does not exist (Issues > Labels). Keep the token value out of email, chat, notes and `settings.ini`.

**(c) Store the token as an encrypted credential.** Production needs `[secrets] credentials_key` first (step 3.2). Sign in as a superuser, open admin > Encrypted credentials > Add, and enter:
- Name: `GitHub feedback issues`
- Service: `github_feedback` (exactly this)
- Kind: Generic; Owner group and Owner user: blank (staff only); Is active: checked
- New secret (JSON object): `{"token": "<GITHUB_TOKEN>"}`

Save. **Expected:** the list shows the row with secret `token=••••`. Check without printing the token:
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py shell -c "from YSE_App.services.feedback import github_token; print(bool(github_token()))"
```
**Expected:** `True`.

**(d) Turn the button on.** In `[site_settings]` of `<STACK_DIR>/YSE_PZ/settings.ini` (or env `YSE_FEEDBACK_ENABLED=1`):
```ini
FEEDBACK_ENABLED: True
```
Optional keys, with their defaults: `FEEDBACK_MAX_PER_HOUR: 5` (submissions per user per hour), `FEEDBACK_GITHUB_REPO: Young-Supernova-Experiment/YSE_PZ`, `FEEDBACK_GITHUB_LABEL: feedback`. Then:
```bash
cd <STACK_DIR> && <STACK_PYTHON> manage.py check && sudo systemctl restart apache2
```
**Expected:** `System check identified no issues (0 silenced).`

**(e) Check.**
1. Sign in as any user. **Expected:** a "Feedback" button left of "Light mode". Send a test message. **Expected:** "Thank you. Your feedback was saved for the YSE-PZ team to review."
2. Sign in as staff and open the user menu > "Feedback review". **Expected:** the test item with status New. Open it, then press "Dismiss" (do not post a test issue unless you want one on GitHub). **Expected:** status Dismissed.
3. Optional, posts a real public issue: on a second test item, edit the draft and press "Create GitHub issue". **Expected:** "GitHub issue created." and a link to the issue, labelled `feedback`, with no sender name. Close that issue on GitHub afterwards.

**(f) Explorer.** The schema browser already hides `YSE_App_feedback`. If you did [Step E](#authz-flags) (per-table grants), the new table is not granted and the generator there now leaves it out:
```bash
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "SHOW GRANTS FOR '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';" | grep -c 'YSE_App_feedback'
```
**Expected:** `0`. If the Explorer user still has the database-wide `GRANT SELECT ON <DB>.*`, feedback is readable through Explorer by Explorer users; do Step E before turning the button on.

**Rollback:** set `FEEDBACK_ENABLED: False` (or delete the line) and restart Apache. Stored feedback stays in the table and in the staff review page. To stop posting, untick "Is active" on the credential, or revoke the token on GitHub.

<a id="ghost-removal"></a>

### GHOST removal and requests 2.32.4 (2026-10-12, #613) one check recommended, the rest optional, per stack

The legacy GHOST host-association code is removed ([#613](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/613)). Host association is done by Prost inside the TNS crons, as before. Three classes leave `CRON_CLASSES`: `TNS_uploads.UpdateGHOST`, `host_associate.YSE` and `PS1_PhotoZ.YSE` (none had a crontab line in the inventory). `astro-ghost` is dropped from `requirements.txt`, and `requests` goes from 2.28.2 to 2.32.4 (Python 3.8; the file still resolves as a whole). `[main] ghost_path` stays: it is Prost's scratch directory. No migration. `<STACK_DIR>` / `<STACK_PYTHON>` are from [section 0](#paths).

**(a) Recommended check: no crontab line names a removed cron.** Run as each user that runs crons for the stack:
```bash
crontab -l | grep -n -E 'UpdateGHOST|host_associate|PS1_PhotoZ|add_ghost_hosts'
```
**Expected:** no output. If a line prints, delete it (`crontab -e`): that cron no longer exists, and `runcrons` would fail with an import error on that line. A bare `manage.py runcrons` line (no class name) needs no change; it just no longer runs the three classes.

**(b) requests 2.32.4 on venvs the deploy does not install** (yse\_test\_virtual, production; the yse\_experimental venv is updated by Deploy Stack). Optional: the code also works on 2.28.2.
```bash
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip install -r <STACK_DIR>/requirements.txt
<STACK_PYTHON> -c "import requests; print(requests.__version__)"
sudo systemctl restart apache2
```
**Expected:** `2.32.4`. While `astro-ghost` is still installed, `pip` may print a warning that astro-ghost requires `requests<2.29`. That warning is harmless; step (c) removes it. **Check:** log in on that stack and open a transient page.

**(c) Optional: uninstall astro-ghost.** pip does not remove a package that was dropped from the requirements file.
```bash
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip uninstall -y astro-ghost
<STACK_PYTHON> -c "import importlib.util; print(importlib.util.find_spec('astro_ghost'))"
```
**Expected:** `None`. Leave TensorFlow and the other packages alone; their removal is #186.

**(d) Optional: delete the GHOST files.** Nothing reads them any more. `<GHOST_PATH>` is the `[main] ghost_path` value in that stack's `settings.ini`; `<CRON_WORKDIR>` is the directory the crons run from.
```bash
ls -d <GHOST_PATH>/database 2>/dev/null && du -sh <GHOST_PATH>/database
rm -rf <GHOST_PATH>/database                     # GHOST.csv and the GHOST catalogue files
ls -d <CRON_WORKDIR>/transients_20* 2>/dev/null  # dated GHOST scratch trees, if any
```
Remove the dated trees only after checking that the listing is GHOST output. Keep `<GHOST_PATH>` itself: Prost writes there. `MLP_lupton.hdf5` and `sfddata-master/` in the stack's `STATIC_ROOT` were read only by `PS1_PhotoZ`; delete them only after checking that nothing else on the server uses them.

**(e) Optional: uninstall the packages only GHOST needed (#622).** `opencv-python`, `seaborn`, `joypy`, `imblearn`, `imbalanced-learn`, `rfpimp` and `mastcasjobs` are dropped from `requirements.txt` and `docker/requirements.web.lock`; nothing imports them. They stay installed on existing venvs and are harmless there.
```bash
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip uninstall -y opencv-python seaborn joypy imblearn imbalanced-learn rfpimp mastcasjobs
<STACK_PYTHON> -m pip check
```
**Expected:** `No broken requirements found.` Keep `scikit-learn`, `joblib`, `threadpoolctl` and `sfdmap` (Prost needs them) and TensorFlow, `protobuf` and `typing-extensions` (#186).

**Rollback:** redeploy the previous commit; `pip install -r requirements.txt` from that commit restores `astro-ghost` and `requests` 2.28.2.

### IERS table refresh (optional cron, per stack) nothing required

Web workers no longer download IERS Earth-orientation tables (#466: `iers.conf.auto_download = False` in `views.py` and `view_utils.py`, as `services/observability.py` already did), so a request never waits on the network for rise/set times. Refresh the astropy cache from a cron so command-line jobs (default `auto_download`) find a current table. Without it everything still works; UT1 accuracy only changes by milliseconds.

Copy

```
# check: prints the newest predicted MJD in the table (expected: an MJD roughly 100+ days after today)
<CRON_PYTHON> -c "from astropy.utils import iers; iers.conf.auto_download=True; t=iers.IERS_Auto.open(); print(t['MJD'].max())"
# crontab (weekly, Sunday 04:15), as the Unix user that owns the astropy cache
15 4 * * 0 <CRON_PYTHON> -c "from astropy.utils import iers; iers.conf.auto_download=True; iers.IERS_Auto.open()" >> <CRON_LOG_DIR>/iers_refresh.log 2>&1
```

<a id="cron-locks"></a>

### Cron locking and ingest HTTP timeouts (recommended, per stack) nothing required

From the `perf/ingest-cron` PR ([#472](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/472), [#473](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/473), [#474](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/474)) onward, django_cron takes a per-job **file lock** (`DJANGO_CRON_LOCK_BACKEND = YSE_App.common.cron_lock.FileLock (a thin subclass; django_cron 0.5.1 FileLock never refuses on Django 3.x)`). The old cache lock lived in each process's own memory, so two `runcrons` processes never saw each other and a slow job ran twice at once. The ingest crons' HTTP calls now time out (15 s to connect, 300 s per read, 900 s for their own POSTs to `/add_transient/`) and retry a connection error or a 502/503/504 on a GET up to 3 times. No migration, no pip step. With nothing changed, locks go to a per-checkout directory under `/tmp`, which is correct when one Unix user runs `runcrons` for that checkout. Repeat these steps for each stack: `<CHECKOUT>` is `/data/yse_pz/YSE_PZ_experimental`, `/data/yse_pz/YSE_PZ_test` or `/data/yse_pz/YSE_PZ`, and `<CRON_PYTHON>` is the interpreter on its crontab line.

**Step 1: find the crontab lines and the user that runs them.**

```bash
crontab -l | grep -n runcrons
whoami
```
**Expected:** one or more lines such as `* * * * * cd <CHECKOUT> && <CRON_PYTHON> manage.py runcrons >> <LOG> 2>&1`. Call the user `<CRON_USER>`. If `runcrons` lines for the same checkout are in two users' crontabs, do step 2. Otherwise step 2 is optional.

**Step 2 (needed only when two users run `runcrons` for one checkout): give the locks a shared, writable directory.** It is simpler to move every `runcrons` line for a checkout into one user's crontab. If that is not possible, use a directory owned by a group that both users belong to (`<SHARED_GROUP>`):

```bash
mkdir -p <LOCK_DIR> && chgrp <SHARED_GROUP> <LOCK_DIR> && chmod 2775 <LOCK_DIR>
test -w <LOCK_DIR> && echo writable
```
Then add this to `<CHECKOUT>/YSE_PZ/settings.ini`:
```
[site_settings]
CRON_LOCKFILE_PATH: <LOCK_DIR>
```
Then start each of those crontab lines with `umask 002 &&`, so each lock file is group-writable.
**Expected:** `writable`, run as each user that runs `runcrons`. django_cron treats a lock file it cannot open as "already running" and skips that job. A directory or lock file one user cannot write would stop that user's jobs from running at all.

**Check (both cases):**

```bash
cd <CHECKOUT> && <CRON_PYTHON> manage.py shell -c "from django.conf import settings; print(settings.DJANGO_CRON_LOCK_BACKEND); print(settings.DJANGO_CRON_LOCKFILE_PATH)"
```
**Expected:** `YSE_App.common.cron_lock.FileLock`, then `<LOCK_DIR>` (or `/tmp/yse_pz_cron_locks_<hash>` with no `CRON_LOCKFILE_PATH`). During a pass, `ls <that directory>` shows one `<CronClass>.lock` file per running job.

**Step 3 (recommended): wrap each `runcrons` line in `flock -n`, so a pass that is still running makes the next one exit at once instead of piling up.** Give each crontab line its own lock file, so the per-minute job-runner line is never held up by a slow ingest pass:

```bash
command -v flock
```
**Expected:** `/usr/bin/flock`. Then edit with `crontab -e`, changing each line from:
```
* * * * * cd <CHECKOUT> && <CRON_PYTHON> manage.py runcrons >> <LOG> 2>&1
```
to:
```
* * * * * flock -n <LOCK_DIR>/runcrons-<NAME>.flock -c 'cd <CHECKOUT> && <CRON_PYTHON> manage.py runcrons >> <LOG> 2>&1'
```
where `<NAME>` is different on each line (for example `all`, `jobs`, `lsst`).

**Check:**

```bash
flock -n <LOCK_DIR>/runcrons-test.flock -c 'sleep 20' &
sleep 1; flock -n <LOCK_DIR>/runcrons-test.flock -c 'echo ran'; echo "exit $?"
```
**Expected:** `exit 1` and no `ran`: the second command gave up because the lock was held. After the next minute, `<LOG>` grows as before, and the `django_cron_cronjoblog` rows keep appearing.

**Optional settings** (`[site_settings]` in `<CHECKOUT>/YSE_PZ/settings.ini`; defaults shown; restart Apache after a change, and the crons pick it up on their next pass):
```
QUERY_API_CACHE_SECONDS: 300              # /query_api/ result reuse; 0 = run the saved SQL every call
INGEST_HTTP_CONNECT_TIMEOUT_SECONDS: 15
INGEST_HTTP_READ_TIMEOUT_SECONDS: 300
INGEST_HTTP_UPLOAD_TIMEOUT_SECONDS: 900    # the crons' own POSTs to /add_transient/
INGEST_HTTP_RETRIES: 3
```
If a cron log starts showing `ReadTimeout` on `/add_transient/`, raise `INGEST_HTTP_UPLOAD_TIMEOUT_SECONDS`. Before the PR those uploads had no timeout.

<a id="data"></a>

### API token authentication for scripts (optional, per stack) nothing required

Issues [#457](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/457) (token auth) and [#458](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/458) (`request.user` set after Basic auth). `rest_framework.authtoken` was already installed and migrated, so there is no migration and nothing to run for the deploy. Basic and Session auth keep working. Scripts that call the DRF endpoints under `/api/` can move to a token, which skips the password hash that Basic auth runs on every request. The decorated data-upload views in `data_utils.py` (`add_transient` and similar) still use Basic auth only. Per-user API keys behind a flag are tracked separately in [#449](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/449).

Check that the token table exists (run from the checkout of the stack, with that stack's Python):

```
cd <STACK_CHECKOUT> && <PROD_PYTHON> manage.py showmigrations authtoken
```

Expected: `[X] 0001_initial`, `[X] 0002_auto_20160226_1747` and `[X] 0003_tokenproxy` (the last only on newer DRF). Create a token for a script's account (placeholders; do not paste the token anywhere shared):

```
cd <STACK_CHECKOUT> && <PROD_PYTHON> manage.py drf_create_token <SCRIPT_USERNAME>
```

Expected: `Generated token <TOKEN> for user <SCRIPT_USERNAME>`. Use `drf_create_token -r <SCRIPT_USERNAME>` to rotate it. Check from the script's host:

```
curl -s -H "Authorization: Token <TOKEN>" "<SITE_URL>/api/transients/?limit=1"
```

Expected: HTTP 200 JSON with `count` and `results`. A wrong token returns 401 `Invalid token.`. In Python: `requests.get(url, headers={"Authorization": "Token <TOKEN>"})`.

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

## Credential rotation (#528)

Run on Ziggy as the checkout owner. Use placeholders only; never paste the real values into GitHub, chat or this file. Rotate when a value may have been exposed, or on the schedule you prefer. Each step ends with the same restart and check.

**Django SECRET_KEY.** Generate a new key and set it as `SECRET_KEY` under `[site_settings]` in `YSE_PZ/settings.ini` (or the `DJANGO_SECRET_KEY` environment variable):

```bash
<PROD_PYTHON> -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```
**Effects:** every logged-in session is invalidated (users log in again) and password-reset links issued before the change stop working. The stored-credentials Fernet key (`EncryptedCredential`) is separate: with `IS_DEBUG` False it must be set on its own (`[secrets] credentials_key` or `YSE_CREDENTIALS_KEY`, from `<PROD_PYTHON> manage.py generate_credentials_key`) and rotating `SECRET_KEY` does not change it. Only a stack running with `IS_DEBUG` True and no credentials key derives it from `SECRET_KEY`; there, rotating `SECRET_KEY` makes stored credentials unreadable, so set a fixed credentials key first (keep the old key in the comma-separated list until credentials are re-saved).

**ZTF forced-photometry account password.** Change it with the ZTF forced-photometry service for the registered account, then set `ztfforcedphotpass=<NEW_ZTF_PASSWORD>` under `[ztf]` in every `settings.ini` that uses it (production and test checkouts), or the `ZTF_FORCED_PHOT_PASS` environment variable.

**Shared SMTP/IMAP mail account password.** Change the password of the shared mail account at the mail provider (use an app password where the provider requires one), then update `SMTP_PASSWORD` under `[SMTP_provider]` and the matching IMAP/mail password keys under `[main]` (`dbemailpassword` and any other key holding the same password) in every `settings.ini`.

**Other keys.** `DATABASE_PASSWORD` / `EXPLORER_PASSWORD` (or `YSE_DATABASE_PASSWORD` / `YSE_EXPLORER_PASSWORD`) must be changed in MySQL first (`ALTER USER ... IDENTIFIED BY '<NEW_PASSWORD>'`) and then in settings.ini. SciServer (photo-z crons) reads `sciserveruser` / `sciserverpass` under `[main]` or `SCISERVER_USER` / `SCISERVER_PASSWORD`.

```bash
sudo systemctl restart apache2
curl -s -o /dev/null -w '%{http_code}\n' https://<HOST>/<PATH_PREFIX>/login/
```
**Expected:** `200`. **Check:** log in, open a transient, and for the ZTF and mail steps request ZTF forced photometry once (alert `success: ...`). **If not:** restore the previous `settings.ini` value, restart Apache again and tell Ryan.

**New CI job (#529):** a `gitleaks` job scans each PR's commits. It needs nothing on Ziggy. A new finding on a PR means a secret-like value was committed; remove it and rotate the value.

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
| Slack events check, then optional login/session/transport keys per stack | `/slack/events/` needs both `SLACK_ENABLED` and `SLACK_SIGNING_SECRET`; lockout, session length, host/proxy, HTTPS cookies and HSTS are opt-in `[security]` keys | [login, session and transport settings](#authn) | pending |
| Runbook Part A (yse\_test on develop, dedupe, chown) | Deploy green 07:34; dedupe run 07:37; Ryan confirmed 07:41 | [runbook](https://claude.ai/artifact/Epp8VK8jZm2VqNBDBLUj7c) | done |
| Migration 0010 on the shared YSE\_test database | Applied by the experimental deploy of #333, 17:23-17:42 UTC | [run 36604581592](https://github.com/Young-Supernova-Experiment/YSE_PZ/actions/runs/36604581592) | done |

<a id="changelog"></a>

## 6 · Changelog

* 2026-10-12 v45, optional. Prost scratch isolation ([#617](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/617)): each Prost run now writes to its own `prost-*` subdirectory under the scratch base and removes only that subdirectory (it used to delete the whole `[main] ghost_path`, which could break overlapping TNS crons). Nothing required: `[main] ghost_path` keeps working. Optionally add `[main] prost_path=<PROST_SCRATCH_DIR>` to the stack's `settings.ini`; when set it is used instead of `ghost_path`. Stale `prost-*` directories older than one day are cleaned up on the next run. No migration.
* 2026-10-12 v44, optional. Requirements cleanup after GHOST ([#622](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/622)): `opencv-python`, `seaborn`, `joypy`, `imblearn`, `imbalanced-learn`, `rfpimp` and `mastcasjobs` leave `requirements.txt` and `docker/requirements.web.lock`. New optional step (e) in [GHOST removal](#ghost-removal): `pip uninstall` them. No migration.
* 2026-10-12 v43, one check recommended, the rest optional. GHOST removal ([#613](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/613)): `UpdateGHOST`, `host_associate` and `PS1_PhotoZ` leave `CRON_CLASSES`, `astro-ghost` leaves `requirements.txt`, and `requests` goes to 2.32.4. New section [GHOST removal](#ghost-removal): check that no crontab line names a removed cron; optional pip upgrade on yse\_test\_virtual and production, `pip uninstall astro-ghost`, and deletion of the GHOST database files. No migration.
* 2026-10-12 v42, optional (off by default). Feedback button ([#443](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/443)): new section [Feedback button](#feedback). Migration `0035_feedback` (one new table, `YSE_App_feedback`; safe on the shared database). New `[site_settings]` keys `FEEDBACK_ENABLED` (default off; env `YSE_FEEDBACK_ENABLED`), `FEEDBACK_MAX_PER_HOUR`, `FEEDBACK_GITHUB_REPO`, `FEEDBACK_GITHUB_LABEL`. The GitHub token is an encrypted credential with service `github_feedback`, never a settings.ini value. Step E's grant generator now also leaves out `YSE_App_feedback`. No new dependency.
* 2026-10-12 v41, informational, nothing required. https follow-up ([#441](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/441)): hard-coded http links replaced (own links from `YSE_PUBLIC_BASE_URL`; dead hosts repointed; the two ZTF hosts that refuse https are now `[ztf]` settings with http defaults), new `manage.py https_urls` (dry run by default, `--apply` to write). New section [https follow-up](#https-followup) lays out the Apache vs Django redirect choice and the order redirect, then HSTS with a short max-age; **a decision for David and Ryan, nothing is switched on**. No migration, no dependency.
* 2026-10-12 v40, one check recommended. Photometry statistics leftovers ([#269](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/269), [#270](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/270), [#271](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/271)): migrations `0033_photstat_extrapolated` (four nullable columns) and `0034_transient_photstat_view` (Explorer view `yse_transient_photstat_v`), applied by the deploy; new section [Photometry statistics view and upkeep jobs](#photstat-view): check the view and the Explorer user's SELECT on it, create it by hand if the deploy user lacks `CREATE VIEW`. New hourly `PhotStatRefresh` cron (in `CRON_CLASSES`, runs from the existing `runcrons` pass; Bazin write-back off by default). No new dependencies.
* 2026-10-11 v39, optional (off by default). Two-factor sign-in for staff and superusers ([#512](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/512)): new section [Two-factor sign-in for staff](#staff-mfa). New dependencies django-otp 1.5.4 and segno 1.6.6; django-otp's own tables (`otp_totp`, `otp_static`) are created by `manage.py migrate` once the flag is on. New `[security]` keys `STAFF_MFA_REQUIRED` and `STAFF_MFA_ENFORCE_AFTER` (grace date), env `YSE_STAFF_MFA_REQUIRED` / `YSE_STAFF_MFA_ENFORCE_AFTER`, `manage.py reset_staff_mfa` and an admin action to reset a user's authenticator. The Explorer grant generator (Step E) now also leaves out `otp_*` tables. No YSE\_App migration; nothing changes until the flag is set.
* 2026-10-11 v38, **required on venvs the deploy does not install** (yse\_test\_virtual, production). Low-risk dependency pin bumps ([#539](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/539)): new section [Dependency pin bumps](#dep-pins) (`pip install -r requirements.txt`, `pip check`, version check). The yse\_experimental venv is updated by Deploy Stack. Django stays on 3.2. No migration.
* 2026-10-11 v37, informational. Transient tables show "Last Det." and "Last Obs." from TransientPhotStat ([#557](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/557)); personal-dashboard section counts are visible rows only ([#558](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/558)). Nothing to run on yse\_experimental / yse\_test (the shared database is backfilled). On production the two columns are empty for transients without a stat row until `rebuild_photstats` (step 3.10, already required for Peak Mag) has run. No migration.
* 2026-10-11 v36, one check recommended. `/query_api/` saved-query allowlist and row visibility ([#524](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/524)): the crons' four titles are on the built-in allowlist and service accounts are exempt from the row filter; check that the `get_YSE_PAs.py` accounts are staff or in `api_writers` ([section](#query-api)). Optional `QUERY_API_ALLOWED_QUERIES` and rollback switch `QUERY_API_VISIBILITY_FILTER`. No migration.
* 2026-10-11 v35, **required before relying on the new defaults**. Hard-coded credentials removed ([#528](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/528)): the template `public_settings.ini` now holds placeholders (it is not used on Ziggy, whose own `settings.ini` is unchanged), docker needs `YSE_DATABASE_PASSWORD` / `YSE_EXPLORER_PASSWORD` in `docker/.env`, and the SciServer photo-z crons read `sciserveruser` / `sciserverpass` from `[main]` of `settings.ini` (add them on each stack that runs those crons, else the crons stop with a clear error). New section "Credential rotation". ZTF forced phot and the legacy position-angle helpers no longer use a shell, so ZTF forced phot no longer needs `wget` ([#530](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/530), [#531](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/531)). Gemini now verifies the server certificate; if the ODB uses a private CA set `gemini_ca_bundle=<PATH_TO_CA_BUNDLE>` under `[main]` or `GEMINI_CA_BUNDLE` ([#532](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/532)). The healpix loader no longer uses `LOAD DATA LOCAL INFILE` ([#533](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/533)). gitleaks CI job added ([#529](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/529)).
* 2026-10-11 v34, optional. Performance audit server settings ([#482](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/482)-[#490](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/490)): new section "Performance audit 2026-10-10: server settings" (mod_deflate, hashed static files + Cache-Control, `prune_finder_charts`, `prune_auditlog`, LOGGING / cached_db sessions, plot-stack preload, slow query log). Nothing required; no migration.
* 2026-10-11 v33, informational. Write authorization and Explorer access ([#520](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/520)-[#527](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/527)): migration `0032_api_writer_groups` (data only: groups `api_writers` and `explorer_users`). Nothing is required; the three new `[site_settings]` flags are off by default. New optional section [Write authorization and Explorer access](#authz-flags): check the groups, add the cron and script accounts to `api_writers`, turn on `API_WRITE_PERMISSIONS_ENFORCED` / `UPLOAD_GROUP_CHECKS_ENFORCED` / `EXPLORER_RESTRICTED_ACCESS`, and limit the Explorer MySQL user's grants to science tables.
* 2026-10-11 v32, **one required check** (Slack events: `SLACK_ENABLED` and `SLACK_SIGNING_SECRET` must both be set on a stack that receives Slack events), the rest optional: [login, session and transport settings](#authn) (sec/authn PR). New optional dependency django-axes 5.41.1 (+ django-ipware 5.0.2) with `migrate axes` once per database; new `[security]` keys (session length, lockout, API rate limits, ALLOWED\_HOSTS requirement, forwarded headers, password-reset domain, HTTPS cookies / redirect / HSTS, CSP report-only, inactive-account exemptions), optional `[database]` TLS keys, `manage.py deactivate_inactive_users`. Every default is today's behaviour.
* 2026-10-11 v31, optional. IERS refresh cron ([#466](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/466)): web workers no longer download IERS tables; new "IERS table refresh" subsection (weekly cron plus a check). **Nothing required on Ziggy.**

* 2026-10-11 v30, informational. Legacy views hardening (#495-#504): every page needs a login except a public allowlist, logout is POST-only, and the legacy TNS submit, follow-up notice, on-call, target-list, finder-chart and image/status views check login (and POST / ownership where they change data). **Nothing required on Ziggy.** Verify with the two curls in "Login required on every page"; rollback switch `LOGIN_REQUIRED_MIDDLEWARE = False` is documented there.
* 2026-10-10 v29, informational. Docker compose hardening (localhost-only published ports, Redis password required when the optional Redis service starts, pinned MySQL / nginx / Redis image tags). The compose files are for local development and CI only; Ziggy does not use them: **nothing required on Ziggy**.
* 2026-10-10 v28, informational. Deploy Stack now triggers on `push` to `experimental` / `develop` only (a merged PR is a push, so deploys still happen) and all workflows declare least-privilege `permissions`. **Nothing required on Ziggy.** After the first merge following this change, check on GitHub Actions that Deploy Stack fired once on the push.
* 2026-10-10, optional. Ingest / cron performance ([#471](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/471)-[#476](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/476)): django_cron now uses a per-job file lock, and ingest HTTP calls have timeouts and retries. New subsection [Cron locking and ingest HTTP timeouts](#cron-locks): an optional shared `CRON_LOCKFILE_PATH`, recommended `flock -n` on each `runcrons` crontab line, and optional `QUERY_API_CACHE_SECONDS` / `INGEST_HTTP_*` keys. No migration, no pip step.
* 2026-10-10, informational. Perf audit P16/P17 ([#477](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/477), [#478](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/478)): migration `0031_spatial_time_indexes` (seven indexes, no column changes) applies on the shared `YSE_test` database with the experimental deploy, nothing to run there. New optional production step 3.32: row counts, `SHOW INDEX` and EXPLAIN before and after 3.9, build time to budget.
* 2026-10-10 informational, nothing required. API token authentication for scripts ([#457](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/457)) and Basic auth now sets `request.user` ([#458](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/458)); optional token setup is in the "API token authentication" subsection.
* 2026-10-07 v27, **required**. Job runner for the shared `YSE_test` database ([#431](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/431)): drain with `run_jobs`, then one per-minute crontab line; without it SALT fits and other queued work never run on yse_test / yse_experimental. **Required** `settings.ini` check for ZTF forced phot (ZTF answers 400; it is the submitting address/password) and the YSE forced-phot cron (fixed by #419, via #428).
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
