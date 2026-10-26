"""One source vocabulary and one precedence list for redshifts (#436, #437).

Ryan's rules (2026-10-18), shared by hosts (``Host.redshift``) and transients
(``Transient.redshift``):

* **Override first.** A redshift set by hand with ``redshift_override`` True wins over
  every catalogue and is never overwritten by a later query or upload.
* **Then by source:** NED > DESI > our own (YSE spectra, values entered by hand) >
  other catalogues > TNS. A value with no recorded source (everything written before
  provenance existed) ranks below our own and above TNS, so NED and DESI replace stale
  GHOST/legacy values, while TNS never replaces a value it did not write.
* **Photo-z only when there is no spec-z:** a photometric source ranks below every
  spectroscopic one (and below an unsourced value), so it fills only an empty field.
* **Every value records its source** in ``redshift_source``.

Every automatic writer (NED, DESI, the TNS re-sync, the upload endpoints) asks
:func:`can_replace` before writing and writes through :func:`apply_redshift`, which also
records the change in the change log (``services.change_log``, #737).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.conf import settings

# --- vocabulary ----------------------------------------------------------------

SOURCE_NED = "NED"
SOURCE_DESI = "DESI"  # the label written is DESI_SOURCE_LABEL (e.g. "DESI DR1")
SOURCE_YSE = "YSE spectrum"  # our own classification / host spectra
SOURCE_MANUAL = "manual"  # entered by hand (with or without the override flag)
SOURCE_PROST = "Prost"
SOURCE_SHERLOCK = "Sherlock"
SOURCE_HERMES = "Hermes"
SOURCE_ANTARES = "ANTARES"  # ANTARES catalogue matches, written as "ANTARES <catalogue>"
SOURCE_TNS = "TNS"

RANK_NED = 60
RANK_DESI = 50
RANK_OWN = 40
RANK_CATALOGUE = 30
RANK_UNKNOWN = 25  # no recorded source (legacy rows)
RANK_TNS = 10
RANK_PHOTO_Z = 0

# Exact (case-insensitive) labels; anything else is an "other catalogue".
_RANKS = {
    SOURCE_NED.lower(): RANK_NED,
    SOURCE_YSE.lower(): RANK_OWN,
    "yse": RANK_OWN,
    SOURCE_MANUAL.lower(): RANK_OWN,
    SOURCE_TNS.lower(): RANK_TNS,
}

#: Highest first; the table shown in docs/redshift-precedence.md.
PRECEDENCE = (
    ("override", "redshift_override set (by hand)", None),
    (SOURCE_NED, "NED (NED::API)", RANK_NED),
    (SOURCE_DESI + " <release>", "DESI via Astro Data Lab", RANK_DESI),
    (SOURCE_YSE + " / " + SOURCE_MANUAL, "our own spectra; values entered by hand", RANK_OWN),
    ("other", "Prost, Sherlock, Hermes, ANTARES <catalogue>, ...", RANK_CATALOGUE),
    ("(blank)", "no recorded source (legacy)", RANK_UNKNOWN),
    (SOURCE_TNS, "TNS", RANK_TNS),
    ("... photo-z", "any photometric redshift", RANK_PHOTO_Z),
)


def desi_label() -> str:
    """The source label for DESI values, e.g. ``DESI DR1`` (``[redshifts] DESI_SOURCE_LABEL``)."""
    return str(getattr(settings, "DESI_SOURCE_LABEL", "") or "DESI DR1").strip()


def is_photometric(source: Optional[str]) -> bool:
    text = (source or "").strip().lower()
    return "photo" in text or "phot-z" in text or "photz" in text


def source_rank(source: Optional[str]) -> int:
    """The rank of a ``redshift_source`` label (higher wins)."""
    text = (source or "").strip()
    if not text:
        return RANK_UNKNOWN
    if is_photometric(text):
        return RANK_PHOTO_Z
    low = text.lower()
    if low in _RANKS:
        return _RANKS[low]
    if low == "desi" or low.startswith("desi "):
        return RANK_DESI
    return RANK_CATALOGUE


def is_override(obj) -> bool:
    return bool(getattr(obj, "redshift_override", False))


# --- decisions -----------------------------------------------------------------

@dataclass
class Decision:
    write: bool
    reason: str
    old: Optional[float] = None
    old_source: Optional[str] = None


def decide(obj, new_value, new_source: Optional[str], *, tolerance: float = 1e-6) -> Decision:
    """Whether an automatic writer with ``new_source`` may set ``obj.redshift`` to ``new_value``.

    ``obj`` is a Host or a Transient (anything with ``redshift``, ``redshift_source`` and
    ``redshift_override``). Equal ranks replace (a catalogue refreshes its own value).
    """
    old = getattr(obj, "redshift", None)
    old_source = getattr(obj, "redshift_source", None)
    if new_value is None:
        return Decision(False, "no value", old, old_source)
    if is_override(obj):
        return Decision(False, "override", old, old_source)
    if old is None:
        return Decision(True, "empty", old, old_source)
    same_value = abs(float(old) - float(new_value)) <= tolerance
    if same_value and (old_source or "") == (new_source or ""):
        return Decision(False, "unchanged", old, old_source)
    if source_rank(new_source) < source_rank(old_source):
        return Decision(False, "kept higher-ranked %s" % (old_source or "unsourced value"), old, old_source)
    return Decision(True, "replaces %s" % (old_source or "unsourced value"), old, old_source)


def can_replace(obj, new_source: Optional[str], new_value=0.0) -> bool:
    return decide(obj, new_value, new_source).write


def apply_redshift(obj, value, error, source: Optional[str], *, user=None, save: bool = True,
                   service: str = "") -> Decision:
    """Set ``redshift``/``redshift_err``/``redshift_source`` on ``obj`` if precedence allows.

    A saved write is recorded in the change log (#737) as one entry by ``service`` (the
    writer's ExternalService slug; default: the source label), with the system as actor.
    """
    decision = decide(obj, value, source)
    if not decision.write:
        return decision
    obj.redshift = float(value)
    obj.redshift_err = float(error) if error is not None else None
    obj.redshift_source = (source or None) and str(source)[:64]
    fields = ["redshift", "redshift_err", "redshift_source"]
    if user is not None and hasattr(obj, "modified_by"):
        obj.modified_by = user
        fields.append("modified_by")
    if save:
        if hasattr(obj, "modified_date"):
            fields.append("modified_date")
        obj.save(update_fields=fields)
        from YSE_App.services import change_log

        change_log.record_change(obj, "redshift", decision.old, obj.redshift, service=service or (source or ""),
                                 category="redshift", source=obj.redshift_source, old_source=decision.old_source,
                                 note=decision.reason)
    return decision


def set_override(obj, value, error=None, *, source: str = SOURCE_MANUAL, user=None, save: bool = True,
                 actor=None, service: str = "manual"):
    """Set a redshift by hand and mark it as an override (wins over every catalogue).

    Saved, it is recorded in the change log (#737) with ``actor`` (None: unknown/system).
    """
    from YSE_App.services import change_log

    before = change_log.redshift_state(obj)
    obj.redshift = None if value is None else float(value)
    obj.redshift_err = None if error is None else float(error)
    obj.redshift_source = (source or SOURCE_MANUAL)[:64]
    obj.redshift_override = value is not None
    if user is not None and hasattr(obj, "modified_by"):
        obj.modified_by = user
    if save:
        obj.save()
        change_log.record_redshift_edit(obj, before, actor=actor, service=service)
    return obj


def clear_override(obj, *, user=None, save: bool = True, actor=None, service: str = "manual"):
    """Drop the override flag; the value stays (as ``manual``) until a catalogue outranks it."""
    from YSE_App.services import change_log

    before = change_log.redshift_state(obj)
    obj.redshift_override = False
    if user is not None and hasattr(obj, "modified_by"):
        obj.modified_by = user
    if save:
        obj.save()
        change_log.record_redshift_edit(obj, before, actor=actor, service=service)
    return obj


def filter_upload_fields(existing, fields: dict) -> dict:
    """Guard for the upload endpoints (``add_transient``, host uploads): drop the redshift keys
    from ``fields`` when ``existing`` outranks them, and record the incoming source otherwise.

    An upload without ``redshift_source`` is treated as unsourced (the legacy behaviour:
    it replaces other unsourced values and TNS values, never an override or a catalogue).
    """
    fields.pop("redshift_override", None)  # only a person sets the override (API, admin, command)
    if existing is None or "redshift" not in fields:
        return fields
    source = fields.get("redshift_source") or None
    value, old = fields.get("redshift"), getattr(existing, "redshift", None)
    same_unsourced = (source is None and value is not None and old is not None
                      and abs(float(value) - float(old)) <= 1e-6)
    if same_unsourced or not decide(existing, value, source).write:
        for key in ("redshift", "redshift_err", "redshift_source"):
            fields.pop(key, None)
        return fields
    fields["redshift_source"] = source
    return fields


# --- reading -------------------------------------------------------------------

@dataclass
class BestRedshift:
    value: Optional[float]
    error: Optional[float] = None
    source: Optional[str] = None
    origin: str = ""  # "transient" | "host" | "host photo-z" | ""
    override: bool = False

    @property
    def kind(self) -> str:
        """Short source label for display (#832): ``phot``, ``host`` or ``spec``; ``""`` if unknown."""
        if self.value is None or not self.origin:
            return ""
        if self.origin == "host photo-z" or is_photometric(self.source):
            return "phot"
        if self.origin == "host":
            return "host"
        return "spec"

    @property
    def kind_title(self) -> str:
        return {
            "phot": "Photometric redshift",
            "host": "Spectroscopic redshift of the host galaxy",
            "spec": "Spectroscopic redshift of the transient",
        }.get(self.kind, "")


def best_redshift(transient) -> BestRedshift:
    """The redshift to use for ``transient`` under the one precedence list.

    An override (transient, then host) wins; otherwise the higher-ranked of the
    transient's and the host's spec-z (the transient's on a tie); a host photo-z only
    when neither has a spec-z.
    """
    host = getattr(transient, "host", None)
    candidates = []
    if transient.redshift is not None:
        candidates.append((is_override(transient), source_rank(transient.redshift_source), 1,
                           BestRedshift(transient.redshift, transient.redshift_err, transient.redshift_source,
                                        "transient", is_override(transient))))
    if host is not None and host.redshift is not None:
        candidates.append((is_override(host), source_rank(getattr(host, "redshift_source", None)), 0,
                           BestRedshift(host.redshift, host.redshift_err, getattr(host, "redshift_source", None),
                                        "host", is_override(host))))
    if candidates:
        return max(candidates, key=lambda c: c[:3])[3]
    if host is not None and host.photo_z is not None:
        return BestRedshift(host.photo_z, host.photo_z_err, host.photo_z_source or "photo-z", "host photo-z")
    return BestRedshift(None)
