"""The change log: one ``ValueChange`` row per real change of a value (#737).

Every automatic writer records through :func:`record_change` (redshift writes do it
inside :func:`redshift_precedence.apply_redshift`, overrides inside
:func:`redshift_precedence.set_override` / ``clear_override`` and the API through
:func:`record_redshift_edit`, other hand edits through :func:`record_edit`), and the
upload endpoints (``add_transient`` and the host util, including the TNS cron's Prost
hosts) through :func:`record_upload` with the uploader as actor. A value and its
uncertainty go through :func:`record_value`: a change of the uncertainty alone is an
entry of its own. A write that changes nothing records nothing.

Writers that record an ``ExternalServiceRun`` only after the work is done wrap the work
in :func:`collecting` and then :func:`attach_run` the collected rows to the run.

Reading: :func:`visible_changes` applies the visibility rules (see
``models.change_log_models``) and :func:`as_dict` is the one output shape (page and API).
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, List, Optional

from django.db import DatabaseError, transaction
from django.db.models import Q

from YSE_App.models.change_log_models import ValueChange

log = logging.getLogger(__name__)

SERVICE_API = "api"
SERVICE_MANUAL = "manual"
SERVICE_COMMAND = "redshift_override command"
SERVICE_UPLOAD = "upload"  # the upload endpoints (add_transient, host and GW uploads); actor = the uploader
SERVICE_HERMES = "hermes"
MANUAL_SERVICES = frozenset({SERVICE_API, SERVICE_MANUAL, SERVICE_COMMAND})

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

_collectors: contextvars.ContextVar = contextvars.ContextVar("yse_change_log_collectors", default=())


# --- writing --------------------------------------------------------------------

def _target(obj):
    """``("transient" | "host", pk)`` for a saved Transient or Host, else None."""
    from YSE_App.models.host_models import Host
    from YSE_App.models.transient_models import Transient

    if getattr(obj, "pk", None) is None:
        return None
    if isinstance(obj, Transient):
        return "transient"
    if isinstance(obj, Host):
        return "host"
    return None


def as_text(value) -> Optional[str]:
    """How a value is stored: None stays None, numbers keep full precision, models by name."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    name = getattr(value, "name", None)
    if name is not None and not isinstance(value, str):
        return str(name)[:255]
    return str(value)[:255]


def _same(old, new, tolerance: float) -> bool:
    if old is None or new is None:
        return old is None and new is None
    try:
        return abs(float(old) - float(new)) <= tolerance
    except (TypeError, ValueError):
        return as_text(old) == as_text(new)


def is_restricted(source: Optional[str], service: str) -> bool:
    """Whether a change may derive from restricted data: a hand entry, or our own spectra."""
    from YSE_App.services import redshift_precedence as rp

    if service in MANUAL_SERVICES:
        return True
    if service == SERVICE_UPLOAD and not (source or "").strip():
        return True  # an upload without a source: provenance unknown, maybe our own data
    return bool((source or "").strip()) and rp.source_rank(source) == rp.RANK_OWN


def record_change(obj, field: str, old, new, *, service: str, category: str, source: Optional[str] = "",
                  old_source: Optional[str] = "", actor=None, run=None, restricted: Optional[bool] = None,
                  note: str = "", tolerance: float = 1e-9, force: bool = False) -> Optional[ValueChange]:
    """Record one change of ``obj.<field>`` (``obj`` a saved Transient or Host); None when nothing changed.

    ``actor`` None means the system. ``force`` skips the no-change check (the caller made
    it). Never raises: a failure to record is logged and the writer carries on (the value
    itself is already saved).
    """
    target = _target(obj)
    if target is None:
        return None
    source, old_source = (source or "")[:64], (old_source or "")[:64]
    if not force and _same(old, new, tolerance) and source == old_source:
        return None
    if restricted is None:
        restricted = is_restricted(source, service)
    row = ValueChange(category=category, field=field[:64], old_value=as_text(old), new_value=as_text(new),
                      old_source=old_source, source=source, service=(service or "")[:64], run=run,
                      actor=actor if getattr(actor, "pk", None) else None, restricted=bool(restricted),
                      note=(note or "")[:255], **{target: obj})
    try:
        with transaction.atomic():
            row.save()
    except DatabaseError:
        log.exception("change log: could not record %s %s.%s", target, obj.pk, field)
        return None
    for bucket in _collectors.get():
        bucket.append(row)
    return row


def record_value(obj, field: str, err_field: Optional[str], old, new, old_err, new_err, *, service: str,
                 category: str, source: Optional[str] = "", old_source: Optional[str] = "",
                 compare_source: bool = True, tolerance: float = 1e-9, **kwargs) -> Optional[ValueChange]:
    """A value and its uncertainty: one entry for the value (or its source), else one for the error alone.

    ``compare_source=False`` for fields without a stored source (``source`` then only labels
    the entry). Nothing is recorded when value, error and source are all unchanged.
    """
    source, old_source = source or "", old_source or ""
    value_changed = not _same(old, new, tolerance)
    source_changed = compare_source and source != old_source
    if not compare_source:
        old_source = ""
    if value_changed or source_changed:
        return record_change(obj, field, old, new, service=service, category=category, source=source,
                             old_source=old_source, tolerance=tolerance, force=True, **kwargs)
    if err_field and not _same(old_err, new_err, tolerance):
        return record_change(obj, err_field, old_err, new_err, service=service, category=category, source=source,
                             old_source=old_source, tolerance=tolerance, force=True,
                             note=kwargs.pop("note", "") or "uncertainty only", **kwargs)
    return None


# (field, error field, source field, category) recorded for uploads, per target
_UPLOAD_FIELDS = {
    "transient": (
        ("redshift", "redshift_err", "redshift_source", ValueChange.CATEGORY_REDSHIFT),
        ("TNS_spec_class", None, None, ValueChange.CATEGORY_CLASSIFICATION),
        ("best_spec_class", None, None, ValueChange.CATEGORY_CLASSIFICATION),
    ),
    "host": (
        ("redshift", "redshift_err", "redshift_source", ValueChange.CATEGORY_REDSHIFT),
        ("photo_z", "photo_z_err", "photo_z_source", ValueChange.CATEGORY_PHOTO_Z),
        ("photo_z_internal", "photo_z_err_internal", None, ValueChange.CATEGORY_PHOTO_Z),
    ),
}


def record_upload(obj, fields: dict, *, actor=None, service: str = SERVICE_UPLOAD, source_hint: str = "",
                  created: bool = False) -> List[ValueChange]:
    """Record what an upload's ``fields`` change on ``obj`` (read ``obj`` *before* the update is applied).

    ``created``: ``obj`` was just created from ``fields``, so every given value is new.
    ``source_hint`` labels fields that carry no source of their own (a classification from
    the TNS cron: ``TNS``; a Prost host: ``Prost``).
    """
    target = _target(obj)
    if target is None or not fields:
        return []
    out = []
    for field, err_field, source_field, category in _UPLOAD_FIELDS[target]:
        if field not in fields and (err_field is None or err_field not in fields):
            continue
        old = None if created else getattr(obj, field, None)
        old_err = None if (created or not err_field) else getattr(obj, err_field, None)
        new = fields.get(field, old)
        new_err = fields.get(err_field, old_err) if err_field else None
        if source_field:
            old_source = "" if created else (getattr(obj, source_field, None) or "")
            source = (fields.get(source_field) or "") if source_field in fields else (
                source_hint if created else old_source)
        else:
            old_source = source = source_hint
        if created and new is None and new_err is None:
            continue
        out.append(record_value(obj, field, err_field, old, new, old_err, new_err, service=service,
                                category=category, source=source, old_source=old_source,
                                compare_source=bool(source_field), actor=actor))
    return [c for c in out if c is not None]


_EDIT_ATTRS = {
    "transient": ("TNS_spec_class", "best_spec_class_id", "host_id"),
    "host": ("photo_z", "photo_z_err", "photo_z_source", "photo_z_internal", "photo_z_err_internal"),
}


def edit_state(obj) -> dict:
    """The classification / host-link / photo-z values of ``obj`` before a hand edit (see :func:`record_edit`)."""
    target = _target(obj)
    return {a: getattr(obj, a, None) for a in _EDIT_ATTRS.get(target, ())} if target else {}


def _class_name(class_id) -> Optional[str]:
    if not class_id:
        return None
    from YSE_App.models.enum_models import TransientClass

    return TransientClass.objects.filter(pk=class_id).values_list("name", flat=True).first()


def record_edit(obj, before: dict, *, actor=None, service: str = SERVICE_API) -> List[ValueChange]:
    """Record a hand edit (API) of the classification, host link or photo-z (redshift: :func:`record_redshift_edit`)."""
    target = _target(obj)
    if target is None or not before:
        return []
    after = edit_state(obj)
    out = []
    kw = dict(service=service, actor=actor, restricted=True)
    if target == "transient":
        if (before["TNS_spec_class"] or "") != (after["TNS_spec_class"] or ""):
            out.append(record_change(obj, "TNS_spec_class", before["TNS_spec_class"], after["TNS_spec_class"],
                                     category=ValueChange.CATEGORY_CLASSIFICATION, force=True, **kw))
        if before["best_spec_class_id"] != after["best_spec_class_id"]:
            out.append(record_change(obj, "best_spec_class", _class_name(before["best_spec_class_id"]),
                                     _class_name(after["best_spec_class_id"]),
                                     category=ValueChange.CATEGORY_CLASSIFICATION, force=True, **kw))
        if before["host_id"] != after["host_id"]:
            out.append(record_change(obj, "host", host_label(before["host_id"]), host_label(after["host_id"]),
                                     category=ValueChange.CATEGORY_HOST, force=True, **kw))
    else:
        out.append(record_value(obj, "photo_z", "photo_z_err", before["photo_z"], after["photo_z"],
                                before["photo_z_err"], after["photo_z_err"], category=ValueChange.CATEGORY_PHOTO_Z,
                                source=after["photo_z_source"], old_source=before["photo_z_source"], **kw))
        out.append(record_value(obj, "photo_z_internal", "photo_z_err_internal", before["photo_z_internal"],
                                after["photo_z_internal"], before["photo_z_err_internal"],
                                after["photo_z_err_internal"], category=ValueChange.CATEGORY_PHOTO_Z,
                                compare_source=False, **kw))
    return [c for c in out if c is not None]


def host_label(host_id) -> Optional[str]:
    """``"<name> (host <id>)"`` for a host link entry (None for no host)."""
    if not host_id:
        return None
    from YSE_App.models.host_models import Host

    name = Host.objects.filter(pk=host_id).values_list("name", flat=True).first()
    return "%s (host %s)" % (name or "", host_id)


@contextmanager
def collecting():
    """Collect the rows recorded inside the block (nested blocks each get them)."""
    bucket: List[ValueChange] = []
    token = _collectors.set(_collectors.get() + (bucket,))
    try:
        yield bucket
    finally:
        _collectors.reset(token)


def attach_run(changes: Iterable[ValueChange], run) -> int:
    """Point the collected rows that have no run yet at ``run``."""
    pks = [c.pk for c in changes if c.pk and c.run_id is None]
    if run is None or not pks:
        return 0
    for c in changes:
        if c.pk in pks:
            c.run = run
    return ValueChange.objects.filter(pk__in=pks, run__isnull=True).update(run=run)


# --- redshift edits by hand (override, API) ----------------------------------------

@dataclass(frozen=True)
class RedshiftState:
    value: Optional[float]
    error: Optional[float]
    source: str
    override: bool


def redshift_state(obj) -> RedshiftState:
    return RedshiftState(getattr(obj, "redshift", None), getattr(obj, "redshift_err", None),
                         getattr(obj, "redshift_source", None) or "", bool(getattr(obj, "redshift_override", False)))


def record_redshift_edit(obj, before: RedshiftState, *, actor=None, service: str = SERVICE_MANUAL) -> List[ValueChange]:
    """Record a hand edit of ``obj``'s redshift: override set or cleared, or a new value."""
    after = redshift_state(obj)
    out = []
    if before.override and not after.override:
        out.append(record_change(obj, "redshift_override", True, False, service=service, actor=actor,
                                 category=ValueChange.CATEGORY_OVERRIDE, source=after.source,
                                 old_source=before.source, restricted=True, note="override cleared"))
    value_changed = not _same(before.value, after.value, 1e-9) or before.source != after.source
    if after.override and (not before.override or value_changed):
        out.append(record_change(obj, "redshift", before.value, after.value, service=service, actor=actor,
                                 category=ValueChange.CATEGORY_OVERRIDE, source=after.source,
                                 old_source=before.source, restricted=True,
                                 note="override set" if not before.override else "override changed")
                   or record_change(obj, "redshift_override", before.override, True, service=service,
                                    actor=actor, category=ValueChange.CATEGORY_OVERRIDE, source=after.source,
                                    old_source=before.source, restricted=True, note="override set"))
    elif value_changed:
        out.append(record_change(obj, "redshift", before.value, after.value, service=service, actor=actor,
                                 category=ValueChange.CATEGORY_REDSHIFT, source=after.source,
                                 old_source=before.source, restricted=True))
    return [c for c in out if c is not None]


# --- reading --------------------------------------------------------------------

def visible_changes(user, transient):
    """The change log of ``transient`` and its host that ``user`` may see (newest first).

    Nothing unless the transient is listed for ``user``; restricted entries only when
    ``user`` may see the transient's data.
    """
    from YSE_App.services.visibility import filter_transients_by_data_access, user_can_list_transient

    if transient is None or not user_can_list_transient(user, transient.pk):
        return ValueChange.objects.none()
    where = Q(transient_id=transient.pk)
    if transient.host_id:
        where |= Q(host_id=transient.host_id)
    qs = ValueChange.objects.filter(where)
    if not filter_transients_by_data_access(user, [transient]):
        qs = qs.filter(restricted=False)
    return qs.select_related("actor", "run").order_by("-created_at", "-pk")


def clamp_limit(raw, default: int = DEFAULT_LIMIT) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, value))


def as_dict(change: ValueChange) -> dict:
    return {
        "id": change.pk,
        "created_at": change.created_at.isoformat() if change.created_at else None,
        "target": "transient" if change.transient_id else "host",
        "transient_id": change.transient_id,
        "host_id": change.host_id,
        "category": change.category,
        "field": change.field,
        "old_value": change.old_value,
        "new_value": change.new_value,
        "old_source": change.old_source,
        "source": change.source,
        "service": change.service,
        "run": str(change.run.uuid) if change.run_id else None,
        "actor": change.actor_name,  # username only, or "system"
        "note": change.note,
    }
