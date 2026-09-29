# Personal dashboard performance: saved queries, cache, indexes

Ryan Foley's personal dashboard on `yse_experimental` took 5 minutes to finish loading (Safari HAR,
2026-09-29 15:59 UTC): five of its seven saved-query sections ran their Explorer SQL for 216-299 s each,
concurrently, on a cold cache; two more came back empty because the SQL guard rejected them (#258).
This page records what was changed (issues #331, #332, #258, #233, #248), what each setting does, and the
commands David runs on Ziggy before and after promoting the work.

## Where the time went

| section | saved query | cold run in the HAR | cause |
|---|---|---|---|
| 472 | YSE Magnitude-Limited Sample (min mag < 18.6) | 299 s | one dependent `ORDER BY mag LIMIT 1` subquery per YSE-tagged transient |
| 480 | Fast & Young Target Search | 259 s | `GROUP BY t.id` MIN/MAX/COUNT over every photometry row |
| 468 | YSE Forced Phot Only | 266 s | text only on Ziggy; not in the repository |
| 469 | Auto Ignore | 216 s | text only on Ziggy; not in the repository |
| 470 | YSE Volume-Limited Sample (z < 0.06) | 255 s | no photometry; 175 ms synthetic. Most plausibly contention with the other four; confirm with a solo run |
| 481 | Interesting New Targets | 204 in 91 ms | rejected by the `startswith('select')` guard (#258) |
| 483 | New Transients Last Two Days | 204 in 168 ms | rejected by the guard (#258) |

Server wait was 99.9% of every slow request; transfer under 4 ms. The result cache
(`run_explorer_query_cached`) was a per-process `LocMemCache` with a one-hour TTL, so the first visitor each hour,
in each Apache process, and after every restart paid the full cold run (#233).

## What changed

1. **Result-identical SQL rewrites** of the four photometry saved queries (`YSE_App/queries/dashboard_saved_queries.py`),
   installed by `python manage.py rewrite_dashboard_queries` (dry run by default, `--apply` to write, JSON backup of the
   previous text, `--revert <backup.json>`). The command matches `explorer_query` rows by title and changes a row only
   when its text is exactly the original the rewrite was proven against; anything else is printed for review and left
   alone. Every rewrite, its measured plan and its revert steps: `docs/sql-rewrites-2026-09-29.md`.
2. **Guard fix (#258)**: `YSE_App.services.dashboard_queries.dashboard_sql_is_supported` accepts saved SQL that starts
   with whitespace, `--`/`#`/`/* */` comments or a `WITH` common-table expression, still refuses anything that is not a
   SELECT, and a rejected section now renders the reason (HTTP 200) instead of a blank box (204).
3. **Cache TTL and warmer (#233)**: `[site_settings] EXPLORER_QUERY_CACHE_SECONDS` (default 3600) is the TTL of a saved
   query's cached name list. The django_cron job `YSE_App.data_ingest.Dashboard_Cache_Warm.WarmDashboardQueries` re-runs
   every saved query attached to any user's dashboard with the statement cap disabled and refreshes the cache; it is
   registered in `CRON_CLASSES` and does nothing unless `DASHBOARD_CACHE_WARM_ENABLED` is true
   (`DASHBOARD_CACHE_WARM_MINUTES`, default 60, is its interval).
4. **Indexes (#248)**: migration `0010_dashboard_indexes` adds `Transient(name)`, `(disc_date)`, `(status, disc_date)`,
   `(modified_date)`, `(ra)`, `(dec)` and `TransientPhotData(obs_date)`, `(photometry, obs_date)`. It deliberately does
   **not** add `TransientPhotData(photometry, mag)`: that index cut Interesting New Targets from 5.2 s to 0.8 s on the
   synthetic data but made the rewritten Magnitude-Limited query 2.7x slower by luring the optimizer into an
   index-ordered aggregate. Decide it from the production EXPLAIN (below).
5. The code-side `RawSQL` fragments (recent magnitude sorters, days since discovery, rising-transient band annotations)
   were simplified with identical output; see the same log.

Measured on the synthetic MySQL 8.0.25 dataset (35k transients / 800k photometry rows), wall time of the statement,
today's indexes: Magnitude-Limited 3.6 s -> 0.7 s, Fast & Young 1.7 s -> 0.4 s, New Last Two Days 1.6 s -> 0.4 s,
Interesting New 3.2 s -> 2.7 s (55% of synthetic transients are status 1; production `New` is a small fraction, so
the gain there is larger). That dataset is 1/10-1/50 of production photometry and fits in RAM; on Ziggy the same
plan changes apply to minutes rather than seconds.

## Settings (settings.ini `[site_settings]`, all optional)

| key | default | meaning |
|---|---|---|
| `EXPLORER_QUERY_MAX_EXECUTION_MS` | 0 (#253, #261: off until the saved queries are rewritten; 20000 recommended afterwards) | MySQL `max_execution_time` for saved SQL run inside a web request; 0 disables. The warmer ignores it. |
| `EXPLORER_QUERY_CACHE_SECONDS` | 3600 | how long a saved query's cached result is reused (env `YSE_EXPLORER_QUERY_CACHE_SECONDS`) |
| `DASHBOARD_CACHE_WARM_ENABLED` | False | run the warmer cron (env `YSE_DASHBOARD_CACHE_WARM=1`) |
| `DASHBOARD_CACHE_WARM_MINUTES` | 60 | warmer interval; set it to the photometry ingest cadence |
| `DASHBOARD_QUERY_BACKUP_DIR` | `<repo>/backups/saved_queries` | where `rewrite_dashboard_queries --apply` writes its JSON backups |

Set `DASHBOARD_CACHE_WARM_MINUTES` at or below `EXPLORER_QUERY_CACHE_SECONDS / 60` so the cache never expires
between warms (e.g. 60 min and 7200 s), and run `manage.py runcrons` at least that often (it already runs the other
crons).

### Shared cache: `REDIS_URL` (speed plan item P14)

`YSE_PZ/settings.py` switches `CACHES['default']` from `LocMemCache` to `django_redis.cache.RedisCache` (the
`django-redis` package; Django 3.2 has no built-in Redis backend, see #338) when the `REDIS_URL` environment
variable is set. Without it every Apache/mod_wsgi process (and every `runcrons` process) has its own cache, so the
warmer warms only its own process and each web process still pays a cold run once per TTL and after every restart.
With it, one warm serves every process and survives restarts.

If `REDIS_URL` is set but `django-redis` is not installed in that stack's virtualenv, the site still starts: settings
logs `REDIS_URL is set but no Redis cache backend is importable ... using the per-process LocMemCache instead`
(Apache error log / cron output) and behaves as if `REDIS_URL` were unset.

On Ziggy, in this order:

1. Install the packages into the stack's virtualenv (`requirements.txt` pins them; the deploy workflow skips pip on the
   shared interpreters): `<venv>/bin/pip install django-redis==5.4.0 redis==5.0.8`.
2. Install Redis (`apt install redis-server`, bind to localhost).
3. Export `REDIS_URL=redis://127.0.0.1:6379/1` in the environment the WSGI application and the cron user see. For
   mod_wsgi that is `WSGIDaemonProcess ... ` plus a `SetEnv`/`os.environ` line in `YSE_PZ/wsgi.py`
   (`os.environ.setdefault('REDIS_URL', ...)`), or the `/etc/apache2/envvars` file; for the docker stack, an entry in
   the web container's environment. Use a different Redis database number per stack (`/1` experimental, `/2` test,
   ...) so they do not share cached results.
4. Check it took: `REDIS_URL=redis://127.0.0.1:6379/1 python manage.py shell -c "from django.conf import settings; print(settings.CACHES['default']['BACKEND'])"`
   should print `django_redis.cache.RedisCache`; `...locmem.LocMemCache` plus the warning above means step 1 is missing.

## Ziggy runbook (David)

The `yse_experimental` and `yse_test` stacks share the `YSE_test` database, so **the migration runs there
automatically on deploy** (`python manage.py migrate` in the deploy workflow). On a 10^7-row InnoDB table
`CREATE INDEX` is online (`ALGORITHM=INPLACE, LOCK=NONE`) but takes minutes of I/O; each index also adds write cost to
every photometry ingest. Before promoting to production run the checks below and adjust the index list if the plans
do not use an index.

Replace `$DB` and `$USER`.

```bash
# 1. sizes, engine, buffer pool: is transientphotdata in RAM at all?
mysql -u $USER -p $DB -e "SHOW TABLE STATUS WHERE Name IN ('YSE_App_transient','YSE_App_transientphotometry','YSE_App_transientphotdata','YSE_App_transientphotdata_data_quality','YSE_App_transient_tags','YSE_App_host','YSE_App_transientdiffimage')\G"
mysql -u $USER -p $DB -e "SELECT COUNT(*) FROM YSE_App_transientphotdata; SELECT COUNT(*) FROM YSE_App_transientphotdata WHERE obs_date >= NOW() - INTERVAL 2 DAY; SELECT COUNT(*) FROM YSE_App_transient; SELECT status_id, COUNT(*) FROM YSE_App_transient GROUP BY status_id;"
mysql -u $USER -p -e "SELECT @@version, @@innodb_buffer_pool_size/1024/1024/1024 AS bp_gb, @@max_execution_time; SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';"
mysql -u $USER -p $DB -e "SHOW INDEX FROM YSE_App_transientphotdata; SHOW INDEX FROM YSE_App_transient;"

# 2. the exact SQL of Ryan's seven queries (UserQuery ids 468 469 470 472 480 481 483)
mysql -u $USER -p $DB -e "SELECT uq.id AS section, q.id, q.title, q.sql FROM YSE_App_userquery uq JOIN explorer_query q ON q.id = uq.query_id WHERE uq.id IN (468,469,470,472,480,481,483)\G" > ryan_queries.txt

# 3. plans without executing (safe any time); MySQL 8.0.16+ has FORMAT=TREE
mysql -u $USER -p $DB -e "EXPLAIN FORMAT=TREE <paste one query>\G"
# 4. real timings: EXPLAIN ANALYZE executes the query. One at a time, off-peak, capped:
mysql -u $USER -p $DB -e "SET SESSION max_execution_time=600000; EXPLAIN ANALYZE <paste one query>\G"
#    (MySQL 5.7 / MariaDB: use EXPLAIN EXTENDED / ANALYZE FORMAT=JSON respectively)

# 5. slow query log for one dashboard load (then reload the page once, then turn it off)
mysql -u $USER -p -e "SET GLOBAL slow_query_log=ON; SET GLOBAL long_query_time=5; SET GLOBAL log_queries_not_using_indexes=OFF; SHOW VARIABLES LIKE 'slow_query_log_file';"
sudo mysqldumpslow -s t -t 10 /var/lib/mysql/<host>-slow.log     # or pt-query-digest
mysql -u $USER -p -e "SET GLOBAL slow_query_log=OFF;"

# 6. web tier: how many processes/threads share the LocMem cache and the DB
grep -r "WSGIDaemonProcess\|WSGIProcessGroup" /etc/apache2/sites-enabled/
grep -c personaldashboard/section /var/log/apache2/*access*.log   # add %D to LogFormat to get per-request µs
```

Item 2 also settles what 468/469 do and whether 481/483 failed the guard because of leading whitespace or comments.
Item 4 on each of the five slow queries, before and after the migration, is the go/no-go for the index list: an index
MySQL does not pick still costs write time on every ingest, so drop it from `Meta.indexes` (and a follow-up migration)
if the plans never use it. If `Innodb_buffer_pool_reads` is a large fraction of `Innodb_buffer_pool_read_requests`
and Ziggy has the RAM, raising `innodb_buffer_pool_size` (default 128 MB) so `transientphotdata` and its indexes
fit is the single cheapest change and helps every page.

Then, per stack:

```bash
# after deploy, in the web container / virtualenv of the stack
python manage.py rewrite_dashboard_queries --dry-run      # diffs; REVIEW lines show 468/469/470's text
python manage.py rewrite_dashboard_queries --apply        # writes; prints the JSON backup path
# undo if needed:
python manage.py rewrite_dashboard_queries --revert <backup.json> --apply
```

A saved query whose text differs from the fixture original is reported as `SKIP ... not the text this rewrite was
proven against` with a diff; paste it into an issue for a second pass rather than editing by hand.

Finally turn the warmer on in that stack's `settings.ini` (`DASHBOARD_CACHE_WARM_ENABLED: True`,
`EXPLORER_QUERY_CACHE_SECONDS: 7200`), install `django-redis` and set `REDIS_URL` (steps above), restart Apache, and
check the `CronJobLog` rows of
`YSE_App.Dashboard_Cache_Warm.WarmDashboardQueries` (or the `dashboard warm:` log lines) for per-query timings.

## Next step: a per-transient photometry summary table

Every one of these queries, and the dashboard's own `recent_mag` / `recent_magdate` annotations, rebuilds a per-transient
summary (latest point, brightest point, first/last detection, point count) from the raw photometry table. The
structural fix is a summary table maintained on upload, like SkyPortal's `PhotStat`: then each query becomes a join on
a 10^5-row table and the two recent-photometry subqueries per dashboard row disappear. That is tracked as a
SkyPortal-parity prerequisite in #270 and is not part of this change.
