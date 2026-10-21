"""
Measure every SQL rewrite in the repo (docs/sql-rewrites-2026-09-29.md, issue #332) against a MySQL
database: wall time and EXPLAIN ANALYZE plan of old vs new, once with today's index set and once with
the indexes migration 0010_dashboard_indexes adds, plus a sorted-row and ordered-row diff proving
identical output. Run from the repository root against a synthetic or copied database (never
production: it CREATEs and DROPs the 0010 indexes on the target schema):

  YSE_SWEEP_DSN="host=127.0.0.1 port=3307 user=root password=root db=YSE" \
      python docs/perf/sql_sweep_measure.py docs/perf/sql_sweep_plans.md

The synthetic dataset used for the numbers in the docs came from docs/perf's P8 seeding (35k
transients, 40k photometry rows, 800k points, 28k YSE-tagged, hosts for every transient).
"""
import json
import os
import sys
import time

import pymysql

sys.path.insert(0, os.getcwd())
from YSE_App.queries import dashboard_saved_queries as d  # noqa: E402
from YSE_App.queries import raw_sql as r  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else 'sql_sweep_plans.md'
DSN = dict(kv.split('=', 1) for kv in os.environ.get(
    'YSE_SWEEP_DSN', 'host=127.0.0.1 port=3307 user=root password=root db=YSE').split())
FINAL_INDEXES = [
    ('YSE_App_transient', 'yse_transient_name_idx', '(name)'),
    ('YSE_App_transient', 'yse_transient_disc_date_idx', '(disc_date)'),
    ('YSE_App_transient', 'yse_transient_status_disc_idx', '(status_id, disc_date)'),
    ('YSE_App_transient', 'yse_transient_mod_date_idx', '(modified_date)'),
    ('YSE_App_transient', 'yse_transient_ra_idx', '(ra)'),
    ('YSE_App_transient', 'yse_transient_dec_idx', '(`dec`)'),
    ('YSE_App_transientphotdata', 'yse_photdata_obs_date_idx', '(obs_date)'),
    ('YSE_App_transientphotdata', 'yse_photdata_phot_obs_idx', '(photometry_id, obs_date)'),
]
STALE = ['yse_transient_modified_date_idx', 'yse_photdata_phot_mag_idx']  # from earlier experiments
c = pymysql.connect(host=DSN['host'], port=int(DSN['port']), user=DSN['user'], password=DSN['password'], database=DSN['db'], autocommit=True, charset='utf8mb4')
def q(sql, args=None):
    with c.cursor() as cur:
        cur.execute(sql, args); return cur.fetchall()
def idx_exists(table, name):
    return bool(q("SELECT 1 FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s AND index_name=%s LIMIT 1", (DSN['db'], table, name)))
def set_indexes(create):
    for table in ('YSE_App_transient', 'YSE_App_transientphotdata'):
        for name in STALE:
            if idx_exists(table, name): q(f'DROP INDEX {name} ON {table}')
    for table, name, cols in FINAL_INDEXES:
        ex = idx_exists(table, name)
        if create and not ex: q(f'CREATE INDEX {name} ON {table} {cols}')
        if not create and ex: q(f'DROP INDEX {name} ON {table}')
    q('ANALYZE TABLE YSE_App_transient, YSE_App_transientphotdata, YSE_App_transientphotometry, YSE_App_transient_tags, YSE_App_host, YSE_App_photometricband, YSE_App_instrument')

def timed(sql, reps=2):
    """min wall ms over reps runs of the statement itself (cache warm), plus the EXPLAIN ANALYZE plan."""
    best = None; rows = None
    for _ in range(reps):
        t0 = time.perf_counter()
        with c.cursor() as cur:
            cur.execute('SET SESSION max_execution_time=300000')
            cur.execute(sql); rows = cur.fetchall()
        ms = (time.perf_counter() - t0) * 1000
        best = ms if best is None else min(best, ms)
    with c.cursor() as cur:
        cur.execute('EXPLAIN ANALYZE ' + sql); plan = '\n'.join(x[0] for x in cur.fetchall())
    return best, rows, plan

def norm(rows):
    out = []
    for row in rows:
        out.append(tuple(round(v, 6) if isinstance(v, float) else v for v in row))
    return out

ids = [x[0] for x in q('SELECT id FROM YSE_App_transient WHERE id % 17 = 0 ORDER BY id LIMIT 2000')]
ids_sql = ','.join(map(str, ids))
rising_ids_sql = ','.join(map(str, ids[:400]))
def wrap(frag):
    return f"SELECT YSE_App_transient.id, ({frag.strip()}) AS v FROM YSE_App_transient WHERE YSE_App_transient.id IN ({ids_sql}) ORDER BY YSE_App_transient.id"
def wrap_rising(tmpl):
    r_filters = "('r-ZTF', 'r', 'rp', 'r-Sloan')"; g_filters = "('g-ZTF', 'g', 'V', 'gp', 'g-Sloan')"
    cols = [('pd.mag', r_filters, 0), ('pd.mag_err', r_filters, 0), ('UNIX_TIMESTAMP(pd.obs_date)', r_filters, 0),
            ('pd.mag', r_filters, 1), ('UNIX_TIMESTAMP(pd.obs_date)', r_filters, 1),
            ('-2.5*LOG10(pd.flux+3*pd.flux_err)+27.5 as lim', r_filters, 1), ('pd.flux', r_filters, 1), ('pd.flux_err', r_filters, 1),
            ('pd.mag', g_filters, 0), ('pd.mag', g_filters, 1)]
    sel = ', '.join('(%s) AS a%d' % ((tmpl % col).strip(), i) for i, col in enumerate(cols))
    return f"SELECT YSE_App_transient.id, {sel} FROM YSE_App_transient WHERE YSE_App_transient.id IN ({rising_ids_sql}) ORDER BY YSE_App_transient.id"

PAIRS = [
    ('saved 62 YSE Magnitude-Limited Sample (min mag < 18.6)', d.MAG_LIMITED_ORIGINAL_M2M, d.MAG_LIMITED_REWRITE_M2M, 'set'),
    ('saved 254 Fast & Young Target Search', d.FAST_YOUNG_ORIGINAL, d.FAST_YOUNG_REWRITE, 'ordered'),
    ('saved 217 New Transients Last Two Days', d.NEW_TWO_DAYS_ORIGINAL, d.NEW_TWO_DAYS_REWRITE, 'ordered'),
    ('saved 11 Interesting New Targets', d.INTERESTING_ORIGINAL.rstrip(';'), d.INTERESTING_REWRITE.rstrip(';'), 'ordered'),
    ('code recent_mag annotation (table_utils/yse_views/views), 2000 rows', wrap(r.RECENT_MAG_SQL_LEGACY), wrap(r.RECENT_MAG_SQL), 'ordered'),
    ('code days_since_disc annotation (yse_views), 2000 rows', wrap(r.DAYS_SINCE_DISC_SQL_LEGACY), wrap(r.DAYS_SINCE_DISC_SQL), 'ordered'),
    ('code rising-transient last_mag_query x10 annotations (yse_python_queries), 400 rows', wrap_rising(r.LAST_MAG_BY_BAND_SQL_LEGACY), wrap_rising(r.LAST_MAG_BY_BAND_SQL), 'ordered'),
]
results = {}
for phase, create in (('before', False), ('after', True)):
    set_indexes(create); print('indexes', phase, flush=True)
    for label, old, new, mode in PAIRS:
        ms_old, rows_old, plan_old = timed(old)
        ms_new, rows_new, plan_new = timed(new)
        same_set = sorted(norm(rows_old)) == sorted(norm(rows_new))
        same_order = norm(rows_old) == norm(rows_new)
        results.setdefault(label, {})[phase] = dict(ms_old=ms_old, ms_new=ms_new, n=len(rows_old), n_new=len(rows_new), same_set=same_set, same_order=same_order, plan_old=plan_old, plan_new=plan_new, mode=mode)
        print('  %-80s old %8.1f ms  new %8.1f ms  rows %d/%d  set=%s order=%s' % (label[:80], ms_old, ms_new, len(rows_old), len(rows_new), same_set, same_order), flush=True)
with open(OUT, 'w') as f:
    f.write('# SQL sweep measurements (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows, 128 MB buffer pool)\n\n')
    f.write('before = PK + FK indexes only (production today); after = the eight indexes in migration 0010. Wall time = best of 2 warm runs of the statement itself.\n\n')
    f.write('| query | before old ms | before new ms | after old ms | after new ms | rows | identical row set | identical order |\n|---|---|---|---|---|---|---|---|\n')
    for label, *_ in PAIRS:
        b = results[label]['before']; a = results[label]['after']
        f.write('| %s | %.0f | %.0f | %.0f | %.0f | %d | %s | %s |\n' % (label, b['ms_old'], b['ms_new'], a['ms_old'], a['ms_new'], b['n'], b['same_set'] and a['same_set'], b['same_order'] and a['same_order']))
    for label, old, new, mode in PAIRS:
        f.write('\n## %s\n' % label)
        for phase in ('before', 'after'):
            x = results[label][phase]
            f.write('\n### %s: old plan (%.0f ms)\n```\n%s\n```\n### %s: new plan (%.0f ms)\n```\n%s\n```\n' % (phase, x['ms_old'], x['plan_old'][:5000], phase, x['ms_new'], x['plan_new'][:5000]))
json.dump({k: {p: {kk: vv for kk, vv in v.items() if not kk.startswith('plan')} for p, v in ph.items()} for k, ph in results.items()}, open(OUT.replace('.md', '.json'), 'w'), indent=1)
print('wrote', OUT)
