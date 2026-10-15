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
