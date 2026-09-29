# Background jobs and the notification queue

YSE-PZ has a database-backed job queue (issue #263) and, on top of it, a
notification queue with in-app, email and Slack-webhook delivery (issue #266,
part of #69). There is no broker: jobs are rows in the `YSE_App_job` table
(model `YSE_App.models.Job`), so the queue runs on the existing MySQL host,
under Apache + mod_wsgi, with no Redis or Celery. Work is executed by
`manage.py run_jobs`, either one pass at a time from the existing
`manage.py runcrons` crontab entry (the default, no new infrastructure) or as a
long-running worker under systemd.

## Concepts

| term | meaning |
|---|---|
| `Job` | one unit of deferred work: `kind` (handler name), `payload` (JSON), `status` (`queued`, `running`, `done`, `failed`, `cancelled`), `attempts` / `max_attempts`, `run_after`, `locked_at` / `locked_by`, `result` (JSON), `error` (traceback), optional `transient` and `created_by`, timestamps |
| handler | a Python function registered with `@job("kind")`; receives the payload dict (and `job=` the row when it accepts that keyword) and returns a JSON-serialisable result |
| pass | `run_jobs` claims due jobs one at a time until none are left, `--limit` is reached or the time budget is spent |
| claim | atomic: `SELECT ... FOR UPDATE SKIP LOCKED` on MySQL 8, then a conditional `UPDATE ... WHERE status='queued'` everywhere (the row count decides who won), so several runners can share a queue |
| retry | a handler that raises is retried after `JOB_RUNNER_BACKOFF_SECONDS * 2**(attempt-1)` (capped) until `max_attempts`; `JobRetry(delay=...)` picks the delay, `JobFailed` fails at once |
| stale reap | a `running` job whose `locked_at` is older than `JOB_RUNNER_STALE_MINUTES` (worker died) is put back on the queue at the start of every pass |

## Writing and enqueueing a job

```python
from YSE_App.jobs import enqueue, job, JobRetry, JobFailed

@job("photstat.backfill", max_attempts=5, backoff_seconds=120)
def backfill(payload, job=None):
    n = do_work(payload["transient_ids"])
    return {"updated": n}          # stored in Job.result

enqueue("photstat.backfill", {"transient_ids": [1, 2, 3]}, created_by=request.user)
enqueue("photstat.backfill", {...}, delay=300)          # run_after = now + 5 min
```

`YSE_App.services.job_queue` re-exports the same API (`from YSE_App.services.job_queue import enqueue`) for code in the services layer.

Handlers register at import time. The runner imports
`YSE_App.services.notify` itself and every module listed in
`JOB_HANDLER_MODULES`; a web process only needs to import the module that
defines the handler before calling `enqueue()` (a `kind` with no handler is
not rejected at enqueue time; the runner marks it `failed` with
"no handler registered", no retry).

With `JOB_RUNNER_INLINE = True` (settings.ini, or env `YSE_JOB_RUNNER_INLINE=1`)
`enqueue()` runs the job synchronously in the calling process: development
and tests. The default test settings leave it off so tests exercise the real
claim/run path (`run_pass()`).

## Running the queue

### 1. From the existing crontab (default)

`YSE_App.data_ingest.Job_Queue.RunQueuedJobs` is in `CRON_CLASSES`. Every
`manage.py runcrons` run (already in Ziggy's crontab) executes one pass with a
`JOB_RUNNER_CRON_BUDGET_SECONDS` (50 s) budget, so a long job cannot hold the
other crons. Nothing to install. If `runcrons` does not run every minute
today, add a dedicated line so notifications go out promptly:

```cron
* * * * *  cd /data/yse_pz/YSE_PZ && /path/to/venv/bin/python manage.py runcrons YSE_App.data_ingest.Job_Queue.RunQueuedJobs >> /var/log/yse_pz/run_jobs.log 2>&1
```

or run the command directly (no django_cron log row):

```cron
* * * * *  cd /data/yse_pz/YSE_PZ && /path/to/venv/bin/python manage.py run_jobs --budget 50 >> /var/log/yse_pz/run_jobs.log 2>&1
```

### 2. As a systemd worker (lower latency, long jobs)

`/etc/systemd/system/yse-pz-jobs.service`:

```ini
[Unit]
Description=YSE-PZ background job runner
After=network.target mysql.service

[Service]
Type=simple
User=yse
WorkingDirectory=/data/yse_pz/YSE_PZ
Environment=DJANGO_SETTINGS_MODULE=YSE_PZ.settings
ExecStart=/path/to/venv/bin/python manage.py run_jobs --loop --sleep 5
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now yse-pz-jobs.service
sudo systemctl status yse-pz-jobs.service
journalctl -u yse-pz-jobs.service -f
```

The loop stops cleanly on SIGTERM after the current job. When a worker owns
the queue, set `JOB_RUNNER_CRON_ENABLED: False` in settings.ini (or env
`YSE_JOB_RUNNER_CRON=0`) so `runcrons` skips its pass; leaving both on is
safe (claims are atomic), just redundant. For the experimental and test
stacks run one runner per stack: each has its own database and its own queue.

### Command reference

```
manage.py run_jobs                 # one pass, print a summary (exit 0)
manage.py run_jobs --loop --sleep 5
manage.py run_jobs --limit 10 --budget 30 --kind notifications.deliver
manage.py run_jobs --status        # counts per status + registered handler kinds, runs nothing
manage.py run_jobs --worker-id ziggy-A
```

## Operating

- **Status page** (staff): `/jobs/` shows counts per status, per kind and the
  50 most recent jobs; `/jobs/status.json` is the same for monitoring.
- **Admin**: `/admin/YSE_App/job/` lists jobs with filters on status and
  kind; actions "Retry selected failed/cancelled jobs" (resets attempts,
  clears the error) and "Cancel selected queued jobs". Failed jobs keep the
  full traceback in `error`.
- **Logs**: the runner logs one line per job at INFO, retries at WARNING,
  final failures at ERROR, through the `YSE_App.jobs.runner` logger.
- **Housekeeping**: finished rows are kept; delete old `done` rows from the
  admin or with `Job.objects.filter(status="done", finished_at__lt=...).delete()`
  once the table grows (a retention cron can follow).

## Settings (`settings.ini` `[site_settings]`, all optional)

| key | default | meaning |
|---|---|---|
| `JOB_RUNNER_CRON_ENABLED` | True | run one pass from `manage.py runcrons` (env `YSE_JOB_RUNNER_CRON=0` disables) |
| `JOB_RUNNER_CRON_MINUTES` | 1 | django_cron interval of that pass |
| `JOB_RUNNER_CRON_BUDGET_SECONDS` | 50 | wall-clock budget of one cron pass |
| `JOB_RUNNER_MAX_ATTEMPTS` | 3 | default `max_attempts` when neither the handler nor `enqueue()` sets one |
| `JOB_RUNNER_BACKOFF_SECONDS` | 60 | base retry delay (doubles per attempt) |
| `JOB_RUNNER_BACKOFF_MAX_SECONDS` | 3600 | cap on the retry delay |
| `JOB_RUNNER_STALE_MINUTES` | 60 | `running` jobs locked longer than this are requeued (0 disables) |
| `JOB_RUNNER_PASS_LIMIT` | 100 | max jobs per pass unless `--limit` is given |
| `JOB_RUNNER_INLINE` | False | run jobs synchronously inside `enqueue()` (env `YSE_JOB_RUNNER_INLINE=1`) |
| `JOB_HANDLER_MODULES` | (empty) | comma-separated extra modules to import for `@job` handlers |

## Notifications

```python
from YSE_App.services.notify import notify

notify([user_a, user_b], "SN 2026abc was classified as SN Ia",
       url="/transient_detail/2026abc/", kind="followup_status",
       subject="2026abc classified", transient=transient)
```

`notify()` writes one `Notification` row per recipient (the in-app list at
`/notifications/` and the navbar bell badge read those rows) and enqueues one
`notifications.deliver` job per notification that also needs email or a
Slack webhook, so a web request never waits on SMTP. The handler records each
channel's outcome in `Notification.delivered` (`{"email": {"sent_at": ...}}`
or `{"error": ..., "attempts": n}`), skips channels already sent on a retry,
and raises `JobRetry` while any channel failed.

Per-user switches live in `NotificationPreference` (`/notifications/preferences/`,
also in the user menu): `in_app` (default on), `email` (default on; needs an
account email and the site switch below), `slack_webhook_url` (optional; each
notification is posted there as text). A user with `in_app` off but email on
still gets a row, pre-marked read, so it never shows as unread.

Endpoints: `/notifications/` (`?unread=1`, paginated), `/notifications/unread_count.json`
(the badge), `POST /notifications/<id>/read/` (`follow=1` redirects to the
notification's URL), `POST /notifications/read_all/`, `/notifications/preferences/`.
Admin: `/admin/YSE_App/notification/` (delivery column) and
`/admin/YSE_App/notificationpreference/`.

| key | default | meaning |
|---|---|---|
| `NOTIFICATION_EMAIL_ENABLED` | False | send notification emails through Django's mail backend (env `YSE_NOTIFICATION_EMAIL=1`). Off until `[SMTP_provider]` holds real credentials |
| `NOTIFICATION_SLACK_ENABLED` | True | honour per-user Slack webhook URLs |
| `NOTIFICATION_SLACK_TIMEOUT_SECONDS` | 10 | webhook POST timeout |
| `NOTIFICATION_EMAIL_SUBJECT_PREFIX` | `[YSE-PZ]` | prefix of every notification email subject |
| `NOTIFICATION_BASE_URL` | `YSE_PUBLIC_BASE_URL` | absolute prefix for links in emails and Slack posts (env `YSE_NOTIFICATION_BASE_URL`) |
| `NOTIFICATION_LIST_PAGE_SIZE` | 50 | rows per page on `/notifications/` |

Django's mail settings (`EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`,
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL`)
are now filled from the existing `[SMTP_provider]` block (`SMTP_HOST`,
`SMTP_PORT`, `SMTP_LOGIN`, `SMTP_PASSWORD`; optional `FROM_ADDRESS`,
`SMTP_USE_TLS`, `EMAIL_BACKEND`, `SMTP_TIMEOUT_SECONDS`), the same account
`YSE_App/common/alert.py` uses. A `<...>` placeholder counts as unset.

The existing senders (`alert.py` transient alerts, comment-mention emails in
`YSE_App/services/notifications.py`, follow-up notices) are unchanged in this
step; moving them onto `notify()` is the next part of #266 / #320.

## Deploy checklist (Ziggy)

```sh
cd /data/yse_pz/YSE_PZ            # the stack's checkout
git pull
python manage.py migrate YSE_App  # 0012_job_queue_notifications: YSE_App_job, YSE_App_notification, YSE_App_notificationpreference
python manage.py check
python manage.py run_jobs --status
```

Then either rely on the existing `runcrons` crontab line (nothing else to do,
the `RunQueuedJobs` cron is on by default), add the per-minute crontab line
above, or install the systemd unit and set `JOB_RUNNER_CRON_ENABLED: False`.
To turn on email delivery once `[SMTP_provider]` is real:
`NOTIFICATION_EMAIL_ENABLED: True` in settings.ini and reload Apache.

Smoke test after deploy (Django shell):

```python
from django.contrib.auth.models import User
from YSE_App.services.notify import notify
notify([User.objects.get(username="djones")], "Job queue is live", "/jobs/", "system")
```

The bell in the navbar shows 1; `/jobs/` shows the `notifications.deliver`
job (only when email or a Slack webhook is enabled for that user).
