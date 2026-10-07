"""Polling ingest, candidate upsert and save / reject flows (issues #277, #279).

``run_ingest(broker)`` is what the ``brokers.ingest`` job runs: for every
enabled ``BrokerFilter`` of that broker it asks the provider for recent
alerts (``query_alerts``), evaluates the filter's criteria on each normalised
alert and upserts a ``Candidate`` for the ones that pass. A filter with
``auto_save`` promotes new candidates at once through
:func:`save_candidate`, which is also what the scanning page and the API
call. Saving goes through the existing ``/add_transient`` code path
(``data_utils.add_transient_payload``) so aliases, duplicate detection by
position and the photometry passthrough behave exactly like a TNS or ZTF
upload. The Kafka consumer (:mod:`YSE_App.brokers.streams`, #278) feeds the
same :func:`process_alert` path, one message at a time.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Iterable, List, Optional

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from YSE_App.brokers import registry
from YSE_App.brokers.base import (
    PHOTOMETRY,
    SAVE_AS_TRANSIENT,
    BrokerAlert,
    BrokerError,
    BrokerProvider,
    mjd_now,
    points_to_upload_blocks,
)
from YSE_App.brokers.filters import evaluate
from YSE_App.models.candidate_models import BrokerFilter, Candidate
from YSE_App.models.phot_models import TransientPhotometry
from YSE_App.models.transient_models import AlternateTransientNames, Transient

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """A save could not be completed (no user, unknown status, broker error)."""


def match_radius_arcsec() -> float:
    return float(getattr(settings, "BROKER_MATCH_RADIUS_ARCSEC", 2.0) or 2.0)


def audit_user(preferred: Optional[User] = None) -> Optional[User]:
    """User to stamp rows with when there is no request: the caller's, else
    ``BROKER_AUTO_SAVE_USERNAME`` (default ``admin``), else any superuser."""
    if preferred is not None and getattr(preferred, "pk", None):
        return preferred
    username = getattr(settings, "BROKER_AUTO_SAVE_USERNAME", "admin") or "admin"
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User.objects.filter(is_superuser=True).order_by("pk").first()
    return user


# --- matching existing transients -------------------------------------------------

def nearby_transients(ra: float, dec: float, radius_arcsec: Optional[float] = None) -> List[Transient]:
    """Transients within ``radius_arcsec`` of the position, nearest first."""
    from YSE_App.common.utilities import getSeparation

    radius = float(radius_arcsec if radius_arcsec is not None else match_radius_arcsec())
    box = radius / 3600.0
    cosdec = max(math.cos(math.radians(min(89.0, abs(float(dec))))), 1e-3)
    qs = Transient.objects.filter(
        Q(dec__gte=float(dec) - box) & Q(dec__lte=float(dec) + box)
        & Q(ra__gte=float(ra) - box / cosdec) & Q(ra__lte=float(ra) + box / cosdec)
    )
    out = []
    for t in qs[:50]:
        sep = getSeparation(float(ra), float(dec), t.ra, t.dec)
        if sep <= radius:
            out.append((sep, t))
    out.sort(key=lambda pair: pair[0])
    return [t for _, t in out]


def known_transient_for(alert: BrokerAlert) -> Optional[Transient]:
    """Existing transient by broker name / alias, else by position."""
    t = Transient.objects.filter(name=alert.object_id).first()
    if t is None:
        alias = AlternateTransientNames.objects.filter(name=alert.object_id).select_related("transient").first()
        if alias is not None:
            t = alias.transient
    if t is None:
        near = nearby_transients(alert.ra, alert.dec)
        t = near[0] if near else None
    return t


# --- candidates -------------------------------------------------------------------

def upsert_candidate(alert: BrokerAlert, passed: Iterable[BrokerFilter], *, now=None, topic: str = "") -> Candidate:
    """Create or refresh the ``Candidate`` row for ``alert``; never re-opens a
    saved / rejected one (a scanner's decision stands), but records the new
    alert on it."""
    passed = list(passed)
    now = now or timezone.now()
    values = {
        "latest_alert_id": alert.alert_id or "",
        "ra": float(alert.ra),
        "dec": float(alert.dec),
        "discovery_mjd": alert.discovery_mjd,
        "last_mjd": alert.mjd,
        "last_mag": alert.mag,
        "last_band": alert.band or "",
        "rb": alert.rb if alert.rb is not None else alert.drb,
        "classification": (alert.classification or "")[:128],
        "payload": alert.to_dict(),
        "last_seen": now,
    }
    if topic:
        values["topic"] = str(topic)[:128]
    with transaction.atomic():
        candidate, created = Candidate.objects.get_or_create(
            broker=alert.broker, alert_id=alert.object_id,
            defaults=dict(values, first_seen=now, passed_filter_names=[f.name for f in passed]),
        )
        if not created:
            names = set(candidate.passed_filter_names or []) | {f.name for f in passed}
            for k, v in values.items():
                setattr(candidate, k, v)
            candidate.passed_filter_names = sorted(names)
            candidate.n_alerts = (candidate.n_alerts or 0) + 1
            candidate.save()
        if passed:
            candidate.filters.add(*passed)
    return candidate


def notify_filter_pass(bf: BrokerFilter, candidate: Candidate) -> int:
    """#279 hook: tell the filter's group that an alert passed (kind ``candidate``).

    Only for filters with ``notify_group`` and a group; returns the number of
    notifications created. Failures are logged, never raised into the ingest.
    """
    if not bf.notify_group or bf.group_id is None:
        return 0
    try:
        from YSE_App.services.notify import notify

        users = list(bf.group.user_set.filter(is_active=True))
        if not users:
            return 0
        mag = " at %.1f %s" % (candidate.last_mag, candidate.last_band) if candidate.last_mag is not None else ""
        text = "Broker filter %s (%s) passed %s%s%s." % (
            bf.name, bf.broker, candidate.alert_id, mag,
            " (%s)" % candidate.classification if candidate.classification else "")
        url = "%s?filter=%d&status=all" % (reverse("candidate_list"), bf.pk)
        return len(notify(users, text, url, kind="candidate", subject="Broker candidate %s" % candidate.alert_id,
                          payload={"candidate_id": candidate.pk, "filter_id": bf.pk, "broker": bf.broker}))
    except Exception as exc:  # noqa: BLE001 - notification trouble must not stop the ingest
        logger.warning("candidate notification for %s failed: %s", bf.name, exc)
        return 0


def process_alert(alert: BrokerAlert, filters: Iterable[BrokerFilter], provider: BrokerProvider, *,
                  now_mjd: Optional[float] = None, dry_run: bool = False, topic: str = "",
                  result: Optional[Dict] = None) -> Optional[Candidate]:
    """Evaluate ``filters`` on one alert; register a candidate when any passes.

    Shared by the polling ingest (one filter at a time) and the stream consumer
    (every filter of the broker / topic at once). Auto-saves through the first
    passing filter with ``auto_save`` and fires the notification hook for the
    filters that passed. ``result`` counters (``passed``, ``new``, ``saved``,
    ``errors``, ``notified``) are incremented when given. Returns the candidate
    (None when nothing passed or in ``dry_run``).
    """
    now_mjd = now_mjd if now_mjd is not None else mjd_now()
    counters = result if result is not None else {}
    passed = [bf for bf in filters if evaluate(bf.criteria or {}, alert, now_mjd=now_mjd)[0]]
    if not passed:
        return None
    counters["passed"] = counters.get("passed", 0) + 1
    if dry_run:
        return None
    existed = Candidate.objects.filter(broker=alert.broker, alert_id=alert.object_id).exists()
    candidate = upsert_candidate(alert, passed, topic=topic)
    if not existed:
        counters["new"] = counters.get("new", 0) + 1
        for bf in passed:
            counters["notified"] = counters.get("notified", 0) + notify_filter_pass(bf, candidate)
    saver = next((bf for bf in passed if bf.auto_save), None)
    if saver is not None and candidate.status == Candidate.NEW:
        try:
            save_candidate(candidate, audit_user(saver.created_by), status=saver.save_status,
                           obs_group=saver.save_obs_group or None, import_photometry=saver.import_photometry,
                           provider=provider, note="auto-saved by filter %s" % saver.name)
            counters["saved"] = counters.get("saved", 0) + 1
        except Exception as exc:  # noqa: BLE001 - one bad object must not stop the ingest
            counters["errors"] = counters.get("errors", 0) + 1
            logger.exception("auto-save of %s failed: %s", candidate, exc)
    return candidate


def poll_filter(bf: BrokerFilter, provider: BrokerProvider, *, limit: Optional[int] = None,
                now_mjd: Optional[float] = None, dry_run: bool = False) -> Dict:
    """Fetch, evaluate and register the alerts of one filter; returns counters."""
    now_mjd = now_mjd if now_mjd is not None else mjd_now()
    limit = int(limit or bf.max_alerts or 200)
    alerts = provider.query_alerts(bf.query or {}, limit=limit)
    result = {"filter": bf.name, "fetched": len(alerts), "passed": 0, "new": 0, "saved": 0, "errors": 0, "notified": 0}
    for alert in alerts:
        process_alert(alert, [bf], provider, now_mjd=now_mjd, dry_run=dry_run, result=result)
    summary = "fetched %(fetched)d, passed %(passed)d, new %(new)d, saved %(saved)d, errors %(errors)d" % result
    if not dry_run:
        bf.record_run(summary)
    result["summary"] = summary
    return result


def run_ingest(broker: str, *, filter_ids: Optional[Iterable[int]] = None, limit: Optional[int] = None,
               dry_run: bool = False) -> Dict:
    """Poll every enabled filter of ``broker`` (or the given ids). Returns per-filter results."""
    provider = registry.get_provider(broker, require_available=True)
    if provider is None:
        raise BrokerError("broker %r is unknown or disabled (BROKERS_ENABLED)" % broker)
    qs = BrokerFilter.objects.filter(broker=broker, enabled=True)
    if filter_ids:
        qs = qs.filter(pk__in=list(filter_ids))
    results = []
    for bf in qs.order_by("pk"):
        try:
            results.append(poll_filter(bf, provider, limit=limit, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - keep polling the other filters
            logger.exception("broker poll %s/%s failed", broker, bf.name)
            bf.record_run("failed: %s" % exc)
            results.append({"filter": bf.name, "error": str(exc)})
    totals = {k: sum(int(r.get(k, 0) or 0) for r in results) for k in ("fetched", "passed", "new", "saved", "errors")}
    return {"broker": broker, "filters": results, "dry_run": dry_run, **totals}


# --- save / reject ----------------------------------------------------------------

def photometry_blocks(provider: BrokerProvider, alert: BrokerAlert) -> Dict[str, Dict]:
    """``transientphotometry`` value (one block per instrument) for the alert's light curve."""
    if not provider.has(PHOTOMETRY):
        return {}
    points = provider.get_photometry(alert.object_id)
    blocks = points_to_upload_blocks(points, alert.instrument, alert.obs_group)
    return {"%s_%s" % (provider.slug, key): block for key, block in blocks.items()}


def _upload_payload(alert: BrokerAlert, *, status: str, obs_group: str, blocks: Dict[str, Dict]) -> Dict:
    entry = {
        "name": alert.object_id,
        "ra": float(alert.ra),
        "dec": float(alert.dec),
        "status": status,
        "obs_group": obs_group,
    }
    if alert.discovery_date is not None:
        entry["disc_date"] = alert.discovery_date.strftime("%Y-%m-%dT%H:%M:%S")
    if alert.rb is not None or alert.drb is not None:
        entry["real_bogus_score"] = alert.rb if alert.rb is not None else alert.drb
    if blocks:
        entry["transientphotometry"] = {"mjdmatchmin": 0.0005, "clobber": False, **blocks}
    return {alert.object_id: entry, "noupdatestatus": True}


def save_alert_as_transient(provider: BrokerProvider, alert: BrokerAlert, user, *, status: str = "New",
                            obs_group: Optional[str] = None, import_photometry: bool = True) -> Transient:
    """Create (or update) the YSE-PZ transient for ``alert`` through ``add_transient``.

    The provider's ``obs_group`` becomes the transient's ``obs_group`` (created
    when missing); a broker error while fetching photometry is logged and the
    transient is still created without the light curve.
    """
    from YSE_App.data_utils import add_transient_payload
    from YSE_App.models.enum_models import ObservationGroup, TransientStatus

    if not provider.has(SAVE_AS_TRANSIENT):
        raise IngestError("%s cannot save transients" % provider.slug)
    user = audit_user(user)
    if user is None:
        raise IngestError("no user to stamp the transient with (set BROKER_AUTO_SAVE_USERNAME)")
    if not TransientStatus.objects.filter(name=status).exists():
        raise IngestError("unknown transient status %r" % status)
    group_name = obs_group or alert.obs_group or provider.default_obs_group
    ObservationGroup.objects.get_or_create(name=group_name, defaults={"created_by_id": user.id, "modified_by_id": user.id})
    blocks = {}
    if import_photometry:
        try:
            blocks = photometry_blocks(provider, alert)
        except BrokerError as exc:
            logger.warning("photometry for %s from %s unavailable: %s", alert.object_id, provider.slug, exc)
    response = add_transient_payload(_upload_payload(alert, status=status, obs_group=group_name, blocks=blocks), user)
    if getattr(response, "status_code", 200) != 200:
        raise IngestError("add_transient failed: %s" % response.content.decode(errors="replace")[:300])
    transient = Transient.objects.filter(name=alert.object_id).first()
    if transient is None:
        alias = AlternateTransientNames.objects.filter(name=alert.object_id).select_related("transient").first()
        transient = alias.transient if alias else None
    if transient is None:
        raise IngestError("add_transient reported success but %s is missing" % alert.object_id)
    return transient


def link_existing_transient(alert: BrokerAlert, transient: Transient, user, *, obs_group: Optional[str] = None) -> None:
    """Record the broker name as an alias of a transient found by position."""
    from YSE_App.models.enum_models import ObservationGroup

    user = audit_user(user)
    if transient.name == alert.object_id or AlternateTransientNames.objects.filter(name=alert.object_id).exists():
        return
    group_name = obs_group or alert.obs_group
    group, _ = ObservationGroup.objects.get_or_create(name=group_name, defaults={"created_by_id": user.id, "modified_by_id": user.id})
    AlternateTransientNames.objects.create(
        transient=transient, obs_group=group, name=alert.object_id, created_by_id=user.id, modified_by_id=user.id
    )


def save_candidate(candidate: Candidate, user, *, status: Optional[str] = None, obs_group: Optional[str] = None,
                   import_photometry: Optional[bool] = None, provider: Optional[BrokerProvider] = None,
                   note: str = "", link_only_if_known: bool = True) -> Transient:
    """Promote a candidate: link it to a transient already at that position (or
    with that name), else create one through the provider; mark it saved."""
    provider = provider or registry.get_provider(candidate.broker, require_available=True)
    if provider is None:
        raise IngestError("broker %r is unknown or disabled" % candidate.broker)
    alert = candidate.alert
    status = status or "New"
    import_photometry = True if import_photometry is None else bool(import_photometry)
    transient = known_transient_for(alert) if link_only_if_known else None
    if transient is not None:
        link_existing_transient(alert, transient, user, obs_group=obs_group)
        if import_photometry:
            try:
                import_photometry_for(provider, transient, alert, user)
            except BrokerError as exc:
                logger.warning("photometry import for %s failed: %s", transient.name, exc)
        note = note or "linked to existing %s" % transient.name
    else:
        transient = provider.save_as_transient(alert, user, status=status, obs_group=obs_group,
                                               import_photometry=import_photometry)
    apply_filter_tags(candidate, transient, user)
    candidate.set_status(Candidate.SAVED, user, transient=transient, note=note)
    return transient


def apply_filter_tags(candidate: Candidate, transient: Transient, user=None) -> List[str]:
    """Add the ``default_tags`` of every filter the candidate passed to the transient (#279)."""
    added = []
    for bf in candidate.filters.all().prefetch_related("default_tags"):
        for tag in bf.default_tags.all():
            if not transient.tags.filter(pk=tag.pk).exists():
                transient.tags.add(tag)
                added.append(tag.name)
    return added


def save_alert(provider: BrokerProvider, alert: BrokerAlert, user, *, status: str = "New",
               obs_group: Optional[str] = None, import_photometry: bool = True, note: str = "") -> Transient:
    """Save an alert that is not (yet) a candidate: the cone-search page's
    *Save as transient* (#275). Records a ``Candidate`` row in status ``saved``
    for provenance and reuses :func:`save_candidate`."""
    candidate = upsert_candidate(alert, [])
    if candidate.status == Candidate.SAVED and candidate.transient_id:
        return candidate.transient
    return save_candidate(candidate, user, status=status, obs_group=obs_group, import_photometry=import_photometry,
                          provider=provider, note=note or "saved from broker search")


def import_photometry_for(provider: BrokerProvider, transient: Transient, alert: BrokerAlert, user) -> int:
    """Pull the broker light curve onto an existing transient (photometry passthrough)."""
    from YSE_App.data_utils import add_transient_phot_util
    from YSE_App.services import photstat

    user = audit_user(user)
    blocks = photometry_blocks(provider, alert)
    if not blocks:
        return 0
    photdict = {"mjdmatchmin": 0.0005, "clobber": False, **blocks}
    with photstat.deferred_updates():
        # Same two-stage call as add_transient: the first pass returns the
        # TransientPhotometry rows to create in bulk, the second writes the points.
        _response, new_phot = add_transient_phot_util(photdict, transient, user, do_photdata=False)
        if new_phot:
            TransientPhotometry.objects.bulk_create(new_phot)
        add_transient_phot_util(photdict, transient, user, do_photdata=True)
        photstat.schedule_recompute(transient.id)
    return sum(len(b["photdata"]) for b in blocks.values())


def rejecting_groups(candidate: Candidate, user) -> List[Group]:
    """Groups a rejection by ``user`` counts for: the user's groups that own one of
    the candidate's filters. Empty means the rejection is global (staff, a
    shared filter, or a candidate without filters)."""
    if user is None or not getattr(user, "pk", None) or user.is_staff or user.is_superuser:
        return []
    owning = {f.group_id for f in candidate.filters.all()}
    if None in owning or not owning:
        return []
    return list(user.groups.filter(pk__in=[g for g in owning if g is not None]))


def reject_candidate(candidate: Candidate, user, *, note: str = "", scope: str = "auto") -> Candidate:
    """Reject for the user's groups (#279: hidden from their queue, still shown to
    other groups) or globally (``scope='all'``, staff, shared filter). The row
    goes to status ``rejected`` once every owning group has rejected it."""
    groups = [] if scope == "all" else rejecting_groups(candidate, user)
    if not groups:
        candidate.set_status(Candidate.REJECTED, user, note=note)
        return candidate
    candidate.rejected_by_groups.add(*groups)
    owning = {f.group_id for f in candidate.filters.all()} - {None}
    rejected = set(candidate.rejected_by_groups.values_list("pk", flat=True))
    if owning and owning <= rejected:
        candidate.set_status(Candidate.REJECTED, user, note=note)
    elif note:
        candidate.note = note[:255]
        candidate.save(update_fields=["note", "updated_at"])
    return candidate


def reopen_candidate(candidate: Candidate, user) -> Candidate:
    if getattr(user, "pk", None) and not (user.is_staff or user.is_superuser):
        candidate.rejected_by_groups.remove(*user.groups.all())
    else:
        candidate.rejected_by_groups.clear()
    candidate.status = Candidate.NEW
    candidate.status_changed_by = user if getattr(user, "pk", None) else None
    candidate.status_changed_at = timezone.now()
    candidate.save(update_fields=["status", "status_changed_by", "status_changed_at", "updated_at"])
    return candidate
