"""Instrument logs: add, list, and pull from facility APIs (#309: #310).

* :func:`add_log` records one :class:`InstrumentLog` (by hand or from a pull);
  the ``fingerprint`` (sha256 of instrument, start, message and entries) makes
  a repeated pull a no-op.
* :func:`logs_between` lists the logs of an instrument (or every instrument of
  a telescope) that overlap a date range.
* :func:`pull_instrument_logs` asks the facility adapter of an allocation on the
  instrument (``FacilityAPI.fetch_instrument_log``, capability
  ``instrument_log``) for the entries of a window, recorded as an
  :class:`ExternalServiceRun` on the allocation's service; the
  ``instrument_logs.pull`` job (:func:`pull_job`) does it for every instrument
  with such an allocation, and the ``InstrumentLogPull`` cron queues that job
  when ``INSTRUMENT_LOG_PULL_CRON_ENABLED`` is on.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from YSE_App.facilities import FacilityError, get_facility
from YSE_App.jobs import job
from YSE_App.models.allocation_models import Allocation
from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.models.instrument_log_models import InstrumentLog
from YSE_App.models.instrument_models import Instrument
from YSE_App.models.telescope_models import Telescope
from YSE_App.services import external_services as runs
from YSE_App.services.allocations import ensure_service

log = logging.getLogger(__name__)

PULL_JOB_KIND = "instrument_logs.pull"

MESSAGE_KEYS = ("message", "msg", "text", "description", "comment")
TIMESTAMP_KEYS = ("timestamp", "time", "date", "datetime", "created_at", "created", "start", "obstime", "mjd")
LEVEL_KEYS = ("level", "severity", "type", "kind")


# --- entries ----------------------------------------------------------------

def _as_utc(value) -> Optional[datetime.datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        dt = datetime.datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        try:
            if text.replace(".", "", 1).isdigit():
                number = float(text)
                if 30000 < number < 100000:  # an MJD
                    dt = datetime.datetime(1858, 11, 17) + datetime.timedelta(days=number)
                else:  # unix seconds (or milliseconds)
                    dt = datetime.datetime.utcfromtimestamp(number / 1000.0 if number > 1e11 else number)
            else:
                dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _iso(dt: Optional[datetime.datetime]) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def normalize_entry(item: Any) -> Optional[Dict[str, Any]]:
    """One entry as ``{"timestamp", "message", "level"}`` (plus any extra keys under ``extra``)."""
    if item is None:
        return None
    if isinstance(item, str):
        return {"timestamp": "", "message": item.strip(), "level": ""} if item.strip() else None
    if not isinstance(item, dict):
        return {"timestamp": "", "message": str(item), "level": ""}
    message = ""
    for key in MESSAGE_KEYS:
        if item.get(key) not in (None, ""):
            message = str(item[key]).strip()
            break
    timestamp = ""
    for key in TIMESTAMP_KEYS:
        if item.get(key) not in (None, ""):
            timestamp = _iso(_as_utc(item[key])) or str(item[key])
            break
    level = ""
    for key in LEVEL_KEYS:
        if item.get(key) not in (None, ""):
            level = str(item[key]).strip().lower()
            break
    extra = {k: v for k, v in item.items() if k not in MESSAGE_KEYS + TIMESTAMP_KEYS + LEVEL_KEYS}
    if not message and not extra:
        return None
    entry = {"timestamp": timestamp, "message": message or json.dumps(extra, sort_keys=True, default=str), "level": level}
    if extra and message:
        entry["extra"] = extra
    return entry


def normalize_entries(value: Any) -> List[Dict[str, Any]]:
    """Accept a list, ``{"logs": [...]}``, a JSON string or a bare message; return normalised entries."""
    if value in (None, "", {}, []):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [{"timestamp": "", "message": value, "level": ""}]
    if isinstance(value, dict):
        inner = value.get("logs", value.get("entries"))
        if inner is None:
            single = normalize_entry(value)
            return [single] if single else []
        value = inner
    if not isinstance(value, (list, tuple)):
        single = normalize_entry(value)
        return [single] if single else []
    out = []
    for item in value:
        entry = normalize_entry(item)
        if entry:
            out.append(entry)
    return out


def fingerprint(instrument_id: int, start: datetime.datetime, message: str, entries: Iterable[Dict[str, Any]]) -> str:
    payload = json.dumps(
        {"i": instrument_id, "s": _iso(_as_utc(start)), "m": (message or "").strip(),
         "e": [(e.get("timestamp", ""), e.get("message", "")) for e in entries]},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def entry_span(entries: List[Dict[str, Any]]) -> Tuple[Optional[datetime.datetime], Optional[datetime.datetime]]:
    stamps = [_as_utc(e.get("timestamp")) for e in entries]
    stamps = [s for s in stamps if s]
    return (min(stamps), max(stamps)) if stamps else (None, None)


# --- CRUD ---------------------------------------------------------------------

def add_log(instrument: Instrument, user: User, *, start=None, end=None, message: str = "", entries=None,
            source: str = InstrumentLog.SOURCE_MANUAL, source_name: str = "", run=None) -> Tuple[InstrumentLog, bool]:
    """Create an :class:`InstrumentLog`; return ``(log, created)``. A duplicate fingerprint returns the existing row."""
    normalised = normalize_entries(entries)
    start_dt = _as_utc(start)
    end_dt = _as_utc(end)
    if start_dt is None:
        first, last = entry_span(normalised)
        start_dt = first or timezone.now()
        end_dt = end_dt or (last if last and last != first else None)
    if end_dt is not None and end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
    message = (message or "").strip()
    if not message and not normalised:
        raise ValueError("an instrument log needs a message or at least one entry")
    fp = fingerprint(instrument.pk, start_dt, message, normalised)
    existing = InstrumentLog.objects.filter(instrument=instrument, fingerprint=fp).first()
    if existing is not None:
        return existing, False
    row = InstrumentLog.objects.create(
        instrument=instrument, start=start_dt, end=end_dt, message=message,
        log={"logs": normalised} if normalised else {}, source=source, source_name=(source_name or "")[:64],
        fingerprint=fp, run=run, created_by=user, modified_by=user,
    )
    return row, True


def logs_between(*, instrument: Optional[Instrument] = None, telescope: Optional[Telescope] = None,
                 start=None, end=None, source: str = ""):
    """Logs overlapping ``[start, end]`` (either bound optional), newest first."""
    qs = InstrumentLog.objects.select_related("instrument", "instrument__telescope", "created_by", "run")
    if instrument is not None:
        qs = qs.filter(instrument=instrument)
    elif telescope is not None:
        qs = qs.filter(instrument__telescope=telescope)
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    if end_dt is not None:
        qs = qs.filter(start__lte=end_dt)
    if start_dt is not None:
        qs = qs.filter(Q(end__gte=start_dt) | Q(end__isnull=True, start__gte=start_dt))
    if source:
        qs = qs.filter(source=source)
    return qs


def recent_logs_for_telescope(telescope: Telescope, days: int = 7, limit: int = 50):
    since = timezone.now() - datetime.timedelta(days=days)
    return list(logs_between(telescope=telescope, start=since)[:limit])


def can_add_logs(user) -> bool:
    """Staff, or an account holding ``YSE_App.add_instrumentlog`` (facility service accounts with API tokens)."""
    if user is None or not user.is_authenticated:
        return False
    return bool(user.is_staff or user.is_superuser or user.has_perm("YSE_App.add_instrumentlog"))


# --- facility pull --------------------------------------------------------------

def pull_allocations(instrument: Instrument):
    """Active allocations on this instrument (or its telescope) whose adapter publishes instrument logs."""
    now = timezone.now()
    qs = Allocation.objects.filter(is_active=True, start_date__lte=now, end_date__gte=now).exclude(facility="")
    qs = qs.filter(Q(instrument=instrument) | Q(instrument__isnull=True, telescope=instrument.telescope))
    out = []
    for allocation in qs.select_related("telescope", "instrument", "credential").order_by("-instrument_id", "-end_date"):
        adapter = get_facility(allocation.facility)
        if adapter is not None and adapter.can("instrument_log"):
            out.append(allocation)
    return out


def instruments_with_log_source() -> List[Instrument]:
    now = timezone.now()
    allocations = Allocation.objects.filter(is_active=True, start_date__lte=now, end_date__gte=now).exclude(facility="")
    instruments: Dict[int, Instrument] = {}
    for allocation in allocations.select_related("telescope", "instrument"):
        adapter = get_facility(allocation.facility)
        if adapter is None or not adapter.can("instrument_log"):
            continue
        if allocation.instrument_id:
            instruments[allocation.instrument_id] = allocation.instrument
        else:
            for instrument in allocation.telescope.instrument_set.all():
                instruments[instrument.pk] = instrument
    return sorted(instruments.values(), key=lambda i: (i.telescope.name, i.name))


def _actor(user: Optional[User], allocation: Allocation) -> User:
    return user or allocation.modified_by or allocation.created_by


def pull_instrument_logs(instrument: Instrument, start, end, *, user: Optional[User] = None,
                         allocation: Optional[Allocation] = None) -> Dict[str, Any]:
    """Fetch the facility's entries for ``[start, end]`` and store them; returns a summary dict.

    Network work is recorded as an :class:`ExternalServiceRun` on the allocation's
    service. Without an allocation whose adapter publishes logs the result is
    ``{"skipped": "no facility"}``.
    """
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    if start_dt is None or end_dt is None:
        raise ValueError("start and end are required")
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
    if allocation is None:
        candidates = pull_allocations(instrument)
        if not candidates:
            return {"instrument": instrument.pk, "skipped": "no facility with instrument logs", "created": 0}
        allocation = candidates[0]
    adapter = get_facility(allocation.facility)
    actor = _actor(user, allocation)
    service = ensure_service(allocation, actor)
    payload = {"instrument_id": instrument.pk, "instrument": instrument.name,
               "start": _iso(start_dt), "end": _iso(end_dt), "purpose": "instrument_log"}
    run = ExternalServiceRun.objects.create(
        service=service, target=instrument, target_ref="%s instrument log" % instrument.name,
        request_payload=payload, created_by=actor, modified_by=actor,
    )
    run.mark_running()
    try:
        entries = adapter.fetch_instrument_log(allocation, instrument, start_dt, end_dt)
    except (FacilityError, NotImplementedError) as exc:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=str(exc))
        return {"instrument": instrument.pk, "run": str(run.uuid), "error": str(exc), "created": 0}
    except Exception as exc:  # noqa: BLE001 - a broken adapter must not kill the job
        log.exception("instrument log pull failed for %s", instrument)
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="%s: %s" % (type(exc).__name__, exc))
        return {"instrument": instrument.pk, "run": str(run.uuid), "error": str(exc), "created": 0}
    normalised = normalize_entries(entries)
    created = 0
    duplicates = 0
    if normalised:
        first, last = entry_span(normalised)
        row, was_created = add_log(
            instrument, actor, start=first or start_dt, end=(last if last and last != first else None) or (None if first else end_dt),
            entries=normalised, source=InstrumentLog.SOURCE_API, source_name=allocation.facility, run=run,
        )
        created += int(was_created)
        duplicates += int(not was_created)
    result = {"instrument": instrument.pk, "run": str(run.uuid), "entries": len(normalised),
              "created": created, "duplicates": duplicates, "allocation": allocation.pk}
    runs.record_completion(run, ExternalServiceRun.STATUS_SUCCEEDED, result=result)
    return result


def enqueue_pull(instrument: Optional[Instrument] = None, *, hours: Optional[int] = None, start=None, end=None,
                 user: Optional[User] = None):
    """Queue an ``instrument_logs.pull`` job (one instrument, or all with a facility when ``instrument`` is None)."""
    from YSE_App.jobs import enqueue

    payload: Dict[str, Any] = {}
    if instrument is not None:
        payload["instrument_id"] = instrument.pk
    if start is not None and end is not None:
        payload["start"], payload["end"] = _iso(_as_utc(start)), _iso(_as_utc(end))
    else:
        payload["hours"] = int(hours or getattr(settings, "INSTRUMENT_LOG_PULL_HOURS", 24) or 24)
    return enqueue(PULL_JOB_KIND, payload, created_by=user)


@job(PULL_JOB_KIND, max_attempts=1)
def pull_job(payload, job=None):
    """Job handler: pull logs for one instrument (``instrument_id``) or every instrument with a facility."""
    payload = payload or {}
    if payload.get("start") and payload.get("end"):
        start, end = _as_utc(payload["start"]), _as_utc(payload["end"])
    else:
        end = timezone.now()
        start = end - datetime.timedelta(hours=float(payload.get("hours") or getattr(settings, "INSTRUMENT_LOG_PULL_HOURS", 24)))
    if payload.get("instrument_id"):
        instruments = list(Instrument.objects.filter(pk=payload["instrument_id"]).select_related("telescope"))
    else:
        instruments = instruments_with_log_source()
    user = getattr(job, "created_by", None)
    results = [pull_instrument_logs(instrument, start, end, user=user) for instrument in instruments]
    return {"instruments": len(results), "created": sum(r.get("created", 0) for r in results),
            "errors": sum(1 for r in results if r.get("error")), "results": results}
