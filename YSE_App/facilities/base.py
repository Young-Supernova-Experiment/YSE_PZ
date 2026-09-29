"""Base class every facility adapter implements (#299)."""

from __future__ import annotations

import datetime
from typing import Any, Dict, Iterable, List, Optional

from django.conf import settings


class FacilityError(Exception):
    """The facility rejected the request or could not be reached."""


class FacilityValidationError(FacilityError):
    """Request parameters are invalid; ``errors`` maps field -> message."""

    def __init__(self, errors: Dict[str, str]):
        self.errors = dict(errors)
        super().__init__("; ".join("%s: %s" % kv for kv in sorted(self.errors.items())) or "invalid parameters")


class Field:
    """One entry of a facility's request form (rendered by the detail-page hook)."""

    TYPES = ("text", "number", "integer", "boolean", "choice", "datetime", "list")

    def __init__(self, name: str, type: str = "text", *, label: str = "", default: Any = None,
                 required: bool = False, choices: Optional[Iterable] = None, help: str = "",
                 minimum: Optional[float] = None, maximum: Optional[float] = None):
        if type not in self.TYPES:
            raise ValueError("unknown field type %r" % type)
        self.name = name
        self.type = type
        self.label = label or name.replace("_", " ")
        self.default = default
        self.required = required
        self.choices = [list(c) if isinstance(c, (list, tuple)) else [c, c] for c in (choices or ())]
        self.help = help
        self.minimum = minimum
        self.maximum = maximum

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "type": self.type, "label": self.label, "default": self.default,
            "required": self.required, "choices": self.choices, "help": self.help,
            "min": self.minimum, "max": self.maximum,
        }

    def clean(self, value: Any) -> Any:
        """Coerce ``value`` to the field type; raise ``ValueError`` with a message when it cannot."""
        if value is None or value == "" or value == []:
            if self.required and self.default is None:
                raise ValueError("this field is required")
            return self.default if value in (None, "") else value
        if self.type in ("number", "integer"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError("must be a number")
            if self.type == "integer":
                if number != int(number):
                    raise ValueError("must be a whole number")
                number = int(number)
            if self.minimum is not None and number < self.minimum:
                raise ValueError("must be at least %s" % self.minimum)
            if self.maximum is not None and number > self.maximum:
                raise ValueError("must be at most %s" % self.maximum)
            return number
        if self.type == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type == "choice":
            allowed = [c[0] for c in self.choices]
            if allowed and value not in allowed:
                raise ValueError("must be one of %s" % ", ".join(str(a) for a in allowed))
            return value
        if self.type == "datetime":
            if isinstance(value, datetime.datetime):
                return value.replace(tzinfo=None).isoformat(timespec="seconds")
            try:
                parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "").replace(" ", "T"))
            except ValueError:
                raise ValueError("must be an ISO date-time (YYYY-MM-DDTHH:MM)")
            return parsed.replace(tzinfo=None).isoformat(timespec="seconds")
        if self.type == "list":
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            return list(value)
        return str(value)


class SubmitResult:
    """What ``submit`` returns: the facility's answer, normalised."""

    def __init__(self, state: str, *, external_id: str = "", external_url: str = "", detail: str = "",
                 response: Any = None, hours: Optional[float] = None):
        self.state = state
        self.external_id = str(external_id or "")
        self.external_url = external_url or ""
        self.detail = detail or ""
        self.response = response
        self.hours = hours

    def as_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "external_id": self.external_id, "external_url": self.external_url,
                "detail": self.detail, "response": self.response, "hours": self.hours}


class StatusResult:
    def __init__(self, state: str, *, detail: str = "", response: Any = None):
        self.state = state
        self.detail = detail or ""
        self.response = response


def http_timeout() -> float:
    return float(getattr(settings, "FACILITY_HTTP_TIMEOUT_SECONDS", 30) or 30)


class FacilityAPI:
    """Interface of a facility adapter. Subclass, set ``slug``, implement what the facility supports.

    ``request`` arguments are :class:`YSE_App.models.FacilityRequest` rows; the
    adapter reads ``request.allocation`` (credentials, defaults, endpoint),
    ``request.transient`` and ``request.payload`` (validated parameters).
    """

    slug: str = ""
    name: str = ""
    description: str = ""
    #: subset of {"submit", "update", "delete", "status"}
    capabilities = frozenset({"submit"})
    #: names the credential payload should hold (documentation and the allocations page)
    credential_keys: List[str] = []
    #: whether the state must be advanced by a person (no API to poll)
    manual_status: bool = True

    # -- form ---------------------------------------------------------------
    def fields(self, allocation=None) -> List[Field]:
        return []

    def form_schema(self, allocation=None) -> List[Dict[str, Any]]:
        schema = []
        defaults = dict(getattr(allocation, "default_request_params", None) or {})
        for field in self.fields(allocation):
            entry = field.as_dict()
            if field.name in defaults:
                entry["default"] = defaults[field.name]
            schema.append(entry)
        return schema

    def validate(self, params: Dict[str, Any], allocation=None) -> Dict[str, Any]:
        """Merge the allocation defaults under ``params`` and coerce every declared field.

        Unknown keys are kept as given. Raises :class:`FacilityValidationError`.
        """
        merged = dict(getattr(allocation, "default_request_params", None) or {})
        merged.update({k: v for k, v in (params or {}).items()})
        errors = {}
        cleaned = dict(merged)
        for field in self.fields(allocation):
            try:
                cleaned[field.name] = field.clean(merged.get(field.name))
            except ValueError as exc:
                errors[field.name] = str(exc)
        if errors:
            raise FacilityValidationError(errors)
        return self.validate_extra(cleaned, allocation)

    def validate_extra(self, params: Dict[str, Any], allocation=None) -> Dict[str, Any]:
        """Hook for cross-field checks; return the (possibly amended) params."""
        return params

    def estimate_hours(self, params: Dict[str, Any], allocation=None) -> float:
        """Hours this request will cost the allocation (default: exposure_time * exposure_count)."""
        try:
            exposure = float(params.get("exposure_time") or 0)
            count = float(params.get("exposure_count") or 1)
        except (TypeError, ValueError):
            return 0.0
        return round(exposure * count / 3600.0, 4)

    # -- transport ----------------------------------------------------------
    def build_payload(self, request) -> Any:
        """What is sent to the facility (also stored in the run's request payload)."""
        return dict(request.payload or {})

    def submit(self, request) -> SubmitResult:
        raise NotImplementedError("%s cannot submit requests" % self.slug)

    def update(self, request) -> SubmitResult:
        raise FacilityError("%s cannot modify submitted requests" % self.slug)

    def delete(self, request) -> StatusResult:
        raise FacilityError("%s cannot cancel submitted requests" % self.slug)

    def get_status(self, request) -> StatusResult:
        raise FacilityError("%s has no status endpoint; mark the request complete by hand" % self.slug)

    # -- helpers ------------------------------------------------------------
    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def describe(self) -> Dict[str, Any]:
        return {
            "slug": self.slug, "name": self.name or self.slug, "description": self.description,
            "capabilities": sorted(self.capabilities), "credential_keys": list(self.credential_keys),
            "manual_status": self.manual_status,
        }

    @staticmethod
    def transient_context(request) -> Dict[str, Any]:
        transient = request.transient
        allocation = request.allocation
        return {
            "transient_name": transient.name, "ra": transient.ra, "dec": transient.dec,
            "transient_id": transient.pk, "transient_slug": getattr(transient, "slug", ""),
            "allocation_id": allocation.pk, "allocation_name": allocation.name,
            "proposal_id": allocation.proposal_id, "telescope": allocation.telescope.name,
            "instrument": allocation.instrument.name if allocation.instrument_id else "",
            "requester": request.submitted_by.username, "request_id": request.pk,
        }
