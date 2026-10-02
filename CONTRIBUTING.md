# Contributing to YSE_PZ

All development happens in [Young-Supernova-Experiment/YSE_PZ](https://github.com/Young-Supernova-Experiment/YSE_PZ).
The old `astrofoley/YSE_PZ` fork, its `main` branch, and `davecoulter/YSE_PZ` are no
longer part of the workflow; do not open pull requests against them.

## Branches

| Branch | Deployed to | Gets code from |
| --- | --- | --- |
| `experimental` | ziggy `/yse_experimental/` (automatic, Deploy Stack workflow) | feature/fix branches, by PR |
| `develop` | ziggy `/yse_test/` (automatic) | **only** `experimental`, by a promotion PR |
| `master` | production (deployed by hand) | **only** `develop`, by the release PR |

Details, the promotion guard, and dependency notes: [docs/branching.md](docs/branching.md).

## Workflow

1. **One GitHub issue per change.** Open an issue for each individual bug, feature or
   chore before working on it, even small ones.
2. **One worktree branch per group of related issues**, branched from `experimental`:
   ```bash
   git fetch origin +refs/heads/experimental:refs/remotes/origin/experimental
   git worktree add -b fix/<short-description> ../wt-<short-description> origin/experimental
   ```
   Unrelated issues get separate branches. Non-conflicting small fixes may share a PR
   as long as every issue is linked.
3. **Open a detailed PR into `experimental`.** The body says what changed and why, how it
   was tested, any migrations (numbered by merge order: renumber if another PR landed
   first), new dependencies, and any step someone must run on the server. Link every
   issue on its own line with `Fixes #N`.
4. **Auto-merge into `experimental`** (merge commit, not squash/rebase) once CI
   (`lint`, `docker-test`) is green. The merge deploys to `/yse_experimental/`.
   On each issue, comment that it is implemented in experimental and add the label
   `landed:experimental`. Do **not** close the issue.
5. **Ryan tests on `/yse_experimental/`.** Follow-up fixes go through steps 1 to 4 again.
6. **Promotion PR `experimental` -> `develop`**, opened from the `experimental` branch
   itself (never push a branch named `experimental`). Its body lists the PRs and issues it
   carries, the migrations, and server steps. Merging deploys to `/yse_test/`.
7. **David tests on `/yse_test/`.** If he hotfixes `develop` directly while reviewing, the
   same change is backported to `experimental` right away (issue + PR) so the branches
   do not drift.
8. **David merges `develop` -> `master`** (the standing release PR) and deploys
   production by hand. Only David merges to `master`.

Issues close **only** when the `develop` -> `master` PR merges: GitHub auto-closes
issues only for PRs into the default branch (`master`), so the release PR body carries a
"Closes on merge" list with a `Fixes #N` line for every issue promoted since the last
release.

### Tests in PRs

- Tests must not reach external services (TNS, brokers, MAST, Gaia, ...): mock HTTP.
- In CI the `explorer` database alias is a read-only MySQL user; do not declare
  `databases = {'default', 'explorer'}` in a test case.
- A `docker-test` exit code 137 before any test ran is the runner being killed for memory;
  re-run once before debugging.

## Local Docker

Web container image: **`ghcr.io/young-supernova-experiment/yse_pz:latest`** from [Young-Supernova-Experiment/YSE_PZ](https://github.com/Young-Supernova-Experiment/YSE_PZ) (not the legacy `davecoulter` GHCR image). `yse-docker.sh up` pulls this image; your clone is mounted at `/app`.

### First-time setup

```bash
cp YSE_PZ/public_settings.ini YSE_PZ/settings.ini
cp docker/public.env docker/.env
```

Edit `docker/.env` so paths are **absolute** (required for Docker Desktop on macOS):

- `VOL` — repo root
- `VOL_DB` — e.g. `…/YSE_PZ/database`
- `STATIC_VOL` — e.g. `…/YSE_PZ/YSE_PZ/static`
- `VOL_GHOST` — e.g. `…/YSE_PZ/ghost_logs`

Then from the repo root:

```bash
./docker/scripts/yse-docker.sh up
```

Open **http://127.0.0.1:8080/login/** (port from `LOCAL_HTTP_PORT` in `.env`).

More detail: [docker/readme.txt](docker/readme.txt).

### Static files (`collectstatic`)

Nginx serves files from `STATIC_VOL` (host `YSE_PZ/static/`). Most assets are committed; if the admin UI or CSS looks broken after a fresh clone, gather static files into that directory:

```bash
./docker/scripts/yse-docker.sh collectstatic
```

This runs `manage.py collectstatic --noinput` inside `ysepz_web_container` and writes into `YSE_PZ/static/` on the host (via the app volume).

### Disk space and pruning

Docker pull/build cycles can use 10–20 GB per iteration on macOS. **`yse-docker.sh` prunes automatically** after successful `up`, `pull`, and `rebuild` unless disabled.

| Command | What it does |
|---------|----------------|
| `./docker/scripts/yse-docker.sh up` | Start stack; light prune after success |
| `./docker/scripts/yse-docker.sh pull` | Pull `ghcr.io/davecoulter/yse_pz:latest`; aggressive prune |
| `./docker/scripts/yse-docker.sh rebuild` | Build local dev image + start; aggressive prune |
| `./docker/scripts/yse-docker.sh prune` | Prune only (`prune aggressive` for more) |
| `./docker/scripts/yse-docker.sh down` | Stop stack (**keeps** MySQL data in `VOL_DB`) |
| `./docker/scripts/yse-docker.sh collectstatic` | Populate `YSE_PZ/static/` for nginx |

**Skip pruning for one command:**

```bash
YSE_DOCKER_PRUNE=0 ./docker/scripts/yse-docker.sh up
```

**What pruning removes:** dangling layers, build cache, and old `ghcr.io/davecoulter/yse_pz` / `local/yse_pz_web` images not used by `ysepz_*` containers.

**What pruning does not remove:** MySQL data under `VOL_DB`.

**Intentional database wipe:**

```bash
cd docker && docker compose down -v
```

**If Docker Desktop still shows a huge disk image:** Docker Desktop → Troubleshoot → Clean / Purge data, or:

```bash
docker system df
./docker/scripts/yse-docker.sh prune aggressive
```

Keep several GB free on the host; git and Docker both fail when the disk is full.

## Tests

```bash
docker exec ysepz_web_container python3 manage.py test YSE_App.tests --verbosity=2
```

### Performance baselines

Page-load regression tests track **SQL query count** and **wall-clock load time (ms)** for each page. A summary table prints at the end of the run.

```bash
docker exec ysepz_web_container python3 manage.py test YSE_App.tests.test_performance --verbosity=2
```

| Variable | Effect |
|----------|--------|
| `YSE_PERF_SKIP_TIMING=1` | Skip load-time ceiling assertions (queries still checked) |
| `YSE_PERF_RECORD_PATH=/tmp/yse_perf.json` | Export metrics JSON for CI or local comparison |

Targets: `/transient_detail/<slug>/` (shell + synthetic loaded), `/personaldashboard/`, `/dashboard/`, `/explorer/` (including 200-row catalog + logs; query count only).

CI runs the same flow via [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Secrets (optional)

For production or shared machines, prefer environment variables over committing secrets in `settings.ini`:

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | Django `SECRET_KEY`. Required (here or as `[site_settings] SECRET_KEY` in `settings.ini`) whenever `IS_DEBUG` is `False`; startup raises `ImproperlyConfigured` otherwise. With `IS_DEBUG: True` a fixed development key is used. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated `ALLOWED_HOSTS` (or `[site_settings] ALLOWED_HOSTS`). Unset accepts every host, as before. |
| `YSE_EXPLORER_MAX_EXECUTION_MS` | Time cap (ms) for statements on the explorer DB connection (or `[site_settings] EXPLORER_QUERY_MAX_EXECUTION_MS`; MySQL only). Default 0 = no cap; recommended 20000 once the saved dashboard queries are rewritten (#233). |
| `TNS_API_KEY` | TNS bot API key |
| `TNS_DECAM_API_KEY` | DECam TNS bot key |
| `SLACK_BOT_TOKEN` | TNS Slack notifications (`TNS_Bot.py`) |

## Database

Local Docker uses init SQL under `docker/db_init/` only. Do not import the full production database on a laptop unless you intend to.
