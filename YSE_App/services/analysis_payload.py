"""Package a transient's data for an analysis service (#313).

:func:`build_payload` returns the JSON document both runner styles receive:
in-process runners get it as a dict, webhook services as the POST body.
Only data the requesting user may see is included (the same authorization
as the detail page: ``PhotometryService`` / ``SpectraService``), and only
the sections the service's ``input_spec`` asks for.

Shape (every key present, empty when not requested)::

    {
      "transient": {"id", "name", "ra", "dec", "redshift", "redshift_source", "mw_ebv",
                    "disc_date", "status", "spec_class", "host": {...} | null},
      "redshift": 0.031 | null,
      "photometry": [{"mjd", "instrument", "band", "sncosmo_band", "mag", "mag_err",
                      "flux", "flux_err", "zp", "zpsys", "upper_limit", "flagged"}],
      "spectra": [{"id", "mjd", "instrument", "obs_group", "n_points",
                   "wavelength": [...], "flux": [...], "flux_err": [...] | null}],
      "photstat": {...} | null,
      "params": {...}            # the run's request parameters
    }

Fluxes are on the 27.5 zero point used by the light-curve plots and the
Bazin fit (``services.bazin.FLUX_ZERO_POINT``); when a row has only a
magnitude the flux is derived from it. Rows without a magnitude but with a
flux and flux error are upper limits when ``flux / flux_err < 3`` (the
detail plot's rule). ``sncosmo_band`` comes from ``common.bandpassdict``
(``"Band: <instrument> - <band>"`` -> sncosmo bandpass name) and is ``null``
for bands it does not know.
"""

from __future__ import annotations

import datetime
import math
from typing import Dict, List, Optional

from YSE_App.common.bandpassdict import bandpassdict
from YSE_App.services import bazin

FLUX_ZERO_POINT = bazin.FLUX_ZERO_POINT
UPPER_LIMIT_SNR = 3.0
MAX_SPECTRUM_POINTS = 20000


def _finite(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mjd_of(value) -> Optional[float]:
    if value is None:
        return None
    return round(bazin.datetime_to_mjd(value), 5)


def sncosmo_band_name(instrument_name: str, band_name: str) -> Optional[str]:
    """The sncosmo bandpass registered for ``Band: <instrument> - <band>`` in ``bandpassdict``."""
    return bandpassdict.get("Band: %s - %s" % (instrument_name, band_name))


def redshift_for(transient):
    """``(z, source)``: the transient's own redshift, else the host's, else ``(None, "")``."""
    z = _finite(getattr(transient, "redshift", None))
    if z is not None:
        return z, "transient"
    host = getattr(transient, "host", None)
    if host is not None:
        z = _finite(getattr(host, "redshift", None))
        if z is not None:
            return z, "host"
    return None, ""


def transient_section(transient) -> Dict:
    z, z_source = redshift_for(transient)
    host = getattr(transient, "host", None)
    host_section = None
    if host is not None:
        host_section = {
            "id": host.pk,
            "name": getattr(host, "name", None),
            "ra": _finite(getattr(host, "ra", None)),
            "dec": _finite(getattr(host, "dec", None)),
            "redshift": _finite(getattr(host, "redshift", None)),
            "photo_z": _finite(getattr(host, "photo_z", None)),
        }
    disc = getattr(transient, "disc_date", None)
    return {
        "id": transient.pk,
        "name": transient.name,
        "ra": _finite(transient.ra),
        "dec": _finite(transient.dec),
        "redshift": z,
        "redshift_source": z_source,
        "mw_ebv": _finite(getattr(transient, "mw_ebv", None)),
        "disc_date": disc.isoformat() if isinstance(disc, (datetime.date, datetime.datetime)) else None,
        "status": str(transient.status) if getattr(transient, "status_id", None) else None,
        "spec_class": str(transient.best_spec_class) if getattr(transient, "best_spec_class_id", None) else None,
        "host": host_section,
    }


def photometry_rows(user, transient) -> List[Dict]:
    """Every photometry point the user may see, oldest first."""
    from YSE_App.data import PhotometryService

    rows = (
        PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(user, transient.pk, includeBadData=True)
        .select_related("band", "band__instrument", "mag_sys")
        .prefetch_related("data_quality")
        .order_by("obs_date")
    )
    out = []
    for p in rows:
        band = p.band
        instrument = band.instrument.name if band.instrument_id else ""
        mag = _finite(p.mag)
        mag_err = _finite(p.mag_err)
        flux = _finite(p.flux)
        flux_err = _finite(p.flux_err)
        zp = _finite(p.flux_zero_point) if p.flux_zero_point is not None else None
        if flux is not None and zp is not None and zp != FLUX_ZERO_POINT:
            scale = 10.0 ** (0.4 * (FLUX_ZERO_POINT - zp))
            flux *= scale
            if flux_err is not None:
                flux_err *= scale
        if flux is None and mag is not None:
            flux = float(bazin.mag_to_flux(mag))
            if mag_err is not None:
                flux_err = float(bazin.flux_err_from_mag_err(flux, max(mag_err, bazin.MAG_ERR_FLOOR)))
        upper_limit = mag is None and flux is not None and flux_err not in (None, 0) and flux / flux_err < UPPER_LIMIT_SNR
        flagged = bool(p.data_quality.all())
        sncosmo_band = sncosmo_band_name(instrument, band.name)
        out.append({
            "id": p.pk,
            "mjd": mjd_of(p.obs_date),
            "instrument": instrument,
            "band": band.name,
            "sncosmo_band": sncosmo_band,
            "mag": mag,
            "mag_err": mag_err,
            "flux": flux,
            "flux_err": flux_err,
            "zp": FLUX_ZERO_POINT,
            "zpsys": "vega" if sncosmo_band and "bessell" in sncosmo_band else "ab",
            "mag_sys": p.mag_sys.name if p.mag_sys_id else None,
            "upper_limit": bool(upper_limit),
            "flagged": flagged,
        })
    return out


def spectra_rows(user, transient, *, include_data: bool = True) -> List[Dict]:
    from YSE_App.data import SpectraService
    from YSE_App.models import TransientSpecData

    spectra = (
        SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(user, transient.pk, includeBadData=True)
        .select_related("instrument", "obs_group")
        .order_by("obs_date")
    )
    out = []
    for spec in spectra:
        entry = {
            "id": spec.pk,
            "mjd": mjd_of(spec.obs_date),
            "instrument": spec.instrument.name if spec.instrument_id else "",
            "obs_group": spec.obs_group.name if spec.obs_group_id else "",
            "redshift": _finite(spec.redshift),
            "n_points": 0,
            "wavelength": [],
            "flux": [],
            "flux_err": None,
        }
        if include_data:
            points = list(
                TransientSpecData.objects.filter(spectrum=spec)
                .order_by("wavelength")
                .values_list("wavelength", "flux", "flux_err")[:MAX_SPECTRUM_POINTS]
            )
            entry["n_points"] = len(points)
            entry["wavelength"] = [_finite(w) for w, _f, _e in points]
            entry["flux"] = [_finite(f) for _w, f, _e in points]
            errs = [_finite(e) for _w, _f, e in points]
            entry["flux_err"] = errs if any(e is not None for e in errs) else None
        out.append(entry)
    return out


def photstat_section(transient) -> Optional[Dict]:
    """The PhotStat row (#268) as plain numbers, or None without one."""
    stat = getattr(transient, "photstat", None)
    if stat is None:
        try:
            from YSE_App.models import TransientPhotStat

            stat = TransientPhotStat.objects.filter(transient=transient).first()
        except Exception:  # pragma: no cover - model absent
            return None
    if stat is None:
        return None
    out = {}
    for field in stat._meta.fields:
        if field.name in ("id", "transient", "created_by", "modified_by"):
            continue
        value = getattr(stat, field.name, None)
        if isinstance(value, (datetime.date, datetime.datetime)):
            value = value.isoformat()
        elif value is not None and not isinstance(value, (int, float, str, bool)):
            value = str(value)
        out[field.name] = value
    return out


def build_payload(transient, user, input_spec=None, params=None) -> Dict:
    """The document an analysis service receives; see the module docstring."""
    spec = list(input_spec or ("photometry", "redshift"))
    want_all = "all" in spec
    z, _source = redshift_for(transient)
    payload = {
        "transient": transient_section(transient),
        "redshift": z,
        "photometry": [],
        "spectra": [],
        "photstat": None,
        "params": dict(params or {}),
    }
    if want_all or "photometry" in spec:
        payload["photometry"] = photometry_rows(user, transient)
        payload["photstat"] = photstat_section(transient)
    if want_all or "spectra" in spec:
        payload["spectra"] = spectra_rows(user, transient)
    return payload


def detections(payload: Dict, *, drop_flagged: bool = True) -> List[Dict]:
    """Photometry rows usable for a fit: a magnitude and error, not an upper limit, not flagged."""
    out = []
    for row in payload.get("photometry") or []:
        if row.get("upper_limit") or row.get("mag") is None or row.get("mag_err") is None:
            continue
        if drop_flagged and row.get("flagged"):
            continue
        if not bazin.is_usable_detection(row.get("mag"), row.get("mag_err"), row.get("flux"), row.get("flux_err")):
            continue
        out.append(row)
    return out
