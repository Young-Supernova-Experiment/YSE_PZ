"""Handler registry for the background job queue (issue #263).

A handler is a plain function taking the job's payload dict (and, as a
keyword argument, the ``Job`` row) and returning a JSON-serialisable result::

    from YSE_App.jobs import job

    @job("photstat.backfill")
    def backfill(payload, job=None):
        ...
        return {"updated": n}

Handlers register at import time. The runner imports the modules named in
``settings.JOB_HANDLER_MODULES`` (``autodiscover``) before its first pass so a
worker process knows every kind even when no web request has imported it.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
from typing import Callable, Dict, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Modules whose import registers the handlers the application itself ships.
DEFAULT_HANDLER_MODULES = ("YSE_App.services.notify", "YSE_App.brokers.jobs",
                           "YSE_App.services.external_services", "YSE_App.sharing.handlers",
                           "YSE_App.analysis.runners")

_registry: Dict[str, "Handler"] = {}
_lock = threading.Lock()
_discovered = False


class Handler:
    """A registered job handler with its per-kind defaults."""

    def __init__(self, kind: str, func: Callable, *, max_attempts: Optional[int] = None,
                 backoff_seconds: Optional[float] = None):
        self.kind = kind
        self.func = func
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        params = inspect.signature(func).parameters
        self.accepts_job = "job" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    def __call__(self, payload, job=None):
        if self.accepts_job:
            return self.func(payload, job=job)
        return self.func(payload)

    def __repr__(self):
        return "<Handler %s -> %s.%s>" % (self.kind, self.func.__module__, self.func.__qualname__)


def job(kind: str, *, max_attempts: Optional[int] = None, backoff_seconds: Optional[float] = None):
    """Decorator: register ``func`` as the handler for jobs of ``kind``.

    Re-registering the same kind with the same function (module reload in
    tests) is a no-op; a different function replaces the old one with a
    warning so a typo does not silently shadow another handler.
    """
    if not kind or not isinstance(kind, str):
        raise ValueError("job kind must be a non-empty string")

    def decorator(func):
        handler = Handler(kind, func, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        with _lock:
            existing = _registry.get(kind)
            if existing is not None and existing.func is not func and (
                existing.func.__module__, existing.func.__qualname__
            ) != (func.__module__, func.__qualname__):
                logger.warning("job handler %r replaced: %r -> %r", kind, existing, handler)
            _registry[kind] = handler
        func.job_kind = kind
        return func

    return decorator


def register(kind: str, func: Callable, **options) -> Callable:
    """Function form of :func:`job` for handlers defined elsewhere."""
    return job(kind, **options)(func)


def unregister(kind: str) -> None:
    with _lock:
        _registry.pop(kind, None)


def get_handler(kind: str) -> Optional[Handler]:
    handler = _registry.get(kind)
    if handler is None and not _discovered:
        autodiscover()
        handler = _registry.get(kind)
    return handler


def registered_kinds():
    return sorted(_registry)


def handler_modules():
    configured = getattr(settings, "JOB_HANDLER_MODULES", None)
    modules = list(DEFAULT_HANDLER_MODULES)
    for name in configured or ():
        if name and name not in modules:
            modules.append(name)
    return modules


def autodiscover(force: bool = False) -> None:
    """Import every module in ``JOB_HANDLER_MODULES`` so its handlers register."""
    global _discovered
    if _discovered and not force:
        return
    _discovered = True
    for name in handler_modules():
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a broken handler module must not stop the others
            logger.exception("job handler module %s failed to import", name)
