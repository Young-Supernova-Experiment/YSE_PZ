# YSE\_PZ: commands for David to run on Ziggy

> **Canonical copy.** This file replaces the claude.ai runbook artifact (last artifact version v23, then v24 as Markdown), moved into the repo on 2026-10-02 (#400). Update it in the same PR as any change that needs a step on Ziggy. Steps use placeholders (`<PROD_PYTHON>`, `<PROD_DB>`, `<PROD_DB_USER>`, `<PROD_DB_HOST>`, `<CRON_PYTHON>`); never put secrets, credentials or personal data here.


A living checklist for David Jones, in the order things should happen. It replaces the scattered deploy notes in [PR #143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143), [PR #335](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/335) and the [2026-09-29 runbook](https://claude.ai/artifact/Epp8VK8jZm2VqNBDBLUj7c). Every command is copy-pasteable with real paths; anything we could not read from the repo is a <PLACEHOLDER> to fill in once.

**Done:** [#335](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/335) merged to `develop` (5da66de) and deployed green to yse\_test at 18:01 UTC; migrations `0010_dashboard_indexes` (18 min) through 0029 (#341 PhotStat 18:43 UTC to #386 broker streams) already applied on the shared `YSE_test` database, nothing on yse\_test to run for them; [#143](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/143) (develop to master) is conflict-free with 8/8 checks green and its body carries all 66 `Fixes` lines.

**OPTIONAL MITIGATION (2026-10-12): onboarding gate on `/yse_test/`** ([#634](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/634)). Ryan reported that after #633 pages on `/yse_test/` break unless you start on the dashboard. Until the fix PR for #634 is merged and deployed, you may turn the gate off on yse\_test only:

```bash
cd /data/yse_pz/YSE_PZ_test
grep -n "ONBOARDING_GATE_ENABLED" YSE_PZ/settings.ini || echo "not set"
```
**Expected:** `not set` (or an existing line). Add under the existing `[site_settings]` section of `YSE_PZ/settings.ini` (do not add a second `[site_settings]` header):

```ini
ONBOARDING_GATE_ENABLED: False
```
(or export `YSE_ONBOARDING_GATE=0` in that stack's web environment), then `sudo systemctl restart apache2`.
**Check:** `/data/yse_pz/yse_test_virtual/bin/python manage.py shell -c "from django.conf import settings; print(settings.ONBOARDING_GATE_ENABLED)"` prints `False`, and a signed-in user who has not accepted the agreement opens a transient page on `/yse_test/` without a redirect. After the fix deploys, delete the line and restart Apache again (the check then prints `True`).

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

<a id="onboarding"></a>

### User agreement, researcher profile and onboarding gate (develop-direct PR; #627-#631) required key step, flags optional

Lands on `develop` first (a `develop-direct/*` PR merged by Ryan), then is merged back into `experimental`. Migrations `0031_researcher_profile` (new tables only) and `0032_seed_user_agreement` (loads agreement v1) run automatically on deploy and are safe on the shared `YSE_test` database. No new Python dependency (`cryptography` is already pinned).

**Effect on deploy:** the onboarding gate is **on by default**. Every signed-in browser user is sent to `/agreement/` and then `/profile/edit/` on their next page view. Scripts, crons and brokers are not affected (`/api/`, Authorization headers, every `login_or_basic_auth_required` ingest view, `query_api`, Slack, callbacks, `.json` status endpoints, static/media). Superusers can always reach `/admin/`.

**Step A (required for the encrypted fields): install ONLY the research public key.** Ryan generates the key pair on his own machine (`python manage.py generate_research_keypair --out-dir <DIR> --passphrase`) and sends David `research_public_key.pem` and its fingerprint. The private key never goes to Ziggy. On each stack (`<CHECKOUT>` = `/data/yse_pz/YSE_PZ_test`, `/data/yse_pz/YSE_PZ_experimental`, later production; `<PYTHON>` is that stack's Python from [section 0](#paths)):

```bash
install -m 0644 research_public_key.pem <CHECKOUT>/YSE_PZ/research_public_key.pem
# then in <CHECKOUT>/YSE_PZ/settings.ini:
#   [research]
#   public_key_path: <CHECKOUT>/YSE_PZ/research_public_key.pem
sudo systemctl restart apache2
```

**Check:**

```bash
cd <CHECKOUT> && <PYTHON> manage.py shell -c "from YSE_App.services import research_crypto as r; print(r.key_available(), r.current_fingerprint())"
```

Expected: `True <fingerprint>`, matching what Ryan sent. If it prints `False`, the admin index shows a warning to staff; the site still works and the encrypted fields (country, gender, PhD year) show "not available yet", with the PhD-year requirement waived. **Never copy a file named `research_private_key.pem` to Ziggy.**

**Step B (optional): rollback flag.** To turn the gate off without a deploy, add to `<CHECKOUT>/YSE_PZ/settings.ini` and restart Apache:

```ini
[site_settings]
ONBOARDING_GATE_ENABLED: False
```

(or export `YSE_ONBOARDING_GATE=0` in the web environment). **Check:** a signed-in user who has not accepted the agreement opens `/dashboard/` without a redirect. Remove the line (or set `True`) to turn it back on.

**Step C (optional, off by default): SciX researcher metrics.** Admin > Encrypted credentials > Add: kind Generic, service `scix`, name e.g. "SciX service account", secret `{"token": "<SciX API token of the service account>"}`. Then:

```bash
cd <CHECKOUT> && <PYTHON> manage.py refresh_researcher_metrics --even-if-disabled --limit 1
```

Expected: `researcher metrics: fetched 1, skipped 0, failed 0` (or `fetched 0` if no profile has an ORCID yet). To run it monthly from the existing `runcrons` crontab, add to settings.ini:

```ini
[research]
RESEARCHER_METRICS_ENABLED: True
```

Optional `[research]` keys (defaults): `RESEARCHER_METRICS_CRON_MINUTES: 43200`, `REQUEST_INTERVAL_SECONDS: 2.0`, `MAX_PER_RUN: 200`, `MIN_AGE_DAYS: 25`, `SCIX_API_URL: https://api.adsabs.harvard.edu/v1`.

**Step D (optional, recommended): Explorer grants.** The new tables are hidden from SQL Explorer and refused by its SQL blacklist. The hard barrier is the Explorer MySQL user: if it has database-wide `SELECT`, consider replacing that with table-level grants that leave out `YSE_App_researcherprofile*`, `YSE_App_encryptedprofilevalue`, `YSE_App_profilefieldhistory`, `YSE_App_mentorship`, `YSE_App_affiliationhistory`, `YSE_App_researchermetrics`, `YSE_App_useragreement`, `YSE_App_agreementacceptance`, `YSE_App_datasharingdelegate`, `YSE_App_dataaccessgrant` and `YSE_App_allocationdelegate`. Check with `SHOW GRANTS FOR '<EXPLORER_DB_USER>'@'<host>';`.

Production: the same steps A-D after the `develop -> master` merge (`<PROD_PYTHON>`, `/data/yse_pz/YSE_PZ`).

### Per-stack session and CSRF cookies (develop-direct PR; #637) optional, recommended

Ziggy serves production, `/yse_test/` and `/yse_experimental/` on one host. With Django's default cookie names (`sessionid`, `csrftoken`) and path `/`, signing in to one stack replaces the cookies of the others, so users bounce to login or lose their place. The PR makes the names and paths configurable; **defaults are unchanged**, so nothing changes until you set them. No migration, no dependency.

**Effect:** setting these logs **everyone on that stack out once** (their old cookie is no longer read). Do it at a quiet time.

Set, per stack, in `<CHECKOUT>/YSE_PZ/settings.ini` (or the env vars `YSE_SESSION_COOKIE_NAME`, `YSE_SESSION_COOKIE_PATH`, `YSE_CSRF_COOKIE_NAME`, `YSE_CSRF_COOKIE_PATH`, which win over the file), then `sudo systemctl restart apache2`:

```ini
# yse_test (/data/yse_pz/YSE_PZ_test)
[site_settings]
SESSION_COOKIE_NAME: sessionid_yse_test
SESSION_COOKIE_PATH: /yse_test/
CSRF_COOKIE_NAME: csrftoken_yse_test
CSRF_COOKIE_PATH: /yse_test/

# yse_experimental (/data/yse_pz/YSE_PZ_experimental)
[site_settings]
SESSION_COOKIE_NAME: sessionid_yse_experimental
SESSION_COOKIE_PATH: /yse_experimental/
CSRF_COOKIE_NAME: csrftoken_yse_experimental
CSRF_COOKIE_PATH: /yse_experimental/
```

Production: leave these keys out (it keeps `sessionid`, `csrftoken` and `/`). Add them to the existing `[site_settings]` section rather than a second one. The path must match the URL prefix the stack is served under (with a trailing slash).

**Check:**

```bash
cd <CHECKOUT> && <PYTHON> manage.py shell -c "from django.conf import settings as s; print(s.SESSION_COOKIE_NAME, s.SESSION_COOKIE_PATH, s.CSRF_COOKIE_NAME, s.CSRF_COOKIE_PATH)"
```

Expected: the values you set for that stack. Then sign in to `/yse_test/` and `/yse_experimental/` in the same browser: the browser's cookie list shows `sessionid_yse_test` (path `/yse_test/`) and `sessionid_yse_experimental` (path `/yse_experimental/`), and signing in to one no longer signs you out of the other. To undo, remove the keys and restart (one more logout).

<a id="teams"></a>

### Research teams: group owners, invitation links and user settings (develop-direct PR; #445, #440) nothing required, owners optional

Lands on `develop` first (a `develop-direct/*` PR merged by Ryan), then is merged back into `experimental`. Migrations `0033_group_profiles` (new tables only: `YSE_App_groupprofile` with its owners table, `YSE_App_groupinvitation`, `YSE_App_groupinvitationuse`, `YSE_App_userpreference` with three group tables) and `0034_seed_group_profiles` (one profile per existing group, **no owners**, every group active; Public, `api_writers`, `explorer_users` and any group holding model permissions marked *system*) run automatically on deploy and are safe on the shared `YSE_test` database. No new dependency.

**Effect on deploy:** nothing changes for users until owners are assigned. New pages: `/teams/` (My teams), `/teams/<id>/` (team page), `/teams/join/...` (invitation links) and `/settings/` (group pickers and defaults), linked from the user menu and from My profile. Audience pickers stop offering `api_writers` / `explorer_users` (Public stays).

**Step A (optional): check the seed.**

```bash
cd <CHECKOUT> && <PYTHON> manage.py ensure_group_profiles
```

Expected: `group profiles: created 0, marked system 0` (the migration already did it). A non-zero `created` only means groups were added by code that predates group profiles; running it again prints zeros.

**Step B (David or Ryan, in the admin): assign owners.** Owners are data, not code (Ryan, 2026-10-11).

1. Admin > Authentication and Authorization > Groups > click the team.
2. In the **Group profile** box at the bottom: move one or more users into **Owners**, optionally add a **Description**, leave **Is active** ticked, leave **Is system** unticked. Save.
3. The admin refuses owners on a system group ("System groups cannot have owners"). Public, `api_writers`, `explorer_users` and permission groups keep staff-only membership, edited on the same Group page as before.

**Check:** the owner signs in, opens User menu > My teams, and sees the team with role "Owner"; its page shows Members, "Invite people" and the invitation list.

**Step C (optional): mark a group inactive.** Undecided which groups start inactive, so all start active. Staff: the same Group page, untick **Is active**; or an owner on `/teams/<id>/`. Inactive groups disappear from pickers and default audiences only: members, data and who can see existing data are unchanged. Tick it again to undo.

**Invite flow (for owners; nothing to run on the server):**

1. On `/teams/<id>/`, "Invite people": choose the expiry (default 14 days, at most 60) and how many people may use the link (default 1). Click "Create link" and copy the link: it is shown **once** and only its hash is stored. Send it yourself (email, Slack). No email address is stored.
2. The invitee opens the link. Signed out: they sign in and come back to it (or create an account at `/signup/` when self-signup is on, [section "Self-service signup"](#self-signup)). Signed in: they land on `/teams/join/`.
3. A new user is first sent to the User Agreement and the profile (the onboarding gate), then back to `/teams/join/`. They click "Join <team>" and become a member; owners get a notification.
4. Owners can revoke an open link, and remove members (not owners) on the team page.

**Step D (optional): purge old links.** Expired, revoked or used-up invitations older than 90 days can be deleted (open links and memberships are never touched):

```bash
cd <CHECKOUT> && <PYTHON> manage.py purge_group_invitations --dry-run
cd <CHECKOUT> && <PYTHON> manage.py purge_group_invitations
```

Expected: `group invitations: would delete N ...` then `deleted N`.

**Optional settings** (`<CHECKOUT>/YSE_PZ/settings.ini`, restart Apache), defaults shown:

```ini
[groups]
SYSTEM_GROUP_NAMES: Public, api_writers, explorer_users
PICKER_VISIBLE_SYSTEM_GROUPS: Public
INVITE_DEFAULT_DAYS: 14
INVITE_MAX_DAYS: 60
INVITE_MAX_USES_LIMIT: 50
INVITE_PURGE_DAYS: 90
```

**Explorer:** `YSE_App_groupinvitation*` and `YSE_App_userpreference*` are hidden from SQL Explorer and its SQL blacklist; add them to the table-level grant exclusions of [step D above](#onboarding) if you set those up.

Production: steps A-D after the `develop -> master` merge (`<PROD_PYTHON>`, `/data/yse_pz/YSE_PZ`).

<a id="data-pis"></a>

### PIs on private data (develop-direct PR; #446) nothing required, PI accounts and backfill optional

Lands on `develop` first (a `develop-direct/*` PR merged by Ryan), then is merged back into `experimental`. Migration `0035_data_pis` adds one nullable column, `YSE_App_principalinvestigator.user_id`, and two new join tables, `YSE_App_transientphotometry_pis` and `YSE_App_transientspectrum_pis`. It runs automatically on deploy, writes no data, and is safe on the shared `YSE_test` database. No new dependency.

**Effect on deploy:** nothing changes until PIs are set. The upload-spectrum form, the `add_transient_spec` / `add_transient_phot` upload APIs (`"pis": [<PrincipalInvestigator id>, ...]`), the REST API and the admin can set PIs on a spectrum or photometry set. The spectra and photometry tables show the PI names, and the "restricted data" hint shows the PI names of hidden data. The "Register interest" form has two opt-in boxes: "Tell the PIs" and "Also request access".

**Step A (optional, David or Ryan, in the admin): link PIs to their accounts.** Only a linked PI is notified or can accept and decline requests.

1. Admin > YSE_App > Principal investigators > click the PI.
2. Set **User** to the PI's YSE-PZ account (the magnifier opens a user search). Save.

**Check:** on a transient with private data that has this PI, a second user who cannot see the data registers an interest with both boxes ticked. The PI gets one "Data access request" notification, and the request is in the PI's Data access requests inbox.

**Step B (optional): backfill PIs on existing data.** This fills only datasets that have no PI yet, from an allocation that covers the spectrum (same instrument, or same telescope, with the observation date inside its dates) or from the PIs linked to the dataset's observation group. Anything ambiguous or unmatched stays blank. By default it is a dry run on restricted data only.

```bash
cd <CHECKOUT> && <PYTHON> manage.py backfill_data_pis
```

Expected: `datasets to fill: N (allocation A, obs_group O); ambiguous X; unmatched U` then `dry run: nothing written (pass --apply to write)`. If the numbers look right:

```bash
cd <CHECKOUT> && <PYTHON> manage.py backfill_data_pis --apply
```

Expected: the same first line, then `written: N datasets`. **Check:** running the dry run again prints `datasets to fill: 0`. Options: `--include-public` also fills public data, `--max-pis 2` accepts up to two PIs per dataset, and `--source obs_group` uses one source only. The command only adds PIs and never removes any. To undo, remove PIs per dataset in the admin.

Production: steps A-B after the `develop -> master` merge (`<PROD_PYTHON>`, `/data/yse_pz/YSE_PZ`).

<a id="research-consent"></a>
### User Agreement v2, research consent and the research export (develop-direct PRs; #743, #744, #746) optional notice setting; manual purge when notified; export only when Ryan asks

Lands on `develop` first (`develop-direct/*` PRs merged by Ryan: #745, then the follow-up), then is merged back into `experimental`. Four migrations run automatically on deploy and are safe on the shared `YSE_test` database:

* `0038_user_agreement_v2`: adds User Agreement version 2 and makes it current (v1 and its acceptances stay). Idempotent.
* `0039_research_consent`: two new tables, `YSE_App_researchconsentform` and `YSE_App_researchconsentrecord`. No change to existing tables.
* `0040_seed_research_consent`: loads the consent form text as version 1. Its go-live date is the day this migration first runs on that database (production: its deploy day). Idempotent.
* `0041_research_consent_prompts`: one new table, `YSE_App_researchconsentprompt` (when the form was offered, for reminders), filled once for users who already accepted agreement v2. Idempotent.

Experimental has its own `0038`-`0050` under other names; the develop -> experimental backport adds a merge migration. No new dependency.

**Effect on deploy:**

* Every signed-in browser user accepts agreement v2 once (the existing gate). They then see the research consent form, which offers Consent, Decline and Not now. It never blocks the site and is always available from My profile.
* Users who have not answered are reminded once more, after 10 logins or 1 month (whichever comes first), then once a year at their first login. Anyone who answered is never reminded.
* Only the essential profile fields are asked of everyone: name, publication name, acknowledgment text, institution, ORCID and observing experience. Everything else in the profile is research data, offered only to users who consent.
* Declining or withdrawing deletes nothing automatically. The user is excluded from research at once, and the administrators below get an in-app notification and an email.
* Superusers see read-only consent records in the admin (no values). The three new tables are hidden from SQL Explorer. If you set up table-level Explorer grants ([step D above](#onboarding)), also exclude `YSE_App_researchconsentform`, `YSE_App_researchconsentrecord` and `YSE_App_researchconsentprompt`.

**Step A (optional): who is notified of opt-outs.** By default every active superuser is notified. To notify only some accounts (for example David), add to `<CHECKOUT>/YSE_PZ/settings.ini` and restart Apache:

```ini
[research]
optout_notify_usernames: <USERNAME>[,<USERNAME>...]
```

(or export `YSE_RESEARCH_OPTOUT_NOTIFY_USERNAMES=<USERNAME>,...` in the web environment). Email arrives only while site email is on and the recipient has email on for "Interests and data access" in `/notifications/preferences/`.

**Step B (each time you are notified): purge that user's research data by hand.** The notice names the user. First a dry run, which prints counts and deletes nothing:

```bash
cd <CHECKOUT> && <PYTHON> manage.py purge_unconsented_demographics --user <USERNAME>
# then:
cd <CHECKOUT> && <PYTHON> manage.py purge_unconsented_demographics --user <USERNAME> --apply
```

Without `--user` the command covers every user who declined or withdrew. What it deletes:

* the encrypted values (country, PhD year, gender);
* the research profile fields (career level, department, pronouns, interests, website, GitHub, time zone);
* their field history;
* the advisor and student rows the user entered themselves (rows the other person entered stay), affiliation history and SciX metrics.

It keeps the username, names, email, institution, ORCID, observing experience, groups and roles, and all site activity. Consenting users and users who have not answered are never touched. A second run prints `Nothing to delete.`

**Step C (only when Ryan asks): write the anonymized research database.**

```bash
cd <CHECKOUT> && <PYTHON> manage.py export_research_database --out <SECURE_DIR>/yse-research-<DATE>.sqlite
```

The command writes a new SQLite file readable only by its owner, and refuses to overwrite an existing file unless you pass `--force`. Hand the file to Ryan and do not leave copies on shared disks. The file holds no names, usernames, emails, ORCID iDs or credentials: people, also where comments mention them, appear only as random strings drawn fresh for each export, so two exports are hard to join.

**Check (optional):**

```bash
cd <CHECKOUT> && <PYTHON> manage.py shell -c "from YSE_App.models import UserAgreement as A, ResearchConsentForm as F; print(list(A.objects.filter(is_current=True).values_list('version', flat=True)), list(F.objects.values_list('version', 'effective_date', 'is_current')))"
```

Expected: `['2'] [('1', datetime.date(<first deploy day>), True)]`.

Production: the migrations run with the others after the `develop -> master` merge; Steps A-C apply there too (`/data/yse_pz/YSE_PZ`).

<a id="data-sharers"></a>
### Who may share private data: PIs and their deputies (develop-direct PR; #447) nothing required, but check PI links first

Lands on `develop` first (a `develop-direct/*` PR merged by Ryan), then is merged back into `experimental`. Migration `0037_data_sharing_delegates` adds one new table, `YSE_App_datasharingdelegate`, and one nullable column, `YSE_App_dataaccessrequest.decided_as`. It runs automatically on deploy, writes no data, and is safe on the shared `YSE_test` database. No new dependency.

**Effect on deploy (behaviour change, Ryan 2026-10-13):**

* Only the data's PIs (linked accounts, see [Step A of "PIs on private data"](#data-pis)) and the deputies they designate may accept or decline data access requests, or change the groups of restricted data through the REST API. Members and owners of the owner group no longer can.
* Data without an explicit PI uses its allocation's PI, at read time: the follow-up's ToO / classical / queued resource's allocation PI (or the resource's own PI), or the follow-up's facility request's allocation PI. Nothing is written.
* Data with neither can only be shared by staff. Such requests notify the active superusers only (#650; staff who are not superusers can still decide, and see them in the inbox), and the staff inbox marks them "No PI assigned". Every staff decision is recorded as an administrator override (`decided_as = staff`, shown in the inbox and the admin) and logged by the `YSE_App.data_access` logger at WARNING.
* Pending requests filed before the deploy follow the same rule.
* New page `/data_sharing_deputies/` (linked from the Data access requests page): a PI adds or removes deputies, for all of their data or for one group's data. Staff can manage any PI's deputies in the admin (Principal investigators > a PI > "Data sharing delegates", or Data sharing delegates).
* TNS imports now set `reference` (`TNS <prefix> <name>`) on new photometry sets and spectra.
* SQL Explorer shows the PI tables again (Refs #643).
* `YSE_App_datasharingdelegate` (the deputies) is hidden from SQL Explorer and its SQL blacklist (#650). If you set up table-level Explorer grants ([step D above](#onboarding)), also exclude `YSE_App_datasharingdelegate`.

**Step A (recommended before or right after deploy, Ryan or David): link the PIs of private data to accounts.** Otherwise only staff can decide requests for that data. Use [Step A](#data-pis) and, if wanted, the `backfill_data_pis` [Step B](#data-pis) of "PIs on private data".

**Check:** sign in as a member of a group with restricted data who is not its PI: `/data_access_requests/` says "Nothing to decide". Sign in as the linked PI: the request is listed with Accept / Decline. As staff, deciding shows "You decided request #N as an administrator; the override is logged."

Production: Step A after the `develop -> master` merge (`/data/yse_pz/YSE_PZ`).

<a id="self-signup"></a>

### Self-service signup and team membership requests (develop-direct PR; #448, #440 follow-up, #643) nothing required, signup off until turned on

Lands on `develop` first (a `develop-direct/*` PR merged by Ryan), then is merged back into `experimental`. Migration `0036_group_membership_requests` adds one new table, `YSE_App_groupmembershiprequest`, runs automatically on deploy, writes no data, and is safe on the shared `YSE_test` database. No new dependency (plain `django.contrib.auth`; no allauth, no CAPTCHA service).

**Effect on deploy (flag off):** nothing opens. `/signup/`, `/signup/verify/...`, `/settings/email/` and new membership requests return 404, and the login page shows no "Create an account" link. Always on after deploy:

* the login page has a "Forgot password?" link (Django's password reset, already routed);
* the **Public** group is always offered in audience pickers: a user's hidden groups, an inactive flag and `PICKER_VISIBLE_SYSTEM_GROUPS` can no longer take it out (#440 follow-up);
* the PrincipalInvestigator REST API returns only `url`, `id` and `name` to non-staff (staff still see email, phone and the rest; non-staff can still send those fields when they create or update a PI), and `YSE_App_groupmembershiprequest` is hidden from SQL Explorer and its SQL blacklist (#643). If you set up table-level Explorer grants ([step D above](#onboarding)), also exclude `YSE_App_groupmembershiprequest`. The PI tables (`YSE_App_principalinvestigator`, `YSE_App_principalinvestigator_obs_group`) stay visible in Explorer (Ryan, 2026-10-13; changed by the #447 PR): do not exclude them.

**Before Step A:** owners should exist on the teams that will take requests ([section "Research teams"](#teams), Step B). Only active, non-system teams with at least one active owner can be requested.

**Step A (Ryan or David): turn signup on**, on one stack at a time (yse\_test first). In `<CHECKOUT>/YSE_PZ/settings.ini`:

```ini
[site_settings]
SELF_SIGNUP_ENABLED: True
```

Then restart Apache (or the stack's WSGI process) as for any settings change. **Check:**

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<STACK_URL>/signup/
```

Expected: `200` (it was `404` before). The login page now shows "Create an account". Rollback: set `SELF_SIGNUP_ENABLED: False` (or env `YSE_SELF_SIGNUP=0`) and restart; existing accounts and pending requests stay, and owners can still decide pending requests.

**What a new user sees:** `/signup/` asks for a username, an email address and a password (Django's password validators). The account is **active at once** (no staff approval, Ryan 2026-10-13) and only in Public. They are signed in and sent through the User Agreement and the researcher profile (the onboarding gate), then land on `/teams/`, where "Ask to join a team" lists the requestable teams. The owners get a notification (in-app, email and Slack by default: "Interests and data access" preference group) and approve or deny on the team page; the requester is notified of the decision.

**Email verification** (on by default when email delivery is on, `NOTIFICATION_EMAIL_ENABLED`): the address is not stored on the account until its owner opens the emailed link and presses Confirm (the link holds the address encrypted with a key derived from `SECRET_KEY`, expires after 72 hours and works once). Until then the account gets no email and cannot reset its password by email; the user can resend the link from `/settings/email/`. If the server cannot send mail, signup still works and the page says so. **Check:** sign up with a test address you control on yse\_test, receive the email, confirm, and see the address under `/settings/email/` ("has a confirmed email address").

**Spam guard** (no external service): a hidden honeypot field, a minimum fill time, and hourly limits kept in the cache (per client IP, hashed, and site-wide). Behind Apache's reverse proxy every request may come from the proxy address, so the per-IP limit then acts site-wide; to count real client addresses set `SELF_SIGNUP_CLIENT_IP_HEADER: HTTP_X_FORWARDED_FOR` (only if the proxy always sets that header). Without Redis (`REDIS_URL`) the counters are per WSGI process.

**Step B (optional): purge old requests.** Approved, denied and withdrawn requests decided more than 90 days ago (pending ones and memberships are never touched):

```bash
cd <CHECKOUT> && <PYTHON> manage.py purge_group_membership_requests --dry-run
cd <CHECKOUT> && <PYTHON> manage.py purge_group_membership_requests
```

Expected: `group membership requests: would delete N ...` then `deleted N`.

**Optional settings** (`<CHECKOUT>/YSE_PZ/settings.ini`, restart), defaults shown:

```ini
[site_settings]
SELF_SIGNUP_ENABLED: False
SELF_SIGNUP_EMAIL_VERIFICATION: <same as NOTIFICATION_EMAIL_ENABLED>
SELF_SIGNUP_VERIFY_HOURS: 72
SELF_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR: 10
SELF_SIGNUP_ACCOUNTS_PER_HOUR: 30
SELF_SIGNUP_MIN_FILL_SECONDS: 3
SELF_SIGNUP_CLIENT_IP_HEADER:

[groups]
REQUEST_PURGE_DAYS: 90
REQUESTS_PER_USER_PER_DAY: 10
```

Production: Step A only when Ryan decides, after the `develop -> master` merge (`<PROD_PYTHON>`, `/data/yse_pz/YSE_PZ`).

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

<a id="transient-visibility"></a>

### One visibility policy for transient lists, per-scope photometry statistics (2026-10-16, #607) migration applied by the deploy; one backfill required per database

**What changes.** Non-staff users now see the same transients on the search page, the dashboards, the API and the exports: every object with a TNS name, plus the objects whose data they may see (`docs/transient-search.md`, section "Who sees which transients and which values"). Values derived from photometry (Last Det., Last Obs., Peak Mag, filters, sorting, `/api/transientphotstats/`, the export, Bazin magnitudes) come only from photometry they may see. Staff see what they saw before (until #706: staff now see only what they have access to; [section](#staff-visibility)).

**Migration:** `0042_transient_photstat_scope` adds one new table, `YSE_App_transientphotstatscope`. It changes no existing table and the deploy applies it. **No new package.**

**Required, once per database** (the shared `YSE_test` once, production after the promotion), from the stack's checkout with its Python:

```
<PYTHON> manage.py backfill_photstat_scopes            # dry run: prints how many rows would be written
<PYTHON> manage.py backfill_photstat_scopes --apply    # writes them; safe to re-run, unchanged rows are not written
```

**Expected:** the dry run counts the transients with photometry restricted to a group (TNS imports in the Public group included) and writes nothing; `--apply` prints `Applied: N transients; scope rows created M, ...`; a second `--apply` prints `created 0, updated 0, deleted 0`. It reads photometry in batches of 500 and can run while the site is up. Until it has run, non-staff users see empty derived values for transients with restricted photometry (never the values of data they may not see).

**Re-run `--apply`** (optional) after deleting a group in the admin, or after photometry was uploaded through yse\_test while it still runs older code (both stacks share the database and only the newer code writes the scope rows).

**Explorer grants (only if you limited them, [Step E](#authz-flags)):** a search that a non-staff user saves as a dashboard query reads the new table; add `SELECT` on `YSE_App_transientphotstatscope` for the Explorer MySQL user, as for `YSE_App_transientphotstat`.

<a id="enable-access-rules"></a>

### Enable access rules on yse\_experimental (#648 PR 6) when Ryan asks; first yse\_experimental only

The access-rule work (#648 PRs 1-6a, #706, #714) is merged with `DATA_ACCESS_GRANTS_ENABLED` **off**, which is the default. Leave it off until Ryan asks. Then turn it on **on yse\_experimental first**. The stacks share the database, so the data steps below run once and serve both stacks; only the flag differs per stack. User guide: [`docs/data-access-rules.md`](data-access-rules.md). Developer notes: [`docs/access-rules-engine.md`](access-rules-engine.md).

**1. Prerequisites (any order; each is a dry run first):**

```
<PYTHON> manage.py showmigrations YSE_App | tail -3      # 0045, 0046, 0047 applied
<PYTHON> manage.py backfill_data_programs                # optional: link datasets to programs (counts only)
<PYTHON> manage.py backfill_data_programs --apply
<PYTHON> manage.py migrate_point_programs                # optional: copy header program links onto points
<PYTHON> manage.py migrate_point_programs --apply
```

**2. Turn the flag on for yse\_experimental only.** Either:
- set `DATA_ACCESS_GRANTS_ENABLED: True` under `[site_settings]` in `/data/yse_pz/YSE_PZ_experimental/YSE_PZ/settings.ini`; or
- export `YSE_DATA_ACCESS_GRANTS_ENABLED=1` in that stack's service environment.

Then restart its gunicorn.

**3. Rebuild the stored PhotStat scopes once, so they include existing group rules:**

```
<PYTHON> manage.py backfill_photstat_scopes              # dry run
<PYTHON> manage.py backfill_photstat_scopes --apply
```

After this, rule changes rebuild the affected scopes through the `photstat.sync_scopes` job, which the existing job runner (`RunQueuedJobs`) executes.

**4. Check:**
- **As a PI:** `/data_access/` (the dashboard) and `/data_access/programs/` load, and a transient's photometry tab shows "Access rules for this object's data".
- **As a user outside a group:** the transient lists and tables match what they saw before, because no rules exist yet.
- **The crons** keep uploading and reading (`DATA_SERVICE_READERS`, [section](#staff-visibility)).

**Rollback:** set the flag back to `False` (or unset the variable) and restart. Every read returns to the collaboration-group rule at once, and the rules stay stored. If you rolled back after scopes were rebuilt with rules, run `backfill_photstat_scopes --apply` again.

**Later, yse\_test and production:** repeat step 2 on that stack when Ryan asks. Step 3 is only needed once per database. Run it again after any period in which the flag was off on every stack while rules changed.

<a id="ned-desi-redshifts"></a>

### Host redshifts from NED and DESI, with provenance (2026-10-18, #436, #437): migration applied by the deploy; backfill and the hourly sweep optional (off by default)

**What changes on deploy.** Migration `0049_redshift_provenance` adds three nullable columns: `Host.redshift_source`, `Host.redshift_override` and `Transient.redshift_override`. They are online in MySQL 8, with no table copy, and safe for the shared `YSE_test` database. No new package: NED and Data Lab are queried with plain HTTP. One precedence list now governs every automatic redshift writer, including the TNS re-sync, the TNS crons and the upload endpoints (see `docs/redshift-precedence.md`):

- An override set by hand always wins.
- Otherwise NED > DESI > our own > other catalogues > no source > TNS > photo-z.

Uploads therefore no longer overwrite a NED or DESI host redshift, or an override. The detail page shows each redshift's source.

The deploy also adds `manage.py host_redshifts` (the backfill, dry run by default), `manage.py redshift_override` and the `Redshift_Catalogs.RedshiftCatalogSweep` cron. The cron is in `CRON_CLASSES` but does nothing until `REDSHIFT_CATALOG_CRON_ENABLED` is set. Each check is recorded as a run of the `ned-redshift` or `desi-redshift` external service (created on first use).

**Before any `--apply`:** a host redshift that someone entered by hand and that must stay should be marked as an override first (`<PYTHON> manage.py redshift_override --host <id> --z <z>`). Older rows have no source, so NED and DESI rank above them.

**Backfill, per database (both test stacks share one), dry run first:**

```bash
cd <CHECKOUT> && <PYTHON> manage.py host_redshifts --max 50                 # what would change
<PYTHON> manage.py host_redshifts --max 500 --apply                         # repeat until "0 hosts"
```
**Expected:** lines such as `host 123 (...) [ned]: set z None (no source) -> 0.0312 (NED)`, then a `Done:` summary with the request counts. Requests are spaced 1 s apart per catalogue. Each run skips hosts checked in the last 30 days, so repeating the same line continues the backfill. `--services ned` limits the run to one catalogue, and `--only-empty` to hosts without a redshift.

**Hourly sweep (optional, after the backfill looks right).** Add a new `[redshifts]` section to `YSE_PZ/settings.ini` (every other key has a default; see `public_settings.ini`):

```ini
[redshifts]
REDSHIFT_CATALOG_CRON_ENABLED: True
```
Or set `YSE_REDSHIFT_CATALOG_CRON=1` for the cron. Then restart. The job runner must be running (the `run_jobs` crontab line). Each hour one `redshifts.sweep` job checks up to `REDSHIFT_CATALOG_MAX_PER_RUN` (100) hosts, starting with those never checked, then those last checked more than `REDSHIFT_CATALOG_RECHECK_DAYS` (90) ago. While the sweep is enabled, a new host association from an upload also queues one `redshifts.host_lookup` job. **Check:** `/yse_test/jobs/` lists `redshifts.sweep` jobs as `done`, with a summary of what changed.

<a id="osc-step1"></a>
### Open Supernova Catalog, step 1: download and offline dry run (2026-10-30, #887, #905, #906): when Ryan asks; writes nothing to any database

**What this is.** The first step of the OSC import plan (docs/osc-import.md, "Plan for the full import"). It downloads the OSC data once (about 13 GB of git data, 55,500 files) and the TNS public-objects file, and runs two dry runs. **Nothing is written to any database.** These commands need #896 deployed on yse\_experimental, so they use that checkout. Its Python is `/data/yse_pz/YSE_PZ_experimental/venv/bin/python`, called `<EXP_PYTHON>` below. `<OSC_DIR>` is a new directory of your choice on a disk with at least 50 GB free, e.g. `/data/osc`.

**O1. Disk space.**
```bash
df -h /data
```
**Expected:** at least 50G in the `Avail` column for the disk holding `<OSC_DIR>`. **If not:** pick another disk for `<OSC_DIR>` and tell Ryan.

**O2. Download the OSC data** (30-90 minutes; it keeps running if the session drops):
```bash
mkdir -p <OSC_DIR>/reports && cd <OSC_DIR>
nohup sh -c 'for r in sne-pre-1990 sne-1990-1999 sne-2000-2004 sne-2005-2009 sne-2010-2014 sne-2015-2019; do git clone --depth 1 https://github.com/astrocatalogs/$r.git; done' > reports/clone.log 2>&1 &
```
**Check, when `jobs` (or `pgrep -f "git clone"`) shows nothing running:**
```bash
for r in <OSC_DIR>/sne-*; do echo "$r $(ls $r | grep -c json)"; done
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

**If not:** send Ryan `<OSC_DIR>/reports/clone.log`.

**O3. TNS public-objects file** (one request to TNS, with the bot credentials the TNS crons use):
```bash
cd /data/yse_pz/YSE_PZ_experimental && <EXP_PYTHON> manage.py osc_tns_check --download-tns-file <OSC_DIR>/tns_public_objects.csv.zip
```
**Expected:** `saved <OSC_DIR>/tns_public_objects.csv.zip; N objects`, where N is in the hundreds of thousands. Keep this file; production uses the same one. **If it fails:** send Ryan the error. The TNS bot credentials may need to be named; nothing else depends on this step.

**O4. Offline dry run of every entry** (no database access; 1-3 hours):
```bash
cd /data/yse_pz/YSE_PZ_experimental && nohup <EXP_PYTHON> manage.py import_osc <OSC_DIR>/sne-* --offline --quiet --report <OSC_DIR>/reports/offline.jsonl > <OSC_DIR>/reports/offline.log 2>&1 &
```
**Expected, when it finishes:** the last line of `<OSC_DIR>/reports/offline.log` reads `Dry run: 55xxx entries, 0 written (0 new transients), N failed, ... s`. N may be a few dozen (unreadable files are expected and are what this step is for).

**O5. Summary of the dry run:**
```bash
cd /data/yse_pz/YSE_PZ_experimental && <EXP_PYTHON> manage.py osc_report_summary <OSC_DIR>/reports/offline.jsonl --review-dir <OSC_DIR>/reports/offline-review > <OSC_DIR>/reports/offline-summary.txt
```
**Expected:** `offline-summary.txt` starts with `entries: 55xxx`. `offline-review/` holds four CSV files.

**O6. Instrument census** (reads the database only to name known instruments; writes nothing):
```bash
cd /data/yse_pz/YSE_PZ_experimental && <EXP_PYTHON> manage.py catalog_instruments --census <OSC_DIR>/sne-* --top 300 > <OSC_DIR>/reports/instrument-census.txt
```
**Expected:** the first line names the database (`Using database YSE_test on ...`), then up to 300 lines of `count kind telescope instrument -> target`.

**O7. Send Ryan** `offline-summary.txt`, `instrument-census.txt` and `offline-review/failures.csv` from `<OSC_DIR>/reports/`. They hold only public catalogue data. Keep `<OSC_DIR>`: every later step reads it.

<a id="spectrum-arrays"></a>
### Spectra stored as arrays (2026-10-28, #492, #891): migration applied by the deploy; test conversion on yse\_experimental when Ryan asks

**What changes on deploy.** Migration `0056_spectrum_arrays` adds `YSE_App_transientspectrumarrays` and `YSE_App_hostspectrumarrays` (new tables only). New uploads on yse\_experimental store pixels as arrays. Existing spectra keep their pixel rows and display as before.

**Test conversion (yse\_experimental, `YSE_test`), only when Ryan asks:**

```bash
cd <CHECKOUT> && <PYTHON> manage.py convert_spectra_to_arrays --transient --max 200            # dry run
<PYTHON> manage.py convert_spectra_to_arrays --transient --max 200 --apply
```
**Expected:** a count of spectra to convert, then `converted N` with no verification failures. **Check:** open a few converted transients on `/yse_experimental/`: the spectra plot, the spectra tab point counts and "Download spectra" look as before. **Never** add `--delete-rows` on `YSE_test` until this change is on `develop` (yse\_test still reads pixel rows).

**Rollback:** `SPECTRUM_PIXEL_STORAGE: rows` in `[site_settings]` makes uploads write pixel rows again; reading needs no change.

<a id="tns-resync"></a>

### TNS re-sync and backfill (2026-10-18, #438) one crontab line to remove; backfill and the hourly sweep optional (off by default)

**What changes on deploy.** Nothing runs by itself. The deploy adds `manage.py tns_resync` (backfill, dry run by default) and the `TNS_Resync.TNSResync` cron (in `CRON_CLASSES`, a no-op until `TNS_RESYNC_CRON_ENABLED` is set). It applies only differences from TNS: the TNS classification (`best_spec_class` only when ours is empty or was the earlier TNS class), the redshift when ours is empty or came from TNS, and new photometry points and spectra. New data are public and carry the TNS reference, the same as the TNS cron's imports. Each re-sync is recorded as a run of the `tns` external service (created on first use), so the sweep and the backfill resume. No migration, no new package.

**`TNS_recent_realtime` is removed** from `CRON_CLASSES`. It ran only at 00:00 and 08:00 with a 10-minute window. `TNS_emails`, `TNS_recent` and the gap import of the new sweep cover it. The class stays as a no-op, so an old crontab line is harmless, but please remove it:

```bash
crontab -l | grep -n "realtime"          # -> the tns_updates_realtime.bash line, if any
crontab -e                               # delete that line
```

**Required before any `--apply`:** the `[main] dblogin` cron account must be staff, a superuser or in `api_writers`; this is already needed for the TNS crons (v68). The command uses the `[main]` TNS bot keys, or `[sharing] TNS_RESYNC_SERVICE` (a production TNS sharing service), and never the sandbox.

**Backfill, per database (both test stacks share one), dry run first.** Find the cron gaps first (django_cron keeps 30 days of logs, so older gaps come from your notes):

```bash
cd <CHECKOUT> && <PYTHON> manage.py shell -c "from django_cron.models import CronJobLog as L; [print(l.code, l.start_time) for l in L.objects.filter(code__contains='TNS_recent').order_by('start_time')]"
```

1. Gap import of objects made public while the crons were down (for example since 2025-01-01):

```bash
<PYTHON> manage.py tns_resync --missing-only --since 2025-01-01            # counts only
<PYTHON> manage.py tns_resync --missing-only --since 2025-01-01 --apply
```
**Expected:** `Gap import: N TNS objects public since ..., M not in YSE-PZ`, then one `imported` line per object.

2. Re-sync of the existing TNS-named transients, in batches (each run skips the transients done in the last 7 days, so repeat the same line until it reports `Re-sync: 0 transients`):

```bash
<PYTHON> manage.py tns_resync --since 2025-01-01 --max 200                 # what would change
<PYTHON> manage.py tns_resync --since 2025-01-01 --max 500 --apply
<PYTHON> manage.py tns_resync --max 500 --apply                            # then everything else, oldest sync first
```
**Expected:** lines such as `2025abc: updated: TNS_spec_class None -> 'SN Ia'; 3 new photometry points` and a `Done:` summary with the number of TNS requests. Requests are spaced by `TNS_REQUEST_INTERVAL_SECONDS` (1 s); on a TNS rate limit the command waits and continues. `--no-spectra` skips spectrum downloads, and `--status Ignore` limits the run to one status.

**Hourly sweep (optional, after the backfill looks right).** Under the existing `[sharing]` section of `YSE_PZ/settings.ini`:

```ini
TNS_RESYNC_CRON_ENABLED: True
```
(or `YSE_TNS_RESYNC_CRON=1` for the cron), then restart. The job runner must be running (`run_jobs` crontab line, see above). Each hour one `tns.resync_sweep` job imports objects public in the last `TNS_RESYNC_GAP_DAYS` (3) that are missing, then re-syncs up to `TNS_RESYNC_MAX_PER_RUN` (50) transients: active statuses every `TNS_RESYNC_ACTIVE_DAYS` (2), others every `TNS_RESYNC_OTHER_DAYS` (30), within `TNS_RESYNC_BUDGET_SECONDS` (900). **Check:** `/yse_test/jobs/` lists `tns.resync_sweep` jobs as `done`, with a summary of what changed.

Note: `manage.py expire_service_runs` also deletes old `tns` runs; those transients are then re-synced again, which is harmless.

<a id="duplicates"></a>

### Duplicate transients are reported, merged only by hand (2026-10-21, #752, #753, #754) nothing required; no migration, no new package

**What changes on deploy.** The `CheckDuplicates` cron no longer deletes or merges anything. It finds the same candidate pairs as before (an internal `1...` name at the position of another transient; a transient whose name is another one's slug) and sends each new pair once as a notification to the reviewers (default: every active superuser), with the merge command in the text. Uploads (`/add_transient`, broker saves) that find several transients with the same name update the oldest and report the others instead of deleting them. A failed check is reported the same way (no more SMTP from the cron).

**Optional: who gets the reports.** Under the existing `[site_settings]` section of `YSE_PZ/settings.ini`, then restart:

```ini
DUPLICATE_REPORT_USERNAMES: <USERNAME_1>,<USERNAME_2>
```

**Reviewing a pair** (on either stack's checkout, with its python):

```bash
cd /data/yse_pz/YSE_PZ_experimental
venv/bin/python manage.py transient_duplicates list
venv/bin/python manage.py transient_duplicates merge <DUPLICATE_ID> <KEEP_ID>          # dry run
venv/bin/python manage.py transient_duplicates merge <DUPLICATE_ID> <KEEP_ID> --yes    # merge
```

Expected: `list` ends with `N candidate pair(s)`. The dry run prints both transients, the rows that a merge would NOT move (they are deleted or unlinked with the duplicate), and `dry run: nothing changed`. With `--yes` it prints `merged: alternate_names=..., photometry=..., spectra=..., tags=...`. **Check:** the duplicate's page is gone and the kept transient lists the old name under alternate names. A merge is one transaction: on any error nothing is changed.

**Old automatic behaviour (not recommended):** `DUPLICATE_AUTO_MERGE: True` under `[site_settings]` (or `YSE_DUPLICATE_AUTO_MERGE=1` in the cron environment) makes the cron merge every pair it finds, using the same transactional merge.

<a id="followup-enrich"></a>

### Automatic TNS and ZTF refresh on a follow-up request (2026-10-18, #114) job runner must be running; no migration, no new package

**What changes on deploy.** Each new follow-up on a classical night (ToO and queued follow-ups are unchanged) queues one `followup.enrich` job after the request is saved. The job re-syncs the object from TNS (the #438 function, as the service account) and submits ZTF forced photometry the same way the transient page button does (a `ztf` allocation open to the requester first, then the legacy script). An object that was re-synced from TNS, or queried at ZTF, in the last 24 hours is skipped for that service. Each ZTF submission, from the button too, is now recorded as a run of the `ztf_forced` external service (created on first use); no migration. Failures (TNS rate limit or outage, failed ZTF submission) retry through the job queue, three attempts, and never delay the request. With `PROST_HOST_MATCH_ENABLED` (off by default, see [Enrichment framework](#enrichment)) the job also runs a Prost host match for an object without a host.

**Needed:** the job runner (`run_jobs` crontab line, see above), and for TNS the same bot keys and cron account as the [TNS re-sync](#tns-resync) step. **Check:** after a classical-night request, `/yse_test/jobs/` lists a `followup.enrich` job as `done`, with a summary of what the TNS and ZTF steps did.

**Turn it off** (per stack) under the existing `[site_settings]` section of `YSE_PZ/settings.ini`, then restart:

```ini
FOLLOWUP_AUTO_ENRICH: False
```
(or `YSE_FOLLOWUP_AUTO_ENRICH=0` in the web environment). `FOLLOWUP_ENRICH_WINDOW_HOURS` (default 24) sets the skip window.

<a id="enrichment"></a>

### Enrichment framework: `manage.py enrich` and one sweep (2026-10-18, #439) nothing required; no migration, no new package

**What changes on deploy.** One registry of enrichment services and one run log (`ExternalServiceRun`, one row per object and service; a check that changes nothing, or fails again, updates the previous row). Registered services: `tns`, `ned`, `desi`, `ztf-forced` (the #438, #436/#437 and #114 functions, unchanged), `prost`, `photoz-ml`, `sdss-photoz`, `ps1-cutouts`, `gaia-lc` and `blast`. GHOST is gone (#613) and is not a service. The deploy adds `manage.py enrich` (dry run by default) and the `Enrichment.EnrichmentSweep` cron (in `CRON_CLASSES`, a no-op until `ENRICH_SWEEP_CRON_ENABLED` is set).

What runs differently by itself:

- **`PS1_cutouts.YSE` no longer downloads again.** Same schedule, same files and rows. It skips hosts that already have PS1 (GPC1) cutout images, and a cutout that failed is retried after `PS1_CUTOUT_RETRY_DAYS` (30) and at most `PS1_CUTOUT_MAX_ATTEMPTS` (3) times in a row, instead of every 30 minutes.
- `Photo_Z.YSE`, `SDSS_Photo_Z.YSE` and `Gaia_LC.GaiaLC` keep their schedules and selections. They now record each object's outcome as a run (`photoz-ml`, `sdss-photoz`, `gaia-lc`). TensorFlow and SciServer are imported only when a prediction runs.
- The Prost matching in the TNS crons is now `YSE_App.services.prost_hosts` (the same priors, catalogues and scratch directory), so the crons behave as before.

**Check (optional, per stack):**

```bash
cd <CHECKOUT> && <PYTHON> manage.py enrich --list
```
**Expected:** one line per service (`blast` shows `[needs BLAST_ENABLED]`).

**Backfill a service over a selection (optional), dry run first.** The defaults select only objects that lack the service's data and are due by the run log, so repeating a line continues where it stopped:

```bash
<PYTHON> manage.py enrich --service prost --since 2025-01-01 --max 50            # lists what would run
<PYTHON> manage.py enrich --service prost --since 2025-01-01 --max 200 --apply   # repeat until "0 transient(s)"
<PYTHON> manage.py enrich --service ps1-cutouts --retry-failed --rerun --max 20 --apply
<PYTHON> manage.py enrich --service sdss-photoz --max 1000 --apply --queue        # as enrich.objects jobs
```
**Expected:** `Applying prost: N transient(s)`, one line per change or error, then `Done: updated N, no_match N, ...`. Other options: `--names`, `--ids`, `--status`, `--until`, `--never-run`, `--all-candidates`. A Prost match links the host through the upload path; its spectroscopic redshift is written only where the redshift precedence list allows it (never over an override or a NED/DESI value). `ztf-forced` needs an explicit selection.

**Prost for objects without a host (optional).** Under a new `[enrichment]` section of `YSE_PZ/settings.ini`, then restart:

```ini
[enrichment]
PROST_HOST_MATCH_ENABLED: True
```
(or `YSE_PROST_HOST_MATCH=1`). Then the TNS gap import (#438) queues one `enrich.objects` job per imported object without a host, and the follow-up enrichment job (#114) runs a Prost match for an object without a host. `[main] prost_path` (or `ghost_path`) is the scratch directory, as for the TNS crons. **Check:** `/yse_test/jobs/` lists `enrich.objects` jobs as `done`.

**Hourly sweep (optional).** Under `[enrichment]`:

```ini
ENRICH_SWEEP_CRON_ENABLED: True
ENRICH_SWEEP_SERVICES: prost,ps1-cutouts
```
Then restart; the job runner must be running. Each hour one `enrich.sweep` job runs each listed service on up to `ENRICH_SWEEP_MAX_PER_SERVICE` (100) due objects: never run, a failure older than the service's retry wait, or a success older than its recheck period. It stops after `ENRICH_SWEEP_BUDGET_SECONDS` (900). Do not list `tns`, `ned` or `desi` there while their own sweeps are on.

**Blast (optional; needs a Blast account).** The Blast API needs an account token. With one, under `[enrichment]`:

```ini
BLAST_ENABLED: True
BLAST_API_TOKEN: <BLAST_TOKEN>
```
Then `<PYTHON> manage.py enrich --service blast --max 20 --apply`. Each transient with a TNS name gets a `blast` annotation (host name, position and redshift as Blast reports them, and the local and global SED fits: stellar mass, SFR, sSFR, age, metallicity, dust). The annotation has no groups, like the other catalogue annotations. No Host column is changed and no redshift is written. Requests are spaced `BLAST_REQUEST_INTERVAL_SECONDS` (1 s) apart.

<a id="per-point-access"></a>

### Access is per point; program link on each photometry point (2026-10-18, #714) migration applied by the deploy; one optional command

**Ryan's decision (2026-10-18):** access to photometry is decided **per point**. A photometry header (one set per transient, instrument and observation group) only groups points for storage. Its collaboration `groups` are the default for its points, and per-point rules override them. "All of an object's points from one night" is just the default scope of an access action.

**What changes with `DATA_ACCESS_GRANTS_ENABLED` on:**
- Every page, table, plot, export, API and report shows exactly the points a viewer may see, including when that is only some of a header's points. A header is listed when at least one of its points is visible.
- The program a point was taken under is stored on the point. Forced photometry from two programs in one header follows each program's standing grants.
- Facility results put their request's program on the new points. They no longer claim an existing header.

With the flag off, reads are unchanged.

**Migration:** `0047_point_program_link` adds a nullable, indexed `allocation_id` column to `YSE_App_transientphotdata`. It has no foreign-key constraint, so MySQL adds it online without copying the table. Existing rows stay empty, and their header's link applies. Safe on the shared database.

**Optional, once per database (both stacks share it), dry run first:**

```
<PYTHON> manage.py migrate_point_programs            # counts only
<PYTHON> manage.py migrate_point_programs --apply
```

This copies each header's program onto its points that have none, and never changes a point's own link. Access does not change, because unlinked points already follow their header. Afterwards every point carries its program explicitly.

**Expected:** `headers: H; points to link: N`, then `written: N`.

<a id="explorer-for-everyone"></a>

### SQL Explorer for everyone: every query reads only the viewer's data (2026-10-18, #723) new package; one grant check; opening Explorer optional

**What changes on deploy.** Every SQL Explorer query (run, playground, download, stream and email exports, saved queries, the schema browser) and every saved query that feeds a page (dashboards, personal dashboards, `/yse_sky/`, the pointing tool, Summary View, `/query_api/`) is parsed and rewritten before it runs, so each table it reads holds only the rows its viewer may see, per photometry point and per spectrum (the same rules as the pages). Service readers (`DATA_SERVICE_READER_USERNAMES`, the `[main] dblogin` account) and the cache warmer are not rewritten.

- **Staff in Explorer** see only the data they have access to, like everywhere else since #706. They still read the collaboration and operations tables (follow-ups, comments, resources, ...) unchanged, and `auth_user` only as id, username and names. Queries on `information_schema` or other databases, on the personal-data, account, session, token and credential tables, and anything other than one `SELECT` are refused with a reason.
- **Page-feeding queries over photometry or spectra** (refused since #716) run again for viewers who do not see all data, limited to their data. Their cached results are kept per viewer.
- **Limits for rewritten queries:** at most 100000 rows and 120 s per statement (settings below).
- `/explorer/` stays limited to staff (or the Explorer group) until you turn on `EXPLORER_OPEN_TO_ALL_USERS` (step 3).

**1. Package (required on any venv the deploy does not install).** New dependency `sqlglot==26.33.0`: pure Python, no dependencies of its own, the last release that supports Python 3.8. The deploy installs `requirements.txt`. Elsewhere (`--no-deps` is fine):

```
<STACK_PYTHON> -m pip install --no-deps "sqlglot==26.33.0"
<STACK_PYTHON> -c "import sqlglot; print(sqlglot.__version__)"
```

**Expected:** `26.33.0`. Do not install the optional `sqlglotrs` extension. Without the package the site still starts, but every rewritten query fails with an import error.

**2. Explorer database user grants (check required, per database).** The rewritten queries still run on the `explorer` connection (`[database] EXPLORER_USER`). Besides the application tables the user already reads, the added conditions read `auth_group` (group names). Check the grants:

```
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "SHOW GRANTS FOR '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';"
```

- **Database-wide** `GRANT SELECT ON <DB>.*` (the default): nothing to do.
- **Table-level grants** ([Step E](#authz-flags)): add the group names (two columns only):

```
mysql -u <PROD_DB_USER> -p -h <PROD_DB_HOST> -e "GRANT SELECT (id, name) ON \`<DB>\`.\`auth_group\` TO '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';"
```

- **Only once `DATA_ACCESS_GRANTS_ENABLED` is turned on** ([Enable access rules](#enable-access-rules)) and only with table-level grants: the conditions also read the access rules, so grant `SELECT` on `YSE_App_dataaccessrule` and `YSE_App_datasetgroupshare` to the Explorer user at the same time. Explorer users still cannot name these tables: the rewrite refuses them, the schema browser hides them and Explorer's SQL blacklist still lists them. This replaces the earlier "do not grant" notes for these two tables only.

**Check:** as a staff account, open `/explorer/play/` and run `SELECT COUNT(*) FROM YSE_App_transientphotdata`. **Expected:** a number (the points that account may see). `SELECT command denied ... 'auth_group'` means the grant above is missing.

**3. Optional: open Explorer to every signed-in user.** In `[site_settings]` set `EXPLORER_OPEN_TO_ALL_USERS: True` and restart Apache. Every signed-in active user may then run and export queries, use the playground and the schema browser (which lists only the tables they may read), and create, edit and delete their own saved queries. They do not see the query log or the email export, cannot edit or delete other people's queries, and cannot reuse a title another saved query has. Staff and the Explorer group keep full access.

**Check:** sign in as a non-staff account; `/explorer/play/` opens, `SELECT COUNT(*) FROM YSE_App_transientphotdata` returns only that account's points, `SELECT * FROM information_schema.tables` says "Query refused", and `/explorer/logs/` answers 403.

**Optional settings** (`[site_settings]`; restart Apache after a change):
```
# rollback switch: off = Explorer as before #723 (and EXPLORER_OPEN_TO_ALL_USERS has no effect), page queries back to the #715 table check
# EXPLORER_QUERY_SCOPE: enforce
# per-statement time cap (ms) and row limit for rewritten queries; 0 = none (the EXPLORER_QUERY_MAX_EXECUTION_MS cap still applies)
# EXPLORER_SCOPED_MAX_EXECUTION_MS: 120000
# EXPLORER_SCOPED_MAX_ROWS: 100000
# derived-value columns (table.column, comma-separated): selectable, never returned; none today
# EXPLORER_DERIVED_VALUE_COLUMNS: <TABLE>.<COLUMN>
```
Env equivalents: `YSE_EXPLORER_QUERY_SCOPE`, `YSE_EXPLORER_OPEN_TO_ALL_USERS`, `YSE_EXPLORER_SCOPED_MAX_EXECUTION_MS`, `YSE_EXPLORER_SCOPED_MAX_ROWS`, `YSE_EXPLORER_DERIVED_VALUE_COLUMNS`.

**Performance (not measured on production data).**
- The rewrite itself costs one parse and a few small queries (the viewer's groups and rules) per run.
- Each data table becomes a derived table limited by `id IN (visible rows)`. MySQL 8 merges such derived tables into the outer query, so the outer filters still use their indexes. The added conditions are `EXISTS` lookups on the indexed group-link tables (and, with access rules on, the rule table).
- The transient table costs the most: each transient is checked against the listing rule (TNS name, or some visible data). A query over many transients or all photometry may take seconds longer than before; the caps above bound it.
- Page results are cached per viewer, so the cache warmer now helps only service readers. Each viewer's first load of a heavy SQL section runs the query; the #720 Python queries are the faster option for those sections.

**Optional check of a plan:**
```
cd <STACK> && <STACK_PYTHON> manage.py shell -c "from django.contrib.auth.models import User; from YSE_App.services.explorer_scope import rewrite; print(rewrite('SELECT COUNT(*) FROM YSE_App_transientphotdata', User.objects.get(username='<NON_STAFF_ACCOUNT>')).sql)" > /tmp/scoped.sql
(printf 'EXPLAIN '; cat /tmp/scoped.sql) | mysql -u <EXPLORER_DB_USER> -p -h <PROD_DB_HOST> <DB>
```
Compare with `EXPLAIN SELECT COUNT(*) FROM YSE_App_transientphotdata` run the same way; report full scans (`type` `ALL`) that only the rewritten plan has.

**Rollback:** `EXPLORER_QUERY_SCOPE: off` and restart.

### Dashboard queries as viewer-scoped Python queries (2026-10-18, #720) no migration; one command per database, plus a mapping for the queries only the database knows

**Why.** Since #716 a saved SQL query that reads photometry or spectra is refused for anyone who cannot see all data, so sections such as the magnitude-limited sample and Fast & Young are empty for them. Python queries select only from the viewer's visible data (#710), so the same selections now exist as Python queries:

| Saved SQL query (title) | Python query |
|---|---|
| YSE Magnitude-Limited Sample (min mag < 18.6) | `yse_magnitude_limited_sample` |
| Fast & Young Target Search | `fast_and_young_targets` |
| New Transients Last Two Days | `new_transients_last_two_days` |
| Interesting New Targets | `interesting_new_targets` |

`best_spectrum_id` (derived from spectra) can no longer be read by saved queries that feed pages for viewers who do not see all data. Explorer itself is unchanged.

**1. Dry run (changes nothing):**

```
<PYTHON> manage.py switch_dashboard_queries
```

**Expected:** one `SWITCH` line per dashboard section that uses one of the four queries, then `UNMAPPED` lines for every other saved query on a dashboard that still reads photometry or spectra (with its id, title and the tables). Send Ryan the `UNMAPPED` lines. Their SQL is only in the production database, so each needs a Python query written by someone who can read it; the section is switched once that exists (`--map "<id or title>=<python name>"`, or `--mapping-file map.json`). Candidates named in the repository: `YSE Forced Phot Only` and `Auto Ignore` (SQL only in the database); `YSE Volume-Limited Sample (z < 0.06)` reads no photometry and needs nothing.

**2. Apply:**

```
<PYTHON> manage.py switch_dashboard_queries --apply
<PYTHON> manage.py dedupe_dashboard_queries
```

The first writes a JSON backup (path printed) and switches the sections; the SQL queries stay in Explorer. The second removes a duplicate section if a user already had the Python query. **Expected:** `Switched N section(s)`, then `Duplicate groups: 0` (or a short list that is removed).

**Undo:** `<PYTHON> manage.py switch_dashboard_queries --revert <backup.json> --apply`.

**Check:** a signed-in user who is in no collaboration group opens their dashboard: the four sections list transients (from public data), none empty with a failure notice.

<a id="staff-visibility"></a>

### Staff see only the data they have access to (2026-10-18, #706) check the script accounts below; no migration

**What changes.** Ryan's decision (2026-10-17): staff and superusers see only the photometry, spectra and derived values they have access to, the same as other users. That applies to transient lists, the dashboards and search, PhotStat values (Last Det., Peak Mag), plots and fits, the detail page, exports, saved queries and `/query_api/` rows, the REST API, annotations and analysis runs.

Staff keep their administrative actions:
- deciding data-access requests;
- the Django admin;
- SQL Explorer access (it reads the database directly, as before);
- the access-rule actions they have as administrators (#648);
- moderation of comments, follow-ups and logs, which keep their staff view.

**Service readers.** Only the accounts in `DATA_SERVICE_READER_USERNAMES` read all data. These are:
- the `[main] dblogin` ingest account, as before;
- the comma-separated usernames in `[site_settings] DATA_SERVICE_READERS` (empty by default).

**What David should check, on each stack's `settings.ini` (usernames only, never passwords):**
1. List the accounts the crons and scripts authenticate as. Look at the `[main] dblogin` / `dbpassword` users of `YSE_App/data_ingest/*` and any external script that reads `/api/...`, `/query_api/...`, `/download_photometry/...` or the bulk photometry download.
2. If such an account is **not** `[main] dblogin` and must keep reading all data, add it to `DATA_SERVICE_READERS` under `[site_settings]`. One case is a pipeline that reads restricted photometry it is not a group member for. Restart gunicorn.
3. Accounts that only write (uploads, `api_writers`) need nothing: writing is unchanged.

**Expected:** staff who are not in a collaboration group no longer see its restricted photometry, spectra or derived values. The crons keep working.

<a id="access-dashboard"></a>

### Data access dashboard; restricting shared data; turning sharing off forward or backwards (2026-10-18, #648 PR 6a) migration applied by the deploy; nothing else required (hidden while the flag is off)

**Ryan's decisions (2026-10-17):**
- A PI, their deputies, or the program's PI or deputies may restrict any non-public data from anyone, including data already shared.
- Turning sharing off applies **going forward** by default: data taken before the change stay visible to that grantee. "Turning off" means stopping a standing grant, turning off an allow, or removing a group from a dataset's own groups.
- A **backwards** choice also removes the earlier data. It asks for confirmation.
- Public data, including anything reported to TNS, are never restricted.

**Removing a group from a dataset's own groups** now writes `kept` allow rules for the points shared with that group before the removal. This happens from every path: uploads, the API, the admin and the dashboard. The rules are dated when each point was shared, so people who were members then keep them. Reads use these rules only once `DATA_ACCESS_GRANTS_ENABLED` is on. Until then a removal still hides the data, as today.

**Dashboard** (`/data_access/`, with the flag on), for each PI, deputy or program deputy:
- their programs' standing access, with a forward/backwards Stop;
- their restricted datasets: groups (remove forward/backwards), grantees (turn off forward/backwards, lift a restriction), and the automatic-publishing opt-in (#691);
- administrators get no opt-in toggles here.

New API endpoints are `/api/access/datasets/<kind>/<id>/groups/remove/` and `.../autopublish/`. Stop and rule removal take `backwards`.

**Migration:** `0046_access_rule_kept` only adds a choice (no SQL).

<a id="access-pages"></a>

### Access rule pages and API (2026-10-17, #648 PR 5) nothing required (hidden while the flag is off)

**What changes with `DATA_ACCESS_GRANTS_ENABLED` on:**
- **Program page** (`/data_access/programs/`, then one page per program), for the program's PI, its program deputies and staff:
  - its standing access, with a Stop button;
  - "Add access" for the whole program, one classical night, or one night by date;
  - program deputies, which the program's PI or staff manage.
- **Per-object page** (`/transient_detail/<id>/data_access/rules/`, linked from the photometry tab), for the data's PIs, their deputies, program deputies and staff:
  - a night action covering all of the object's points from one night, in any filter, plus that night's spectra;
  - an action per photometry set or spectrum;
  - the rules on those data, with staff-only "Remove (staff)" for mistakes (logged).
- **API** under `/api/access/`: program grants (list, create, stop), program deputies, dataset rules (list, create, staff remove), and an object's nights (list, act).
- **"Kept shared":** results say how many items stayed shared because the grantee could already see them. A restriction never takes back shared data.

**With the flag off (now):** every page and API above returns 404 and the links are hidden. There is no migration.

<a id="access-program-links"></a>

### Program links of photometry and spectra (2026-10-17, #648 PR 3) nothing required; backfill optional (dry run first)

**What changes.** A photometry set or spectrum can record the program (`Allocation`) it was taken under, in the `allocation_id` column added by 0043. The program's access rules then cover it once `DATA_ACCESS_GRANTS_ENABLED` is on (still off). Nothing users see changes. No migration.
- **At ingest, derived:** a new dataset whose follow-up points to exactly one allocation is linked. That covers the follow-up's ToO, classical or queued resource, and its facility request. With `ASTROSCHEDULER_ENABLED`, the classical night of a matching observation also counts. Nothing is linked when there is no allocation or more than one.
- **Facility results:** the request's allocation is used.
- **Uploads:** an optional `allocation` key (an id, a proposal id or an allocation name) in the photometry dict, the photheader or the spectrum header. A new dataset may be linked by the program's PI, a program deputy or staff. An existing one may be linked only by someone who may share it. Otherwise the upload still succeeds, and the response lists `program_link_skipped`. Machine clients that send no `allocation` see no change.
- **Manually:** `GET` / `POST /api/datasets/<photometry|spectrum>/<id>/program/` with `{"allocation": <id, proposal id, name or null>}`, for whoever may share the dataset.
- **After a link:** the program's active standing grants are applied to the dataset's points. Removing a link removes no rule.

**Backfill (optional, either stack; the stacks share the database, so once is enough):**

```
<PYTHON> manage.py backfill_data_programs            # dry run: counts only
<PYTHON> manage.py backfill_data_programs --apply
```

It links only datasets with no link yet, and only unambiguous ones. The sources, in order:
1. the follow-up's allocation;
2. a matching AstroScheduler observation;
3. for spectra, the one allocation on the same instrument whose dates cover the spectrum.

Options:
- `--include-public` also links public datasets;
- `--source` picks and orders the sources;
- `--limit N` looks at N datasets of each kind.

**Expected:** a line `datasets to link: N (...); ambiguous A; unmatched U`, then `written: N` with `--apply`. Ambiguous and unmatched data stay unlinked; their PIs (or staff) can link them with the endpoint.

<a id="access-rules-engine"></a>

### Per-point access rules engine (2026-10-17, #648 PR 2) nothing required (flag stays off)

**What changes with the flag off (now):** nothing users see, no migration. Every page, API, export and plot keeps today's rule with the same SQL. New photometry points and spectra of a program (an `Allocation`) that has a standing grant get their per-point rule at ingest, so they are ready when the flag is turned on.

**What the flag will turn on (steps to enable it: [section "Enable access rules"](#enable-access-rules)):**
- Per-point allow and restrict rules for groups and single users. Precedence: a rule for the person beats a rule for their groups, a restriction beats an allow, and either beats the dataset's own groups. Public data is never hidden.
- Someone who leaves a group keeps the points shared with it before they left.
- Plot caches are keyed by the rules version, so they never cross users with different access.
- PhotStat: list columns and sorting use the best stored summary. Table cells and the detail page are exact for people with rules.

**When the flag is turned on (not yet):** run `<PYTHON> manage.py backfill_photstat_scopes --apply` once per database, so the stored group summaries include existing group rules. After that, every rule change rebuilds them through the job queue (the `photstat.sync_scopes` job, run by the existing job runner).

**Cost with the flag on:** one small read of `YSE_App_accessrulesversion` per request. Users without rules get today's SQL.

<a id="access-rules-per-point"></a>

### Per-point access rules and access history, schema revision (2026-10-17, #648 PR 1b) migration applied by the deploy; nothing else required (flag stays off)

**What changes.** Nothing visible to users yet. Ryan revised the design of [#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648): access rules are stored **per photometry point and per spectrum**, and program, night and dataset become actions that write those rules. This replaces the PR 1 rule table below (it was always empty). Once a point is shared it stays shared; only staff may delete a rule, to fix a mistake, and the deletion is logged. A program or night action taken before the data exist is kept as a standing grant and applied to new points; stopping it affects only points that arrive later. Admin pages for staff: Data access rules, Standing access grants (with a "Stop" action, no delete), and two read-only history lists.

**History is recorded from now on, flag or not:** every change of a user's groups opens or closes a dated membership period, and adding a group to a photometry set's or spectrum's own groups records when. The engine (a later PR) uses this so that someone who leaves a group keeps what was shared with it while they were a member. Memberships that exist when the migration runs are recorded as "since before tracking".

**Migration:** `0045_per_point_access_rules` drops the empty `YSE_App_dataaccessgrant` table (only experimental code knew it) and adds five tables: `YSE_App_dataaccessrule`, `YSE_App_standingaccessgrant`, `YSE_App_groupmembershipperiod`, `YSE_App_datasetgroupshare` and `YSE_App_accessrulesversion`. It writes rows only into the new tables: one version row, and one membership row per current user-group membership. Safe on the shared `YSE_test` database. `YSE_App_allocationdelegate` and the `allocation_id` columns from 0043 are unchanged. The deploy applies it. **No new package.**

**Shared database note:** group changes made through yse\_test (develop code) are not recorded in the history until this code reaches develop. That only affects test accounts.

**Flag, leave it off:** `DATA_ACCESS_GRANTS_ENABLED` stays `False`.

**Explorer:** the new tables say who may see which data and when people were in which group, so they are hidden from SQL Explorer and refused by its SQL blacklist. If you limited the Explorer MySQL user to table-level grants ([Step E](#authz-flags)), do **not** grant `SELECT` on them.

**Check (optional), on either stack after the deploy:**

```
<PYTHON> manage.py showmigrations YSE_App | tail -2
```

**Expected:** `[X] 0045_per_point_access_rules`.

<a id="access-grants-schema"></a>

### Access grants and program deputies, schema only (2026-10-17, #648 PR 1) migration applied by the deploy; nothing else required (flag stays off)

**Superseded by [PR 1b](#access-rules-per-point):** its rule table `YSE_App_dataaccessgrant` is dropped by migration 0045; program deputies and the `allocation_id` columns stay as described here.

**What changes.** Nothing visible to users yet. This is the storage for PI-controlled access rules ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648)): a rule allows or restricts one group or one user on a program (an `Allocation`), one night of a program, one photometry set or spectrum, or one night of a photometry set. Revoking a rule never deletes it. Program deputies are a separate list from the PIs' data-sharing deputies. Staff can view rules and deputies in the admin (Data access grants, Allocation delegates, and an inline on each Allocation), add a rule as an administrator, and revoke rules; rules cannot be deleted. No page, API or export reads the rules: the read path ignores them until a later PR adds the precedence engine **and** the flag below is turned on.

**Migration:** `0043_data_access_grants` adds two new tables, `YSE_App_dataaccessgrant` and `YSE_App_allocationdelegate`, and one nullable, indexed column, `allocation_id`, on `YSE_App_transientphotometry` and on `YSE_App_transientspectrum` (no default needed, no data written). It is safe on the shared `YSE_test` database: develop code ignores the new tables and columns. The deploy applies it. **No new package.**

**Flag, leave it off:** `DATA_ACCESS_GRANTS_ENABLED` (`[site_settings]`, or `YSE_DATA_ACCESS_GRANTS_ENABLED=1`) defaults to `False`. Do not set it until Ryan asks; the steps are in [section "Enable access rules"](#enable-access-rules).

**Explorer:** both new tables say who may see which program's data, so they are hidden from SQL Explorer and refused by its SQL blacklist. If you limited the Explorer MySQL user to table-level grants ([Step D](#onboarding), [Step E](#authz-flags)), do **not** grant `SELECT` on `YSE_App_dataaccessgrant` or `YSE_App_allocationdelegate`. The new `allocation_id` columns need nothing: they are on tables the Explorer user can already read.

**Check (optional), on either stack after the deploy:**

```
<PYTHON> manage.py showmigrations YSE_App | tail -2
```

**Expected:** `[X] 0043_data_access_grants`.

<a id="astroscheduler-130"></a>

### AstroScheduler 1.3.0 (2026-10-16, #678) required on any venv the deploy does not install

`requirements.txt` moves `astroscheduler` from v1.2.0 to v1.3.0. The new version adds a checklist of several reasons for Failed/Punted in the planner, and a solver about 3x faster with identical schedules. The planner's JavaScript changed, so `collectstatic` must run after the install. The Deploy Stack job does both on the yse\_experimental venv. On `yse_test_virtual` (yse\_test, and Apache for yse\_experimental per [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187)) and on production, repeat the [planner page](#astroscheduler-page) commands. There is no migration.

**Expected:**
- the import prints `1.3.0`;
- `collectstatic` reports at least one file copied (the planner's `app.js` changed).

**Check, with the flag on:** in the planner's Real-Time Mode, the Failed/Punted reasons are checkboxes.

<a id="astroscheduler-observations"></a>

### AstroScheduler execution feedback, phase 5 (2026-10-16, #666-#668) migration applied by the deploy; nothing else required

**Migration:** `0040_observed_blocks` adds two new tables, `YSE_App_outcomereason` and `YSE_App_observedblock`, and seeds the 12 outcome reasons. It changes no existing table, and the deploy applies it. On a database without any user account the seed is skipped and happens on first use. **No new package.**

**Check:**
1. On the stack's database, run `SELECT code FROM YSE_App_outcomereason ORDER BY code;`. **Expected:** 12 rows, from `classified_elsewhere` to `wrong_object`.
2. With the flag on, in the planner's Real-Time Mode, mark a target **Completed**.
3. The row appears under "Tonight's observing log" as "Data taken", and the follow-up's status on the observing-night page becomes InProcess.
4. **Good** on that row makes the request Successful. So does uploading a spectrum for it.

**Optional, only when Ryan asks: closing ended requests.** The daily `ExpireFollowupRequests` cron is a no-op until a start date is set. A date in the future is the safe choice: only requests whose window ends on or after it are ever touched.

1. Dry run, to see which requests the date would affect:

```
cd <STACK_DIR> && <STACK_PYTHON> manage.py expire_followup_requests --since <YYYY-MM-DD>
```

2. Then set the date under `[site_settings]` in that stack's `YSE_PZ/settings.ini`:

```ini
ASTROSCHEDULER_EXPIRE_AFTER: <YYYY-MM-DD>
```

**Expected:** the dry run prints "Would change: N to Failed, M to Successful; K left for a judgement" and changes nothing. After the setting is in place, the cron log line (`runcrons`) reads "follow-up expiry cron: ...". **Rollback:** remove the line. Statuses already changed stay changed; set them back in the admin if needed.

**Rollback:** turn the flag off. The tables can stay.

<a id="astroscheduler-plans"></a>

### AstroScheduler saved plans, phase 4 (2026-10-16, #661-#664, #77) migration applied by the deploy; package install required on venvs the deploy does not install

- **Migration:** `0039_night_plans` adds two new tables, `YSE_App_nightplan` and `YSE_App_plannedblock`, and changes no existing table. It is safe on the shared `YSE_test` database, and the deploy applies it.
- **Package:** `requirements.txt` moves `astroscheduler` to v1.2.0, so re-opened plans re-solve stably. On venvs the deploy does not install, repeat the [planner page](#astroscheduler-page) commands: install, check that the import prints `1.2.0`, run `collectstatic`, restart Apache.

**Check, once the flag is on (yse\_experimental):**
1. Open a night's planner, press **Recalculate Schedule**, then **Save plan**. The page reloads with "Opened v1" and the version is listed under "Saved plans".
2. **Target list** downloads a text file.
3. Staff or the night's PI see **Approve**.
4. Admin > Night plans lists the version, read-only.

**Rollback:** the flag hides everything. The tables can stay: nothing reads them while the flag is off.

<a id="astroscheduler-page"></a>

### AstroScheduler planner page, phase 3 (2026-10-16, #658, #659) required on any venv the deploy does not install; flag optional

`requirements.txt` moves `astroscheduler` to the v1.1.0 release. That version serves the planner page, and its front-end files (JavaScript, CSS) are now collected as static files under `astroscheduler/`. The Deploy Stack job installs the package into the yse\_experimental venv and runs `collectstatic` itself. On any venv it does not install (`yse_test_virtual`: yse\_test, and Apache for yse\_experimental per [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187); production), install the package **and then run `collectstatic`**, because the deploy's `collectstatic` ran before the package was there. There is no migration.

```
# <STACK_DIR> / <STACK_PYTHON> from section 0; <VENV_OWNER>: owner of the venv directory
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip install --no-deps "$(grep -m1 '^astroscheduler ' <STACK_DIR>/requirements.txt)"
<STACK_PYTHON> -c "import astroscheduler; print(astroscheduler.__version__)"
cd <STACK_DIR> && <STACK_PYTHON> manage.py collectstatic --noinput
ls <STACK_DIR>/static/astroscheduler/
sudo systemctl restart apache2
```

**Expected:**
- the import prints `1.1.0`;
- `collectstatic` reports some files copied;
- `ls` shows `app.js`, `standards.json` and `style.css`.

**Optional, yse\_experimental only, when Ryan wants to try it:** the flag from the phase 2 section below, `ASTROSCHEDULER_ENABLED: True`.

**Check:**
1. Open any classical observing night on `/yse_experimental/`. A **Plan this night** button sits next to the page title.
2. Click it. The planner opens inside the YSE_PZ page, with the night's targets in its target list.
3. Press **Recalculate Schedule**. A schedule appears.
4. If the planner area shows the raw page without colours, the static files are missing: rerun `collectstatic`.

**Rollback:** remove the flag line and restart Apache.

<a id="astroscheduler"></a>

### AstroScheduler night planner, phase 2 (2026-10-15, #653-#656) required on any venv the deploy does not install; flag optional

`requirements.txt` adds one package: `astroscheduler`, pinned to the v1.0.1 release tarball on GitHub (no git needed). It needs only astropy, astroplan, numpy and pytz, which are already pinned, and it supports Python 3.8. The Deploy Stack job installs it into the yse\_experimental venv by itself. It does NOT install into `yse_test_virtual` (yse\_test, and Apache for yse\_experimental per [#187](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/187)) or into production. Run this there, once the stack's checkout has this change. There is no migration.

```
# <STACK_DIR> / <STACK_PYTHON> from section 0; <VENV_OWNER>: owner of the venv directory
sudo -u <VENV_OWNER> <STACK_PYTHON> -m pip install --no-deps "$(grep -m1 '^astroscheduler ' <STACK_DIR>/requirements.txt)"
<STACK_PYTHON> -c "import astroscheduler, astroscheduler.api; print(astroscheduler.__version__)"
<STACK_PYTHON> -m pip check
sudo systemctl restart apache2
```

**Expected:**
- the install ends with `Successfully installed astroscheduler-1.0.1`;
- the import prints `1.0.1`;
- `pip check` prints `No broken requirements found.` (any line it prints for another package was there before).

The planner is **off** by default, and its endpoints return 404 until it is turned on. It does nothing on its own.

**Optional, yse\_experimental only, when Ryan wants to try it:** add this under the existing `[site_settings]` section of that stack's `YSE_PZ/settings.ini`, then `sudo systemctl restart apache2`:

```ini
ASTROSCHEDULER_ENABLED: True
```

**Check:** logged in on `/yse_experimental/`, open `/yse_experimental/api/observing_nights/<NIGHT_ID>/planner_targets/`, where `<NIGHT_ID>` is any classical observing night's id (Admin > Classical observing dates). It returns JSON with `targets` and `observatory`. If the telescope's name does not contain "shane", "lick", "keck i" or "keck ii", `warnings` names it. In that case, report the telescope names so `ASTROSCHEDULER_SITE_MAP` can be set ([docs/astroscheduler.md](astroscheduler.md)).

**Rollback:** remove the `ASTROSCHEDULER_ENABLED` line and restart Apache. The package can stay installed; nothing imports it while the flag is off.

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

**Expected** HEAD is the #143 merge commit; each pip line ends `Successfully installed` or `Requirement already satisfied`. `requirements.txt` is resolver-consistent since [#223](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/223), so no `--no-deps`; it pinned `Django>=3.2,<4.0` at #143 (production then had Django 4.0, so this step moved it to 3.2.25; from #781 the pin is Django 4.2.30, see [Django 4.2 LTS](#django-42)) and `protobuf==3.20.3`. If pip is not writable as your user, prefix with `sudo -u <VENV_OWNER>` (`stat -c %U $(dirname $(dirname <PROD_PYTHON>))`). ML extras for TensorFlow crons live in `requirements-ingest.txt` and are for the cron venv only.

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
<PROD_PYTHON> manage.py mark_tns_imports_public
<PROD_PYTHON> manage.py mark_tns_imports_public --apply
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

<a id="quality-mask"></a>

### Data-quality mask columns, migration 0036 (2026-10-12, #80-#82, #480): one check recommended, per database

Migration `0036_quality_mask` adds these columns:

- `quality_mask` (`BIGINT UNSIGNED NOT NULL DEFAULT 0`) to `YSE_App_transientphotdata`, `YSE_App_hostphotdata`, `YSE_App_transientspectrum` and `YSE_App_hostspectrum`;
- `pixel_mask` (`SMALLINT UNSIGNED NOT NULL DEFAULT 0`) to `YSE_App_transientspecdata` and `YSE_App_hostspecdata`;
- nullable `wavelength_medium` (both spectrum tables), `host_subtraction` (transient spectra) and `phot_type` (host photometry).

The defaults are real MySQL column defaults, so `develop`'s INSERTs on the shared `YSE_test` database keep working (#395).

Each `ALTER TABLE` asks for `ALGORITHM=INSTANT` (MySQL 8.0.12+). That is a metadata-only change, so it takes seconds however large the table is, including the very large `YSE_App_transientspecdata`. If MySQL refuses INSTANT for a table, `migrate` logs `0036: MySQL refused ALGORITHM=INSTANT on <table>` and repeats the ALTER without the clause. That fallback may copy the table, which takes minutes to hours for the spec-data table.

The migration sets `lock_wait_timeout` to 60 s. If a long-running query holds a table, the ALTER fails with `Lock wait timeout exceeded` instead of making the site wait behind it. Re-run `migrate`: columns that already exist are skipped.

The migration then fills `quality_mask` for every row that has a DataQuality flag, which reads only the small through tables. It also sets the "wavelength medium unknown" bit on every spectrum, with one UPDATE per spectrum table. The deploy runs all of this, and the readers switch to the mask in the same deploy. Docs: [docs/quality-mask.md](https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/docs/quality-mask.md).

Placeholders: `<STACK>` and `<STACK_PYTHON>` come from section 0. For the shared `YSE_test` database, use the yse\_experimental checkout, which has the migration first; use production after the promotion. `<DB>`, `<DB_USER>` and `<DB_HOST>` are that stack's database.

1. **Before migrating (production; on YSE\_test the deploy has already run it).** Check the MySQL version and look for long-running queries:

```
mysql -u <DB_USER> -p -h <DB_HOST> <DB> -e "SELECT VERSION(); SELECT id, time, LEFT(info, 60) FROM information_schema.processlist WHERE command <> 'Sleep' AND time > 30;"
```

**Expected:** a version of `8.0.12` or later, and no rows from the second query. If the version is older than 8.0.12, budget a maintenance window: each ALTER then copies its table. If the second query returns rows, wait for those queries to finish before you migrate.

2. **After migrating.** Check the migration, the columns and that the ALTERs were instant:

```
cd <STACK> && <STACK_PYTHON> manage.py showmigrations YSE_App | grep 0036
mysql -u <DB_USER> -p -h <DB_HOST> <DB> -e "SELECT table_name, column_name, column_type, column_default, is_nullable FROM information_schema.columns WHERE table_schema = DATABASE() AND column_name IN ('quality_mask','pixel_mask','wavelength_medium','host_subtraction','phot_type') ORDER BY table_name, column_name;"
mysql -u <DB_USER> -p -h <DB_HOST> <DB> -e "SELECT name, instant_cols, total_row_versions FROM information_schema.innodb_tables WHERE name LIKE CONCAT(DATABASE(), '/yse_app_%spec%') OR name LIKE CONCAT(DATABASE(), '/yse_app_%photdata');"
```

**Expected:**
- `[X] 0036_quality_mask`.
- Ten rows. The mask columns are `bigint unsigned` or `smallint unsigned`, with default `0` and nullable `NO`. The other three are `varchar(16)`, default `NULL`, nullable `YES`.
- In the last query, `total_row_versions` (MySQL 8.0.29+) or `instant_cols` (older 8.0) is greater than 0 for each table. That shows the ALTER was instant. If that column is missing on your MySQL version, the `migrate` log saying nothing about `refused ALGORITHM=INSTANT` is the check.

3. **Backfill check, dry run** (from the stack's checkout):

```
cd <STACK> && <STACK_PYTHON> manage.py backfill_quality_mask
```

**Expected:**
- One line per model: `TransientPhotData: N row(s) scanned, M with DataQuality, 0 to update (...)`, and the same for the others.
- `DataQuality '<name>' on K row(s) -> <FLAG>` lines listing the flag names in use and the bit each maps to (only `Bad` maps to REJECTED).
- At the end, `dry run: 0 row(s) would change`.

The scan reads every row in 5000-row primary-key batches. Allow about a minute per few million photometry rows.

On `YSE_test`, a non-zero count is expected after `develop` has flagged points, because `develop`'s code does not know the mask. A non-zero count on a stack that runs only this code means something wrote around the signals: post the output, which lists primary keys and flag names only.

4. **Backfill, for real (only if step 3 reported changes):**

```
cd <STACK> && <STACK_PYTHON> manage.py backfill_quality_mask --apply
cd <STACK> && <STACK_PYTHON> manage.py backfill_quality_mask
```

**Expected:** the first command ends with `done: N row(s) updated`, with N the count from step 3. The second ends with `dry run: 0 row(s) would change`. On yse\_test, repeat steps 3 and 4 after each promotion until `develop` has this code.

5. **Verify that good data is unchanged.** Count photometry by the old rule and by the new one:

```
mysql -u <DB_USER> -p -h <DB_HOST> <DB> -e "SELECT (SELECT COUNT(*) FROM YSE_App_transientphotdata p WHERE NOT EXISTS (SELECT 1 FROM YSE_App_transientphotdata_data_quality d WHERE d.transientphotdata_id = p.id)) AS good_old, (SELECT COUNT(*) FROM YSE_App_transientphotdata WHERE (quality_mask & 3384314062438400) = 0) AS good_new;"
```

**Expected:** `good_old` equals `good_new`.

**Rollback:**

```
cd <STACK> && <STACK_PYTHON> manage.py migrate YSE_App 0035_feedback
```

Run this from this code's checkout, then roll the code back. The older code works with the columns still present, because they have defaults, so you can also roll back only the code. The migration drops the ten columns, using an instant DROP COLUMN where MySQL supports it (8.0.29+); otherwise it rebuilds the table. The DataQuality M2M is untouched, so the masks can be rebuilt from it. What is lost is any `wavelength_medium`, `host_subtraction`, `phot_type` or `pixel_mask` value that someone set by hand.

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

### Write authorization and Explorer access (per stack; #520-#527, #697) upload checks are ON by default

Migration `0032_api_writer_groups` is data only. It creates two groups, `api_writers` and `explorer_users`. It gives `api_writers` the add and change permissions on the models the ingest crons and scripts write. It adds the `[main] dblogin` account to `api_writers` if that account exists. Three flags in `settings.ini` `[site_settings]` control it. `UPLOAD_GROUP_CHECKS_ENFORCED` is **on by default since changelog v68 ([#697](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/697))**; `API_WRITE_PERMISSIONS_ENFORCED` and `EXPLORER_RESTRICTED_ACCESS` stay **off** until you turn them on. Each flag can be turned on per stack. yse\_test and yse\_experimental share `YSE_test`, so group membership is shared, but each stack reads its own `settings.ini`.

| Flag (`[site_settings]`, or the env var `=1`) | When on |
| --- | --- |
| `API_WRITE_PERMISSIONS_ENFORCED` (`YSE_API_WRITE_PERMISSIONS_ENFORCED`) | `POST`/`PUT`/`PATCH` on the older `/api/` resources needs one of: staff, superuser, or the model permission (members of `api_writers`). Status and tag buttons, follow-up status, survey scheduling and "new tag" stay open to every signed-in user. Reads are unchanged. |
| `UPLOAD_GROUP_CHECKS_ENFORCED` (`YSE_UPLOAD_GROUP_CHECKS_ENFORCED`), **default on** | `add_transient` is limited to staff, superusers and `api_writers`. `add_transient_phot`, `add_transient_spec` and the spectrum upload form are open to those accounts as well as other collaboration-group members, with two limits: a member may share only with their own groups, and may replace or re-share only rows they created. |
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

**Step C: turn the flags on, one at a time.** `UPLOAD_GROUP_CHECKS_ENFORCED` needs no line, because it is already on; write it only to turn it off (see "Upload checks on by default" below). In `<STACK>/YSE_PZ/settings.ini`:

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

<a id="upload-checks-default-on"></a>

**Upload checks on by default ([#697](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/697)).** From changelog v68 a stack with no `UPLOAD_GROUP_CHECKS_ENFORCED` line in `settings.ini` and no `YSE_UPLOAD_GROUP_CHECKS_ENFORCED` env var enforces the upload checks. Ryan chose this on 2026-10-17, overriding the usual rule that new flags default to today's behaviour.

*What changes on deploy.* `POST /add_transient/` returns 403 for accounts that are not staff, superusers or in `api_writers`. `add_transient_phot`, `add_transient_spec` and the spectrum upload form return 403 for a non-trusted account that is in no collaboration group (Public does not count), that shares with a group it is not in, or that replaces or re-shares rows another user created. Browser uploads by ordinary members who share only with their own groups keep working.

*What the cron and script audit found (code only; David confirms the accounts).* Everything below posts to the HTTP endpoints with Basic auth and passes if the account is staff, a superuser or in `api_writers`. Trusted accounts skip the group and ownership checks entirely, so the groups they send do not matter.

| Caller | Endpoint | Account | Passes when |
| --- | --- | --- | --- |
| `data_ingest/DECam_upload.py`, `DECam_upload_clusters.py`, `QUB_data.py`, `YSE_Forced_Phot.py`, `Gaia_LC.py`, `TNS_uploads.py`, `Query_ZTF.py` | `/add_transient/` | `--dblogin`, default `[main] dblogin` | that account is staff, superuser or in `api_writers` (migration 0032 adds `[main] dblogin` to `api_writers` if it existed then) |
| `util/uploadTransientData.py`, `util/TNS_Synopsis/TNS_Synopsis_coreapi.py` | `/add_transient_phot/`, `/add_transient_spec/` | `--dblogin` | the account is trusted, or is a member of a collaboration group and shares only with its own groups and replaces only its own rows |
| Broker ingest (`brokers/ingest.py`) | none: calls the save code in-process | the audit user | not affected; the checks live in the HTTP views only |
| Facility results ingest | no call to the upload endpoints found in the code | n/a | not affected |

Two cases to watch. (1) The QUB script logs in to QUB with its own credentials, but it posts to YSE-PZ as `--dblogin`; only that account matters. (2) The checks recognise staff, superusers and `api_writers` only; a PI or deputy account that is not staff is not trusted, so an ordinary member's own rules apply to it.

*David: confirm the cron accounts first.* Run Step B's second command (the 30-day listing of non-staff, non-`api_writers` writers) and add any script account it shows to `api_writers`. Then check the cron logs after the next TNS, QUB and forced-phot runs: no `403` and no `Bulk transient ingest is limited`.

*Turn it off temporarily.* Either set `UPLOAD_GROUP_CHECKS_ENFORCED: False` under `[site_settings]` in `<STACK>/YSE_PZ/settings.ini`, or set the env var `YSE_UPLOAD_GROUP_CHECKS_ENFORCED=0` for the web process, then restart (`sudo systemctl restart apache2` for production). **Check:** `<STACK_PYTHON> manage.py shell -c "from django.conf import settings; print(settings.UPLOAD_GROUP_CHECKS_ENFORCED)"` prints `False`.

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
'YSE_App_encryptedcredential','YSE_App_profile','YSE_App_externalservicerun','YSE_App_feedback');" > /tmp/explorer_grants.sql
wc -l /tmp/explorer_grants.sql
```

The PI tables (`YSE_App_principalinvestigator` and its `_obs_group` join table) are **not** excluded any more (Ryan, 2026-10-13, [#643](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/643)). If you ran this step before 2026-10-15, grant them: `GRANT SELECT ON \`<DB>\`.\`YSE_App_principalinvestigator\` TO '<EXPLORER_DB_USER>'@'<EXPLORER_DB_HOST>';` (and the same for `YSE_App_principalinvestigator_obs_group`). The personal-data tables of the onboarding and teams sections (`YSE_App_researcherprofile*`, `YSE_App_groupinvitation*`, `YSE_App_userpreference*`, `YSE_App_groupmembershiprequest`, ...) stay excluded as those sections say.

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

### Saved Explorer queries that feed pages: viewer-scoped tables (2026-10-18, #715) one check recommended, per stack

Saved Explorer queries that select the rows of dashboards, personal dashboards, `/yse_sky/`, the pointing tool, Summary View, the bulk photometry download, change-status-for-query and `/query_api/` now run for a viewer who does not see all data only when they read transient metadata tables (transient, tags, status, class, host, group, survey, alternate names). A query that reads photometry, spectrum or photometry-statistics tables is refused for that viewer (dashboard sections say why, the API answers 404), because access to data is decided per photometry point or spectrum by the application and not by the database. Staff, superusers, `/query_api/` service accounts and the nightly cache warmer are unchanged. SQL Explorer itself is unchanged. No migration; no new package.

**Superseded by #723** ([SQL Explorer for everyone](#explorer-for-everyone)): such queries now run limited to the viewer's data instead of being refused; this metadata-only rule applies again only with `EXPLORER_QUERY_SCOPE: off`.

Effect on the saved queries that exist today: those that read `YSE_App_transientphotdata` (for example the magnitude-limited sample and the fast-and-young searches) and saved searches that filter on photometry statistics show an explanatory message to non-staff viewers and keep working for staff. This is the conservative option; the alternative (database views limited to the viewer, set per connection) needs a product decision from Ryan and a database privilege change by David.

**Check (recommended): after the deploy, open a personal dashboard as a non-staff account that has a saved SQL section reading photometry; it should show the message, and a staff account should show rows as before.**

**Optional settings** (`[site_settings]`; restart Apache after a change):
```
# rollback switch: off = no table check (the old behaviour); default enforce
# PAGE_QUERY_TABLE_POLICY: enforce
# extra table or view names page queries may read (for example a view that exposes only derived fit values), comma-separated
# PAGE_QUERY_EXTRA_ALLOWED_TABLES: <VIEW_1>, <VIEW_2>
```
Env equivalents: `YSE_PAGE_QUERY_TABLE_POLICY=off`, `YSE_PAGE_QUERY_EXTRA_ALLOWED_TABLES`. If you add a view to the extra list, grant the Explorer database user `SELECT` on it; for those queries a non-staff viewer gets only the identifying columns (name, id, position) back from `/query_api/`, never the derived columns.

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

Web workers no longer download IERS Earth-orientation tables (#466, #783: `astropy_offline()` in `YSE_App/apps.py` sets `iers.conf.auto_download = False` for every Django process, as `services/observability.py` already did for its block), so a request never waits on the network for rise/set times. Refresh the astropy cache from a cron so command-line jobs (default `auto_download`) find a current table. Without it everything still works; UT1 accuracy only changes by milliseconds.

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

<a id="watchdog"></a>
### Server watchdog and `/healthz/` (2026-10-21, #768, #769) optional, recommended; root on Ziggy

`GET /healthz/` ([#768](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/768)) answers `200 ok` when Django can run `SELECT 1`, `503 db error` when it cannot. It is public, sets no cookies and makes one query. It reaches each stack with that stack's code: yse\_experimental now, yse\_test after the next promotion, production after #143.

The watchdog ([#769](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/769)) is `scripts/ops/yse-watchdog.sh`, run every minute by a systemd timer. It checks MySQL directly, the Django URL, and a static file Apache serves without Django. After 3 failed runs in a row it saves a diagnostic snapshot, then restarts MySQL (only if MySQL itself does not answer) and gracefully reloads Apache (a full restart if static files fail too). It waits 15 minutes between actions, acts at most 3 times an hour, and emails a summary each time. One installation watches one stack; install it for production.

Run as a user with sudo. The files come from the yse\_experimental checkout, which the deploy keeps current.

**W1. Install the script, units and config.**
```bash
E=/data/yse_pz/YSE_PZ_experimental/scripts/ops
sudo install -o root -g root -m 700 $E/yse-watchdog.sh /usr/local/sbin/yse-watchdog.sh
sudo install -o root -g root -m 644 $E/yse-watchdog.service $E/yse-watchdog.timer /etc/systemd/system/
sudo install -d -o root -g root -m 700 /etc/yse-watchdog
sudo install -o root -g root -m 600 $E/watchdog.conf.example /etc/yse-watchdog/watchdog.conf
```
**Check:** `sudo ls -l /usr/local/sbin/yse-watchdog.sh /etc/yse-watchdog/` shows `-rwx------ root` for the script and `-rw------- root` for `watchdog.conf`.

**W2. A monitoring login for MySQL.** `PROCESS` lets it read the process list and InnoDB status for the snapshot; it can read no table data. Pick a new random password.
```bash
mysql -u root -p -e "CREATE USER 'yse_watchdog'@'localhost' IDENTIFIED BY '<WATCHDOG_DB_PASSWORD>'; GRANT PROCESS ON *.* TO 'yse_watchdog'@'localhost';"
printf '[client]\nuser=yse_watchdog\npassword="<WATCHDOG_DB_PASSWORD>"\n' | sudo tee /etc/yse-watchdog/my.cnf >/dev/null
sudo chmod 600 /etc/yse-watchdog/my.cnf
sudo mysql --defaults-extra-file=/etc/yse-watchdog/my.cnf -N -e 'SELECT 1'
```
**Expected:** `1`. Optional: to let the watchdog cancel runaway statements (W3 `KILL_QUERY_*`), also `GRANT CONNECTION_ADMIN ON *.* TO 'yse_watchdog'@'localhost';`.

**W3. Fill in `/etc/yse-watchdog/watchdog.conf`** (`sudo nano /etc/yse-watchdog/watchdog.conf`). For production:
```bash
DB_HEALTH_URL="https://ziggy.ucolick.org/yse/login/"
STATIC_URL="https://ziggy.ucolick.org/static/YSE_App/dist/css/AdminLTE.min.css"
NOTIFY="<YOUR_EMAIL>"
DRY_RUN=1
```
Production runs `master`, which has no `/healthz/` yet. Until it does, the login page (Django, no database) catches a stuck web process, and the direct MySQL check covers the database. After #143 is deployed, change `DB_HEALTH_URL` to `https://ziggy.ucolick.org/yse/healthz/`. Optional: `KILL_QUERY_SECONDS=300` and `KILL_QUERY_USERS="explorer"` cancel Explorer statements older than 5 minutes before Apache is reloaded (needs the W2 grant).

**W4. Check the probes and mail by hand.**
```bash
curl -s -o /dev/null -w '%{http_code}\n' https://ziggy.ucolick.org/yse/login/
curl -s -o /dev/null -w '%{http_code}\n' https://ziggy.ucolick.org/static/YSE_App/dist/css/AdminLTE.min.css
echo "yse-watchdog mail test" | mail -s "yse-watchdog mail test" <YOUR_EMAIL>
sudo /usr/local/sbin/yse-watchdog.sh; echo "exit $?"
```
**Expected:** `200`, `200`, the test mail arrives (check spam; Ziggy's postfix sends directly), and `exit 0` with no other output. `exit 2` means a setting in `watchdog.conf` is missing or not a number; the message says which.

**W5. Turn it on (dry run first).**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yse-watchdog.timer
systemctl list-timers yse-watchdog.timer
```
**Expected:** a NEXT time within a minute. Follow it with `journalctl -t yse-watchdog -f`. It logs only when something is unhealthy. With `DRY_RUN=1` it logs and emails `[dry run] ... would run: ...` and changes nothing. After a day without false alarms, set `DRY_RUN=0` in `watchdog.conf`; no reload is needed, because each run reads the file.

**Where things are.** State and snapshots: `/var/lib/yse-watchdog/` (root only). Each snapshot holds the MySQL process list (full SQL text of running statements), InnoDB status, memory, disk and recent journal lines. Snapshots are deleted after 30 days (`SNAPSHOT_DAYS`). Treat them as sensitive and do not copy them off the server.

**Rollback.** `sudo systemctl disable --now yse-watchdog.timer`. To remove it completely, also delete `/usr/local/sbin/yse-watchdog.sh`, the two unit files, `/etc/yse-watchdog/` and `/var/lib/yse-watchdog/`, and run `DROP USER 'yse_watchdog'@'localhost';`.
<a id="mysql-sizing"></a>
### MySQL memory and connection limits (2026-10-21, #777) recommended, the shared MySQL server

On 2026-10-21 MySQL ran with the defaults: `innodb_buffer_pool_size` 128 MB and `max_connections` 151. Both settings change online with `SET PERSIST` (no restart), and the value survives restarts.

**M1. Check memory headroom.** ZFS (the `/data` pools) keeps a cache that shows as "used" but gives memory back under pressure:
```bash
free -g | head -2
awk '/^size/ {printf "ZFS ARC now %.1f GB\n", $3/2^30} /^c_max/ {printf "ZFS ARC max %.1f GB\n", $3/2^30}' /proc/spl/kstat/zfs/arcstats
mysql -u root -p -t -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.TABLES GROUP BY 1 ORDER BY 2 DESC LIMIT 5;"
```
Size the buffer pool to the hot data, not the whole database. Keep at least 8 GB of `available` memory after the change. With about 23 GB available and a ZFS cache of several GB, **8 GB** is a safe first step.

**M2. Raise the limits.**
```bash
mysql -u root -p -e "SET PERSIST innodb_buffer_pool_size = 8589934592; SET PERSIST max_connections = 300;"
mysql -u root -p -t -e "SHOW STATUS LIKE 'Innodb_buffer_pool_resize_status'; SELECT @@innodb_buffer_pool_size/1073741824 bp_gb, @@max_connections;"
```
**Expected:** `bp_gb 8.0000` and `300`. The resize runs in the background in 128 MB chunks; the status ends with `Completed resizing buffer pool`. Queries keep running, but run it at a quiet time anyway.

**M3. Check after a day.**
```bash
mysql -u root -p -t -e "SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests','Max_used_connections','Uptime');"; free -g | head -2
```
`Innodb_buffer_pool_reads / Innodb_buffer_pool_read_requests` (reads that had to go to disk) should be well under 1%. `Max_used_connections` should stay well below 300, and `available` memory above 8 GB.

**Rollback:** `mysql -u root -p -e "RESET PERSIST innodb_buffer_pool_size; RESET PERSIST max_connections; SET GLOBAL innodb_buffer_pool_size = 134217728; SET GLOBAL max_connections = 151;"`.

<a id="wsgi-sizing"></a>
### mod_wsgi daemon size and timeouts (2026-10-21, #771) recommended, all three stacks

With no options, a `WSGIDaemonProcess` runs mod\_wsgi's defaults: **one process with 15 threads** and no request or queue timeout. Apache's `Timeout 300` gives up on a slow request after 5 minutes, but the thread keeps working on it. Once the threads are all busy, new requests queue, and Apache logs `Unable to connect to WSGI daemon process ... Resource temporarily unavailable` and returns 503. The options below run several processes, fail queued requests quickly instead of letting them pile up, and restart a process whose threads stay stuck.

Each thread can hold one connection per database alias (`default` and `explorer`), so keep processes × threads × 2, summed over the stacks, plus the crons, under `max_connections` (300 after [M2](#mysql-sizing)). The values below use at most 3×10×2 + 2×(1×10×2) = 100.

| Option | Production | yse\_test, yse\_experimental | Why |
|---|---|---|---|
| `processes` / `threads` | `3` / `10` | `1` / `10` | a stuck process leaves the others serving |
| `request-timeout` | `240` | `240` | restarts a process whose threads have been busy on requests for 4 minutes on average |
| `queue-timeout` | `45` | `45` | a request that waited 45 s for a thread gets a fast 504 instead of holding Apache |
| `socket-timeout` | `300` | `300` | matches Apache's `Timeout` |
| `connect-timeout` | `15` | `15` | |
| `graceful-timeout` / `shutdown-timeout` | `30` / `10` | `30` / `10` | how long a restart waits for running requests |
| `display-name` | `%{GROUP}` | `%{GROUP}` | `ps` shows `(wsgi:yse_pz.django)` instead of `apache2` |

**A1. Back up the site files and look at today's lines.**
```bash
for f in <PROD_SITE_CONF> <TEST_SITE_CONF> <EXP_SITE_CONF>; do sudo cp -p "$f" "$f.bak-$(date +%Y%m%d)"; grep -n '^WSGIDaemonProcess' "$f"; done
```
**Expected:** one `WSGIDaemonProcess <group> python-home=... python-path=...` line per file, with nothing after `python-path=...`.

**A2. Append the options to each `WSGIDaemonProcess` line** (production first):
```bash
sudo sed -i -E 's#^(WSGIDaemonProcess [^ ]+ python-home=[^ ]+ python-path=[^ ]+)[[:space:]]*$#\1 processes=3 threads=10 display-name=%{GROUP} request-timeout=240 queue-timeout=45 socket-timeout=300 connect-timeout=15 graceful-timeout=30 shutdown-timeout=10#' <PROD_SITE_CONF>
sudo sed -i -E 's#^(WSGIDaemonProcess [^ ]+ python-home=[^ ]+ python-path=[^ ]+)[[:space:]]*$#\1 processes=1 threads=10 display-name=%{GROUP} request-timeout=240 queue-timeout=45 socket-timeout=300 connect-timeout=15 graceful-timeout=30 shutdown-timeout=10#' <TEST_SITE_CONF> <EXP_SITE_CONF>
grep -n '^WSGIDaemonProcess' <PROD_SITE_CONF> <TEST_SITE_CONF> <EXP_SITE_CONF>
```
**Expected:** each line now ends with `... shutdown-timeout=10`. If a line did not change, it has other options already: edit it by hand.

**A3. Apply.**
```bash
sudo apache2ctl configtest && sudo systemctl reload apache2
ps -eo pid,nlwp,rss,cmd | grep '[w]sgi:'
```
**Expected:** `Syntax OK`, then three `(wsgi:yse_pz.django)` processes and one each for the two test daemons. Load a few production pages.

**A4. Check the next day.**
```bash
sudo grep -c 'Unable to connect to WSGI daemon' /var/log/apache2/error.log
sudo grep -c 'Queue timeout expired' /var/log/apache2/error.log
sudo grep -E -c 'Daemon process request time limit exceeded|Aborting process' /var/log/apache2/error.log
```
The first count should fall to near zero. Some queue timeouts and request-time-limit restarts are expected while slow pages remain; each line names the URL or process. Tell Ryan which ones recur.

**Rollback:** copy each `.bak-<date>` file back, then `sudo apache2ctl configtest && sudo systemctl reload apache2`.

<a id="backup"></a>
### Nightly database backup without table locks (2026-10-21, #772) recommended, production

A plain `mysqldump` locks each database's tables for the whole dump. Requests that write wait until it finishes, and the web process runs out of threads, which is the midnight spike of 503s. Passing the password with `--password=` also shows it in `ps` to every user on the host while the dump runs. With `--single-transaction`, InnoDB tables are dumped from one consistent snapshot and no locks are held. `--quick` streams rows instead of buffering whole tables. A defaults file keeps the password off the command line.

Placeholders: `<BACKUP_SCRIPT>` is the script the backup cron runs, `<BACKUP_USER>` the Unix user that runs it, `<BACKUP_CNF>` a new file in that user's home (for example `~/.yse-backup.cnf`), `<BACKUP_DIR>` where the dumps go, `<PROD_DB>` the production database.

**B1. Check every table is InnoDB.** `--single-transaction` is consistent only for InnoDB:
```bash
mysql -u root -p -N -e "SELECT engine, COUNT(*) FROM information_schema.TABLES WHERE table_schema='<PROD_DB>' AND table_type='BASE TABLE' GROUP BY engine;"
```
**Expected:** one line, `InnoDB <n>`. Any MyISAM table would still be dumped, without a consistent snapshot. List them and tell Ryan.

**B2. A backup login with only what `mysqldump` needs** (new random password):
```bash
mysql -u root -p -e "CREATE USER 'yse_backup'@'localhost' IDENTIFIED BY '<BACKUP_DB_PASSWORD>'; GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON *.* TO 'yse_backup'@'localhost';"
printf '[mysqldump]\nuser=yse_backup\npassword="<BACKUP_DB_PASSWORD>"\n' | sudo -u <BACKUP_USER> tee <BACKUP_CNF> >/dev/null
sudo -u <BACKUP_USER> chmod 600 <BACKUP_CNF>
```
**Check:** `sudo ls -l <BACKUP_CNF>` shows `-rw------- <BACKUP_USER>`.

**B3. Change the dump command in `<BACKUP_SCRIPT>`.** Keep the output path and file name it uses today; only the options change:
```bash
mysqldump --defaults-extra-file=<BACKUP_CNF> --single-transaction --quick --no-tablespaces --routines --events --triggers <PROD_DB> > <BACKUP_DIR>/<DATE>_<PROD_DB>.sql
```
`--defaults-extra-file` must be the first option. Remove `-u root` and `--password=...`. Optional: `--ignore-table=<PROD_DB>.explorer_querylog` skips SQL Explorer's run history, which is large and not needed to restore the site. Decide this with Ryan before using it.

**B4. Run it once by hand, off-peak, and watch the site while it runs.**
```bash
sudo -u <BACKUP_USER> bash -c 'time python <BACKUP_SCRIPT>'
tail -c 200 <BACKUP_DIR>/<DATE>_<PROD_DB>.sql
```
**Expected:** the last line is `-- Dump completed on <date>`. While it runs, pages keep loading, and `mysql -u root -p -e "SHOW PROCESSLIST"` shows the dump's `SELECT` but no `Waiting for table metadata lock` or `Waiting for table flush`.

**B5. Optional restore check** (needs free disk about the size of the dump; it can take hours on the full database, so run it off-peak):
```bash
mysql -u root -p -e "CREATE DATABASE yse_restore_check"
mysql -u root -p yse_restore_check < <BACKUP_DIR>/<DATE>_<PROD_DB>.sql
mysql -u root -p -e "SELECT (SELECT COUNT(*) FROM <PROD_DB>.YSE_App_transient) live, (SELECT COUNT(*) FROM yse_restore_check.YSE_App_transient) restored; DROP DATABASE yse_restore_check;"
```
**Expected:** the two counts match, or `live` is a little higher because of rows added since the dump.

**Rollback:** put the old command back in `<BACKUP_SCRIPT>`. The new login can stay or be dropped: `DROP USER 'yse_backup'@'localhost';`.

<a id="db-timeouts"></a>
### Database client timeouts (2026-10-21, #770) optional, recommended, per stack

Without client timeouts, a web thread waits as long as MySQL takes. With a fixed number of mod_wsgi threads, a few slow statements make every DB-backed page fail while static files still load. Four optional keys in `[database]` of each stack's `settings.ini` set the limits, in whole seconds (`YSE_`-prefixed environment variables win). Unset or `0` means no timeout, which is today's behaviour. The code is on yse\_experimental now, yse\_test after the next promotion, production after #143.

| Key (env var) | Applies to | Recommended |
|---|---|---|
| `DB_CONNECT_TIMEOUT` (`YSE_DB_CONNECT_TIMEOUT`) | opening a connection, both aliases | `10` |
| `DB_WRITE_TIMEOUT` (`YSE_DB_WRITE_TIMEOUT`) | sending a statement, both aliases | `60` |
| `DB_READ_TIMEOUT` (`YSE_DB_READ_TIMEOUT`) | waiting for a reply, both aliases | `40` |
| `EXPLORER_DB_READ_TIMEOUT` (`YSE_EXPLORER_DB_READ_TIMEOUT`) | replaces `DB_READ_TIMEOUT` for the `explorer` alias (saved SQL, dashboards) | `100` |

The MySQL client retries a timed-out read, so a statement fails after up to **three times** the read timeout: `40` gives about 2 minutes, `100` about 5 minutes, which is Apache's `Timeout 300`. When it fails, the page gets "Lost connection to MySQL server" and the thread is free again. MySQL keeps running the statement until it next tries to send to the closed connection; the Explorer statement cap (`EXPLORER_QUERY_MAX_EXECUTION_MS`, #233) is what stops it on the server.

**T1. Keep long cron statements unlimited.** Crons read the same `settings.ini`, and some run single statements longer than 2 minutes (PhotStat rebuilds, bulk updates, the dashboard cache warmer). In every cron wrapper for the stack, export the override before `manage.py`:
```bash
export YSE_DB_READ_TIMEOUT=0 YSE_EXPLORER_DB_READ_TIMEOUT=0
```
Put it in the file the wrappers source if they share one (for example an `*_envs.sourceme` file). One-off commands such as `migrate` or `rebuild_photstats` from a shell: prefix them with `YSE_DB_READ_TIMEOUT=0`.

**T2. Set the keys** under the existing `[database]` header of `<CHECKOUT>/YSE_PZ/settings.ini`:
```ini
DB_CONNECT_TIMEOUT: 10
DB_WRITE_TIMEOUT: 60
DB_READ_TIMEOUT: 40
EXPLORER_DB_READ_TIMEOUT: 100
```
Then `sudo systemctl reload apache2`.

**T3. Check.**
```bash
cd <CHECKOUT> && <STACK_PYTHON> manage.py shell -c "from django.conf import settings as s; print({a: {k: v for k, v in s.DATABASES[a].get('OPTIONS', {}).items() if k.endswith('timeout')} for a in ('default', 'explorer')})"
```
**Expected:** `{'default': {'connect_timeout': 10, 'write_timeout': 60, 'read_timeout': 40}, 'explorer': {'connect_timeout': 10, 'write_timeout': 60, 'read_timeout': 100}}`. A typo, such as `40s`, stops the stack with `ValueError: [database] DB_READ_TIMEOUT must be a whole number of seconds`. Then load a dashboard and a transient page. In the next day's Apache error log, `Lost connection to MySQL server during query` lines name the requests that hit the limit.

**Rollback:** delete the four lines (or set them to `0`) and reload Apache.

<a id="django-42"></a>
### Django 4.2 LTS on every stack (2026-10-22, #781) required per stack, in promotion order

Production had Django 4.0 (the 2022 install), while `requirements.txt` pinned the 3.2 line, so the #143 deploy step 3.6 moved production down to 3.2.25. From #781 every stack runs **Django 4.2.30**, the newest line that supports Python 3.8 (8 known advisories, against 13 for 3.2.25 and 27 for 4.0). With it: `asgiref` 3.8.1 (Django 4.2 needs 3.6 or newer), `djangorestframework` 3.15.2, `django-el-pagination` 4.1.2 and `django-cron` 0.6.0 (3.3.0 and 0.5.1 import names Django 4.0 removed; #782). django-cron 0.6.0 adds its migration `django_cron.0003_cronjoblock` (one new table, unused by our crons).

Migration `0053_related_name_as_declared` comes with it. Its 42 `AlterField`s only record `related_name` the way Django 4.1+ writes it; `related_name` is Python-only, so it runs **no SQL**, and a stack still on 3.2 reads it fine. On the shared `YSE_test` database whichever stack deploys first applies it.

**Order matters for one package:** install `asgiref` before Django. Django 4.2 cannot import with the old asgiref 3.4.1, while asgiref 3.8.1 runs Django 3.2 and 4.0 too, so a web process that starts between the two installs still works.

**yse\_experimental:** nothing to do. The deploy after the merge runs `pip install -r requirements.txt` in its venv. Expect a few seconds of errors on `/yse_experimental/` during that pip run (Apache restarts at the end of the deploy).

**yse\_test (after the promotion to develop; its venv is not updated by the deploy):**
```bash
cd /data/yse_pz/YSE_PZ_test
/data/yse_pz/yse_test_virtual/bin/python -m pip install "asgiref==3.8.1"
/data/yse_pz/yse_test_virtual/bin/python -m pip install -r requirements.txt
/data/yse_pz/yse_test_virtual/bin/python manage.py check
sudo systemctl restart apache2
```

**Production (after the promotion to master),** in the deploy's pip step (3.6), same order, with `<PROD_PYTHON>` = `/data/yse_pz/yse_virtual/bin/python`:
```bash
<PROD_PYTHON> -m pip install "asgiref==3.8.1"
<PROD_PYTHON> -m pip install -r requirements.txt
```

**Check** (each stack, with its own Python):
```bash
<STACK_PYTHON> -c "import django, asgiref, rest_framework; print(django.get_version(), asgiref.__version__, rest_framework.VERSION)"
cd <CHECKOUT> && <STACK_PYTHON> manage.py showmigrations YSE_App | tail -2
```
**Expected:** `4.2.30 3.8.1 3.15.2`, and `[X] 0053_related_name_as_declared` once the deploy's `migrate` has run.

**Rollback:** check out the previous commit and `pip install -r requirements.txt` from it (that brings back Django 3.2.25, asgiref 3.4.1, DRF 3.15.1), then restart Apache. Migration `0053` can stay: it changes nothing in the database. For production before #143, the saved `pip freeze` from 2026-10-21 restores the original 4.0 environment.

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
| `backfill_quality_mask` dry run on YSE\_test (and `--apply` if it reports changes); production after the promotion carrying 0036 | Confirms the mask matches the DataQuality flags; rows that `develop` flags need the `--apply` until develop has this code | [#80](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/80) · [#81](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/81) · [section](#quality-mask) | pending |

<a id="casjobs-credential"></a>

### Rotate the CasJobs password and set `[casjobs]` in settings.ini (#626) required

The PS1 star/galaxy-score lookups in the ingest jobs no longer carry a CasJobs login in the code. They read it from settings. The old password was in git history, so rotate it.

**(a) Rotate.** Sign in to MAST CasJobs with the project account and change the password. Note the account's WSID from its profile page.

**(b) Set it on each stack** (`<STACK_SETTINGS_INI>` is that stack's `settings.ini`; the cron user's copy too if it differs). Add:
```ini
[casjobs]
wsid=<CASJOBS_WSID>
password=<NEW_CASJOBS_PASSWORD>
```
Or export `CASJOBS_WSID` and `CASJOBS_PW` for the cron environment instead; the environment wins over the file. Nothing needs a restart for the web app, which never reads these.

**(c) Check.** Run any ingest job that calls the PS1 score lookup (for example the DECam cluster upload).
**Expected:** it runs. If the values are missing, only that job stops, with the message "CasJobs credentials are not configured". The website is unaffected either way.

<a id="tns-references"></a>

### TNS references backfill (#645) optional, per stack

New TNS imports write the photometry TNS reports into their own `TNS` header (observation group `TNS`, created on first use) with `reference = "TNS <prefix> <name> (report <ids>)"`, instead of mixing it into the survey's header next to ZTF/ATLAS forced photometry. The report ids appear only when TNS sends them. Nothing is required for new imports. To bring old data in line, run the backfill: a dry run by default, `--apply` writes.

**(a) Dry run.** `<STACK_PYTHON> manage.py mark_tns_imports_public`
**Expected:** three count lines (TNS rows to tag Public, ZTF/ATLAS forced headers to tag Public, references to set) and `Dry run - no changes made`.

**(b) Apply** if the counts look sane: `<STACK_PYTHON> manage.py mark_tns_imports_public --apply`
**Expected:** a `Tagged ...` line; running it again reports zeros. It adds the Public group (never removes one), sets `reference` only where there is none, and leaves a header that also holds forced points alone (old TNS points stay in the shared headers; moving them is not done). The shared `YSE_test` database needs it once.

The TNS cron keeps working under the share rule from #647: its header is new, and a header that already carries Public gains nothing, so no group change is skipped.

<a id="changelog"></a>

## 6 · Changelog

* 2026-10-30 v110, **when Ryan asks: OSC step 1, download and offline dry run, O1-O7** (writes nothing to any database). See the section [Open Supernova Catalog, step 1](#osc-step1). Needs #896 deployed on yse\_experimental.
* 2026-10-29 v109, **nothing required** (the deploy applies the migration; no new package). Open Supernova Catalog import, schema and commands only ([#887](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/887)): migration `0057_catalog_import` creates new tables only (safe on the shared `YSE_test` database). **Do not run `import_osc`, `catalog_instruments` or `osc_tns_check` with `--apply` on any stack until Ryan asks**; the import plan is in docs/osc-import.md.
* 2026-10-29 v108, **nothing to run** (no migration, no package). Spectrum upload from the transient page ([#898](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/898)): a failed upload returns to the transient page with the reason instead of a bare `/upload_spectrum/` page; the file is checked before anything is written; Permissions choices include groups created after the server started; times with seconds are accepted.
* 2026-10-28 v107, **migration applied by the deploy; a test conversion on yse\_experimental when Ryan asks** (no new package). Spectra stored as arrays ([#492](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/492), [#891](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/891)): migration `0056_spectrum_arrays` creates two new tables (safe on the shared database). Uploads now write arrays; every reader reads both storages. **Until this reaches `develop`, never run `convert_spectra_to_arrays --delete-rows` on `YSE_test`** (yse\_test reads pixel rows only), and spectra uploaded on yse\_experimental show only there. Test steps in the section [Spectra stored as arrays](#spectrum-arrays); see docs/spectrum-arrays.md.
* 2026-10-22 v106, **nothing to run** (the deploy applies the migrations). Advisor and advisee relationships, one copy per side, confirmed by the other person (Ryan): migrations `0054_mentorship_kinds` (choices only) and `0055_mentorship_owner_status` (three nullable or defaulted columns on `YSE_App_mentorship`; MySQL keeps `DEFAULT 'confirmed'` on `status` so older code can still insert; existing relations between two users get a confirmed copy on the other side). New notification kind `mentorship` in "Interests and data access". See docs/researcher-profile.md.
* 2026-10-22 v105, **nothing new to run.** Develop backported into experimental (develop-direct PRs [#783](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/783): no astropy IERS downloads in the app, and [#785](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/785): the broker search radius accepts any value). Develop's v39 is v104 here. Experimental already had the downloads off in `views.py` / `view_utils.py` (#466); the setting now lives once, in `YSE_App/apps.py`, next to the Explorer scope hook. The IERS refresh cron is plain Python and is unaffected.
* 2026-10-22 v104 (develop v39), **nothing to run** beyond the deploy (no migration, no package). Observing-night pages timed out (504) on production after #143: the deploy moved astropy from 5.0 to 5.2.2, and twilight times for a recent night made astropy download its IERS (Earth-rotation) table inside the request. `views.py` and `view_utils.py` even set `auto_download = True`. The app now turns those downloads off at startup (`YSE_App/apps.py`) and nothing turns them back on; UT1 refinement is milliseconds, irrelevant for twilight and airmass. Check: an observing night that was never opened loads in a few seconds.
* 2026-10-22 v103, **fixes the yse\_experimental deploy of v102** (nothing new to run; the deploy installs the pins). django-el-pagination 3.3.0 and django-cron 0.5.1 do not import on Django 4.x (`force_text`, `ugettext`), so the v102 deploy stopped at `manage.py check` after pip had installed Django 4.2. Pins: django-el-pagination 4.1.2, django-cron 0.6.0 (adds `django_cron.0003_cronjoblock`, one new table). CI now installs every Django add-on pin into its image, so it tests what the stacks run. yse\_test and production get both with the v102 pip step.
* 2026-10-22 v102, **required per stack, in promotion order** (one migration with no SQL; three package pins). Django 4.2 LTS ([#781](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/781)): Django 4.2.30, asgiref 3.8.1, djangorestframework 3.15.2 on every stack. yse\_experimental: automatic on deploy. yse\_test after the promotion to develop, production after the promotion to master: install asgiref first, then the requirements, see [Django 4.2 LTS on every stack](#django-42). Migration `0053_related_name_as_declared` changes no table. Production ran Django 4.0 until the #143 deploy moved it to 3.2.25.
* 2026-10-21 v101, **recommended** (no code change): server sizing and backup. New sections [MySQL memory and connection limits](#mysql-sizing) (M1-M3, [#777](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/777)), [mod_wsgi daemon size and timeouts](#wsgi-sizing) (A1-A4, [#771](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/771)) and [Nightly database backup without table locks](#backup) (B1-B5, [#772](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/772)). Do M2 before A2.
* 2026-10-21 v100, **nothing new to run** (the deploy applies the migration; v99 needs no step). Develop backported into experimental again (develop-direct PR [#767](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/767): agreement and consent page edits [#766](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/766)). Develop's v38 is v99 here. Migration `0042_agreement_consent_wording` keeps develop's name and is joined to experimental's `0051_merge_develop_consent` by the empty merge migration `0052_merge_develop_consent_wording`; on the shared `YSE_test` database develop already applied it, and the edit is idempotent.
* 2026-10-21 v99 (develop v38), nothing required. Agreement and consent page edits ([#766](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/766)): migration 0042 edits the current User Agreement v2 and Research Consent Form v1 wording in place (titles lose "(v2)"/"(v1)", small text edits; versions, acceptances and consent records untouched, nobody re-accepts; idempotent on the shared database). The pages show only the title; "Research consent (voluntary)" is now "Optional research consent"; Consent, Decline and Not now continue to the researcher profile while its required fields are incomplete.
* 2026-10-21 v97, **optional, recommended** (no migration, no new package): database client timeouts ([#770](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/770)). New section [Database client timeouts](#db-timeouts), T1-T3 per stack; set the cron override (T1) before the keys (T2). Nothing changes until the keys are set.
* 2026-10-21 v96, **optional, recommended** (no migration, no new package): `GET /healthz/` ([#768](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/768)) and the server watchdog ([#769](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/769)). New section [Server watchdog and `/healthz/`](#watchdog), steps W1-W5, root on Ziggy; start with `DRY_RUN=1`. Nothing changes until it is installed.
* 2026-10-21 v95, **nothing required**; one optional check. Cron hygiene: the legacy TNS crons no longer replace our `best_spec_class` on transients we already have; they follow the TNS re-sync rule (`TNS_spec_class` follows TNS, `best_spec_class` only when we have none or ours is the previous TNS class) ([#755](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/755)). `PhotometryUploadExample.PhotometryUploads` is removed from `CRON_CLASSES` and the module is deleted ([#756](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/756)). A cron that finds another instance of itself running still sends its alert email but now returns, so the rest of that `runcrons` run continues ([#757](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/757)). The ZTF forced-photometry download login is read from settings (`[ztf] ztfwebdavuser` / `ztfwebdavpass`, or `ZTF_WEBDAV_USER` / `ZTF_WEBDAV_PASS`); the defaults are ZTF's published generic login, so leave them unset ([#758](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/758)). No migration. **Optional check:** `crontab -l | grep -c PhotometryUploadExample` as the cron user on Ziggy. **Expected:** `0`. **If not:** delete that crontab line (the module no longer exists).
* 2026-10-21 v94, **nothing required** (no migration, no new package; one optional setting). Duplicate transients ([#752](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/752), [#753](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/753), [#754](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/754)): the `CheckDuplicates` cron only reports candidate pairs (notification to `DUPLICATE_REPORT_USERNAMES`, default every active superuser) and uploads no longer delete same-name transients; a person merges a pair with `manage.py transient_duplicates merge <DUPLICATE_ID> <KEEP_ID> --yes` (dry run without `--yes`; one transaction). `DUPLICATE_AUTO_MERGE` (off) restores unattended merging ([section "Duplicate transients"](#duplicates)).
* 2026-10-20 v93, **nothing new to run** (the deploy applies the migrations; the consent steps are in v91/v92). Develop backported into experimental again (develop-direct PRs [#745](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/745) and [#747](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/747): User Agreement v2 [#743](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/743), research consent [#744](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/744), research export [#746](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/746)). Develop's v36/v37 are v91/v92 here. Migrations `0038_user_agreement_v2` to `0041_research_consent_prompts` keep develop's names and are joined to experimental's `0050_value_change` by the empty merge migration `0051_merge_develop_consent`; on the shared `YSE_test` database whichever stack deploys first applies them. On experimental the research export also includes `value_changes`, `night_plans` and `observed_blocks` and the revoke fields of data access requests, under the same consent rules and pseudonyms.
* 2026-10-20 v92 (develop v37), optional (Step A), then by hand each time you are notified (Step B). Research consent follow-ups ([#744](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/744)) and the research export ([#746](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/746)): migration 0041 (one new table). Opt-outs are no longer deleted automatically: administrators are notified (`optout_notify_usernames`, default every superuser) and run `purge_unconsented_demographics --user <USERNAME>` (dry run, then `--apply`). Reminders for users who have not answered. `export_research_database --out <file>` only when Ryan asks ([section "User Agreement v2, research consent and the research export"](#research-consent)).
* 2026-10-20 v91 (develop v36), nothing required (the purge runs later, only when Ryan says). User Agreement v2 ([#743](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/743)) and the voluntary research consent form ([#744](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/744)): migrations 0038-0040 (agreement v2, two new tables, form v1); everyone accepts v2 once; `purge_unconsented_demographics` (dry run by default) after the grace period ([section "User Agreement v2, research consent and the research export"](#research-consent)).
* 2026-10-19 v90, **nothing required** (no migration, no new package, no flag). DESI for host-less transients and a per-object DESI button ([#738](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/738)): the new enrichment service `desi-hostless` (`manage.py enrich --service desi-hostless --names ...`; dry run by default, never swept, needs an explicit selection) queries DESI galaxies within `DESI_HOSTLESS_SEARCH_ARCMIN` (5) of a transient with no host, picks the host by NED's rule (smallest separation x z, within 100 kpc), links it like Prost does and writes the host's redshift only through the redshift precedence list; the link and redshift are in the change log. A transient's own redshift is never set from a nearby galaxy. The transient page has a "Request DESI Lookup" button (any signed-in user, like the ZTF button) that queues the host's DESI check, or the host match when there is no host, and shows the result. It is rate-limited: one request per object per `DESI_BUTTON_COOLDOWN_SECONDS` (600) and `DESI_BUTTON_MAX_PER_HOUR` (20) per user. The job runner must be running for the button to do anything.
* 2026-10-19 v89, **migration `0050_value_change` applied by the deploy (one new table); nothing else required** (no new package, no flag). Change log of automatic updates ([#737](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/737)): every real change made by the TNS re-sync, NED/DESI, Prost, the photo-z services, Hermes and the enrichment framework, and every redshift override set or cleared (command or API), is one timestamped `ValueChange` row with its source, service, run and actor; checks that change nothing record nothing. Shown in a deferred "History" tab on the transient page and at `GET /api/transients/<id>/history/` (read-only). The table names who set an override, so it is on the SQL Explorer personal-data list (`YSE_App_valuechange`); if the Explorer MySQL user is granted tables one by one, do not grant it this table.
* 2026-10-18 v88, **nothing required** (no migration, no new package). Enrichment framework ([#439](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/439)): one registry of enrichment services with a per-object run log, `manage.py enrich` (dry run by default; `--service`, selection options, resumable, `--max`, `--queue`) and the `Enrichment.EnrichmentSweep` cron (off until `ENRICH_SWEEP_CRON_ENABLED`). `PS1_cutouts.YSE` stops downloading cutouts again (failures retried after 30 days, at most 3 times); `Photo_Z`, `SDSS_Photo_Z` and `Gaia_LC` keep their schedules and record each object's outcome. Prost matching is now a per-object function; with `PROST_HOST_MATCH_ENABLED` (off) the TNS gap import and the follow-up enrichment run it for objects without a host. Optional Blast annotation service (off; needs a Blast token). See [Enrichment framework](#enrichment).
* 2026-10-18 v87, **migration `0049_redshift_provenance` applied by the deploy (nullable columns only); backfill optional** (`manage.py host_redshifts`, dry run by default; no new package). Host redshifts from NED::API and DESI via Astro Data Lab, with provenance and one precedence list ([#436](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/436), [#437](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/437)): an override set by hand wins (`manage.py redshift_override`); otherwise NED > DESI > our own > other catalogues > no source > TNS > photo-z; every automatic writer, including uploads and the TNS re-sync, follows it; the hourly `RedshiftCatalogSweep` stays off until `[redshifts] REDSHIFT_CATALOG_CRON_ENABLED` ([section "Host redshifts from NED and DESI"](#ned-desi-redshifts)).
* 2026-10-18 v86, **check that the job runner is running; nothing else required** (no migration, no new package). Automatic TNS and ZTF refresh on a new classical-night follow-up ([#114](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/114)): on by default (Ryan, 2026-10-18). See [Follow-up enrichment](#followup-enrich).
* 2026-10-18 v85, **remove the `tns_updates_realtime.bash` crontab line; backfill optional** (`manage.py tns_resync`, dry run by default; no migration, no new package). TNS re-sync ([#438](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/438)): re-sync of TNS classifications, redshifts, photometry and spectra for transients we have, gap import of objects reported while the crons were down, and an hourly `TNSResync` sweep that stays off until `TNS_RESYNC_CRON_ENABLED`; `TNS_recent_realtime` is removed from `CRON_CLASSES` ([section "TNS re-sync and backfill"](#tns-resync)).
* 2026-10-18 v84, **check external readers of `/query_api/`** (no migration, no new package). `/query_api/` returns unscoped rows only to service readers ([#725](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/725)): the `[main] dblogin` cron account and `[site_settings] DATA_SERVICE_READERS`; staff, superusers and `api_writers` accounts go through the Explorer rewrite layer like everyone else and see only their data. The built-in cron titles (`DECAT_transients_mags`, `yse_transients_goodcell`, `all_yse_transients`, the QUB forced-photometry query) are unchanged. Check: any other account that polls `/query_api/` and must keep reading everything goes in `DATA_SERVICE_READERS` (see [section "Staff see only the data they have access to"](#staff-visibility)); these accounts also no longer run saved queries off the allowed list unless they have SQL Explorer access.
* 2026-10-18 v83, **new package `sqlglot==26.33.0` on any venv the deploy does not install; check the Explorer database user grants** (no migration). SQL Explorer for everyone ([#723](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/723)): every Explorer query and every page-feeding saved query is rewritten to read only its viewer's rows (per point; service readers unchanged), so staff see only their data in Explorer and page queries over photometry run again, scoped and cached per viewer; with table-level Explorer grants add `SELECT (id, name)` on `auth_group`; `EXPLORER_OPEN_TO_ALL_USERS` (off) opens `/explorer/` to every signed-in user; rollback `EXPLORER_QUERY_SCOPE: off` ([section "SQL Explorer for everyone"](#explorer-for-everyone)).
* 2026-10-18 v82, **one command per database** (`manage.py switch_dashboard_queries`, dry run by default; no migration). Dashboard SQL queries as viewer-scoped Python queries ([#720](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/720)): the magnitude-limited sample, Fast & Young, New Transients Last Two Days and Interesting New Targets exist as Python queries that select only from photometry the viewer may see; the command repoints dashboard sections to them (SQL stays in Explorer) and lists the saved queries that still need a Python equivalent; `best_spectrum_id` is no longer readable by page-feeding queries for viewers who do not see all data. Steps in the section "Dashboard queries as viewer-scoped Python queries".
* 2026-10-18 v81, **nothing required now** (docs only; the flag stays off). Access rules documentation ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 6): user guide `docs/data-access-rules.md`, developer notes `docs/access-rules-engine.md`, and the steps to enable `DATA_ACCESS_GRANTS_ENABLED` on yse\_experimental first, with `backfill_photstat_scopes --apply` ([section "Enable access rules"](#enable-access-rules)).
* 2026-10-18 v80, **migration applied by the deploy; one optional command** (`manage.py migrate_point_programs`, dry run by default). Access is per point ([#714](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/714)): every read returns exactly the visible points; migration `0047_point_program_link` adds a nullable, indexed `allocation_id` on photometry points (no FK constraint: online on MySQL); standing grants follow each point's program ([section "Access is per point"](#per-point-access)). No new package.
* 2026-10-18 v79, **check the script accounts** (no migration). Staff see only the data they have access to ([#706](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/706)): staff and superusers no longer bypass collaboration groups or access rules on any data path; only `DATA_SERVICE_READER_USERNAMES` (the `[main] dblogin` account plus `[site_settings] DATA_SERVICE_READERS`) read all data; add any other cron or script account that must keep reading everything ([section "Staff see only the data they have access to"](#staff-visibility)). No new package.
* 2026-10-18 v78, **nothing required** (no migration; one check recommended). Saved Explorer queries that feed pages are viewer-scoped ([#715](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/715)): for viewers who do not see all data, page-feeding queries may read only transient metadata tables; queries over photometry, spectra or their statistics are refused for them (staff, service accounts and the cache warmer unchanged). Rollback switch `PAGE_QUERY_TABLE_POLICY: off`. See the section "Saved Explorer queries that feed pages" above.
* 2026-10-17 v77, **nothing required** (no migration). Data reported to TNS become and stay public ([#708](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/708)): after TNS confirms a report (manual dialog, autopublish, legacy DECam form; never the sandbox), exactly the photometry points and spectra that were sent get the Public group and lose their other collaboration groups; a photometry set of which only some points were sent is split (the sent points move to a new public header, the rest stay restricted). Reports queued before this deploy do not record their points, so earlier reports are not touched (`mark_tns_imports_public` covers TNS imports). Staff can no longer opt data in to autopublish (PIs and deputies only; existing opt-ins stay, staff may still opt out).
* 2026-10-18 v76, **migration applied by the deploy; nothing else required** (dashboard hidden while `DATA_ACCESS_GRANTS_ENABLED` is off). Ryan's access decisions ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 6a): PIs may restrict shared data; turning sharing off is forward by default with a backwards option; removing a group from a dataset writes `kept` rules for the earlier points; data access dashboard with the autopublish opt-ins; migration `0046_access_rule_kept` (choices only) ([section "Data access dashboard"](#access-dashboard)). No new package.
* 2026-10-17 v75, **nothing required** (no migration, no flag). Upload checks trust PIs and deputies for their own data ([#707](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/707)): when an upload targets an existing photometry set or spectrum, that dataset's PIs and data-sharing deputies, and its program's PI and program deputies, pass the upload checks for it (replace, add, re-share). Other users, new datasets and bulk ingest are unchanged.
* 2026-10-17 v74, **nothing required** (hidden while `DATA_ACCESS_GRANTS_ENABLED` is off; no migration). Access rule pages and API ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 5): program page with standing access and program deputies, per-object night and dataset actions, staff rule removal, `/api/access/` endpoints ([section "Access rule pages and API"](#access-pages)). No new package.
* 2026-10-17 v73, **nothing required** (flag stays off; no migration). Standing access grants ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648)): stopping one applies going forward in time; points taken before the stop are still covered when they are uploaded or linked to the program later ([section "Per-point access rules engine"](#access-rules-engine)). No new package.
* 2026-10-17 v72, **nothing required; backfill optional** (`manage.py backfill_data_programs`, dry run by default; flag stays off; no migration). Program links of photometry and spectra ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 3): derived at ingest from the follow-up (or a matching AstroScheduler observation), set by facility results, named by uploads (`allocation`), or set through `/api/datasets/<kind>/<id>/program/`; linking applies the program's standing access grants ([section "Program links"](#access-program-links)). No new package.
* 2026-10-17 v71, **nothing required** (flag stays off; no migration). Per-point access rules engine ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 2): precedence, dated-membership rule, rules-version cache keys, PhotStat scope handling and the actions service; with `DATA_ACCESS_GRANTS_ENABLED` off every read is unchanged; standing grants are applied to new points at ingest ([section "Per-point access rules engine"](#access-rules-engine)). No new package.
* 2026-10-17 v70, **migration applied by the deploy; nothing else required** (flag stays off). Per-point access rules ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 1b): migration `0045_per_point_access_rules` drops the empty PR 1 rule table and adds five tables (per-point rules, standing grants, dated group membership seeded from current memberships, dataset group share times, a version counter; safe on the shared database); group and dataset-share history is recorded from now on; admin pages for staff; all new tables hidden from SQL Explorer ([section "Per-point access rules"](#access-rules-per-point)). No new package.
* 2026-10-17 v69, **migration applied by the deploy; nothing else required**. TNS and Hermes reports send only data the sender may see and share ([#691](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/691)): migration `0044_autopublish_optin` (one new table, no change to existing tables; safe on the shared database). The report dialog, preview and submit include public data plus restricted data the submitter can see and may share as a PI, deputy or staff, and the dialog says how many restricted datasets were left out; automatic publishing sends public data only until a PI opts a dataset in (`POST /sharing/autopublish_optin/<photometry|spectrum>/<id>/`, logged). `YSE_App_autopublishoptin` is hidden from SQL Explorer; leave it out of table-level Explorer grants. No new package.
* 2026-10-17 v68, **David: confirm the cron accounts are staff, superusers or in `api_writers` (otherwise their uploads get 403); no migration**. Upload group checks are now on by default ([#697](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/697)); Ryan overrode the usual "flags default to today's behaviour" rule for this flag. `add_transient` is limited to staff, superusers and `api_writers`; `add_transient_phot` / `add_transient_spec` / the spectrum form apply the group and ownership rules. Turn off temporarily with `UPLOAD_GROUP_CHECKS_ENFORCED: False` (or `YSE_UPLOAD_GROUP_CHECKS_ENFORCED=0`) and restart ([section](#upload-checks-default-on)).
* 2026-10-17 v67, **migration applied by the deploy; nothing else required** (flag stays off). Access grants schema ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648), PR 1): migration `0043_data_access_grants` (two new tables, a nullable `allocation_id` column on transient photometry and spectra; safe on the shared database); admin pages for staff; `DATA_ACCESS_GRANTS_ENABLED` defaults off and nothing reads the rules yet; both tables hidden from SQL Explorer, leave them out of table-level Explorer grants ([section "Access grants and program deputies"](#access-grants-schema)). No new package.
* 2026-10-16 v66, **migration applied by the deploy; one backfill required per database** (`manage.py backfill_photstat_scopes --apply`, dry run by default). One visibility policy for transient lists ([#607](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/607)): migration `0042_transient_photstat_scope` (one new table, safe on the shared database); non-staff users see TNS-named objects plus the objects whose data they may see, everywhere, and derived photometry values only from photometry they may see ([section "One visibility policy"](#transient-visibility)). Explorer grant note for table-level grants. No new package.
* 2026-10-16 v65, optional (backfill). TNS references ([#645](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/645)): TNS-reported photometry now goes in its own `TNS` header with the TNS reference (and report ids when sent); `mark_tns_imports_public` is a dry run by default (`--apply` writes; `--dry-run` still accepted) and also tags ZTF/ATLAS forced photometry Public and sets TNS references on old imports ([section](#tns-references)). No migration.
* 2026-10-16 v64, **required on venvs the deploy does not install**: install `astroscheduler` 1.3.0, then `collectstatic` ([#678](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/678); [section "AstroScheduler 1.3.0"](#astroscheduler-130)). No migration.
* 2026-10-16 v63, **nothing new to run**. AstroScheduler planner: end-to-end smoke test ([#35](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/35)); the plans list and the target-list export now use a fixed number of queries ([#675](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/675)); observer guide and performance notes in `docs/astroscheduler.md` ([#676](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/676)). No migration, no new package.
* 2026-10-16 v62, **migration applied by the deploy; nothing else required (expiry optional, off)**. AstroScheduler outcomes and request status ([#670](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/670), [#672](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/672)-[#674](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/674)): migration `0041_observed_block_outcomes` only changes choices and help text (no SQL). Completed records "Data taken"; a good judgement or an uploaded spectrum/photometry set makes the request Successful; the new daily `ExpireFollowupRequests` cron (already in `CRON_CLASSES`, run by the existing `runcrons`) fails ended classical requests without good data, but only once `ASTROSCHEDULER_EXPIRE_AFTER` is set ([section "AstroScheduler execution feedback"](#astroscheduler-observations)).
* 2026-10-16 v61, **migration applied by the deploy; nothing else required**. AstroScheduler execution feedback ([#666](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/666)-[#668](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/668)): migration `0040_observed_blocks` (two new tables and 12 seeded outcome reasons, safe on the shared database); the planner's Start/Completed/Failed/Punted are recorded and update follow-up statuses, behind `ASTROSCHEDULER_ENABLED` ([section "AstroScheduler execution feedback"](#astroscheduler-observations)).
* 2026-10-16 v60, **migration applied by the deploy; required on venvs the deploy does not install** (install `astroscheduler` 1.2.0, then `collectstatic`). AstroScheduler saved plans ([#661](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/661)-[#664](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/664), [#77](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/77)): migration `0039_night_plans` (two new tables, safe on the shared database); save, approve, re-open and export from the planner page, all behind `ASTROSCHEDULER_ENABLED` ([section "AstroScheduler saved plans"](#astroscheduler-plans)).
* 2026-10-16 v59, **required on venvs the deploy does not install** (yse\_test, Apache's venv for yse\_experimental, production): install `astroscheduler` 1.1.0, then `collectstatic`; flag optional. AstroScheduler planner page and nights list ([#658](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/658), [#659](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/659)): no migration; the "Plan this night" button and `/night_planner/<id>/` appear only with `ASTROSCHEDULER_ENABLED: True` ([section "AstroScheduler planner page"](#astroscheduler-page)).
* 2026-10-15 v58, **required on venvs the deploy does not install** (yse\_test, Apache's venv for yse\_experimental, production); flag optional. AstroScheduler night planner, phase 2 ([#653](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/653)-[#656](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/656)): install the pinned `astroscheduler` 1.0.1 package (no migration). The planner targets and solve APIs stay off until `ASTROSCHEDULER_ENABLED: True` is set; enable it on yse\_experimental only when Ryan asks ([section "AstroScheduler night planner"](#astroscheduler)).
* 2026-10-15 v57, **nothing new to run**. Develop backported into experimental again (develop-direct PRs [#641](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/641), [#642](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/642), [#644](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/644), [#649](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/649) [#651](https://github.com/Young-Supernova-Experiment/YSE_PZ/pull/651) and David's forced-phot URL update): the five develop entries below (develop's own v31-v35) are renumbered v52-v56 in this changelog. On experimental, develop's migrations `0033_group_profiles` to `0037_data_sharing_delegates` sit beside experimental's `0033_photstat_extrapolated` to `0037_merge_develop_profile`; the new no-op merge migration `0038_merge_develop_groups` joins the two lines. All of develop's migrations are already applied on the shared `YSE_test` database by the develop deploy, so the experimental deploy only records `0038`. The develop sections above (Research teams, PIs on private data, Who may share private data, Self-service signup) apply to yse\_experimental unchanged; on experimental the signup, email-confirmation and invitation-link pages are reachable signed out under the login-required default, and staff two-factor sign-in (when on) skips only the invitation and email-confirmation links, so their tokens stay out of `?next=`; `/teams/`, `/teams/join/`, `/settings/` and `/data_sharing_deputies/` ask unverified staff for the code first, then the onboarding gate applies. SQL Explorer: the PI tables are visible again on experimental too (Ryan, 2026-10-13, #643); if you limited the Explorer database user's grants ([Step E](#authz-flags)), see the note there.
* 2026-10-15 v56 (develop v35), nothing required. Data-access requests with no PI notify superusers only, and `YSE_App_datasharingdelegate` is hidden from SQL Explorer; add it to table-level Explorer grant exclusions if you use them ([#650](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/650)).
* 2026-10-13 v55 (develop v34), nothing required (link PI accounts first so PIs, not only staff, can decide). Only PIs and their deputies share private data ([#447](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/447)): migration 0037 (one new table, one nullable column); allocation-PI fallback; logged staff override; `/data_sharing_deputies/`; TNS references; PI tables visible in Explorer again ([section "Who may share private data"](#data-sharers)).
* 2026-10-13 v54 (develop v33), nothing required (signup off by default). Self-service signup and team membership requests ([#448](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/448)): migration 0036 (one new table); turn on with `SELF_SIGNUP_ENABLED: True` when ready; optional `purge_group_membership_requests`. Public always offered in pickers ([#440](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/440)); PI contact fields staff-only in the API and PI table hidden from Explorer ([#643](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/643)) ([section "Self-service signup"](#self-signup)).
* 2026-10-13 v53 (develop v32), nothing required (PI accounts and backfill optional). PIs on private data ([#446](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/446)): migration 0035 (one nullable column, two new join tables); staff link PIs to accounts in the admin; optional `backfill_data_pis` (dry run by default) ([section "PIs on private data"](#data-pis)).
* 2026-10-12 v52 (develop v31), nothing required (owners optional). Research teams, invitation links and user settings ([#445](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/445), [#440](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/440)): migrations 0033/0034 (new tables, one profile per group, no owners, all active, system groups marked); staff assign owners in Admin > Groups ([section "Research teams"](#teams)); optional `ensure_group_profiles` check, inactive flag, `purge_group_invitations` and `[groups]` keys.
* 2026-10-12 v51, **nothing new to run**. Develop backported into experimental (develop-direct PRs #633, #635, #639 and David's develop hotfixes): the three develop entries below (develop's own v28-v30) are renumbered v48-v50 in this changelog. On experimental, develop's migrations `0031_researcher_profile` and `0032_seed_user_agreement` sit beside experimental's `0031_spatial_time_indexes` / `0032_api_writer_groups`; the merge migration `0037_merge_develop_profile` (no schema change) joins the two lines. On the shared `YSE_test` database both lines are applied by whichever stack migrates first; the experimental deploy then only records 0037. The onboarding gate exempts the two-factor setup and verify pages, `/feedback/`, `/query_api/`, the API and the facility callbacks.
* 2026-10-12 v50 (develop v30), optional (recommended). Per-stack session / CSRF cookie names and paths ([#637](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/637)): defaults unchanged; set the four `[site_settings]` keys on yse\_test and yse\_experimental (section "Per-stack session and CSRF cookies"); everyone on that stack is logged out once.
* 2026-10-12 v49 (develop v29), optional. Onboarding gate under the `/yse_test/` prefix ([#634](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/634)): mitigation (turn the gate off on yse\_test) until the fix deploys; the fix itself needs no server step.
* 2026-10-12 v48 (develop v28), **required once the develop-direct PR is merged** (develop first, then experimental). User agreement, researcher profile and onboarding gate ([#627](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/627)-[#631](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/631)): migrations 0031/0032 (new tables, agreement v1); the gate is on by default (rollback `ONBOARDING_GATE_ENABLED: False`); **required:** install only the research public key ([step A](#onboarding)); optional SciX metrics and Explorer grants.
* 2026-10-12 v47, one check recommended. Data-quality mask ([#80](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/80), [#81](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/81), [#82](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/82), [#480](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/480)): migration `0036_quality_mask` adds `quality_mask`, `pixel_mask` and three nullable metadata columns with MySQL defaults (instant ALTERs on MySQL 8.0.12+) and backfills the mask from the DataQuality flags. It is applied by the deploy. New section [Data-quality mask columns](#quality-mask): check the columns, then run `backfill_quality_mask` as a dry run (and with `--apply` if it reports changes; on yse\_test after `develop` flags points). The good-data filters now read the mask, with identical results. No new dependencies.
* 2026-10-12 v46, optional. Prost scratch isolation ([#617](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/617)): each Prost run now writes to its own `prost-*` subdirectory under the scratch base and removes only that subdirectory (it used to delete the whole `[main] ghost_path`, which could break overlapping TNS crons). Nothing required: `[main] ghost_path` keeps working. Optionally add `[main] prost_path=<PROST_SCRATCH_DIR>` to the stack's `settings.ini`; when set it is used instead of `ghost_path`. Stale `prost-*` directories older than one day are cleaned up on the next run. No migration.
* 2026-10-12 v45, **required** (credential rotation). CasJobs login moved out of the ingest code ([#626](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/626)): new section [Rotate the CasJobs password and set `[casjobs]`](#casjobs-credential). Rotate the CasJobs password and add `[casjobs] wsid` / `password` to each stack's `settings.ini` (or `CASJOBS_WSID` / `CASJOBS_PW` in the cron environment); until then only the PS1-score ingest jobs fail, with a clear message. Also: `PhotometryUploadExample.py` and `scrub_private_data.py` now read their login from the environment. A gitleaks rule now flags inline credentials in Python. No migration.
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
