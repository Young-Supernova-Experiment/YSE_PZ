# Branches and promotion order

Three long-lived branches, each deployed to its own stack on Ziggy by
`.github/workflows/deploy.yml`:

| Branch | Stack | How code gets there |
| --- | --- | --- |
| `experimental` | `/yse_experimental/` | Feature and fix branches open PRs here. CI (`lint`, `docker-test`) must pass; no review is required. |
| `develop` | `/yse_test/` | **Only from `experimental`**, by a PR `experimental -> develop`; the exceptions are Dependabot and `develop-direct/*` branches (below). No review is required. |
| `master` | production | **Only from `develop`**, by a PR `develop -> master`. |

Hotfixes are not merged straight into `develop` or `master`: land them on
`experimental` and promote.
If a hotfix does land on `develop` (for example while `develop` is being
reviewed before a release), backport it to `experimental` straight away with an
issue and a PR, so the next promotion does not revert it and the branches stay
in step. The full contributor workflow is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## develop-direct branches

A change that must reach `/yse_test/` without the rest of `experimental` may be
cut from `develop` on a branch named `develop-direct/<slug>` and opened as a PR
into `develop` (#631). Rules:

1. It must not depend on anything that exists only on `experimental`.
2. **Ryan merges it**; nobody else, and no automation.
3. Immediately afterwards, `develop` is merged back into `experimental` by a
   backport PR (merge `develop` into a branch from `experimental`; add a Django
   merge migration if the two branches' migration numbers diverged), so the next
   `experimental -> develop` promotion does not revert it.
4. A `develop-direct/*` branch may never target `master`.

## The promotion guard

`.github/workflows/promotion-guard.yml` runs on every pull request into `develop`
or `master` and fails, with a message naming the allowed source branch, unless
the PR is `experimental -> develop`, `dependabot/* -> develop`, `develop-direct/* -> develop`
or `develop -> master` from this repository (not a fork). Neither a `dependabot/*` nor a
`develop-direct/*` branch is ever allowed into `master`. It runs on `opened`, `synchronize`, `reopened` and `edited`, so
retargeting a PR re-evaluates it.

A failing workflow only blocks the merge when it is a *required status check*,
which is set in the repository settings, not in the repo. The owner has to do
this once:

1. Settings -> Branches -> branch protection rule for `develop`: enable
   "Require status checks to pass before merging" and add **`promotion guard`**
   (the job name). Leave "Require a pull request before merging" -> "Require
   approvals" **off** for `develop`.
2. The same for `master`: add **`promotion guard`** as a required status check
   (keep whatever review requirement `master` already has).
3. Optionally add `lint` and `docker-test` (from `CI`) as required checks on both.

Until the check is marked required, the guard is advisory: the job shows red on
the PR but the Merge button still works.

## Dependencies

`requirements.txt` is the web/app runtime (Apache/mod_wsgi and the django_cron
ingest jobs; it still pins TensorFlow 2.13.1 because the `Photo_Z` cron imports it,
although `astro-ghost`, which first pulled it in, was dropped in #613; removing it is
tracked in #600);
`requirements-ingest.txt` layers the machine-learning extras
nothing in the web venv imports (RAPID/TCN, TensorFlow-addons) on top of it for a
cron or analysis environment. Both install without `--no-deps`. Django is on the
4.2 LTS line (`Django==4.2.30`, #781), the newest that supports the stacks'
Python 3.8; Django 5.2 waits for the Python upgrade (#601, #591).

### Advisory checks and Dependabot

- `.github/workflows/pip-audit.yml` audits the exact pins of both files (Django as
  4.2.30) when they change and every Monday. It fails only on advisories missing from
  `.github/pip-audit-ignore.txt`, the accepted baseline with a reason per group (Django
  4.2 until the Python upgrade, pins held back by Python 3.8 / TensorFlow, older pins
  left for a requirements regeneration). When a package is upgraded past a fix, delete its lines; add a line
  only with a reason.
- `.github/dependabot.yml` opens weekly pip and GitHub Actions update PRs into
  `develop` (at most three open per ecosystem), skipping `experimental`. It never proposes
  Django 5+ or the other versions that the stack cannot take yet (listed with reasons in
  the file), and it excludes the archived `docker/web_image_history/**`.
  - Merge Dependabot PRs into `develop` once CI is green. Merging deploys to ziggy
    `/yse_test/`.
  - Right after a Dependabot merge into `develop`, merge `develop` back into
    `experimental` (same rule as a hotfix backport), so the next promotion does not
    revert the bump.
  - Dependabot reads its configuration only from the default branch (`master`), so the
    `develop` target takes effect once `.github/dependabot.yml` reaches `master`
    (it is on `develop`, not yet on `master`; the next `develop -> master` is #792).
  - GitHub "security updates" PRs always target the default branch (`master`), whatever
    `target-branch` says; handle them as a hotfix (land on `experimental`, promote).
