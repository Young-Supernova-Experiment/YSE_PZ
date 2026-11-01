#!/bin/bash
# YSE-PZ server watchdog (#769). Install and operate: docs/ziggy-runbook.md,
# "Server watchdog". Run as root every minute by yse-watchdog.timer.
#
# Each run checks three things:
#   db_direct   MySQL answers SELECT 1 (monitoring login in $MYCNF)
#   web_db      Django answers $DB_HEALTH_URL (/healthz/, #768) with 200
#   web_static  Apache serves $STATIC_URL (no Django, no DB) with 200
# All healthy: reset the failure counter. Otherwise, after $FAILS_NEEDED
# consecutive failures, and subject to a cooldown and an hourly cap, it
# snapshots diagnostics and then:
#   MySQL down         -> restart MySQL, then gracefully reload Apache
#   only Django stuck  -> optionally KILL long queries, then gracefully reload
#                         Apache (a full restart if static files fail too)
# It emails a summary after every action and when the cap stops it.
#
# Exit status: 0 healthy / waiting / acted and recovered; 1 acted but still
# unhealthy, an action failed, or the cap was reached; 2 bad configuration.
# Limitations: it cannot fix what a restart does not fix (full disk, bad
# config); it only sees what the three probes see; snapshots of a hung MySQL
# may be empty because every query is time-limited.
set -u

CONF=${YSE_WATCHDOG_CONF:-/etc/yse-watchdog/watchdog.conf}

# ---- defaults (override in $CONF) ------------------------------------------
DB_HEALTH_URL=""                 # e.g. https://ziggy.ucolick.org/yse/healthz/
STATIC_URL=""                    # a static file Apache serves without Django
MYCNF=/etc/yse-watchdog/my.cnf   # [client] user/password, mode 600, root-owned
STATE=/var/lib/yse-watchdog
FAILS_NEEDED=3                   # consecutive failed runs before acting
COOLDOWN=900                     # seconds between actions
MAX_PER_HOUR=3                   # actions per rolling hour
NOTIFY=""                        # email address(es); empty = no mail
APACHE_SERVICE=apache2
MYSQL_SERVICE=mysql
HTTP_TIMEOUT=20                  # seconds per HTTP probe
DB_TIMEOUT=15                    # seconds per MySQL probe or snapshot query
RESTART_TIMEOUT=180              # seconds for a service restart
POST_ACTION_WAIT=20              # seconds before the re-check
MYSQL_SETTLE=5                   # seconds after a MySQL restart before touching Apache
SNAPSHOT_DAYS=30                 # delete snapshots older than this
KILL_QUERY_SECONDS=0             # >0: KILL QUERY statements older than this
KILL_QUERY_USERS=""              # space-separated MySQL users eligible for KILL
DRY_RUN=0                        # 1: log and mail what would happen, change nothing

if [[ -r "$CONF" ]]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi

log() { logger -t yse-watchdog -- "$*"; echo "$(date '+%F %T') $*"; }

die_config() { log "configuration error: $*"; exit 2; }
[[ -n "$DB_HEALTH_URL" ]] || die_config "DB_HEALTH_URL is empty ($CONF)"
[[ -n "$STATIC_URL" ]] || die_config "STATIC_URL is empty ($CONF)"
for v in FAILS_NEEDED COOLDOWN MAX_PER_HOUR HTTP_TIMEOUT DB_TIMEOUT RESTART_TIMEOUT POST_ACTION_WAIT MYSQL_SETTLE SNAPSHOT_DAYS KILL_QUERY_SECONDS DRY_RUN; do
    [[ "${!v}" =~ ^[0-9]+$ ]] || die_config "$v must be a non-negative integer (got '${!v}')"
done

umask 077
mkdir -p "$STATE/snapshots" || die_config "cannot create $STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0   # a previous run is still going

mysql_q() { timeout "$DB_TIMEOUT" mysql --defaults-extra-file="$MYCNF" "$@"; }
db_ok() { [[ "$(mysql_q -N -B -e 'SELECT 1' 2>/dev/null)" == 1 ]]; }
http_code() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$HTTP_TIMEOUT" "$1" 2>/dev/null)
    echo "${code:-000}"
}
notify() {   # notify SUBJECT  (body on stdin)
    if [[ -n "$NOTIFY" ]]; then
        mail -s "$1" $NOTIFY || log "mail to $NOTIFY failed"
    else
        cat >/dev/null
    fi
}
run() {      # run CMD...  (honours DRY_RUN)
    if [[ "$DRY_RUN" == 1 ]]; then
        log "dry run: would run: $*"
        return 0
    fi
    "$@"
}
prune_snapshots() {
    find "$STATE/snapshots" -mindepth 1 -maxdepth 1 -type d -mtime +"$SNAPSHOT_DAYS" -exec rm -rf {} + 2>/dev/null
}

prune_snapshots

db_direct=fail; db_ok && db_direct=ok
web_db=$(http_code "$DB_HEALTH_URL")
web_static=$(http_code "$STATIC_URL")
status="db_direct=$db_direct web_db=$web_db web_static=$web_static"

if [[ "$db_direct" == ok && "$web_db" == 200 ]]; then
    if [[ -s "$STATE/fails" && "$(cat "$STATE/fails")" != 0 ]]; then
        log "healthy again: $status"
    fi
    echo 0 > "$STATE/fails"
    exit 0
fi

fails=$(( $(cat "$STATE/fails" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE/fails"
log "unhealthy: $status consecutive=$fails"
(( fails < FAILS_NEEDED )) && exit 0

now=$(date +%s)
last=$(cat "$STATE/last_action" 2>/dev/null || echo 0)
if (( now - last < COOLDOWN )); then
    log "in cooldown ($(( COOLDOWN - (now - last) ))s left); not acting"
    exit 0
fi
recent=$(awk -v t=$(( now - 3600 )) '$1 > t' "$STATE/history" 2>/dev/null | wc -l | tr -d ' ')
if (( recent >= MAX_PER_HOUR )); then
    log "restart cap reached ($recent in the last hour); not acting"
    last_cap=$(cat "$STATE/last_cap_mail" 2>/dev/null || echo 0)
    if (( now - last_cap >= COOLDOWN )); then
        echo "$now" > "$STATE/last_cap_mail"
        printf 'YSE-PZ is still unhealthy on %s: %s\n%s automatic actions in the last hour; the watchdog will not act again until the hourly cap clears.\nLatest snapshots: %s\n' \
            "$(hostname)" "$status" "$recent" "$STATE/snapshots" | notify "YSE-PZ watchdog: manual attention needed"
    fi
    exit 1
fi

# ---- diagnostic snapshot, before changing anything --------------------------
snap="$STATE/snapshots/$now"
mkdir -p "$snap"
echo "$status" > "$snap/status.txt"
mysql_q -e 'SHOW FULL PROCESSLIST' > "$snap/processlist.txt" 2>&1
mysql_q -e 'SHOW ENGINE INNODB STATUS\G' > "$snap/innodb.txt" 2>&1
mysql_q -e "SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running','Max_used_connections','Aborted_connects','Connection_errors_max_connections','Innodb_row_lock_current_waits')" > "$snap/mysql-status.txt" 2>&1
df -h > "$snap/df.txt" 2>&1
free -m > "$snap/free.txt" 2>&1
dmesg -T 2>/dev/null | tail -100 > "$snap/dmesg.txt"
journalctl -u "$MYSQL_SERVICE" -n 200 --no-pager > "$snap/mysql-journal.txt" 2>&1
journalctl -u "$APACHE_SERVICE" -n 100 --no-pager > "$snap/apache-journal.txt" 2>&1

actions=()
ok=1
if [[ "$db_direct" == fail ]]; then
    log "MySQL does not answer; restarting $MYSQL_SERVICE"
    actions+=("restart $MYSQL_SERVICE")
    if ! run timeout "$RESTART_TIMEOUT" systemctl restart "$MYSQL_SERVICE"; then
        log "restart of $MYSQL_SERVICE failed or timed out"
        echo "$now" > "$STATE/last_action"; echo "$now" >> "$STATE/history"
        printf 'systemctl restart %s failed or timed out on %s.\nBefore: %s\nSnapshot: %s\n' \
            "$MYSQL_SERVICE" "$(hostname)" "$status" "$snap" | notify "YSE-PZ watchdog: MySQL restart FAILED"
        exit 1
    fi
    [[ "$DRY_RUN" == 1 ]] || sleep "$MYSQL_SETTLE"
elif (( KILL_QUERY_SECONDS > 0 )) && [[ -n "$KILL_QUERY_USERS" ]]; then
    in_list=$(printf "'%s'," $KILL_QUERY_USERS); in_list=${in_list%,}
    ids=$(mysql_q -N -B -e "SELECT id FROM information_schema.PROCESSLIST WHERE command = 'Query' AND time > $KILL_QUERY_SECONDS AND user IN ($in_list)" 2>/dev/null | tr '\n' ' ')
    if [[ -n "${ids// /}" ]]; then
        log "killing queries running longer than ${KILL_QUERY_SECONDS}s: $ids"
        actions+=("KILL QUERY $ids")
        for id in $ids; do
            [[ "$id" =~ ^[0-9]+$ ]] && run mysql_q -e "KILL QUERY $id" >> "$snap/killed.txt" 2>&1
        done
    fi
fi

if [[ "$web_static" != 200 ]]; then
    log "Apache does not serve static files; restarting $APACHE_SERVICE"
    actions+=("restart $APACHE_SERVICE")
    run timeout "$RESTART_TIMEOUT" systemctl restart "$APACHE_SERVICE" || { log "restart of $APACHE_SERVICE failed"; ok=0; }
else
    log "reloading $APACHE_SERVICE gracefully"
    actions+=("reload $APACHE_SERVICE")
    run timeout "$RESTART_TIMEOUT" systemctl reload "$APACHE_SERVICE" || { log "reload of $APACHE_SERVICE failed"; ok=0; }
fi

echo "$now" > "$STATE/last_action"
echo "$now" >> "$STATE/history"
echo 0 > "$STATE/fails"

[[ "$DRY_RUN" == 1 ]] || sleep "$POST_ACTION_WAIT"
after_db=fail; db_ok && after_db=ok
after_web=$(http_code "$DB_HEALTH_URL")
after_static=$(http_code "$STATIC_URL")
after="db_direct=$after_db web_db=$after_web web_static=$after_static"
log "after: $after"
[[ "$after_db" == ok && "$after_web" == 200 ]] || ok=0

subject="YSE-PZ watchdog: $( ((ok)) && echo recovered || echo STILL UNHEALTHY ) after ${actions[*]}"
[[ "$DRY_RUN" == 1 ]] && subject="[dry run] $subject"
printf 'Host: %s\nBefore: %s\nActions: %s\nAfter:  %s\nSnapshot: %s\n' \
    "$(hostname)" "$status" "${actions[*]}" "$after" "$snap" | notify "$subject"
((ok)) && exit 0 || exit 1
