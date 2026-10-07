# Ziggy: repairing `django_migrations` vs. the live schema

## Symptom

The `Deploy Stack` workflow fails in `manage.py migrate` with

```
django.db.utils.OperationalError: (1054, "Unknown column 'YSE_App_hostfollowup.is_public' in 'field list'")
```

while `showmigrations` claims `YSE_App` 0005-0007 are applied. The rows were
recorded (most likely a `migrate --fake` or a restored dump), but the columns
those migrations add were never created. Django then ran 0008: its `AddField`
/ `CreateModel` DDL autocommitted in MySQL before the `RunPython` backfill hit
the missing column, so 0008 is half-applied and not recorded.

`manage.py repair_migration_state` compares the recorded state with the real
tables for 0005-0008 and fixes exactly the three safe cases:

| Situation | Action |
|-----------|--------|
| recorded as applied, none of its tables/columns exist | `UNRECORD`: delete the `django_migrations` row so `migrate` really runs it |
| recorded as applied, none of its columns exist, but its ManyToMany join tables (`YSE_App_log_groups`, `YSE_App_hostfollowup_groups`, `YSE_App_transientfollowup_groups`) do and every one of them is empty (`SELECT COUNT(*)` = 0) | `RESET`: `DROP` those tables, then delete the `django_migrations` row. A join table with rows keeps this a hand-off and the row count is printed |
| 0008 not recorded, `priority` columns and/or `YSE_App_transientfollowuprequest` exist, `phot_priority`/`spec_priority` still present | `DROP` those partial artifacts so 0008 can run from scratch |
| recorded but only some artifacts exist (other than the empty-join-table case above), or `phot_priority`/`spec_priority` already gone | `HANDOFF`: no change; prints the per-column state and exits non-zero for a human to finish by hand (`sqlmigrate`) |
| everything agrees | "nothing to do" |

It never touches data rows and never edits recorder rows for migrations it did
not inspect. The dry run lists every statement `--apply` would execute as
`will run: ...`; those lines are informational, do not paste them into a
`mysql` shell, let `--apply` run them.

On Ziggy the first dry run stopped with `HANDOFF` for 0005 and 0006: the
`is_public`/`requested_by_id` columns were missing while the empty
`*_groups` join tables existed. The command now recognises that state as
`RESET` and drops the tables and unrecords the rows itself, so the manual
`DROP TABLE` / `DELETE FROM django_migrations` step is no longer needed.

## Runbook (experimental stack; swap the path for test/production)

```bash
cd /data/yse_pz/YSE_PZ_experimental
git fetch origin && git reset --hard origin/experimental   # get the command

# 1. what Django thinks
venv/bin/python manage.py showmigrations YSE_App

# 2. dry run: prints per-column state, join-table row counts and the plan,
#    changes nothing ("will run:" lines are what --apply would execute)
venv/bin/python manage.py repair_migration_state

# 3. review the plan. If it says "Ambiguous state", stop and fix by hand.
#    (empty *_groups join tables left by a --fake are handled: RESET)

# 4. execute the plan (RESET drops + unrecords, then UNRECORD, then DROP;
#    recorder rows for UNRECORD in one transaction; DROPs autocommit)
venv/bin/python manage.py repair_migration_state --apply --yes

# 5. finish exactly what the deploy would do
venv/bin/python manage.py migrate --noinput --skip-checks
venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart apache2
```

Alternatively, after step 4, re-run the failed `Deploy Stack` workflow from the
Actions tab; it performs steps 5 on its own. Run the dry run once more
afterwards: it should report every migration `CONSISTENT` and "nothing to do".

`--database <alias>` targets another configured alias (default `default`).
