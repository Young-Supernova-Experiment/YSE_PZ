"""``reference`` strings for public data imported from outside services (#447).

TNS and literature photometry and spectra must carry a ``reference`` (the TNS
object / report, or an ADS bibcode) on their ``TransientPhotometry`` /
``TransientSpectrum`` header, so public data can be credited without a PI.
"""

from __future__ import annotations

MAX_REFERENCE_LENGTH = 512


def tns_reference(jd) -> str:
    """``"TNS <prefix> <name>"`` for a TNS object reply (``objname``, optional ``name_prefix``), or ``""``."""
    jd = jd or {}
    name = str(jd.get("objname") or "").strip()
    if not name:
        return ""
    prefix = str(jd.get("name_prefix") or "").strip()
    return ("TNS %s %s" % (prefix, name) if prefix else "TNS %s" % name)[:MAX_REFERENCE_LENGTH]


# Keys under which a TNS get/object reply may carry the report id of a photometry point or a spectrum.
# Which of them TNS sends is not guaranteed, so all are tried and an absent id is fine.
TNS_PHOTOMETRY_REPORT_KEYS = ("report_id", "reportid", "photometry_id", "phot_id")
TNS_SPECTRUM_REPORT_KEYS = ("report_id", "reportid", "spectra_id", "spectrum_id")
MAX_REPORT_IDS = 20


def tns_report_id(item, keys) -> str:
    """The first non-empty report id found under ``keys`` in one photometry / spectrum entry, or ``""``."""
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", 0, "0"):
            return str(value).strip()
    return ""


def tns_reference_with_reports(jd, report_ids=()) -> str:
    """:func:`tns_reference` plus ``" (report 12, 13)"`` when TNS gave report ids (#645).

    The ids are kept whole: it is the object part that is cut when the column would overflow.
    """
    base = tns_reference(jd)
    ids = []
    for rid in report_ids:
        rid = str(rid or "").strip()
        if rid and rid not in ids:
            ids.append(rid)
    if not base or not ids:
        return base
    suffix = " (report %s)" % ", ".join(ids[:MAX_REPORT_IDS])
    return (base[: MAX_REFERENCE_LENGTH - len(suffix)] + suffix) if len(base) + len(suffix) > MAX_REFERENCE_LENGTH else base + suffix
