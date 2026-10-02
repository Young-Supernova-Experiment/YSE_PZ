"""Job-queue handlers for the broker ingest (issue #278's polling half).

``brokers.ingest`` polls one broker's enabled filters; ``brokers.import_photometry``
pulls a light curve onto an existing transient. Both are enqueued by the
``BrokerPoll`` cron (``YSE_App.data_ingest.Broker_Ingest``), the
``broker_poll`` management command and the candidate page.
"""

from __future__ import annotations

from django.contrib.auth.models import User

from YSE_App.brokers import ingest, registry
from YSE_App.brokers.base import BrokerError, BrokerUnavailable
from YSE_App.jobs import JobFailed, enqueue, job
from YSE_App.models.transient_models import Transient

INGEST_KIND = "brokers.ingest"
IMPORT_PHOTOMETRY_KIND = "brokers.import_photometry"


@job(INGEST_KIND, max_attempts=2, backoff_seconds=300)
def ingest_job(payload, job=None):
    broker = (payload or {}).get("broker")
    if not broker:
        raise JobFailed("payload needs a broker slug")
    try:
        return ingest.run_ingest(
            broker,
            filter_ids=(payload or {}).get("filter_ids") or None,
            limit=(payload or {}).get("limit"),
            dry_run=bool((payload or {}).get("dry_run")),
        )
    except BrokerUnavailable as exc:
        raise JobFailed(str(exc))


@job(IMPORT_PHOTOMETRY_KIND, max_attempts=3, backoff_seconds=120)
def import_photometry_job(payload, job=None):
    payload = payload or {}
    transient = Transient.objects.filter(pk=payload.get("transient_id")).first()
    if transient is None:
        raise JobFailed("transient %r not found" % payload.get("transient_id"))
    try:
        provider = registry.get_provider(payload.get("broker"), require_available=True)
    except BrokerUnavailable as exc:
        raise JobFailed(str(exc))
    if provider is None:
        raise JobFailed("broker %r is unknown or disabled" % payload.get("broker"))
    user = User.objects.filter(pk=payload.get("user_id")).first()
    from YSE_App.brokers.base import BrokerAlert

    alert = BrokerAlert(broker=provider.slug, object_id=str(payload.get("object_id")), ra=transient.ra, dec=transient.dec,
                        instrument=payload.get("instrument") or provider.default_instrument,
                        obs_group=payload.get("obs_group") or provider.default_obs_group)
    try:
        n = ingest.import_photometry_for(provider, transient, alert, user)
    except BrokerError as exc:
        raise RuntimeError(str(exc))  # retried with backoff
    return {"transient": transient.name, "points": n}


def enqueue_ingest(broker: str, *, filter_ids=None, limit=None, dry_run=False, created_by=None):
    return enqueue(INGEST_KIND, {"broker": broker, "filter_ids": list(filter_ids or []) or None, "limit": limit,
                                 "dry_run": bool(dry_run)}, created_by=created_by)


def enqueue_import_photometry(transient, broker: str, object_id: str, *, user=None, instrument=None, obs_group=None):
    return enqueue(IMPORT_PHOTOMETRY_KIND, {"transient_id": transient.pk, "broker": broker, "object_id": object_id,
                                            "user_id": getattr(user, "pk", None), "instrument": instrument,
                                            "obs_group": obs_group},
                   created_by=user, transient=transient)
