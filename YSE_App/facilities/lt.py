"""Liverpool Telescope adapter (#302): RTML requests to the LT phase-2 socket.

The LT accepts RTML 3.1a documents over a plain TCP connection
(``telescope.livjm.ac.uk:8080``): a ``request`` document with the project
credentials, target, schedule and an instrument setup (IO:O imaging, IO:I
near-IR imaging or SPRAT spectroscopy) is answered by a ``confirm`` (or
``reject``) document carrying the request ``uid``; an ``abort`` document with
the same ``uid`` withdraws it. There is no status query: completion is recorded
by hand once the data appear in the LT archive.

Credential payload: ``{"username": "...", "password": "..."}``; ``proposal_id``
on the allocation is the LT project id. SkyPortal's ``facility_apis/lt.py``
(BSD-3-Clause) documents the RTML layout; no code is copied.
"""

from __future__ import annotations

import datetime
import socket
import uuid
from typing import Any, Dict
from xml.etree import ElementTree as ET

from django.conf import settings

from YSE_App.facilities.base import (
    FacilityAPI,
    FacilityError,
    FacilityUnreachable,
    FacilityValidationError,
    Field,
    StatusResult,
    SubmitResult,
    http_timeout,
)
from YSE_App.facilities.registry import register

HOST = "telescope.livjm.ac.uk"
PORT = 8080
RTML_NS = "http://www.rtml.org/v3.1a"
INSTRUMENTS = (("IOO", "IO:O optical imaging"), ("IOI", "IO:I near-IR imaging (H)"), ("SPRAT", "SPRAT spectroscopy"))
IOO_FILTERS = ("u", "g", "r", "i", "z", "B", "V", "R", "I", "Halpha")


def _rtml_root(mode: str, uid: str) -> ET.Element:
    root = ET.Element("RTML", {"xmlns": RTML_NS, "mode": mode, "uid": uid, "version": "3.1a"})
    return root


def build_request_document(request, uid: str) -> str:
    """The RTML ``request`` document for a facility request."""
    params = dict(request.payload or {})
    allocation = request.allocation
    transient = request.transient
    secret = allocation.secret(touch=True)
    username, password = secret.get("username"), secret.get("password")
    if not (username and password):
        raise FacilityError("the LT credential needs username and password")
    root = _rtml_root("request", uid)
    project = ET.SubElement(root, "Project", {"ProjectID": allocation.proposal_id})
    contact = ET.SubElement(project, "Contact")
    ET.SubElement(contact, "Username").text = username
    ET.SubElement(contact, "Password").text = password
    ET.SubElement(contact, "Name").text = request.submitted_by.get_full_name() or request.submitted_by.username
    ET.SubElement(contact, "Communication")
    instrument = params.get("instrument") or "IOO"
    exposure = float(params["exposure_time"])
    count = int(params.get("exposure_count") or 1)
    priority = int(params.get("priority") or 1)
    if instrument == "SPRAT":
        setups = [("Spectrograph", {"name": "Sprat"}, {"grating": params.get("grating") or "red"})]
    elif instrument == "IOI":
        setups = [("Camera", {"name": "IO:I"}, {"filter": "H"})]
    else:
        setups = [("Camera", {"name": "IO:O"}, {"filter": f}) for f in (params.get("filters") or ["r"])]
    for kind, attrs, optics in setups:
        schedule = ET.SubElement(root, "Schedule")
        device = ET.SubElement(schedule, "Device", {"type": kind.lower(), **attrs})
        if kind == "Spectrograph":
            grating = ET.SubElement(device, "SpectralRegion")
            grating.text = "optical"
            setup = ET.SubElement(device, "Setup")
            ET.SubElement(setup, "Grating", {"name": optics["grating"]})
            detector = ET.SubElement(setup, "Detector")
            ET.SubElement(detector, "Binning").extend([ET.Element("X", {"units": "pixels"}), ET.Element("Y", {"units": "pixels"})])
            for axis in detector.find("Binning"):
                axis.text = "1"
        else:
            ET.SubElement(device, "SpectralRegion").text = "infrared" if instrument == "IOI" else "optical"
            setup = ET.SubElement(device, "Setup")
            ET.SubElement(setup, "Filter", {"type": optics["filter"]})
            detector = ET.SubElement(setup, "Detector")
            binning = ET.SubElement(detector, "Binning")
            for axis in ("X", "Y"):
                ET.SubElement(binning, axis, {"units": "pixels"}).text = "2" if instrument == "IOO" else "1"
        exposure_el = ET.SubElement(schedule, "Exposure", {"count": str(count)})
        ET.SubElement(exposure_el, "Value", {"units": "seconds"}).text = "%.1f" % exposure
        target = ET.SubElement(schedule, "Target", {"name": transient.name})
        coords = ET.SubElement(target, "Coordinates")
        ra = ET.SubElement(coords, "RightAscension")
        ra_h = float(transient.ra) / 15.0
        ET.SubElement(ra, "Hours").text = str(int(ra_h))
        ET.SubElement(ra, "Minutes").text = str(int((ra_h % 1) * 60))
        ET.SubElement(ra, "Seconds").text = "%.2f" % ((((ra_h % 1) * 60) % 1) * 60)
        dec = ET.SubElement(coords, "Declination")
        dec_abs = abs(float(transient.dec))
        ET.SubElement(dec, "Degrees").text = ("-" if transient.dec < 0 else "+") + str(int(dec_abs))
        ET.SubElement(dec, "Arcminutes").text = str(int((dec_abs % 1) * 60))
        ET.SubElement(dec, "Arcseconds").text = "%.2f" % ((((dec_abs % 1) * 60) % 1) * 60)
        ET.SubElement(coords, "Equinox").text = "J2000"
        constraints = ET.SubElement(schedule, "DateTimeConstraint", {"type": "include"})
        ET.SubElement(constraints, "DateTimeStart", {"system": "UT", "value": _rtml_time(params.get("start"))})
        ET.SubElement(constraints, "DateTimeEnd", {"system": "UT", "value": _rtml_time(params.get("end"))})
        airmass = ET.SubElement(schedule, "AirmassConstraint", {"maximum": str(float(params.get("max_airmass") or 2.0))})
        airmass.tail = None
        sky = ET.SubElement(schedule, "SkyConstraint")
        ET.SubElement(sky, "Flux").text = str(params.get("sky_brightness") or 2.0)
        ET.SubElement(sky, "Units").text = "magnitudes/square-arcsecond"
        seeing = ET.SubElement(schedule, "SeeingConstraint", {"maximum": str(float(params.get("max_seeing") or 1.5)), "units": "arcseconds"})
        seeing.tail = None
        ET.SubElement(schedule, "Priority").text = str(priority)
    return ET.tostring(root, encoding="unicode")


def _rtml_time(value) -> str:
    if not value:
        return datetime.datetime.utcnow().replace(microsecond=0).isoformat()
    return str(value).replace(" ", "T")[:19]


def build_abort_document(request) -> str:
    secret = request.allocation.secret(touch=True)
    root = _rtml_root("abort", request.external_id)
    project = ET.SubElement(root, "Project", {"ProjectID": request.allocation.proposal_id})
    contact = ET.SubElement(project, "Contact")
    ET.SubElement(contact, "Username").text = secret.get("username") or ""
    ET.SubElement(contact, "Password").text = secret.get("password") or ""
    return ET.tostring(root, encoding="unicode")


def send_rtml(document: str) -> str:
    """Send an RTML document to the LT socket and return the reply text."""
    host = getattr(settings, "LT_RTML_HOST", HOST)
    port = int(getattr(settings, "LT_RTML_PORT", PORT))
    try:
        with socket.create_connection((host, port), timeout=http_timeout()) as conn:
            conn.sendall(document.encode("utf-8"))
            conn.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        raise FacilityUnreachable("could not reach the LT RTML socket %s:%s: %s" % (host, port, exc)) from exc
    return b"".join(chunks).decode("utf-8", "replace")


def parse_reply(text: str) -> Dict[str, Any]:
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError as exc:
        raise FacilityError("LT answered with something that is not RTML: %s" % text[:200]) from exc
    mode = root.attrib.get("mode", "")
    uid = root.attrib.get("uid", "")
    errors = [el.text or "" for el in root.iter() if el.tag.split("}")[-1] in ("Error", "Reason") and (el.text or "").strip()]
    return {"mode": mode, "uid": uid, "errors": errors}


@register
class LiverpoolTelescope(FacilityAPI):
    slug = "lt"
    name = "Liverpool Telescope (RTML)"
    description = "Sends IO:O / IO:I imaging and SPRAT spectroscopy requests to the LT phase-2 system; abort withdraws them."
    capabilities = frozenset({"submit", "delete"})
    credential_keys = ["username", "password"]
    manual_status = True
    setup_notes = ("proposal_id = the LT project id (e.g. PL26B01); credential {\"username\": \"<phase-2 user>\", "
                   "\"password\": \"...\"}. Settings LT_RTML_HOST / LT_RTML_PORT override the socket.")

    def fields(self, allocation=None):
        now = datetime.datetime.utcnow().replace(microsecond=0)
        return [
            Field("instrument", "choice", label="Instrument", default="IOO", choices=INSTRUMENTS, required=True),
            Field("exposure_time", "number", label="Exposure time (s)", required=True, minimum=1),
            Field("exposure_count", "integer", label="Exposures per filter", default=1, minimum=1, maximum=50),
            Field("filters", "list", label="Filters (IO:O)", default=["g", "r", "i"]),
            Field("grating", "choice", label="SPRAT grating", default="red", choices=[("red", "red"), ("blue", "blue")]),
            Field("start", "datetime", label="Window start (UTC)", default=now.isoformat(), required=True),
            Field("end", "datetime", label="Window end (UTC)", default=(now + datetime.timedelta(days=3)).isoformat(), required=True),
            Field("max_airmass", "number", label="Max airmass", default=2.0, minimum=1.0, maximum=4.0),
            Field("max_seeing", "number", label="Max seeing (arcsec)", default=1.5, minimum=0.5, maximum=5.0),
            Field("priority", "integer", label="Priority (0 urgent .. 3 low)", default=1, minimum=0, maximum=3),
        ]

    def validate_extra(self, params, allocation=None):
        errors = {}
        if allocation is not None and not allocation.proposal_id:
            errors["proposal"] = "set the LT project id on the allocation (proposal_id)"
        if params.get("instrument") == "IOO":
            bad = [f for f in params.get("filters") or [] if f not in IOO_FILTERS]
            if bad:
                errors["filters"] = "unknown IO:O filter(s) %s" % ", ".join(bad)
            if not params.get("filters"):
                params["filters"] = ["r"]
        if params.get("start") and params.get("end") and params["end"] <= params["start"]:
            errors["end"] = "must be after the window start"
        if errors:
            raise FacilityValidationError(errors)
        return params

    def estimate_hours(self, params, allocation=None):
        try:
            n = len(params.get("filters") or []) if params.get("instrument") == "IOO" else 1
            return round(max(1, n) * int(params.get("exposure_count") or 1) * (float(params["exposure_time"]) + 30.0) / 3600.0, 4)
        except (TypeError, ValueError, KeyError):
            return 0.0

    def build_payload(self, request) -> str:
        return build_request_document(request, request.external_id or uuid.uuid4().hex)

    def submit(self, request) -> SubmitResult:
        uid = "ysepz-%d-%s" % (request.pk or 0, uuid.uuid4().hex[:8])
        document = build_request_document(request, uid)
        reply = parse_reply(send_rtml(document))
        if reply["mode"] != "confirm":
            raise FacilityError("LT %s the request: %s" % (reply["mode"] or "did not confirm", "; ".join(reply["errors"]) or "no reason given"))
        return SubmitResult("accepted", external_id=reply["uid"] or uid, detail="RTML request %s confirmed" % (reply["uid"] or uid),
                            response={"reply_mode": reply["mode"], "uid": reply["uid"]})

    def delete(self, request) -> StatusResult:
        if not request.external_id:
            raise FacilityError("request has no LT uid")
        reply = parse_reply(send_rtml(build_abort_document(request)))
        if reply["mode"] not in ("confirm", "abort"):
            raise FacilityError("LT did not confirm the abort: %s" % ("; ".join(reply["errors"]) or reply["mode"]))
        return StatusResult("cancelled", detail="RTML abort of %s confirmed" % request.external_id, response=reply)
