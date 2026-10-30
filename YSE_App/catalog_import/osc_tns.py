"""
TNS check of transients created by a catalogue import (#889).

TNS is the authority for names, coordinates and spectroscopic classifications (Ryan, 2026-10-27).
A transient the OSC import created (``CatalogEntry.created_transient``) waits with ``tns_status``
``pending``; :func:`check_entry` then looks it up on TNS:

1. by each IAU-style name among the entry's name and aliases (TNS also holds historical objects);
2. failing that, for discoveries from ``OSC_TNS_CONE_SINCE_YEAR`` (default 2013) on, by a cone search
   of ``OSC_TNS_CONE_RADIUS_ARCSEC`` (3) around the position, using the result only when it is a
   single object.

Found: the transient takes the TNS name (``tns.record_tns_name``: the old name becomes an alternate;
if another transient already has the TNS name nothing changes and the entry is marked ``conflict``),
the TNS coordinates, ``TNS_spec_class`` and, when TNS has a class we know, ``best_spec_class``. A
field is changed only while it still holds the value the import gave it, so edits made by hand in
between are kept. Not found: ``not_found``, the OSC values stay.

Two ways to look objects up:

* **The TNS bulk file** (#905, preferred): ``tns_public.PublicObjects``, one download of TNS's daily
  file of all public objects. Lookups are by TNS name, then by the transient's names and alternate
  names among the objects' internal (survey) names, then by position; no request per object, and
  the same file serves the test and the production runs (``osc_tns_check --tns-file``).
* **The TNS API**, one object at a time, throttled hard: one request a minute
  (``OSC_TNS_REQUEST_INTERVAL_SECONDS``, default 60; Ryan, 2026-10-28; TNS blocks clients that query
  too often). The sweep job ``osc.tns_check`` handles ``OSC_TNS_MAX_PER_RUN`` (20) entries per run
  and re-queues itself after a rate limit.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from YSE_App.jobs import JobRetry, enqueue, job

logger = logging.getLogger(__name__)

SWEEP_KIND = 'osc.tns_check'
SERVICE = 'osc-tns-check'
_IAU_RE = re.compile(r'^\d{4}[A-Za-z]{1,3}$')


def _setting(name, default):
    return getattr(settings, name, default)


def request_interval() -> float:
    return float(_setting('OSC_TNS_REQUEST_INTERVAL_SECONDS', 60.0))


def pending_entries(limit: Optional[int] = None):
    from YSE_App.models import CatalogEntry

    qs = CatalogEntry.objects.filter(tns_status=CatalogEntry.TNS_PENDING).select_related('transient').order_by('id')
    return qs[:limit] if limit else qs


def _lookup(entry, client, throttle) -> tuple:
    """(TNS reply or None, how it was found / why not)."""
    values = entry.import_values or {}
    for name in values.get('iau_names') or []:
        if not _IAU_RE.match(name):
            continue
        throttle.wait()
        reply = client.get_object(name)
        if reply and reply.get('objname'):
            return reply, 'name %s' % name
    year = values.get('disc_year')
    since = int(_setting('OSC_TNS_CONE_SINCE_YEAR', 2013))
    if year is None or year < since:
        return None, 'no TNS name%s' % ('' if year is None else ' (discovered %d, before cone searches)' % year)
    t = entry.transient
    throttle.wait()
    hits = client.search(t.ra, t.dec, radius_arcsec=float(_setting('OSC_TNS_CONE_RADIUS_ARCSEC', 3.0)))
    names = sorted({h.get('objname') for h in hits or [] if h.get('objname')})
    if len(names) != 1:
        return None, 'cone search found %d objects%s' % (len(names), (': ' + ', '.join(names)) if names else '')
    throttle.wait()
    reply = client.get_object(names[0])
    return (reply, 'position') if reply and reply.get('objname') else (None, 'cone hit %s not readable' % names[0])


def _lookup_bulk(entry, objects) -> tuple:
    """(TNS object or None, how it was found / why not), from the bulk file (#905)."""
    from YSE_App.models import AlternateTransientNames

    values = entry.import_values or {}
    for name in values.get('iau_names') or []:
        obj = objects.name(name)
        if obj is not None:
            return obj, 'name %s' % name
    t = entry.transient
    names = [t.name] + list(AlternateTransientNames.objects.filter(transient=t).values_list('name', flat=True))
    for name in dict.fromkeys(names):
        hits = {o['objname']: o for o in objects.internal(name)}
        if len(hits) == 1:
            return next(iter(hits.values())), 'internal name %s' % name
        if len(hits) > 1:
            return None, 'internal name %s is on %d TNS objects: %s' % (name, len(hits), ', '.join(sorted(hits)))
    year = values.get('disc_year')
    since = int(_setting('OSC_TNS_CONE_SINCE_YEAR', 2013))
    if year is None or year < since:
        return None, 'no TNS name%s' % ('' if year is None else ' (discovered %d, before cone searches)' % year)
    hits = {o['objname']: o for o in objects.cone(t.ra, t.dec, float(_setting('OSC_TNS_CONE_RADIUS_ARCSEC', 3.0)))}
    if len(hits) != 1:
        return None, 'cone search found %d objects%s' % (len(hits), (': ' + ', '.join(sorted(hits))) if hits else '')
    return next(iter(hits.values())), 'position'


def _apply(entry, reply, how, user) -> List[str]:
    """Write the TNS values to the transient; returns what changed."""
    from YSE_App.models import CatalogEntry, TransientClass
    from YSE_App.services import change_log
    from YSE_App.sharing import tns

    t = entry.transient
    values = entry.import_values or {}
    changes = []
    tns_name = str(reply['objname']).strip()
    outcome = tns.record_tns_name(t, tns_name, user=user, rename=True, source='TNS')
    if outcome == 'conflict':
        entry.tns_status = CatalogEntry.TNS_CONFLICT
        entry.tns_note = 'TNS name %s belongs to another transient (found by %s); nothing changed' % (tns_name, how)
        return changes
    if outcome == 'renamed':
        changes.append('name -> %s' % tns_name)
        t.refresh_from_db()
    fields = []
    try:
        ra, dec = float(reply.get('radeg')), float(reply.get('decdeg'))
    except (TypeError, ValueError):
        ra = dec = None
    if ra is not None and t.ra == values.get('ra') and t.dec == values.get('dec'):
        t.ra, t.dec = ra, dec
        fields += ['ra', 'dec']
        changes.append('position -> %.6f %.6f' % (ra, dec))
    object_type = reply.get('object_type') or {}
    tns_class = (object_type.get('name') if isinstance(object_type, dict) else '') or ''
    if tns_class and t.TNS_spec_class != tns_class:
        t.TNS_spec_class = tns_class
        fields.append('TNS_spec_class')
    if tns_class and t.best_spec_class_id == values.get('best_spec_class_id'):
        cls = TransientClass.objects.filter(name=tns_class).order_by('id').first()
        if cls is not None and cls.id != t.best_spec_class_id:
            old = t.best_spec_class.name if t.best_spec_class_id else None
            t.best_spec_class = cls
            fields.append('best_spec_class')
            changes.append('class %s -> %s' % (old, cls.name))
    if fields:
        t.modified_by = user
        t.save(update_fields=fields + ['modified_by', 'modified_date'])
        if 'best_spec_class' in fields:
            change_log.record_change(t, 'best_spec_class', old, tns_class, service=SERVICE,
                                     category='classification', source='TNS')
    entry.tns_status = CatalogEntry.TNS_UPDATED
    entry.tns_note = 'found by %s; %s' % (how, '; '.join(changes) or 'nothing to change')
    return changes


def check_entry(entry, client, throttle, *, user=None, dry_run: bool = True, objects=None) -> str:
    """Look one pending entry up on TNS and apply what it says (unless ``dry_run``). Returns a report line.

    TNS rate limits and transport errors propagate (the caller retries later); other errors mark the
    entry ``error``.
    """
    from YSE_App.models import CatalogEntry
    from YSE_App.sharing import tns

    try:
        reply, how = _lookup_bulk(entry, objects) if objects is not None else _lookup(entry, client, throttle)
    except (tns.TNSRateLimited, tns.TNSTransportError):
        raise
    except tns.TNSError as exc:
        if not dry_run:
            entry.tns_status, entry.tns_note, entry.tns_checked = CatalogEntry.TNS_ERROR, str(exc), timezone.now()
            entry.save(update_fields=['tns_status', 'tns_note', 'tns_checked'])
        return '%s: TNS error: %s' % (entry.entry_name, exc)
    if reply is None:
        if not dry_run:
            entry.tns_status, entry.tns_note, entry.tns_checked = CatalogEntry.TNS_NOT_FOUND, how, timezone.now()
            entry.save(update_fields=['tns_status', 'tns_note', 'tns_checked'])
        return '%s: not on TNS (%s)' % (entry.entry_name, how)
    if dry_run:
        return '%s: TNS %s (found by %s); would update' % (entry.entry_name, reply.get('objname'), how)
    from django.test.utils import override_settings

    # As in the import: saving these historical objects must not trigger automatic TNS publishing.
    with override_settings(SHARING_AUTOPUBLISH_ON_SAVE=False), transaction.atomic():
        changes = _apply(entry, reply, how, user or entry.created_by)
        entry.tns_name = str(reply.get('objname'))[:64]
        entry.tns_checked = timezone.now()
        entry.save(update_fields=['tns_status', 'tns_name', 'tns_note', 'tns_checked'])
    return '%s: TNS %s: %s' % (entry.entry_name, entry.tns_name, entry.tns_status if not changes else '; '.join(changes))


def run(limit: Optional[int] = None, *, dry_run: bool = True, client=None, interval: Optional[float] = None,
        user=None, log=None, objects=None) -> dict:
    """Check up to ``limit`` pending entries, against the bulk file ``objects`` if given, else the API.

    API mode stops at a TNS rate limit (re-raised for the job to retry).
    """
    from YSE_App.services import tns_resync

    if objects is None:
        client = client or tns_resync.default_client()
    throttle = tns_resync.Throttle(request_interval() if interval is None else interval)
    counts = {'checked': 0}
    for entry in pending_entries(limit):
        line = check_entry(entry, client, throttle, user=user, dry_run=dry_run, objects=objects)
        counts['checked'] += 1
        if log:
            log(line)
    counts['requests'] = throttle.requests
    return counts


def queue_sweep(created_by=None):
    """Queue one sweep (never raises; the import must not fail because the job queue does)."""
    try:
        return enqueue(SWEEP_KIND, {}, created_by=created_by)
    except Exception:  # noqa: BLE001
        logger.exception('could not queue %s', SWEEP_KIND)
        return None


@job(SWEEP_KIND, max_attempts=5, backoff_seconds=1800)
def sweep_job(payload, job=None):
    from YSE_App.sharing import tns

    limit = int(_setting('OSC_TNS_MAX_PER_RUN', 20))
    try:
        counts = run(limit, dry_run=False)
    except tns.TNSRateLimited as exc:
        raise JobRetry(str(exc), delay=exc.retry_after)
    except tns.TNSTransportError as exc:
        raise JobRetry(str(exc))
    if pending_entries().exists():
        enqueue(SWEEP_KIND, {}, delay=60)
    return counts
