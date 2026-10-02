# Branches and promotion order

Three long-lived branches, each deployed to its own stack on Ziggy by
`.github/workflows/deploy.yml`:

| Branch | Stack | How code gets there |
| --- | --- | --- |
| `experimental` | `/yse_experimental/` | Feature and fix branches open PRs here. CI (`lint`, `docker-test`) must pass; no review is required. |
| `develop` | `/yse_test/` | **Only from `experimental`**, by a PR `experimental -> develop`. No review is required. |
| `master` | production | **Only from `develop`**, by a PR `develop -> master`. |

Hotfixes are not merged straight into `develop` or `master`: land them on
`experimental` and promote.
If a hotfix does land on `develop` (for example while `develop` is being
reviewed before a release), backport it to `experimental` straight away with an
issue and a PR, so the next promotion does not revert it and the branches stay
in step. The full contributor workflow is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## The promotion guard

`.github/workflows/promotion-guard.yml` runs on every pull request into `develop`
or `master` and fails, with a message naming the allowed source branch, unless
the PR is `experimental -> develop` or `develop -> master` from this repository
(not a fork). It runs on `opened`, `synchronize`, `reopened` and `edited`, so
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
ingest jobs; it still carries TensorFlow because `astro-ghost` declares it as a
hard dependency); `requirements-ingest.txt` layers the machine-learning extras
nothing in the web venv imports (RAPID/TCN, TensorFlow-addons) on top of it for a
cron or analysis environment. Both install without `--no-deps`. Django stays on
the 3.2 LTS line until the Django 4 upgrade (#133) is scheduled.
