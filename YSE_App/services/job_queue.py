"""Service-layer alias for the background job queue (issue #263).

Other services import ``enqueue`` from here so they depend on one stable
path, ``YSE_App.services.job_queue.enqueue``, rather than on the package
layout of ``YSE_App.jobs``::

    from YSE_App.services.job_queue import enqueue
    enqueue("credentials.rotate", {"resource_id": 7})

Everything is re-exported from :mod:`YSE_App.jobs`.
"""

from YSE_App.jobs import (  # noqa: F401
    JobFailed,
    JobRetry,
    autodiscover,
    claim_job,
    enqueue,
    execute,
    get_handler,
    job,
    queue_counts,
    reap_stale,
    register,
    registered_kinds,
    run_forever,
    run_pass,
    unregister,
)

__all__ = [
    "JobFailed", "JobRetry", "autodiscover", "claim_job", "enqueue", "execute",
    "get_handler", "job", "queue_counts", "reap_stale", "register", "registered_kinds",
    "run_forever", "run_pass", "unregister",
]
