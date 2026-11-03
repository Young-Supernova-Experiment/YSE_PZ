"""Fit a subset of the light curve to recreate earlier circumstances (#847).

A Bazin or SALT run may carry ``params["selection"]``: which of the photometry points the
fit uses. The analysis payload (:mod:`YSE_App.services.analysis_payload`) applies it to the
points the requesting user may see, so a subset never adds points beyond that user's access.
The fit tables (#846) store it as ``selection`` with ``is_subset=True``; subset runs never
supply default values and never move the spectral phases (#848).

The stored (canonical) selection, every key optional, at least one present::

    {"until_mjd": 60500.25,              # points observed at or before this MJD
     "bands": ["g", "r"],                 # band names
     "telescopes": ["Pan-STARRS1"],       # telescope names
     "quality": ["best", "method"]}       # quality levels (QUALITY_LEVELS keys)

:func:`normalize_selection` accepts what the form sends (``until`` as an MJD or a UTC
``YYYYMMDD.ddd``, lists or comma-separated strings) and returns that canonical form, so a
stored selection passed back ("Repeat this fit") normalizes to itself.
"""

from __future__ import annotations

import datetime
import math
from typing import Dict, List, Optional

from YSE_App.common import quality_mask as qm

#: Photometry quality levels, best first: ``(key, label, upper bound of the mask)``. A point's
#: level is the first whose bound its mask (without NOT_PUBLISHED, which is not a quality
#: issue) is below; the group boundaries are those of ``common.quality_mask``.
QUALITY_LEVELS = (
    ("best", "Best (no caveat)", qm.PHOT_BEST),
    ("method", "Measurement-method caveat (aperture, not forced, ...)", qm.PHOT_REDUCED),
    ("reduction", "Reduction-stage issue (seeing, template, ...)", qm.PHOT_SCIENCE),
    ("quicklook", "Quicklook, blended or contaminated", qm.PHOT_CLEAN),
    ("artefact", "Image artefact, calibration or other issue", qm.USABLE),
    ("rejected", "Rejected", None),
)
QUALITY_KEYS = tuple(key for key, _label, _bound in QUALITY_LEVELS)
QUALITY_LABELS = {key: label for key, label, _bound in QUALITY_LEVELS}

KEYS = ("until_mjd", "bands", "telescopes", "quality")
MAX_ITEMS = 50
MAX_NAME = 64
MJD_MIN, MJD_MAX = 0.0, 100000.0
#: ``until`` values at or above this are a UTC ``YYYYMMDD.ddd``, below it an MJD.
DATE_FORM_MIN = 10_000_000

_MJD_EPOCH = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)


class SelectionError(ValueError):
    """The selection cannot be used (bad date, unknown quality level, ...)."""


def quality_level(mask: Optional[int]) -> str:
    """The quality level key of a photometry point's ``quality_mask``."""
    value = int(mask or 0) & ~int(qm.PhotFlag.NOT_PUBLISHED)
    for key, _label, bound in QUALITY_LEVELS:
        if bound is None or value < bound:
            return key
    return QUALITY_KEYS[-1]


def parse_until(value) -> Optional[float]:
    """An MJD from an MJD (``60500.25``) or a UTC ``YYYYMMDD.ddd`` (``20240715.25``); None when blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise SelectionError("the date must be an MJD or a UTC date as YYYYMMDD.ddd")
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        raise SelectionError("the date must be an MJD or a UTC date as YYYYMMDD.ddd") from None
    if not math.isfinite(number):
        raise SelectionError("the date must be finite")
    if number >= DATE_FORM_MIN:
        day = int(math.floor(number))
        try:
            midnight = datetime.datetime.strptime("%08d" % day, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            raise SelectionError("%s is not a UTC date as YYYYMMDD.ddd" % text) from None
        number = (midnight - _MJD_EPOCH).total_seconds() / 86400.0 + (number - day)
    if not MJD_MIN < number < MJD_MAX:
        raise SelectionError("the date is out of range")
    return round(number, 5)


def _names(value, what: str) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        raise SelectionError("%s must be a list" % what)
    out = set()
    for item in value:
        if item is None:
            continue
        name = str(item).strip()
        if not name:
            continue
        if len(name) > MAX_NAME:
            raise SelectionError("%s: a name is too long" % what)
        out.add(name)
    if len(out) > MAX_ITEMS:
        raise SelectionError("%s: at most %d" % (what, MAX_ITEMS))
    return sorted(out)


def normalize_selection(raw) -> Optional[Dict]:
    """The canonical selection of ``raw``, or None for "no selection" (None, ``""``, ``{}``).

    Raises :class:`SelectionError` for anything unusable, including a selection that names
    keys but restricts nothing (that would silently become a full fit, which is the default).
    """
    if raw is None or raw == "" or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise SelectionError("the selection must be an object")
    unknown = set(raw) - set(KEYS) - {"until"}
    if unknown:
        raise SelectionError("unknown selection key(s): %s" % ", ".join(sorted(str(k) for k in unknown)))
    out: Dict = {}
    until = parse_until(raw.get("until")) if "until" in raw else None
    if until is None and "until_mjd" in raw:
        until = parse_until(raw.get("until_mjd"))
    if until is not None:
        out["until_mjd"] = until
    for key, what in (("bands", "bands"), ("telescopes", "telescopes")):
        names = _names(raw.get(key), what)
        if names:
            out[key] = names
    quality = _names(raw.get("quality"), "quality")
    bad = [q for q in quality if q not in QUALITY_KEYS]
    if bad:
        raise SelectionError("unknown quality level(s): %s" % ", ".join(bad))
    if quality:
        out["quality"] = [key for key in QUALITY_KEYS if key in quality]
    if not out:
        raise SelectionError("choose a date, or at least one band, telescope or quality level")
    return out


def selection_from_params(params) -> Optional[Dict]:
    """The canonical selection in a run's parameters (raises :class:`SelectionError` when unusable)."""
    if not isinstance(params, dict):
        return None
    return normalize_selection(params.get("selection"))


def point_matches(selection: Optional[Dict], *, mjd, band: str, telescope: str, mask) -> bool:
    """Whether a photometry point is in ``selection`` (always True without one)."""
    if not selection:
        return True
    until = selection.get("until_mjd")
    if until is not None:
        if mjd is None or not float(mjd) <= float(until):
            return False
    bands = selection.get("bands")
    if bands and band not in bands:
        return False
    telescopes = selection.get("telescopes")
    if telescopes and telescope not in telescopes:
        return False
    quality = selection.get("quality")
    if quality and quality_level(mask) not in quality:
        return False
    return True


def _utc(mjd: float) -> str:
    from YSE_App.common.utc_dates import utc_day

    return utc_day(_MJD_EPOCH + datetime.timedelta(days=float(mjd)))


def summary(selection: Optional[Dict]) -> str:
    """One line for the fit history, e.g. ``points to MJD 60500.250 (20240715.250 UTC); bands g, r``."""
    if not selection:
        return ""
    parts = []
    until = selection.get("until_mjd")
    if until is not None:
        parts.append("points to MJD %.3f (%s UTC)" % (float(until), _utc(until)))
    if selection.get("bands"):
        parts.append("bands " + ", ".join(selection["bands"]))
    if selection.get("telescopes"):
        parts.append("telescopes " + ", ".join(selection["telescopes"]))
    if selection.get("quality"):
        parts.append("quality " + ", ".join(QUALITY_LABELS.get(q, q).split(" (")[0].lower()
                                            for q in selection["quality"]))
    return "; ".join(parts)


def options(user, transient) -> Dict[str, List]:
    """What the form offers: the bands, telescopes and quality levels of the points ``user`` may see."""
    from YSE_App.data import PhotometryService

    rows = (PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(user, transient.pk, includeBadData=True)
            .values_list("band__name", "band__instrument__telescope__name", "quality_mask"))
    bands, telescopes, levels = set(), set(), set()
    for band, telescope, mask in rows:
        if band:
            bands.add(band)
        if telescope:
            telescopes.add(telescope)
        levels.add(quality_level(mask))
    return {
        "bands": sorted(bands),
        "telescopes": sorted(telescopes),
        "quality": [(key, label) for key, label, _bound in QUALITY_LEVELS if key in levels],
    }



def as_of_mjd(params, now_mjd: float) -> float:
    """The "today" of a fit: ``now_mjd``, or the subset's date cut when that is earlier (#847).

    A fit to the points up to a date reports its phase and "today" magnitude as of that date.
    """
    selection = params.get("selection") if isinstance(params, dict) else None
    until = selection.get("until_mjd") if isinstance(selection, dict) else None
    try:
        until = float(until)
    except (TypeError, ValueError):
        return now_mjd
    return min(now_mjd, until) if math.isfinite(until) else now_mjd
