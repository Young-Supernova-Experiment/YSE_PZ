"""Enrichment framework: one registry, one run log, one sweep, one command (#439).

An *enrichment service* fills in data about a transient or its host from somewhere else
(TNS, NED, DESI, Prost, SDSS photo-z, PS1 cutouts, Gaia alerts, Blast). Each is described by
an :class:`EnrichmentService` in the registry (:func:`register`; the built-in ones are in
:mod:`YSE_App.services.enrichment_services`) with:

* ``candidates(missing_only)``: the objects it could run on (``missing_only``: only those that
  lack its data);
* ``run_one(obj, ctx)`` (per object) or ``run_batch(objs, ctx)`` (one remote query for many):
  returns :class:`Outcome` objects;
* a cadence and retry policy (``recheck_days``, ``retry_hours``, ``max_attempts``) and a rate
  limit (``rate_limit_seconds`` between per-object calls).

Every attempt is an ``ExternalServiceRun`` of the service's ``ExternalService`` (``slug``),
for the transient (``transient``) or the host (generic target). A check that changes nothing,
or that fails again, updates the object's previous run of the same status instead of adding a
row; ``attempts`` on a failed run is then the number of failures in a row. Services that
already record their own runs (``tns``, ``ned-redshift``, ``desi-redshift``, ``ztf_forced``)
set ``records_itself`` and the framework only reads their runs.

A value a service changes is recorded by the service in the change log
(``services.change_log``, #737); the framework links those entries to the object's run.

:func:`select` combines the candidates with the run log: an object is *due* when it was
never run, its last run failed longer than ``retry_hours`` ago (and fewer than
``max_attempts`` times in a row), or its last success is older than ``recheck_days``. That
is what makes the sweep retry failures and pick up objects missed while a cron was down, and
what makes ``manage.py enrich`` resumable.

* :func:`run` -- run one service over a list of objects (dry run lists only).
* :func:`sweep` -- the ``enrich.sweep`` job (queued by ``Enrichment.EnrichmentSweep``), for the
  services named in ``ENRICH_SWEEP_SERVICES``; off by default.
* :func:`queue_objects` -- the ``enrich.objects`` job for explicit objects (``--queue``, Prost
  after a TNS gap import).
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Case, IntegerField, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from YSE_App.jobs import JobRetry, enqueue, job
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.services import change_log

log = logging.getLogger(__name__)

SWEEP_KIND = "enrich.sweep"
OBJECTS_KIND = "enrich.objects"
TARGET_TRANSIENT = "transient"
TARGET_HOST = "host"

# Outcome actions. ``error`` is recorded as a failed run, the others as succeeded.
UPDATED, UNCHANGED, NO_MATCH, SKIPPED, ERROR = "updated", "unchanged", "no_match", "skipped", "error"


class EnrichmentError(Exception):
    """A service cannot run at all (not configured, unknown name, refused upload)."""


class RateLimited(Exception):
    """The remote service asks us to slow down: stop this pass, retry after ``retry_after`` seconds."""

    def __init__(self, message="rate limited", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


# --- outcome -----------------------------------------------------------------------

@dataclass
class Outcome:
    action: str = UNCHANGED
    summary: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.action == UPDATED

    @property
    def failed(self) -> bool:
        return self.action == ERROR

    def as_dict(self) -> dict:
        out = asdict(self)
        out["changed"] = self.changed
        return out


# --- the registry -----------------------------------------------------------------

@dataclass
class EnrichmentService:
    name: str                       # registry key: ``manage.py enrich --service <name>``
    slug: str                       # ExternalService.slug whose runs are the log
    title: str
    description: str
    target: str                     # TARGET_TRANSIENT | TARGET_HOST
    candidates: Callable            # candidates(missing_only: bool) -> QuerySet
    run_one: Optional[Callable] = None    # run_one(obj, ctx) -> Outcome
    run_batch: Optional[Callable] = None  # run_batch(objs, ctx) -> {obj.pk: Outcome}
    make_context: Optional[Callable] = None  # make_context() -> dict (clients, caches, user)
    records_itself: bool = False
    recheck_days: Optional[float] = None  # re-run a success after this long (None: never)
    retry_hours: float = 24.0             # re-run a failure after this long
    max_attempts: int = 0                 # give up after this many failures in a row (0: never)
    rate_limit_seconds: float = 0.0       # between per-object calls
    enabled_setting: str = ""             # a settings flag that must be true for it to run at all
    needs_selection: bool = False         # refuse to run over every candidate without a selection
    kind: str = ExternalService.KIND_GENERIC

    def is_enabled(self) -> bool:
        return not self.enabled_setting or bool(_setting(self.enabled_setting, False))

    def setting_value(self, value):
        return value() if callable(value) else value

    @property
    def model(self):
        if self.target == TARGET_HOST:
            from YSE_App.models.host_models import Host

            return Host
        from YSE_App.models.transient_models import Transient

        return Transient


_REGISTRY: Dict[str, EnrichmentService] = {}
_loaded = False


def register(service: EnrichmentService) -> EnrichmentService:
    if service.target not in (TARGET_TRANSIENT, TARGET_HOST):
        raise ValueError("target must be %r or %r" % (TARGET_TRANSIENT, TARGET_HOST))
    if (service.run_one is None) == (service.run_batch is None):
        raise ValueError("%s: give exactly one of run_one and run_batch" % service.name)
    _REGISTRY[service.name] = service
    return service


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def _load_builtins():
    global _loaded
    if not _loaded:
        _loaded = True
        from YSE_App.services import enrichment_services  # noqa: F401 - registers the built-ins


def names() -> List[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def get(name: str) -> EnrichmentService:
    _load_builtins()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise EnrichmentError("unknown enrichment service %r (known: %s)" % (name, ", ".join(sorted(_REGISTRY))))


# --- who and the ExternalService row ---------------------------------------------------

def service_user():
    """The cron/service account (``[main] dblogin``, else the sharing system user)."""
    from YSE_App.services.tns_resync import ResyncError, service_user as _user

    try:
        return _user()
    except ResyncError as exc:
        raise EnrichmentError(str(exc)) from exc


def external_service(service: EnrichmentService, user=None, *, create: bool = True) -> Optional[ExternalService]:
    row = ExternalService.objects.filter(slug=service.slug).first()
    if row is None and create:
        user = user or service_user()
        row, _ = ExternalService.objects.get_or_create(
            slug=service.slug,
            defaults={"name": service.title, "kind": service.kind,
                      "description": service.description + " One run per object check (#439); no runner, "
                                                           "nothing to start by hand.",
                      "created_by": user, "modified_by": user},
        )
    return row


# --- the run log ------------------------------------------------------------------------

def _runs_for(service: EnrichmentService):
    return ExternalServiceRun.objects.filter(service__slug=service.slug)


def _target_filter(service: EnrichmentService, ref):
    """Filter kwargs selecting ``service``'s runs about ``ref`` (an OuterRef or a pk)."""
    if service.target == TARGET_TRANSIENT:
        return {"transient_id": ref}
    ct = ContentType.objects.get_for_model(service.model)
    return {"target_content_type": ct, "target_object_id": ref}


def with_last_run(service: EnrichmentService, qs):
    """Annotate ``enrich_last_at`` / ``enrich_last_status`` / ``enrich_last_attempts`` (None when never run)."""
    latest = (_runs_for(service).filter(**_target_filter(service, OuterRef("pk")))
              .exclude(status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING))
              .order_by("-finished_at", "-pk"))
    return qs.annotate(
        enrich_last_at=Subquery(latest.values("finished_at")[:1]),
        enrich_last_status=Subquery(latest.values("status")[:1]),
        enrich_last_attempts=Subquery(latest.values("attempts")[:1]),
    )


def due_q(service: EnrichmentService, now=None) -> Q:
    """Q on the :func:`with_last_run` annotations: never run, a failure due a retry, a stale success."""
    now = now or timezone.now()
    failed = Q(enrich_last_status__in=(ExternalServiceRun.STATUS_FAILED, ExternalServiceRun.STATUS_CANCELLED))
    retry_hours = float(service.setting_value(service.retry_hours) or 0)
    q_failed = failed & Q(enrich_last_at__lte=now - datetime.timedelta(hours=retry_hours))
    max_attempts = int(service.setting_value(service.max_attempts) or 0)
    if max_attempts > 0:
        q_failed &= Q(enrich_last_attempts__lt=max_attempts)
    due = Q(enrich_last_at__isnull=True) | q_failed
    recheck = service.setting_value(service.recheck_days)
    if recheck is not None:
        due |= (Q(enrich_last_status=ExternalServiceRun.STATUS_SUCCEEDED)
                & Q(enrich_last_at__lte=now - datetime.timedelta(days=float(recheck))))
    return due


def select(service: EnrichmentService, *, missing_only: bool = True, names: Sequence[str] = (),
           ids: Sequence[int] = (), statuses: Sequence[str] = (), since=None, until=None,
           retry_failed: bool = False, never_run: bool = False, rerun: bool = False, now=None):
    """Objects ``service`` should run on, never-run first, then the oldest run.

    ``rerun`` ignores the cadence (every selected candidate); ``retry_failed`` keeps only objects
    whose last run failed; ``never_run`` only objects without a run. ``names``, ``statuses``,
    ``since`` and ``until`` (discovery date) select transients, or the hosts of those transients.
    """
    from YSE_App.models.transient_models import AlternateTransientNames

    qs = service.candidates(missing_only)
    prefix = "" if service.target == TARGET_TRANSIENT else "transient__"
    names = [n.strip() for n in names if n and n.strip()]
    if names:
        alt = AlternateTransientNames.objects.filter(name__in=names).values("transient_id")
        qs = qs.filter(Q(**{prefix + "name__in": names}) | Q(**{prefix + "pk__in": alt}))
    if ids:
        qs = qs.filter(pk__in=list(ids))
    if statuses:
        qs = qs.filter(**{prefix + "status__name__in": list(statuses)})
    if since is not None:
        qs = qs.filter(**{prefix + "disc_date__gte": since})
    if until is not None:
        qs = qs.filter(**{prefix + "disc_date__lt": until})
    if prefix and (names or statuses or since is not None or until is not None):
        qs = qs.distinct()
    qs = with_last_run(service, qs)
    if never_run:
        qs = qs.filter(enrich_last_at__isnull=True)
    elif retry_failed:
        qs = qs.filter(enrich_last_status__in=(ExternalServiceRun.STATUS_FAILED,
                                               ExternalServiceRun.STATUS_CANCELLED))
        if not rerun:
            qs = qs.filter(due_q(service, now))
    elif not rerun:
        qs = qs.filter(due_q(service, now))
    return qs.order_by(
        Case(When(enrich_last_at__isnull=True, then=Value(0)), default=Value(1), output_field=IntegerField()),
        "enrich_last_at", "-pk")


def _target_kwargs(service: EnrichmentService, obj) -> dict:
    if service.target == TARGET_TRANSIENT:
        return {"transient": obj, "target_ref": getattr(obj, "name", "") or ""}
    transient = obj.transient_set.order_by("-pk").first()
    return {"transient": transient, "target_content_type": ContentType.objects.get_for_model(service.model),
            "target_object_id": obj.pk, "target_ref": "%s:%s" % (service.target, obj.pk)}


def record(service: EnrichmentService, obj, outcome: Outcome, user, *, started=None) -> Optional[ExternalServiceRun]:
    """Store ``outcome`` as ``obj``'s latest run of ``service`` (re-using a same-status, no-change run)."""
    return record_many(service, [(obj, outcome)], user, started=started).get(obj.pk)


def record_many(service: EnrichmentService, pairs: Iterable, user, *, started=None) -> Dict[int, ExternalServiceRun]:
    """:func:`record` for many objects with one read of their previous runs."""
    pairs = [(obj, outcome) for obj, outcome in pairs if outcome is not None]
    if service.records_itself or not pairs:
        return {}
    row = external_service(service, user)
    now = timezone.now()
    previous = {}
    pks = [obj.pk for obj, _ in pairs]
    runs = ExternalServiceRun.objects.filter(service=row)
    runs = (runs.filter(transient_id__in=pks) if service.target == TARGET_TRANSIENT
            else runs.filter(target_content_type=ContentType.objects.get_for_model(service.model),
                             target_object_id__in=pks))
    key = "transient_id" if service.target == TARGET_TRANSIENT else "target_object_id"
    for run in runs.order_by("finished_at", "pk"):  # ascending: the last one per object wins
        previous[getattr(run, key)] = run
    out = {}
    for obj, outcome in pairs:
        status = ExternalServiceRun.STATUS_FAILED if outcome.failed else ExternalServiceRun.STATUS_SUCCEEDED
        payload = outcome.as_dict()
        run = None
        prev = previous.get(obj.pk)
        if (prev is not None and prev.status == status and not (prev.result or {}).get("changed")
                and not outcome.changed):
            run = prev
            payload["checks"] = int((prev.result or {}).get("checks") or 1) + 1
        if run is None:
            run = ExternalServiceRun(service=row, request_payload={"object": obj.pk, "target": service.target},
                                     created_by=user, modified_by=user, **_target_kwargs(service, obj))
            run.attempts = 0
        run.status = status
        run.started_at = started or now
        run.finished_at = now
        run.result = payload
        run.error = (outcome.error or outcome.summary)[:10000] if outcome.failed else ""
        run.attempts = (run.attempts or 0) + 1
        run.modified_by = user
        run.save()
        out[obj.pk] = run
    return out


# --- running ----------------------------------------------------------------------------

class Throttle:
    def __init__(self, interval: float = 0.0):
        self.interval = max(0.0, float(interval or 0))
        self._last = None
        self.calls = 0

    def wait(self):
        if self._last is not None and self.interval:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
        self._last = time.monotonic()
        self.calls += 1


@dataclass
class RunSummary:
    service: str
    dry_run: bool = True
    selected: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    lines: List[str] = field(default_factory=list)
    stopped: str = ""

    def add(self, obj, outcome: Outcome):
        self.counts[outcome.action] = self.counts.get(outcome.action, 0) + 1
        if outcome.action in (UPDATED, ERROR) and len(self.lines) < 200:
            self.lines.append("%s: %s%s" % (_label(obj), outcome.action,
                                            (" (%s)" % (outcome.summary or outcome.error))
                                            if (outcome.summary or outcome.error) else ""))

    def as_dict(self) -> dict:
        return asdict(self)


def _label(obj) -> str:
    name = getattr(obj, "name", "") or ""
    return ("%s %s" % (obj.__class__.__name__.lower(), obj.pk)) + ((" (%s)" % name) if name else "")


def run(service: EnrichmentService, objs: Iterable, *, dry_run: bool = True, user=None, ctx: Optional[dict] = None,
        deadline: Optional[float] = None) -> RunSummary:
    """Run ``service`` on ``objs`` and record each outcome. A dry run lists the objects and calls nothing.

    Raises :class:`RateLimited` (objects finished so far are recorded) and :class:`EnrichmentError`.
    """
    objs = list(objs)
    summary = RunSummary(service=service.name, dry_run=dry_run, selected=len(objs))
    if dry_run:
        summary.lines = [_label(o) for o in objs[:200]]
        return summary
    if not service.is_enabled():
        raise EnrichmentError("%s is off (%s)" % (service.name, service.enabled_setting))
    user = user or service_user()
    if ctx is None:
        ctx = service.make_context() if service.make_context else {}
    ctx.setdefault("user", user)
    if service.run_batch is not None:
        started = timezone.now()
        try:
            with change_log.collecting() as changes:
                outcomes = service.run_batch(objs, ctx) or {}
        except (RateLimited, EnrichmentError):
            raise
        except Exception as exc:  # noqa: BLE001 - one remote query for all: nothing per object to record
            log.exception("enrich %s: batch of %d failed", service.name, len(objs))
            raise EnrichmentError("%s batch failed: %s: %s" % (service.name, type(exc).__name__, exc)) from exc
        pairs = [(o, outcomes.get(o.pk)) for o in objs if outcomes.get(o.pk) is not None]
        runs = record_many(service, pairs, user, started=started)
        key = "transient_id" if service.target == TARGET_TRANSIENT else "host_id"
        for pk, run_row in runs.items():
            change_log.attach_run([c for c in changes if getattr(c, key) == pk], run_row)
        for obj, outcome in pairs:
            summary.add(obj, outcome)
        return summary
    throttle = ctx.get("throttle") or Throttle(service.setting_value(service.rate_limit_seconds))
    for obj in objs:
        if deadline is not None and time.monotonic() > deadline:
            summary.stopped = "budget"
            break
        throttle.wait()
        started = timezone.now()
        with change_log.collecting() as changes:
            try:
                outcome = service.run_one(obj, ctx)
            except (RateLimited, EnrichmentError):
                raise
            except Exception as exc:  # noqa: BLE001 - one object's failure must not stop the pass
                log.exception("enrich %s: %s failed", service.name, _label(obj))
                outcome = Outcome(ERROR, error="%s: %s" % (type(exc).__name__, exc))
        if outcome is None:
            continue
        change_log.attach_run(changes, record(service, obj, outcome, user, started=started))
        summary.add(obj, outcome)
    return summary


def run_selection(name: str, *, dry_run: bool = True, limit: Optional[int] = None, user=None, ctx=None,
                  deadline=None, **selection) -> RunSummary:
    service = get(name)
    qs = select(service, **selection)
    if limit:
        qs = qs[: int(limit)]
    return run(service, qs, dry_run=dry_run, user=user, ctx=ctx, deadline=deadline)


# --- jobs --------------------------------------------------------------------------------

def sweep_services() -> List[str]:
    raw = _setting("ENRICH_SWEEP_SERVICES", ()) or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    return [s.strip() for s in raw if str(s).strip()]


def sweep(*, services: Optional[Sequence[str]] = None, limit: Optional[int] = None,
          budget_seconds: Optional[float] = None) -> dict:
    """One pass over each sweep service: its due objects (missing data, failures due a retry)."""
    services = list(services) if services is not None else sweep_services()
    limit = int(limit if limit is not None else _setting("ENRICH_SWEEP_MAX_PER_SERVICE", 100))
    budget = float(budget_seconds if budget_seconds is not None else _setting("ENRICH_SWEEP_BUDGET_SECONDS", 900))
    deadline = time.monotonic() + budget
    out = {}
    for name in services:
        if time.monotonic() > deadline:
            out[name] = {"stopped": "budget"}
            continue
        try:
            service = get(name)
            if not service.is_enabled():
                out[name] = {"skipped": "%s is off" % service.enabled_setting}
                continue
            if service.needs_selection:
                out[name] = {"skipped": "needs an explicit selection; not swept"}
                continue
            summary = run_selection(name, dry_run=False, limit=limit, deadline=deadline)
            out[name] = {"selected": summary.selected, "counts": summary.counts, "stopped": summary.stopped,
                         "lines": summary.lines[:20]}
        except RateLimited as exc:
            out[name] = {"stopped": "rate limited: %s" % exc}
        except EnrichmentError as exc:
            out[name] = {"error": str(exc)}
    return out


@job(SWEEP_KIND, max_attempts=3, backoff_seconds=900)
def enrich_sweep(payload=None, job=None):
    payload = payload or {}
    return sweep(services=payload.get("services"), limit=payload.get("limit"),
                 budget_seconds=payload.get("budget_seconds"))


def queue_objects(name: str, ids: Sequence[int], *, created_by=None, transient=None):
    """Queue an ``enrich.objects`` job running service ``name`` on these object ids (never raises)."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return None
    try:
        return enqueue(OBJECTS_KIND, {"service": name, "ids": ids}, created_by=created_by, transient=transient)
    except Exception:  # noqa: BLE001 - enrichment is best effort; never break the caller
        log.exception("enrich: could not queue %s for %s", name, ids[:5])
        return None


@job(OBJECTS_KIND, max_attempts=3, backoff_seconds=900)
def enrich_objects(payload=None, job=None):
    payload = payload or {}
    service = get(payload.get("service") or "")
    objs = list(service.model.objects.filter(pk__in=payload.get("ids") or []))
    try:
        summary = run(service, objs, dry_run=False)
    except RateLimited as exc:
        raise JobRetry(str(exc), delay=exc.retry_after)
    except EnrichmentError as exc:
        return {"skipped": str(exc)}
    return summary.as_dict()
