"""DESI host matching for host-less transients, and the per-object DESI button (#738).

* :func:`select_host` -- the host of a transient with no host, from the DESI galaxies near it,
  by the same rule as NED's ``ned.select_host`` (smallest separation x z, within 100 kpc
  projected). It reuses that function, so the two cannot drift apart.
* :func:`match_transient` -- the enrichment service ``desi-hostless``: finds that host, links it
  the way ``prost_hosts`` does (``data_utils.add_transient_host_util``, the upload path's Host rows),
  and writes the host's redshift only through ``redshift_precedence.apply_redshift``. The link is
  recorded in the change log (#737). A transient's own redshift is never set from a nearby galaxy.
* :func:`request_lookup` -- what the DESI button on the transient page does: rate-limit, then
  queue an ``enrich.objects`` job (the host's DESI redshift check, or this host-less match).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from django.conf import settings
from django.core.cache import cache

from YSE_App.services import desi, ned

log = logging.getLogger(__name__)

SERVICE_SLUG = "desi-hostless"  # the enrichment registry's ExternalService slug
SERVICE_NAME = "desi-hostless"  # registry name (``manage.py enrich --service``)
DEFAULT_SEARCH_ARCMIN = 5.0


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def search_radius_arcmin() -> float:
    return float(_setting("DESI_HOSTLESS_SEARCH_ARCMIN", DEFAULT_SEARCH_ARCMIN))


def _as_ned_objects(matches: List[desi.DESIMatch]) -> List[ned.NEDObject]:
    """DESI rows in the shape ``ned.select_host`` takes. The catalogue already holds only quality-cut
    spectroscopic galaxies (zwarn 0, GALAXY), so each is a usable spec-z galaxy."""
    return [ned.NEDObject(name="DESI %s" % m.targetid, ra=m.ra, dec=m.dec, z=m.z, zflag="S", ptype="G",
                          sep_arcmin=m.sep_arcsec / 60.0) for m in matches]


def select_host(matches: List[desi.DESIMatch], *, max_kpc: float = 100.0) -> Optional[desi.DESIMatch]:
    """The DESI galaxy NED's host rule would pick (smallest separation x z, within ``max_kpc``)."""
    objects = _as_ned_objects(matches)
    best = ned.select_host(objects, max_kpc=max_kpc)
    if best is None:
        return None
    return next(m for m, o in zip(matches, objects) if o is best)


def transient_host(ra: float, dec: float, *, client=None, radius_arcmin: Optional[float] = None
                   ) -> Optional[desi.DESIMatch]:
    """The likely DESI host of a transient at (ra, dec), or None."""
    radius = float(radius_arcmin if radius_arcmin is not None else search_radius_arcmin())
    return select_host(desi.desi_redshifts_near(ra, dec, radius * 60.0, client=client))


def match_transient(transient, *, dry_run: bool = False, user=None, client=None):
    """Find and link a DESI host for one transient without one; returns an enrichment ``Outcome``."""
    from YSE_App.services import redshift_precedence as rp
    from YSE_App.services.enrichment import ERROR, NO_MATCH, SKIPPED, UPDATED, Outcome

    if transient.host_id:
        return Outcome(SKIPPED, "has a host")
    if transient.ra is None or transient.dec is None:
        return Outcome(SKIPPED, "no position")
    try:
        best = transient_host(float(transient.ra), float(transient.dec), client=client)
    except desi.DESIError as exc:
        return Outcome(ERROR, error=str(exc)[:500])
    if best is None:
        return Outcome(NO_MATCH, "no DESI host within 100 kpc")
    doc = {"name": "DESI %s" % best.targetid, "ra": best.ra, "dec": best.dec}
    summary = "host %s (%.5f, %.5f), %.1f arcsec away" % (doc["name"], best.ra, best.dec, best.sep_arcsec)
    data = dict(doc, redshift=best.z, redshift_err=best.z_err, redshift_source=rp.desi_label(),
                sep_arcsec=best.sep_arcsec)
    if dry_run:
        return Outcome(UPDATED, "would link " + summary, data=data)

    from YSE_App import data_utils
    from YSE_App.models.change_log_models import ValueChange
    from YSE_App.services import change_log
    from YSE_App.services.enrichment import service_user

    user = user or service_user()
    data_utils.add_transient_host_util(doc, transient, user)
    transient.refresh_from_db(fields=["host"])
    host = transient.host
    if host is None:
        return Outcome(ERROR, error="the host could not be linked")
    change_log.record_change(transient, "host", None, "%s (host %s)" % (host.name, host.pk), service=SERVICE_SLUG,
                             category=ValueChange.CATEGORY_HOST, source=rp.desi_label())
    decision = rp.apply_redshift(host, best.z, best.z_err, rp.desi_label(), user=user, service=SERVICE_SLUG)
    summary += "; z %s (%s)" % (best.z, decision.reason)
    data["host_id"] = host.pk
    return Outcome(UPDATED, "linked " + summary, data=data)


# --- the button -------------------------------------------------------------------------

def cooldown_seconds() -> int:
    return int(_setting("DESI_BUTTON_COOLDOWN_SECONDS", 600))


def per_hour_limit() -> int:
    return int(_setting("DESI_BUTTON_MAX_PER_HOUR", 20))


def _bump(key: str, ttl: int) -> int:
    cache.add(key, 0, ttl)
    try:
        return cache.incr(key)
    except ValueError:  # evicted between add and incr
        cache.set(key, 1, ttl)
        return 1


def request_lookup(transient, user):
    """Queue a DESI lookup for ``transient``: its host's redshift, or a host match without a host.

    Returns ``(http_status, message, job)``. Rate limits: one request per transient per cooldown
    (``DESI_BUTTON_COOLDOWN_SECONDS``, 600) and ``DESI_BUTTON_MAX_PER_HOUR`` (20) per user.
    """
    from YSE_App.services import enrichment

    host = transient.host if transient.host_id else None
    if host is None and (transient.ra is None or transient.dec is None):
        return 400, "error: this object has no position to search DESI around", None
    if host is not None and (host.ra is None or host.dec is None):
        return 400, "error: the host has no position to search DESI around", None
    limit = per_hour_limit()
    if limit > 0 and _bump("yse:desibtn:user:%s" % user.pk, 3600) > limit:
        return 429, "error: DESI lookup limit reached (%d per hour); try again later" % limit, None
    cooldown = cooldown_seconds()
    if cooldown > 0 and not cache.add("yse:desibtn:obj:%s" % transient.pk, 1, cooldown):
        return 429, "error: a DESI lookup for this object was requested recently; wait a few minutes", None
    if host is not None:
        job = enrichment.queue_objects("desi", [host.pk], created_by=user, transient=transient)
        what = "DESI redshift check of the host"
    else:
        job = enrichment.queue_objects(SERVICE_NAME, [transient.pk], created_by=user, transient=transient)
        what = "DESI host match"
    if job is None:
        cache.delete("yse:desibtn:obj:%s" % transient.pk)
        return 503, "error: could not queue the DESI lookup; nothing was requested", None
    return 200, "queued: %s" % what, job


def lookup_status(transient) -> dict:
    """The latest DESI result for this object: the queued job and the last run of the DESI services."""
    from YSE_App.models.external_service_models import ExternalServiceRun
    from YSE_App.models.job_models import Job

    slugs = ["desi-redshift", SERVICE_SLUG]
    run = (ExternalServiceRun.objects.filter(service__slug__in=slugs, transient=transient)
           .exclude(status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING))
           .order_by("-finished_at", "-pk").first())
    job = Job.objects.filter(kind="enrich.objects", transient=transient).order_by("-pk").first()
    out = {"pending": bool(job and job.status in Job.ACTIVE_STATUSES), "job_status": job.status if job else "",
           "result": "", "when": ""}
    if run is not None:
        result = run.result or {}
        text = result.get("summary") or ""
        if not text and result.get("action"):  # a ``desi-redshift`` host check
            text = "%s%s" % (result["action"], (" (%s)" % result["reason"]) if result.get("reason") else "")
            if result.get("new") is not None:
                text += ", z = %s" % result["new"]
        out["result"] = (text or run.error or run.status)[:300]
        out["when"] = run.finished_at.isoformat() if run.finished_at else ""
    return out
