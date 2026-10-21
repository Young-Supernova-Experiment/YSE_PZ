"""Background job queue for YSE-PZ (issue #263).

Public API::

    from YSE_App.jobs import enqueue, job, JobRetry, JobFailed

    @job("example.echo")
    def echo(payload, job=None):
        return payload

    enqueue("example.echo", {"hello": "world"})

Jobs are rows in ``YSE_App_job`` (``YSE_App.models.Job``). ``manage.py
run_jobs`` (one pass, or ``--loop``) and the ``RunQueuedJobs`` django_cron
class run them; see docs/background-jobs.md.
"""

from YSE_App.jobs.registry import (  # noqa: F401
    autodiscover,
    get_handler,
    job,
    register,
    registered_kinds,
    unregister,
)
from YSE_App.jobs.runner import (  # noqa: F401
    JobFailed,
    JobRetry,
    claim_job,
    enqueue,
    execute,
    queue_counts,
    reap_stale,
    run_forever,
    run_pass,
)

__all__ = [
    "JobFailed", "JobRetry", "autodiscover", "claim_job", "enqueue", "execute",
    "get_handler", "job", "queue_counts", "reap_stale", "register", "registered_kinds",
    "run_forever", "run_pass", "unregister",
]
