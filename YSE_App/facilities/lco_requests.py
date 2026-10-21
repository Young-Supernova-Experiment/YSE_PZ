"""Instrument-specific LCO request builders (#301).

``YSE_App.util.lcogt`` already builds SINISTRO imaging and FLOYDS / SOAR
Goodman spectroscopy request groups; this module wraps those builders behind
one ``build_request(instrument, ...)`` entry point and adds the LCO instruments
the observing-portal API accepts today for which YSE-PZ had no code: MuSCAT
(2m, four simultaneous channels), Spectral (2m imaging) and the 0.4m QHY
imagers. The field names follow the LCO observation-portal request-group
document (``requests[].configurations[]``). SkyPortal's ``facility_apis/lco.py``
(BSD-3-Clause) was consulted for MuSCAT's ``extra_params``; no code is copied.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Sequence

from YSE_App.facilities.base import FacilityError

#: instrument code -> (label, telescope_class, kind)
INSTRUMENTS = {
    "1M0-SCICAM-SINISTRO": ("1m Sinistro imaging", "1m0", "imaging"),
    "2M0-SCICAM-SPECTRAL": ("2m Spectral imaging", "2m0", "imaging"),
    "2M0-SCICAM-MUSCAT": ("2m MuSCAT3/4 imaging (g,r,i,z at once)", "2m0", "imaging"),
    "0M4-SCICAM-QHY600": ("0.4m QHY600 imaging", "0m4", "imaging"),
    "2M0-FLOYDS-SCICAM": ("2m FLOYDS spectroscopy", "2m0", "spectroscopy"),
    "SOAR_GHTS_REDCAM": ("SOAR Goodman red camera spectroscopy", "4m0", "spectroscopy"),
    "SOAR_GHTS_REDCAM_IMAGER": ("SOAR Goodman red camera imaging", "4m0", "imaging"),
    "SOAR_TRIPLESPEC": ("SOAR TripleSpec near-IR spectroscopy", "4m0", "spectroscopy"),
}

IMAGING_FILTERS = {
    "1M0-SCICAM-SINISTRO": ["up", "gp", "rp", "ip", "zs", "B", "V", "R", "I", "w"],
    "2M0-SCICAM-SPECTRAL": ["up", "gp", "rp", "ip", "zs", "B", "V", "R", "I", "w"],
    "0M4-SCICAM-QHY600": ["up", "gp", "rp", "ip", "zs", "B", "V", "w"],
    "SOAR_GHTS_REDCAM_IMAGER": ["u-SDSS", "g-SDSS", "r-SDSS", "i-SDSS", "z-SDSS"],
}
MUSCAT_CHANNELS = ("g", "r", "i", "z")

#: default per-instrument choices for the request form
DEFAULT_INSTRUMENT = "1M0-SCICAM-SINISTRO"


def instrument_choices(only: Sequence[str] = ()) -> List[List[str]]:
    codes = list(only) or list(INSTRUMENTS)
    return [[code, INSTRUMENTS[code][0]] for code in codes if code in INSTRUMENTS]


def kind_of(instrument: str) -> str:
    return INSTRUMENTS.get(instrument, ("", "", "imaging"))[2]


def _fmt(value) -> str:
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return str(value).replace("T", " ")[:19]


def target(name: str, ra: float, dec: float) -> Dict[str, Any]:
    return {"type": "ICRS", "name": name, "ra": float(ra), "dec": float(dec), "proper_motion_ra": 0.0,
            "proper_motion_dec": 0.0, "parallax": 0.0, "epoch": 2000.0}


def constraints(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"max_airmass": float(params.get("max_airmass") or 2.5),
            "min_lunar_distance": float(params.get("min_lunar_distance") or 15)}


def window(params: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"start": _fmt(params["start"]), "end": _fmt(params["end"])}]


def _imaging_configurations(instrument: str, filters: Sequence[str], exposure_time: float, exposure_count: int,
                            name: str, ra: float, dec: float, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if instrument == "2M0-SCICAM-MUSCAT":
        extra = {"exposure_mode": "SYNCHRONOUS"}
        for channel in MUSCAT_CHANNELS:
            extra["exposure_time_%s" % channel] = float(params.get("exposure_time_%s" % channel) or exposure_time)
        return [{
            "type": "EXPOSE", "instrument_type": instrument,
            "instrument_configs": [{
                "exposure_count": exposure_count, "exposure_time": float(exposure_time), "mode": "MUSCAT_FAST",
                "extra_params": extra,
                "optical_elements": {"diffuser_%s_position" % c: "out" for c in MUSCAT_CHANNELS},
            }],
            "acquisition_config": {"mode": "OFF"}, "guiding_config": {"mode": "ON"},
            "constraints": constraints(params), "target": target(name, ra, dec),
        }]
    allowed = IMAGING_FILTERS.get(instrument)
    chosen = [f for f in filters if not allowed or f in allowed] or (allowed[:1] if allowed else list(filters))
    if not chosen:
        raise FacilityError("no filter given for %s" % instrument)
    configs = []
    for filt in chosen:
        instrument_config = {
            "exposure_count": exposure_count, "exposure_time": float(exposure_time),
            "mode": "full_frame" if instrument == "1M0-SCICAM-SINISTRO" else "default",
            "optical_elements": {"filter": filt}, "extra_params": {},
        }
        if instrument == "1M0-SCICAM-SINISTRO":
            instrument_config["extra_params"] = {"defocus": 0.0}
        configs.append({
            "type": "EXPOSE", "instrument_type": instrument, "instrument_configs": [instrument_config],
            "acquisition_config": {"mode": "OFF"}, "guiding_config": {"mode": "ON"},
            "constraints": constraints(params), "target": target(name, ra, dec),
        })
    return configs


def build_request(instrument: str, name: str, ra: float, dec: float, params: Dict[str, Any],
                  telescope_name: str, proposal: str) -> Dict[str, Any]:
    """One ``requests[]`` element for ``instrument``; spectroscopy goes through ``YSE_App.util.lcogt``."""
    if instrument not in INSTRUMENTS:
        raise FacilityError("unknown LCO instrument %r" % instrument)
    _label, telescope_class, kind = INSTRUMENTS[instrument]
    exposure_time = float(params.get("exposure_time") or 0)
    if exposure_time <= 0:
        raise FacilityError("exposure_time must be positive")
    if kind == "spectroscopy" and instrument != "SOAR_TRIPLESPEC":
        return _spectroscopy_request(instrument, name, ra, dec, params, telescope_name, proposal)
    if instrument == "SOAR_TRIPLESPEC":
        configurations = [{
            "type": "SPECTRUM", "instrument_type": instrument,
            "instrument_configs": [{
                "exposure_count": int(params.get("exposure_count") or 1), "exposure_time": exposure_time,
                "mode": "fowler1_coadds1", "rotator_mode": "SKY", "extra_params": {"rotator_angle": 0},
                "optical_elements": {},
            }],
            "acquisition_config": {"mode": "MANUAL"}, "guiding_config": {"mode": "ON"},
            "constraints": constraints(params), "target": target(name, ra, dec),
        }]
    else:
        configurations = _imaging_configurations(instrument, list(params.get("filters") or []), exposure_time,
                                                 int(params.get("exposure_count") or 1), name, ra, dec, params)
    return {"location": {"telescope_class": telescope_class}, "configurations": configurations,
            "windows": window(params)}


def _spectroscopy_request(instrument, name, ra, dec, params, telescope_name, proposal) -> Dict[str, Any]:
    from YSE_App.util.lcogt import lcogt

    hint = "soar" if instrument.startswith("SOAR") else "faulkes"
    builder = lcogt(None, None, proposal, hint, params["start"], params["end"])
    strat = dict(builder.params["strategy"]["spectroscopy"])
    builder.params["constraints"] = constraints(params)
    requests_block = builder.make_requests(name, ra, dec, float(params["exposure_time"]), strat)
    element = requests_block[0]
    if not element.get("configurations"):
        raise FacilityError("the spectroscopy builder produced no configurations for %s" % instrument)
    element["windows"] = window(params)
    return element


def estimate_hours(instrument: str, params: Dict[str, Any]) -> float:
    """Rough charge: per-exposure overheads on top of the exposure time."""
    try:
        exposure = float(params.get("exposure_time") or 0)
    except (TypeError, ValueError):
        return 0.0
    count = int(params.get("exposure_count") or 1)
    if kind_of(instrument) == "spectroscopy":
        return round(count * (exposure + 400.0) / 3600.0, 4)
    if instrument == "2M0-SCICAM-MUSCAT":
        return round(count * (exposure + 60.0) / 3600.0, 4)
    n = len(params.get("filters") or []) or 1
    return round(n * count * (exposure + 90.0) / 3600.0, 4)
