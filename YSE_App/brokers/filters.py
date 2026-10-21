"""Rule evaluator for ``BrokerFilter.criteria`` (issue #277).

A criteria document is a flat JSON object whose keys come from
:data:`CRITERIA` below; every key is optional and all present keys must pass
(logical AND). Unknown keys are rejected at save time so a typo cannot let
every alert through. ``evaluate`` works on the dict
:meth:`YSE_App.brokers.base.BrokerAlert.filter_values` produces, and returns the
list of failed keys, so the candidate page can say *why* an alert missed.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from YSE_App.brokers.base import BrokerAlert

# key -> (type, description). Types: number, integer, boolean, list, mapping.
CRITERIA: Dict[str, Tuple[str, str]] = {
    "mag_min": ("number", "latest magnitude >= (bright cut)"),
    "mag_max": ("number", "latest magnitude <= (faint cut)"),
    "bands": ("list", "latest detection band is one of these (g, r, i, ...)"),
    "rb_min": ("number", "real/bogus score >="),
    "drb_min": ("number", "deep-learning real/bogus score >="),
    "ndet_min": ("integer", "number of detections >="),
    "ndet_max": ("integer", "number of detections <="),
    "gal_lat_min": ("number", "|galactic latitude| >= degrees (avoid the plane)"),
    "dec_min": ("number", "declination >= degrees"),
    "dec_max": ("number", "declination <= degrees"),
    "age_min_days": ("number", "days since first detection >="),
    "age_max_days": ("number", "days since first detection <= (young only)"),
    "positive_only": ("boolean", "require positive difference flux"),
    "sgscore_max": ("number", "star/galaxy score of the nearest PS1 source <= (reject stars)"),
    "distpsnr1_min": ("number", "distance to the nearest PS1 source >= arcsec"),
    "classes": ("list", "broker classification is one of these labels"),
    "exclude_classes": ("list", "broker classification is none of these labels"),
    "class_probabilities": ("mapping", "{class label: minimum probability} (any one suffices)"),
}


class CriteriaError(ValueError):
    """The criteria document is malformed (unknown key or wrong type)."""


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_criteria(criteria: Optional[Dict]) -> Dict:
    """Return a normalised copy of ``criteria`` or raise :class:`CriteriaError`."""
    if criteria is None:
        return {}
    if not isinstance(criteria, dict):
        raise CriteriaError("criteria must be a JSON object")
    out = {}
    for key, value in criteria.items():
        if key not in CRITERIA:
            raise CriteriaError("unknown criteria key %r (known: %s)" % (key, ", ".join(sorted(CRITERIA))))
        kind = CRITERIA[key][0]
        if value is None:
            continue
        if kind == "number":
            if not _is_number(value):
                raise CriteriaError("%s must be a number" % key)
            out[key] = float(value)
        elif kind == "integer":
            if not _is_number(value) or int(value) != value:
                raise CriteriaError("%s must be an integer" % key)
            out[key] = int(value)
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise CriteriaError("%s must be true or false" % key)
            out[key] = value
        elif kind == "list":
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
                raise CriteriaError("%s must be a list of strings" % key)
            out[key] = [str(v) for v in value]
        elif kind == "mapping":
            if not isinstance(value, dict) or not all(_is_number(v) for v in value.values()):
                raise CriteriaError("%s must map class labels to numbers" % key)
            out[key] = {str(k): float(v) for k, v in value.items()}
    if "mag_min" in out and "mag_max" in out and out["mag_min"] > out["mag_max"]:
        raise CriteriaError("mag_min is fainter than mag_max")
    return out


def _lower_set(values: Iterable[str]) -> set:
    return {str(v).strip().lower() for v in values}


def failed_keys(criteria: Dict, values: Dict) -> List[str]:
    """Criteria keys the alert ``values`` fail (empty list = pass).

    A missing value fails a numeric cut (an alert without an rb score does
    not pass ``rb_min``), except ``gal_lat_min`` / age cuts when the position
    or discovery time is unknown, which also fail: the filter asked for it.
    """
    failed = []
    mag = values.get("mag")
    if "mag_min" in criteria and (mag is None or mag < criteria["mag_min"]):
        failed.append("mag_min")
    if "mag_max" in criteria and (mag is None or mag > criteria["mag_max"]):
        failed.append("mag_max")
    if "bands" in criteria and (values.get("band") or "").lower() not in _lower_set(criteria["bands"]):
        failed.append("bands")
    for key, field in (("rb_min", "rb"), ("drb_min", "drb"), ("distpsnr1_min", "distpsnr1"),
                       ("dec_min", "dec"), ("age_min_days", "age_days"), ("ndet_min", "ndet")):
        if key in criteria:
            v = values.get(field)
            if v is None or v < criteria[key]:
                failed.append(key)
    for key, field in (("sgscore_max", "sgscore"), ("dec_max", "dec"),
                       ("age_max_days", "age_days"), ("ndet_max", "ndet")):
        if key in criteria:
            v = values.get(field)
            if v is None or v > criteria[key]:
                failed.append(key)
    if "gal_lat_min" in criteria:
        b = values.get("gal_b")
        if b is None or abs(b) < criteria["gal_lat_min"]:
            failed.append("gal_lat_min")
    if criteria.get("positive_only") and values.get("is_positive") is not True:
        failed.append("positive_only")
    label = (values.get("classification") or "").strip().lower()
    if "classes" in criteria and label not in _lower_set(criteria["classes"]):
        failed.append("classes")
    if "exclude_classes" in criteria and label in _lower_set(criteria["exclude_classes"]):
        failed.append("exclude_classes")
    if "class_probabilities" in criteria:
        probs = {str(k).lower(): v for k, v in (values.get("class_probabilities") or {}).items()}
        wanted = criteria["class_probabilities"]
        if not any(probs.get(k.lower()) is not None and probs[k.lower()] >= v for k, v in wanted.items()):
            failed.append("class_probabilities")
    return failed


def evaluate(criteria: Dict, alert: BrokerAlert, *, now_mjd: Optional[float] = None) -> Tuple[bool, List[str]]:
    """``(passed, failed_keys)`` for one alert."""
    failed = failed_keys(criteria or {}, alert.filter_values(now_mjd))
    return (not failed, failed)


def describe_criteria() -> List[Dict]:
    return [{"key": k, "type": t, "description": d} for k, (t, d) in CRITERIA.items()]
