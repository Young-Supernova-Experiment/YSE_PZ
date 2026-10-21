"""TNS reports: payload builders, API client and the submit / poll jobs (#326).

Payloads follow the TNS bulk-report API schema (``at_report`` for discoveries,
``classification_report`` for classifications; see the TNS "Bulk reports"
manual). Nothing here talks to TNS at import time; every request goes through
:class:`TNSClient`, whose base URL is the sandbox while
``SharingService.testing`` is on.

Flow::

    submission = create_submission(service, transient, KIND_DISCOVERY, user, coauthors=...)
    # -> pending row + a ``sharing.submit`` job
    # submit job: (upload spectrum) -> POST bulk-report -> status submitted, external_id = report_id
    #             -> ``sharing.poll`` job
    # poll job:   POST bulk-report-reply -> accepted (TNS name recorded on the transient) or rejected

Instrument and filter ids are TNS's numeric ids; the defaults below cover the
YSE instruments and can be overridden per service in ``SharingService.config``
(``instrument_ids``, ``filter_ids``). Check unknown ids against the TNS
"values" tables before reporting from a new instrument.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import math
import os
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from YSE_App.jobs import JobFailed, JobRetry, enqueue, job
from YSE_App.models.log_models import Log
from YSE_App.models.phot_models import TransientPhotData
from YSE_App.models.sharing_models import SharingService, SharingSubmission, is_tns_name
from YSE_App.models.spectra_models import TransientSpecData, TransientSpectrum
from YSE_App.models.transient_models import AlternateTransientNames, Transient

log = logging.getLogger(__name__)

SUBMIT_KIND = "sharing.submit"
POLL_KIND = "sharing.poll"

KIND_DISCOVERY = SharingSubmission.KIND_DISCOVERY
KIND_CLASSIFICATION = SharingSubmission.KIND_CLASSIFICATION
KIND_HERMES = SharingSubmission.KIND_HERMES

# Bulk-report API paths (relative to the API root, e.g. https://sandbox.wis-tns.org/api).
BULK_REPORT = "bulk-report"
BULK_REPORT_REPLY = "bulk-report-reply"
FILE_UPLOAD = "set/file-upload"
SEARCH = "get/search"
GET_OBJECT = "get/object"

# TNS flux units: 1 = AB magnitude. AT types: 1 = PSN (probable supernova).
FLUX_UNITS_ABMAG = "1"
AT_TYPE_PSN = "1"
# Spectrum type ids in classification reports: 10 = object.
SPEC_TYPE_OBJECT = "10"
# Feedback codes that carry an object name and mean the report went in.
ACCEPTED_CODES = ("100", "101")
# TNS's own "report not processed yet" code on bulk-report-reply.
NOT_READY_CODE = 404

# Default TNS instrument ids by YSE ``Instrument.name`` (override per service via config.instrument_ids).
DEFAULT_INSTRUMENT_IDS: Dict[str, int] = {
    "GPC1": 155,
    "GPC2": 156,
    "ZTF-Cam": 196,
    "DECam": 172,
    "ATLAS-01": 159,
    "ATLAS-02": 160,
    "ACAM1": 159,
    "Other": 0,
}

# Default TNS filter ids. Keys are "<instrument>:<band>" (most specific) or "<band>".
DEFAULT_FILTER_IDS: Dict[str, int] = {
    # Sloan-like (DECam and generic)
    "u": 20, "g": 21, "r": 22, "i": 23, "z": 24,
    # Pan-STARRS
    "GPC1:g": 56, "GPC1:r": 57, "GPC1:i": 58, "GPC1:z": 59, "GPC1:y": 60, "GPC1:w": 26,
    "GPC2:g": 56, "GPC2:r": 57, "GPC2:i": 58, "GPC2:z": 59, "GPC2:y": 60, "GPC2:w": 26,
    # ZTF
    "ZTF-Cam:g": 110, "ZTF-Cam:r": 111, "ZTF-Cam:i": 112,
    # ATLAS
    "c": 71, "o": 72, "cyan": 71, "orange": 72,
    # Johnson / Cousins
    "U": 9, "B": 10, "V": 11, "R": 12, "I": 13,
    "Clear": 1, "clear": 1,
    "Other": 0,
}

# TNS object type ids by classification name (case-insensitive lookup via _norm).
TNS_OBJECT_TYPE_IDS: Dict[str, int] = {
    "Other": 0, "SN": 1, "SN I": 2, "SN Ia": 3, "SN Ib": 4, "SN Ic": 5, "SN Ib/c": 6, "SN Ic-BL": 7,
    "SN Ib-Ca-rich": 8, "SN Ibn": 9, "SN II": 10, "SN IIP": 11, "SN IIL": 12, "SN IIn": 13, "SN IIb": 14,
    "SN I-faint": 15, "SN I-rapid": 16, "SLSN-I": 18, "SLSN-II": 19, "SLSN-R": 20, "Afterglow": 23,
    "LBV": 24, "ILRT": 25, "Nova": 26, "CV": 27, "Varstar": 28, "AGN": 29, "Galaxy": 30, "QSO": 31,
    "Light-Echo": 40, "Std-spec": 50, "Gap": 60, "Gap I": 61, "Gap II": 62, "LRN": 65, "FBOT": 66,
    "Kilonova": 70, "Impostor-SN": 99, "SN Ia-pec": 100, "SN Ia-SC": 102, "SN Ia-91bg-like": 103,
    "SN Ia-91T-like": 104, "SN Iax[02cx-like]": 105, "SN Ia-CSM": 106, "SN Ib-pec": 107, "SN Ic-pec": 108,
    "SN Icn": 109, "SN Ibn/Icn": 110, "SN II-pec": 111, "SN IIn-pec": 112, "TDE": 120, "TDE-H": 121,
    "TDE-He": 122, "TDE-H-He": 123, "WR": 200, "WR-WN": 201, "WR-WC": 202, "WR-WO": 203, "M dwarf": 210,
}
# YSE class names that differ from TNS spellings.
CLASS_ALIASES = {
    "sn iax": "SN Iax[02cx-like]", "sn ia-91bg": "SN Ia-91bg-like", "sn ia-91t": "SN Ia-91T-like",
    "sn ia 91bg-like": "SN Ia-91bg-like", "sn ia 91t-like": "SN Ia-91T-like", "sn ii-p": "SN IIP",
    "sn ii-l": "SN IIL", "sn iip": "SN IIP", "sn iil": "SN IIL", "slsn": "SLSN-I", "sn ia-csm": "SN Ia-CSM",
    "sn ic-bl": "SN Ic-BL", "sn ibc": "SN Ib/c", "tde": "TDE", "agn": "AGN", "cv": "CV", "nova": "Nova",
}


class SharingError(Exception):
    """Base class for sharing errors."""


class PayloadError(SharingError):
    """The report cannot be built from the data we have (shown in the preview)."""


class TNSError(SharingError):
    """TNS answered with an error we should not retry blindly."""

    def __init__(self, message, response=None, status_code=None):
        super().__init__(message)
        self.response = response
        self.status_code = status_code


class TNSTransportError(TNSError):
    """Network or 5xx problem: worth a retry."""


class TNSRateLimited(TNSTransportError):
    """HTTP 429: wait ``retry_after`` seconds."""

    def __init__(self, message, retry_after=60, response=None):
        super().__init__(message, response=response, status_code=429)
        self.retry_after = retry_after


# --- small helpers ------------------------------------------------------------

def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


def _norm(name: str) -> str:
    return " ".join(str(name or "").strip().lower().replace("_", " ").split())


def _finite(value) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def tns_datetime(dt: datetime.datetime) -> str:
    """TNS accepts ``YYYY-MM-DD HH:MM:SS`` (UTC)."""
    if timezone.is_aware(dt):
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def sexagesimal(ra: float, dec: float) -> Tuple[str, str]:
    from YSE_App.common.utilities import GetSexigesimalString

    return GetSexigesimalString(ra, dec)


def object_type_id(class_name: str) -> Optional[int]:
    """TNS ``objtypeid`` for a YSE classification name, or ``None``."""
    if not class_name:
        return None
    key = _norm(class_name)
    if key in CLASS_ALIASES:
        return TNS_OBJECT_TYPE_IDS[CLASS_ALIASES[key]]
    by_norm = {_norm(k): v for k, v in TNS_OBJECT_TYPE_IDS.items()}
    if key in by_norm:
        return by_norm[key]
    if key.startswith("sn ") and key[3:] in by_norm:
        return by_norm[key[3:]]
    return None


def instrument_id(service: SharingService, instrument_name: str) -> Optional[int]:
    ids = dict(DEFAULT_INSTRUMENT_IDS)
    ids.update({str(k): v for k, v in (service.config_value("instrument_ids", {}) or {}).items()})
    value = ids.get(instrument_name)
    return None if value is None else int(value)


def filter_id(service: SharingService, instrument_name: str, band_name: str) -> Optional[int]:
    ids = dict(DEFAULT_FILTER_IDS)
    ids.update({str(k): v for k, v in (service.config_value("filter_ids", {}) or {}).items()})
    band = (band_name or "").strip()
    for key in ("%s:%s" % (instrument_name, band), band, band.split("-")[0]):
        if key in ids:
            return int(ids[key])
    return None


def tns_name_for(transient: Transient) -> str:
    """The transient's TNS designation (its name or an alternate name), or ''."""
    if is_tns_name(transient.name):
        return transient.name.strip()
    alt = AlternateTransientNames.objects.filter(
        transient=transient, name__regex=r"^20[0-9]{2}[a-zA-Z]{1,5}$").values_list("name", flat=True).first()
    return alt or ""


def system_user() -> Optional[User]:
    """Actor for rows created by rules and jobs (SHARING_SYSTEM_USERNAME, else a superuser)."""
    username = _setting("SHARING_SYSTEM_USERNAME", "")
    user = User.objects.filter(username=username).first() if username else None
    return user or User.objects.filter(is_superuser=True).order_by("pk").first()


# --- photometry selection -----------------------------------------------------

def _photometry_rows(service: SharingService, transient: Transient):
    qs = (
        TransientPhotData.objects.filter(photometry__transient=transient)
        .select_related("photometry__instrument", "photometry__obs_group", "band")
        .annotate(n_dq=Count("data_quality"))
        .order_by("obs_date")
    )
    allowed_inst = service.allowed_instrument_ids()
    allowed_groups = service.allowed_obs_group_ids()
    if allowed_inst:
        qs = qs.filter(photometry__instrument_id__in=allowed_inst)
    if allowed_groups:
        qs = qs.filter(photometry__obs_group_id__in=allowed_groups)
    return [p for p in qs if not p.n_dq]


def _limiting_mag(point) -> Optional[float]:
    """The pre-detection upper limit of a non-detection row, or None (same rule as PhotStat)."""
    if _finite(point.mag) is not None:
        return None
    flux, flux_err, zp = _finite(point.flux), _finite(point.flux_err), _finite(point.flux_zero_point)
    if flux is None or zp is None or not flux:
        return None
    flux_err = flux_err or 0.0
    if flux + 3.0 * flux_err <= 0:
        return None
    return -2.5 * math.log10(flux + 3.0 * flux_err) + zp


def select_photometry(service: SharingService, transient: Transient, max_points: Optional[int] = None):
    """``(detections, last_pre_detection_limit)`` from the allowed instruments/groups.

    Detections are unflagged rows with a magnitude, oldest first; the first one
    is the discovery point (a row flagged ``discovery_point`` wins when set).
    The limit is the latest non-detection *before* the discovery point (limits
    during the decline are not reported).
    """
    rows = _photometry_rows(service, transient)
    detections = [p for p in rows if _finite(p.mag) is not None]
    if not detections:
        return [], None
    flagged = [p for p in detections if p.discovery_point]
    discovery = min(flagged or detections, key=lambda p: p.obs_date)
    others = sorted((p for p in detections if p.pk != discovery.pk and p.obs_date >= discovery.obs_date),
                    key=lambda p: p.obs_date)
    max_points = int(max_points or service.config_value("max_photometry_points", 3) or 3)
    chosen = [discovery] + others[: max(0, max_points - 1)]
    limits = [(p, _limiting_mag(p)) for p in rows if p.obs_date < discovery.obs_date]
    limits = [(p, lim) for p, lim in limits if lim is not None]
    last_limit = max(limits, key=lambda t: t[0].obs_date) if limits else None
    return chosen, last_limit


# --- payload builders ---------------------------------------------------------

def _phot_entry(service, point, *, limiting=False) -> dict:
    inst_name = point.photometry.instrument.name
    inst_id = instrument_id(service, inst_name)
    filt_id = filter_id(service, inst_name, point.band.name)
    missing = []
    if inst_id is None:
        missing.append("instrument '%s'" % inst_name)
    if filt_id is None:
        missing.append("filter '%s' of %s" % (point.band.name, inst_name))
    if missing:
        raise PayloadError(
            "No TNS id for %s; add it to the service's config.instrument_ids / config.filter_ids." % " and ".join(missing)
        )
    observer = service.config_value("observer", "") or service.tns_group_name or ""
    entry = {
        "obsdate": tns_datetime(point.obs_date),
        "flux": "" if limiting else "%.3f" % float(point.mag),
        "flux_error": "" if limiting or _finite(point.mag_err) is None else "%.3f" % float(point.mag_err),
        "limiting_flux": "",
        "flux_units": FLUX_UNITS_ABMAG,
        "filter_value": str(filt_id),
        "instrument_value": str(inst_id),
        "exptime": str(service.config_value("exptime", "") or ""),
        "observer": observer,
        "comments": "",
    }
    return entry


def build_at_report(service: SharingService, transient: Transient, *, coauthors: str = "",
                    remarks: str = "", internal_name: str = "", max_points: Optional[int] = None) -> dict:
    """A TNS ``at_report`` (discovery report) for ``transient``.

    Raises :class:`PayloadError` when the service has no reporting group, the
    transient has no reportable detection, or an instrument/filter has no TNS id.
    """
    if service.kind != SharingService.KIND_TNS:
        raise PayloadError("%s is not a TNS service." % service.name)
    if not service.tns_group_id:
        raise PayloadError("%s has no TNS reporting group id." % service.name)
    if is_tns_name(transient.name):
        raise PayloadError("%s already carries a TNS name." % transient.name)
    detections, last_limit = select_photometry(service, transient, max_points=max_points)
    if not detections:
        raise PayloadError("No unflagged detection from the instruments this service may report.")
    discovery = detections[0]
    ra_str, dec_str = sexagesimal(transient.ra, transient.dec)
    host = transient.host
    host_z = _finite(getattr(host, "redshift", None)) if host is not None else None
    period_days = service.config_value("proprietary_period_days", 0) or 0
    report = {
        "ra": {"value": ra_str, "error": "", "units": "arcsec"},
        "dec": {"value": dec_str, "error": "", "units": "arcsec"},
        "reporting_group_id": str(service.tns_group_id),
        "discovery_data_source_id": str(service.config_value("discovery_data_source_id", service.tns_group_id)),
        "reporter": service.reporter_string(coauthors),
        "discovery_datetime": tns_datetime(discovery.obs_date),
        "at_type": str(service.config_value("at_type", AT_TYPE_PSN)),
        "host_name": (getattr(host, "name", "") or "") if host is not None else "",
        "host_redshift": "" if host_z is None else "%.5f" % host_z,
        "transient_redshift": "" if _finite(transient.redshift) is None else "%.5f" % float(transient.redshift),
        "internal_name": internal_name or transient.name,
        "remarks": (remarks or service.default_remarks or "").strip(),
        "proprietary_period_groups": [],
        "proprietary_period": {"proprietary_period_value": str(int(period_days)), "proprietary_period_units": "days"},
        "photometry": {"photometry_group": {
            str(i): _phot_entry(service, p) for i, p in enumerate(detections)
        }},
    }
    if last_limit is not None:
        point, lim = last_limit
        entry = _phot_entry(service, point, limiting=True)
        entry.update({"limiting_flux": "%.2f" % lim, "archiveid": "", "archival_remarks": ""})
        report["non_detection"] = entry
    elif transient.non_detect_date and _finite(transient.non_detect_limit) is not None \
            and transient.non_detect_date < discovery.obs_date and transient.non_detect_band_id:
        band = transient.non_detect_band
        inst_name = band.instrument.name
        inst_id, filt_id = instrument_id(service, inst_name), filter_id(service, inst_name, band.name)
        if inst_id is None or filt_id is None:
            raise PayloadError("No TNS id for the non-detection band %s of %s." % (band.name, inst_name))
        report["non_detection"] = {
            "obsdate": tns_datetime(transient.non_detect_date),
            "limiting_flux": "%.2f" % float(transient.non_detect_limit),
            "flux_units": FLUX_UNITS_ABMAG,
            "filter_value": str(filt_id), "instrument_value": str(inst_id),
            "exptime": "", "observer": service.tns_group_name or "", "comments": "",
            "archiveid": "", "archival_remarks": "",
        }
    else:
        report["non_detection"] = {
            "archiveid": "0",
            "archival_remarks": str(service.config_value("archival_remarks", "Other") or "Other"),
        }
    return {"at_report": {"0": report}}


def spectrum_ascii(spectrum: TransientSpectrum) -> Optional[bytes]:
    """The spectrum as two/three-column ASCII: from the SpecData rows, else the data file on disk."""
    rows = list(TransientSpecData.objects.filter(spectrum=spectrum).order_by("wavelength")
                .values_list("wavelength", "flux", "flux_err"))
    if rows:
        buf = io.StringIO()
        buf.write("# wavelength flux flux_err\n")
        for wl, flux, err in rows:
            buf.write("%.4f %.6e%s\n" % (wl, flux, "" if err is None else " %.6e" % err))
        return buf.getvalue().encode()
    path = spectrum.spec_data_file or ""
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    return None


def build_classification_report(service: SharingService, transient: Transient, *, spectrum: TransientSpectrum,
                                classification: str = "", redshift=None, coauthors: str = "",
                                remarks: str = "") -> dict:
    """A TNS ``classification_report`` for ``transient`` based on ``spectrum``.

    The report carries ``_yse.spectrum_id`` (stripped before sending) so the
    submit job can upload the spectrum and fill ``ascii_file``.
    """
    if service.kind != SharingService.KIND_TNS:
        raise PayloadError("%s is not a TNS service." % service.name)
    if not service.tns_group_id:
        raise PayloadError("%s has no TNS reporting group id." % service.name)
    name = tns_name_for(transient)
    if not name:
        raise PayloadError("%s has no TNS name yet; report the discovery first (or run TNS retrieval)." % transient.name)
    if spectrum is None or spectrum.transient_id != transient.pk:
        raise PayloadError("A spectrum of this transient is required for a classification report.")
    if not service.instrument_allowed(spectrum.instrument):
        raise PayloadError("Spectra from %s may not be reported through %s." % (spectrum.instrument.name, service.name))
    allowed_groups = service.allowed_obs_group_ids()
    if allowed_groups and spectrum.obs_group_id not in allowed_groups:
        raise PayloadError("Spectra of observation group %s may not be reported through %s."
                           % (spectrum.obs_group.name, service.name))
    class_name = classification or (transient.best_spec_class.name if transient.best_spec_class_id else "")
    type_id = object_type_id(class_name)
    if type_id is None:
        raise PayloadError("Classification '%s' has no TNS object type id." % (class_name or "(none)"))
    inst_id = instrument_id(service, spectrum.instrument.name)
    if inst_id is None:
        raise PayloadError("No TNS id for instrument '%s'; add it to config.instrument_ids." % spectrum.instrument.name)
    z = _finite(redshift)
    if z is None:
        z = _finite(spectrum.redshift)
    if z is None:
        z = _finite(transient.redshift)
    report = {
        "name": name,
        "classifier": service.reporter_string(coauthors),
        "objtypeid": str(type_id),
        "redshift": "" if z is None else "%.5f" % z,
        "groupid": str(service.tns_group_id),
        "remarks": (remarks or service.default_remarks or "").strip(),
        "spectra": {"spectra-group": {"0": {
            "obsdate": tns_datetime(spectrum.obs_date),
            "instrumentid": str(inst_id),
            "exptime": "",
            "observer": service.tns_group_name or "",
            "reducer": "",
            "specTypeid": SPEC_TYPE_OBJECT,
            "ascii_file": "",
            "fits_file": "",
            "remarks": (spectrum.spectrum_notes or "")[:500],
            "spec_proprietary_period": str(int(service.config_value("spec_proprietary_period_days", 0) or 0)),
        }}},
    }
    return {"classification_report": {"0": report}, "_yse": {"spectrum_id": spectrum.pk, "classification": class_name}}


def build_payload(service: SharingService, transient: Transient, kind: str, **options) -> dict:
    if kind == KIND_DISCOVERY:
        return build_at_report(service, transient, coauthors=options.get("coauthors", ""),
                               remarks=options.get("remarks", ""), internal_name=options.get("internal_name", ""))
    if kind == KIND_CLASSIFICATION:
        return build_classification_report(
            service, transient, spectrum=options.get("spectrum"), classification=options.get("classification", ""),
            redshift=options.get("redshift"), coauthors=options.get("coauthors", ""), remarks=options.get("remarks", ""),
        )
    if kind == KIND_HERMES:
        return {"hermes": {"transient": transient.name, "topic": service.hermes_topic,
                           "remarks": options.get("remarks", "")}}
    raise PayloadError("Unknown report kind %r" % kind)


def strip_private(payload: dict) -> dict:
    """The payload without our bookkeeping keys (what actually goes to TNS)."""
    return {k: v for k, v in (payload or {}).items() if not str(k).startswith("_")}


# --- the client ---------------------------------------------------------------

class TNSClient:
    """Thin wrapper over the TNS API (form-encoded POSTs with the tns_marker User-Agent)."""

    def __init__(self, base_url: str, api_key: str, bot_id, bot_name: str, *, marker_type: str = "bot",
                 timeout: Optional[float] = None):
        from YSE_App.data_ingest.tns_api_client import tns_marker_user_agent

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.timeout = float(timeout or _setting("TNS_HTTP_TIMEOUT_SECONDS", 60))
        self.headers = {"User-Agent": tns_marker_user_agent(bot_id, bot_name, marker_type)}

    @classmethod
    def for_service(cls, service: SharingService, touch: bool = True) -> "TNSClient":
        if not service.has_credential:
            raise TNSError("%s has no active credential." % service.name)
        secret = service.credential.get_secret(touch=touch)
        api_key = secret.get("tns_api_key") or secret.get("api_key")
        bot_id = secret.get("tns_bot_id") or secret.get("bot_id")
        bot_name = secret.get("tns_bot_name") or secret.get("bot_name")
        if not (api_key and bot_id and bot_name):
            raise TNSError("%s: the credential must hold tns_bot_id, tns_bot_name and tns_api_key." % service.name)
        return cls(service.api_base_url(), api_key, bot_id, bot_name,
                   marker_type=secret.get("marker_type") or "bot")

    def url(self, path: str) -> str:
        return "%s/%s" % (self.base_url, path.lstrip("/"))

    def _post(self, path: str, data: Optional[dict] = None, files=None, *, allow_404: bool = False) -> dict:
        payload = {"api_key": self.api_key}
        payload.update(data or {})
        try:
            response = requests.post(self.url(path), headers=self.headers, data=payload, files=files,
                                     timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TNSTransportError("TNS unreachable: %s" % exc) from exc
        except requests.RequestException as exc:
            raise TNSTransportError("TNS request failed: %s" % exc) from exc
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {"raw": (response.text or "")[:2000]}
        if status == 429:
            reset = response.headers.get("x-rate-limit-reset") or response.headers.get("Retry-After") or "60"
            try:
                retry_after = max(5, int(float(reset)))
            except ValueError:
                retry_after = 60
            raise TNSRateLimited("TNS rate limit reached; retry in %ds" % retry_after, retry_after=retry_after,
                                 response=body)
        if status >= 500:
            raise TNSTransportError("TNS HTTP %d" % status, response=body, status_code=status)
        if status == 404 and allow_404:
            return body if isinstance(body, dict) else {"id_code": 404, "raw": body}
        if status >= 400:
            message = ""
            if isinstance(body, dict):
                message = body.get("id_message") or json.dumps(body)[:500]
            raise TNSError("TNS HTTP %d: %s" % (status, message), response=body, status_code=status)
        return body if isinstance(body, dict) else {"data": body}

    def send_bulk_report(self, report: dict) -> dict:
        return self._post(BULK_REPORT, {"data": json.dumps(strip_private(report))})

    def bulk_report_reply(self, report_id) -> dict:
        return self._post(BULK_REPORT_REPLY, {"report_id": str(report_id)}, allow_404=True)

    def upload_file(self, filename: str, content: bytes) -> str:
        reply = self._post(FILE_UPLOAD, files={"files[0]": (filename, content)})
        names = reply.get("data") or []
        if isinstance(names, dict):
            names = list(names.values())
        if not names:
            raise TNSError("TNS file upload returned no file name", response=reply)
        return str(names[0])

    def search(self, ra: float, dec: float, radius_arcsec: float = 3.0, **extra) -> List[dict]:
        query = {"ra": "%.6f" % ra, "dec": "%.6f" % dec, "radius": str(radius_arcsec), "units": "arcsec",
                 "objname": "", "internal_name": ""}
        query.update(extra)
        reply = self._post(SEARCH, {"data": json.dumps(query)})
        data = reply.get("data")
        if isinstance(data, dict):
            data = data.get("reply", [])
        return [d for d in (data or []) if isinstance(d, dict)]

    def get_object(self, objname: str, photometry: bool = False, spectra: bool = False) -> dict:
        query = {"objname": objname, "photometry": "1" if photometry else "0", "spectra": "1" if spectra else "0"}
        reply = self._post(GET_OBJECT, {"data": json.dumps(query)})
        data = reply.get("data")
        if isinstance(data, dict) and "reply" in data:
            data = data["reply"]
        return data if isinstance(data, dict) else {}


# --- reply parsing ------------------------------------------------------------

class Outcome:
    def __init__(self):
        self.accepted = False
        self.objname = ""
        self.prefix = ""
        self.messages: List[str] = []

    def summary(self) -> str:
        return "; ".join(m for m in self.messages if m) or ("accepted" if self.accepted else "rejected")


def _walk(node, outcome: Outcome):
    if isinstance(node, dict):
        if "objname" in node and (node.get("objname") or "").strip():
            outcome.accepted = True
            outcome.objname = str(node["objname"]).strip()
            outcome.prefix = str(node.get("prefix") or "")
            if node.get("message"):
                outcome.messages.append(str(node["message"]))
            return
        for key, value in node.items():
            if key == "message" and isinstance(value, str):
                outcome.messages.append(value)
            elif isinstance(value, (dict, list)):
                _walk(value, outcome)
    elif isinstance(node, list):
        for item in node:
            _walk(item, outcome)


def parse_feedback(reply: dict, kind: str) -> Outcome:
    """Read a bulk-report-reply: accepted when a feedback entry carries an object name."""
    outcome = Outcome()
    data = reply.get("data") if isinstance(reply, dict) else None
    feedback = (data or {}).get("feedback") if isinstance(data, dict) else None
    key = "classification_report" if kind == KIND_CLASSIFICATION else "at_report"
    if isinstance(feedback, dict):
        _walk(feedback.get(key, feedback), outcome)
    if not outcome.accepted and not outcome.messages and isinstance(reply, dict):
        msg = reply.get("id_message")
        if msg:
            outcome.messages.append(str(msg))
    return outcome


def reply_not_ready(reply: dict) -> bool:
    code = reply.get("id_code") if isinstance(reply, dict) else None
    return code == NOT_READY_CODE or str(code) == str(NOT_READY_CODE)


# --- recording outcomes ---------------------------------------------------------

def record_tns_name(transient: Transient, tns_name: str, *, service: Optional[SharingService] = None,
                    user: Optional[User] = None, rename: Optional[bool] = None, source: str = "TNS") -> str:
    """Attach ``tns_name`` to ``transient``: rename it (keeping the old name as an alternate) or add an alternate.

    Returns ``"renamed"``, ``"alternate"``, ``"unchanged"`` or ``"conflict"`` (another
    transient already carries that name; nothing is merged automatically).
    """
    tns_name = (tns_name or "").strip()
    if not tns_name:
        return "unchanged"
    user = user or transient.modified_by or system_user()
    if rename is None:
        rename = service.rename_transient if service is not None else bool(_setting("SHARING_RENAME_ON_ACCEPT", True))
    if transient.name == tns_name:
        return "unchanged"
    other = Transient.objects.filter(name=tns_name).exclude(pk=transient.pk).first()
    if other is not None:
        log.warning("%s: TNS name %s already belongs to transient %s; not renaming", transient.name, tns_name, other.pk)
        Log.objects.create(transient=transient, created_by=user, modified_by=user,
                           comment="%s reports this object as %s, which is already transient #%s in YSE-PZ."
                                   % (source, tns_name, other.pk))
        return "conflict"
    with transaction.atomic():
        if rename:
            old_name = transient.name
            if not AlternateTransientNames.objects.filter(name=old_name).exists():
                AlternateTransientNames.objects.create(
                    transient=transient, name=old_name, obs_group=transient.obs_group,
                    created_by=user, modified_by=user, description="Internal name before %s naming." % source)
            transient.name = tns_name
            transient.slug = tns_name
            transient.modified_by = user
            transient.save()
            return "renamed"
        if not AlternateTransientNames.objects.filter(transient=transient, name=tns_name).exists():
            AlternateTransientNames.objects.create(
                transient=transient, name=tns_name, obs_group=transient.obs_group,
                created_by=user, modified_by=user, description="%s designation." % source)
        return "alternate"


def _recipients(submission: SharingSubmission) -> List[User]:
    users = []
    if submission.created_by_id and submission.created_by.is_active:
        users.append(submission.created_by)
    publisher = submission.auto_publisher
    if publisher is not None and publisher.group_id:
        users.extend(publisher.group.user_set.filter(is_active=True).order_by("pk")[:50])
    sys_user = system_user()
    return [u for u in users if sys_user is None or u.pk != sys_user.pk or len(users) == 1]


def _notify(submission: SharingSubmission, text: str):
    try:
        from YSE_App.services.notify import notify

        notify(_recipients(submission), text, url=reverse("sharing_submission_detail", args=[submission.pk]),
               kind="sharing_result", transient=submission.transient, subject="TNS report: %s" % submission.transient.name)
    except Exception:  # noqa: BLE001 - a notification failure must not undo the outcome
        log.exception("sharing: could not notify about submission %s", submission.pk)


def apply_acceptance(submission: SharingSubmission, outcome: Outcome, reply: dict) -> str:
    submission.mark_accepted(outcome.objname, reply)
    transient = submission.transient
    result = "unchanged"
    if submission.kind == KIND_DISCOVERY:
        result = record_tns_name(transient, outcome.objname, service=submission.service,
                                 user=submission.created_by, source="TNS")
    actor = submission.created_by or system_user()
    where = "sandbox" if submission.service.testing else "TNS"
    Log.objects.create(
        transient=transient, created_by=actor, modified_by=actor,
        comment="Submitted to TNS (%s): %s report %s accepted as %s %s" % (
            where, submission.kind, submission.external_id, outcome.prefix, submission.service.object_url(outcome.objname)),
    )
    _notify(submission, "%s report for %s accepted by %s as %s%s." % (
        submission.kind.capitalize(), transient.name, where, outcome.prefix, outcome.objname))
    return result


# --- creating and queueing submissions -----------------------------------------

def create_submission(service: SharingService, transient: Transient, kind: str, user: Optional[User], *,
                      payload: Optional[dict] = None, auto_publisher=None, dispatch: bool = True,
                      **options) -> SharingSubmission:
    """Create a pending submission (building the payload unless one is given) and queue its job."""
    actor = user or system_user()
    if actor is None:
        raise SharingError("No user to attribute the submission to (create a superuser or set SHARING_SYSTEM_USERNAME).")
    if not service.enabled:
        raise SharingError("%s is disabled." % service.name)
    if payload is None:
        payload = build_payload(service, transient, kind, **options)
    submission = SharingSubmission.objects.create(
        service=service, transient=transient, kind=kind, payload=payload, auto_publisher=auto_publisher,
        created_by=actor, modified_by=actor,
    )
    if dispatch:
        dispatch_submission(submission)
    return submission


def dispatch_submission(submission: SharingSubmission):
    job_row = enqueue(SUBMIT_KIND, {"submission_id": submission.pk}, created_by=submission.created_by,
                      transient=submission.transient)
    submission.refresh_from_db()
    if submission.job_id != job_row.pk and submission.status == SharingSubmission.STATUS_PENDING:
        SharingSubmission.objects.filter(pk=submission.pk).update(job=job_row)
        submission.job = job_row
    return job_row


def retry_submission(submission: SharingSubmission, user: Optional[User] = None) -> SharingSubmission:
    """Put a failed or rejected submission back on the queue with the same payload."""
    if not submission.can_retry:
        raise SharingError("Only failed or rejected submissions can be retried (this one is %s)." % submission.status)
    submission.reset_for_retry()
    if user is not None:
        SharingSubmission.objects.filter(pk=submission.pk).update(modified_by=user)
    dispatch_submission(submission)
    return submission


def _prepare_report(submission: SharingSubmission, client: TNSClient) -> dict:
    """Upload the classification spectrum (if any) and return the report to send."""
    payload = submission.payload or {}
    if submission.kind == KIND_CLASSIFICATION:
        private = payload.get("_yse") or {}
        spectrum = TransientSpectrum.objects.filter(pk=private.get("spectrum_id")).first()
        group = payload.get("classification_report", {}).get("0", {}).get("spectra", {}).get("spectra-group", {}).get("0", {})
        if spectrum is not None and not group.get("ascii_file"):
            content = spectrum_ascii(spectrum)
            if content is None:
                raise JobFailed("Spectrum %s has no data rows and no readable data file to upload." % spectrum.pk)
            filename = "%s_%s.ascii" % (submission.transient.name, spectrum.obs_date.strftime("%Y%m%d"))
            uploaded = client.upload_file(filename, content)
            group["ascii_file"] = uploaded
            submission.payload = payload
            submission.save(update_fields=["payload", "modified_date"])
    return strip_private(payload)


def _transport_retry(submission: SharingSubmission, exc: Exception, job_row):
    """Retry a transport problem; when the job is out of attempts, fail the submission."""
    delay = getattr(exc, "retry_after", None)
    attempts = getattr(job_row, "attempts", 0) or 0
    max_attempts = getattr(job_row, "max_attempts", 0) or 0
    submission.attempts = (submission.attempts or 0) + 1
    submission.error = str(exc)
    submission.response = getattr(exc, "response", None) or {}
    submission.save(update_fields=["attempts", "error", "response", "modified_date"])
    if job_row is not None and max_attempts and attempts >= max_attempts:
        submission.mark_failed(str(exc), getattr(exc, "response", None))
        _notify(submission, "%s report for %s failed: %s" % (submission.kind.capitalize(), submission.transient.name, exc))
        raise JobFailed(str(exc))
    raise JobRetry(str(exc), delay=delay)


@job(SUBMIT_KIND, max_attempts=4, backoff_seconds=120)
def submit_job(payload, job=None):
    """Send a pending submission: bulk-report -> submitted, then queue the poll."""
    submission = SharingSubmission.objects.select_related("service", "transient", "created_by").get(
        pk=payload["submission_id"])
    if submission.status != SharingSubmission.STATUS_PENDING:
        return {"submission": submission.pk, "skipped": submission.status}
    if job is not None and submission.job_id != job.pk:
        SharingSubmission.objects.filter(pk=submission.pk).update(job=job)
    service = submission.service
    if service.kind == SharingService.KIND_HERMES or submission.kind == KIND_HERMES:
        from YSE_App.sharing import hermes

        try:
            result = hermes.publish(submission)
        except hermes.HermesNotConfigured as exc:
            submission.mark_failed(str(exc))
            return {"submission": submission.pk, "status": submission.status}
        submission.mark_accepted("", result)
        submission.external_id = str(result.get("uuid", ""))
        submission.save(update_fields=["external_id", "modified_date"])
        return {"submission": submission.pk, "status": submission.status}
    try:
        client = TNSClient.for_service(service)
    except TNSError as exc:
        submission.mark_failed(str(exc))
        return {"submission": submission.pk, "status": submission.status, "error": str(exc)}
    try:
        report = _prepare_report(submission, client)
        reply = client.send_bulk_report(report)
    except TNSTransportError as exc:
        _transport_retry(submission, exc, job)
    except TNSError as exc:
        submission.mark_failed(str(exc), exc.response)
        _notify(submission, "%s report for %s failed: %s" % (submission.kind.capitalize(), submission.transient.name, exc))
        return {"submission": submission.pk, "status": submission.status, "error": str(exc)}
    report_id = (reply.get("data") or {}).get("report_id") if isinstance(reply.get("data"), dict) else None
    if not report_id:
        submission.mark_failed("TNS accepted the request but returned no report_id: %s" % json.dumps(reply)[:500], reply)
        return {"submission": submission.pk, "status": submission.status}
    submission.mark_submitted(report_id, reply)
    poll = enqueue(POLL_KIND, {"submission_id": submission.pk}, delay=float(_setting("SHARING_POLL_DELAY_SECONDS", 10)),
                   max_attempts=int(_setting("SHARING_POLL_MAX_ATTEMPTS", 12)),
                   created_by=submission.created_by, transient=submission.transient)
    SharingSubmission.objects.filter(pk=submission.pk).update(job=poll)
    return {"submission": submission.pk, "status": SharingSubmission.STATUS_SUBMITTED, "report_id": report_id,
            "poll_job": poll.pk}


@job(POLL_KIND, max_attempts=12, backoff_seconds=30)
def poll_job(payload, job=None):
    """Fetch the bulk-report reply and record the outcome (retries while TNS is still processing)."""
    submission = SharingSubmission.objects.select_related("service", "transient", "created_by").get(
        pk=payload["submission_id"])
    if submission.status != SharingSubmission.STATUS_SUBMITTED:
        return {"submission": submission.pk, "skipped": submission.status}
    try:
        client = TNSClient.for_service(submission.service, touch=False)
        reply = client.bulk_report_reply(submission.external_id)
    except TNSTransportError as exc:
        _transport_retry(submission, exc, job)
    except TNSError as exc:
        submission.mark_failed(str(exc), exc.response)
        return {"submission": submission.pk, "status": submission.status, "error": str(exc)}
    if reply_not_ready(reply):
        attempts = getattr(job, "attempts", 0) or 0
        max_attempts = getattr(job, "max_attempts", 0) or 0
        if job is not None and max_attempts and attempts >= max_attempts:
            submission.mark_failed("TNS did not process report %s after %d polls; retry later."
                                   % (submission.external_id, attempts), reply)
            _notify(submission, "%s report for %s: no reply from TNS yet (report %s); retry from the submissions page."
                    % (submission.kind.capitalize(), submission.transient.name, submission.external_id))
            raise JobFailed("no reply after %d polls" % attempts)
        raise JobRetry("report %s not processed yet" % submission.external_id,
                       delay=float(_setting("SHARING_POLL_DELAY_SECONDS", 10)))
    outcome = parse_feedback(reply, submission.kind)
    if outcome.accepted:
        result = apply_acceptance(submission, outcome, reply)
        return {"submission": submission.pk, "status": submission.status, "tns_name": outcome.objname, "name": result}
    submission.mark_rejected(outcome.summary(), reply)
    _notify(submission, "%s report for %s rejected by TNS: %s" % (
        submission.kind.capitalize(), submission.transient.name, outcome.summary()))
    return {"submission": submission.pk, "status": submission.status, "error": outcome.summary()}


# --- legacy bridge (util/submit_to_tns.py) -------------------------------------

def legacy_tns_credentials(prefer_slug: str = "decam") -> Tuple[str, str, str, bool]:
    """``(api_key, bot_id, bot_name, sandbox)`` for the legacy DECam view (#325).

    Prefers the TNS ``SharingService`` with slug ``prefer_slug`` (then any enabled
    TNS service with a credential) and falls back to the ``settings.ini`` bot
    values the view used before.
    """
    services = list(SharingService.objects.filter(kind=SharingService.KIND_TNS, enabled=True)
                    .select_related("credential").order_by("slug"))
    services.sort(key=lambda s: 0 if s.slug == prefer_slug else 1)
    for service in services:
        if service.has_credential:
            secret = service.credential.get_secret(touch=True)
            api_key = secret.get("tns_api_key") or secret.get("api_key")
            bot_id = secret.get("tns_bot_id") or secret.get("bot_id")
            bot_name = secret.get("tns_bot_name") or secret.get("bot_name")
            if api_key and bot_id and bot_name:
                return str(api_key), str(bot_id), str(bot_name), bool(service.testing)
    return (
        str(getattr(settings, "TNSDECAMAPIKEY", "") or ""),
        str(getattr(settings, "TNSDECAMID", "") or ""),
        str(getattr(settings, "TNSDECAMUSER", "") or ""),
        True,
    )


def preview_payload(service: SharingService, transient: Transient, kind: str, **options) -> Tuple[dict, List[str]]:
    """``(payload, problems)`` for the report dialog: never raises for data problems."""
    try:
        return build_payload(service, transient, kind, **options), []
    except PayloadError as exc:
        return {}, [str(exc)]


def iter_json_lines(payload: dict) -> Iterable[str]:
    return json.dumps(strip_private(payload), indent=2, sort_keys=True).splitlines()
