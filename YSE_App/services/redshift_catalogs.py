"""Host redshifts from NED and DESI, applied under the one precedence list (#436, #437).

For a host (a ``Host`` linked to at least one transient) each enabled catalogue is
queried at the host position, highest rank first (NED, then DESI). A catalogue is not
queried when the host's redshift is an override or already comes from a higher-ranked
source. A match is written through :func:`redshift_precedence.apply_redshift`, so it never
replaces an override or a higher-ranked value, and it records its source (``NED`` or
``DESI DR1``). We never set a transient's own redshift from a nearby galaxy.

Each check is recorded as an ``ExternalServiceRun`` of the ``ned-redshift`` or ``desi-redshift`` service
(created on first use), targeted at the host, so the sweep and the backfill resume. A
check that changes nothing re-uses the host's previous no-change run.

* :func:`update_host` -- one host (dry run by default).
* :func:`sweep` -- the regular job (``redshifts.sweep``, queued by the
  ``Redshift_Catalogs.RedshiftCatalogSweep`` cron): hosts never checked first, then the
  ones whose last check is older than ``REDSHIFT_CATALOG_RECHECK_DAYS``.
* :func:`queue_host_lookup` -- queues ``redshifts.host_lookup`` when a host is associated
  (upload path), when the sweep is enabled.
* ``manage.py host_redshifts`` -- the one-off backfill (dry run by default).
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional, Sequence

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone

from YSE_App.jobs import JobRetry, enqueue, job
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.host_models import Host
from YSE_App.services import desi, ned
from YSE_App.services import redshift_precedence as rp

log = logging.getLogger(__name__)

SWEEP_KIND = "redshifts.sweep"
LOOKUP_KIND = "redshifts.host_lookup"
SERVICES = ("ned", "desi")  # rank order
SERVICE_SLUGS = {"ned": "ned-redshift", "desi": "desi-redshift"}  # ExternalService.slug
_SERVICE_INFO = {
    "ned": ("NED host redshifts", "Spectroscopic host redshifts from NED::API (#436)."),
    "desi": ("DESI host redshifts", "Spectroscopic host redshifts from DESI via Astro Data Lab TAP (#437)."),
}


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def enabled_services() -> List[str]:
    raw = _setting("REDSHIFT_CATALOG_SERVICES", "ned,desi")
    if isinstance(raw, str):
        raw = raw.split(",")
    wanted = {str(s).strip().lower() for s in raw if str(s).strip()}
    return [s for s in SERVICES if s in wanted]


def service_rank(slug: str) -> int:
    return rp.RANK_NED if slug == "ned" else rp.RANK_DESI


def service_source(slug: str) -> str:
    return rp.SOURCE_NED if slug == "ned" else rp.desi_label()


def service_user():
    from YSE_App.services.tns_resync import service_user as _user

    return _user()


def catalog_service(name: str, user=None, *, create: bool = True) -> Optional[ExternalService]:
    """The ``ned-redshift`` / ``desi-redshift`` ExternalService whose runs record each host check."""
    slug = SERVICE_SLUGS[name]
    service = ExternalService.objects.filter(slug=slug).first()
    if service is None and create:
        user = user or service_user()
        title, description = _SERVICE_INFO[name]
        service, _ = ExternalService.objects.get_or_create(
            slug=slug,
            defaults={"name": title, "kind": ExternalService.KIND_GENERIC,
                      "description": description + " One run per host check; no runner, nothing to start by hand.",
                      "created_by": user, "modified_by": user},
        )
    return service


# --- result --------------------------------------------------------------------

@dataclass
class CheckResult:
    service: str
    host_id: int
    host_name: str = ""
    action: str = "no_match"  # updated | unchanged | kept | no_match | skipped | error
    dry_run: bool = True
    reason: str = ""
    old: Optional[float] = None
    old_source: Optional[str] = None
    new: Optional[float] = None
    new_err: Optional[float] = None
    source: str = ""
    match: dict = field(default_factory=dict)
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.action == "updated"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["changed"] = self.changed
        return data

    def summary(self) -> str:
        label = "host %s%s [%s]" % (self.host_id, (" (%s)" % self.host_name) if self.host_name else "", self.service)
        if self.action == "updated":
            verb = "would set" if self.dry_run else "set"
            return "%s: %s z %r (%s) -> %r (%s)" % (label, verb, self.old, self.old_source or "no source",
                                                  self.new, self.source)
        if self.action == "error":
            return "%s: error (%s)" % (label, self.error)
        return "%s: %s%s" % (label, self.action, (" (%s)" % self.reason) if self.reason else "")


def _record(host: Host, result: CheckResult, user, *, started=None):
    service = catalog_service(result.service, user)
    ct = ContentType.objects.get_for_model(Host)
    now = timezone.now()
    status = ExternalServiceRun.STATUS_FAILED if result.action == "error" else ExternalServiceRun.STATUS_SUCCEEDED
    payload = result.as_dict()
    runs = ExternalServiceRun.objects.filter(service=service, target_content_type=ct, target_object_id=host.pk)
    run = None
    if not result.changed and status == ExternalServiceRun.STATUS_SUCCEEDED:
        previous = runs.order_by("-finished_at", "-pk").first()
        if previous is not None and previous.status == status and not (previous.result or {}).get("changed"):
            run = previous
            payload["checks"] = int((previous.result or {}).get("checks") or 1) + 1
    if run is None:
        transient = host.transient_set.order_by("-pk").first()
        run = ExternalServiceRun(service=service, transient=transient, target_content_type=ct,
                                 target_object_id=host.pk, target_ref="host:%s" % host.pk,
                                 request_payload={"host": host.pk, "ra": host.ra, "dec": host.dec},
                                 created_by=user, modified_by=user)
    run.status = status
    run.started_at = started or now
    run.finished_at = now
    run.result = payload
    run.error = result.error[:10000] if status == ExternalServiceRun.STATUS_FAILED else ""
    run.attempts = (run.attempts or 0) + 1
    run.modified_by = user
    run.save()
    return run


# --- one host ------------------------------------------------------------------

def _match(slug: str, host: Host, client):
    if slug == "ned":
        m = ned.host_match(host.ra, host.dec, client=client)
        return (m.z, m.z_err, m.as_dict()) if m is not None else None
    m = desi.host_match(host.ra, host.dec, client=client)
    return (m.z, m.z_err, m.as_dict()) if m is not None else None


def check_host(host: Host, slug: str, *, client, dry_run: bool = True, user=None) -> CheckResult:
    """Query one catalogue for one host and apply the result if precedence allows."""
    result = CheckResult(service=slug, host_id=host.pk, host_name=host.name or "", dry_run=dry_run,
                         old=host.redshift, old_source=host.redshift_source, source=service_source(slug))
    if rp.is_override(host):
        result.action, result.reason = "skipped", "override"
        return result
    if host.redshift is not None and rp.source_rank(host.redshift_source) > service_rank(slug):
        result.action, result.reason = "skipped", "has %s redshift" % host.redshift_source
        return result
    try:
        found = _match(slug, host, client)
    except (ned.NEDError, desi.DESIError) as exc:
        result.action, result.error = "error", str(exc)[:500]
        return result
    if found is None:
        result.action = "no_match"
        return result
    result.new, result.new_err, result.match = found
    if dry_run:
        decision = rp.decide(host, result.new, result.source)
    else:
        decision = rp.apply_redshift(host, result.new, result.new_err, result.source, user=user)
    result.reason = decision.reason
    if decision.write:
        result.action = "updated"
    elif decision.reason == "unchanged":
        result.action = "unchanged"
    else:
        result.action = "kept"
    return result


def default_clients() -> dict:
    return {"ned": ned.default_client(), "desi": desi.default_client()}


def update_host(host: Host, *, services: Optional[Sequence[str]] = None, dry_run: bool = True,
                clients: Optional[dict] = None, user=None) -> List[CheckResult]:
    """Check ``host`` against each enabled catalogue (rank order) and record the runs."""
    services = [s for s in SERVICES if s in (services if services is not None else enabled_services())]
    clients = clients or default_clients()
    if not dry_run and user is None:
        user = service_user()
    results = []
    for slug in services:
        started = timezone.now()
        result = check_host(host, slug, client=clients[slug], dry_run=dry_run, user=user)
        if not dry_run:
            _record(host, result, user, started=started)
        results.append(result)
    return results


# --- selection -----------------------------------------------------------------

def _last_run(slug: str, ct):
    runs = (ExternalServiceRun.objects.filter(service__slug=SERVICE_SLUGS[slug], target_content_type=ct,
                                              target_object_id=OuterRef("pk"),
                                              status__in=ExternalServiceRun.FINAL_STATUSES)
            .order_by("-finished_at").values("finished_at")[:1])
    return Subquery(runs)


def select_hosts(*, services: Optional[Sequence[str]] = None, recheck_days: Optional[float] = None,
                 host_ids: Iterable[int] = (), transient_names: Iterable[str] = (), only_empty: bool = False,
                 now=None):
    """Hosts linked to a transient that are due a check: never checked first, then oldest check."""
    services = [s for s in SERVICES if s in (services if services is not None else enabled_services())]
    ct = ContentType.objects.get_for_model(Host)
    qs = Host.objects.filter(transient__isnull=False).distinct()
    host_ids, transient_names = list(host_ids), list(transient_names)
    if host_ids:
        qs = qs.filter(pk__in=host_ids)
    if transient_names:
        qs = qs.filter(transient__name__in=transient_names)
    if only_empty:
        qs = qs.filter(redshift__isnull=True)
    qs = qs.exclude(redshift_override=True)
    if not services:
        return qs.none()
    for slug in services:
        qs = qs.annotate(**{"last_%s" % slug: _last_run(slug, ct)})
    if recheck_days is not None:
        cutoff = (now or timezone.now()) - datetime.timedelta(days=float(recheck_days))
        due = Q()
        for slug in services:
            due |= Q(**{"last_%s__isnull" % slug: True}) | Q(**{"last_%s__lt" % slug: cutoff})
        qs = qs.filter(due)
    return qs.order_by(F("last_%s" % services[0]).asc(nulls_first=True), "-pk")


# --- the regular job -----------------------------------------------------------

def sweep(*, limit: Optional[int] = None, recheck_days=None, budget_seconds=None, clients=None) -> dict:
    limit = int(limit if limit is not None else _setting("REDSHIFT_CATALOG_MAX_PER_RUN", 100))
    recheck = float(recheck_days if recheck_days is not None else _setting("REDSHIFT_CATALOG_RECHECK_DAYS", 90))
    budget = float(budget_seconds if budget_seconds is not None
                   else _setting("REDSHIFT_CATALOG_BUDGET_SECONDS", 900))
    deadline = time.monotonic() + budget
    user = service_user()
    clients = clients or default_clients()
    summary = {"checked": 0, "updated": 0, "errors": 0, "changes": []}
    for host in select_hosts(recheck_days=recheck)[:limit]:
        if time.monotonic() > deadline:
            summary["stopped"] = "budget"
            break
        results = update_host(host, dry_run=False, clients=clients, user=user)
        summary["checked"] += 1
        for r in results:
            if r.changed:
                summary["updated"] += 1
                if len(summary["changes"]) < 50:
                    summary["changes"].append(r.summary())
            elif r.action == "error":
                summary["errors"] += 1
    summary["requests"] = {slug: getattr(c, "requests", 0) for slug, c in clients.items()}
    return summary


def lookups_enabled() -> bool:
    return bool(_setting("REDSHIFT_CATALOG_CRON_ENABLED", False))


def queue_host_lookup(host) -> Optional[object]:
    """Queue a check of a newly associated host (only while the regular sweep is enabled)."""
    if host is None or not getattr(host, "pk", None) or not lookups_enabled():
        return None
    try:
        return enqueue(LOOKUP_KIND, {"host_id": host.pk})
    except Exception:  # never break an upload because the queue is unavailable
        log.exception("redshifts: could not queue a lookup for host %s", host.pk)
        return None


@job(LOOKUP_KIND, max_attempts=3, backoff_seconds=900)
def host_lookup(payload=None, job=None):
    payload = payload or {}
    host = Host.objects.filter(pk=payload.get("host_id")).first()
    if host is None:
        return {"skipped": "no such host"}
    results = update_host(host, dry_run=False)
    if results and all(r.action == "error" for r in results):
        raise JobRetry("; ".join(r.error for r in results))
    return {"results": [r.summary() for r in results]}


@job(SWEEP_KIND, max_attempts=3, backoff_seconds=900)
def redshift_sweep(payload=None, job=None):
    payload = payload or {}
    return sweep(limit=payload.get("limit"), recheck_days=payload.get("recheck_days"),
                 budget_seconds=payload.get("budget_seconds"))
