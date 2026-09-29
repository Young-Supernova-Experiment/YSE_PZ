"""Robotic facility APIs (#298, #299).

A facility adapter is a :class:`~YSE_App.facilities.base.FacilityAPI` subclass
registered under a slug (``@register``); an :class:`~YSE_App.models.Allocation`
names the slug in its ``facility`` field. The adapters shipped here are
``generic`` (HTTP POST, email or Slack webhook) and ``lco`` (Las Cumbres
Observatory request groups, built with the existing ``YSE_App.util.lcogt``
code). Extra modules are imported from ``settings.FACILITY_API_MODULES``.

The design follows SkyPortal's ``facility_apis`` package (BSD-3-Clause):
the same submit / update / delete / get-status capability set, adapted for
Django and the job queue; no SkyPortal code is copied.
"""

from YSE_App.facilities.base import (  # noqa: F401
    FacilityAPI,
    FacilityError,
    FacilityValidationError,
    Field,
    SubmitResult,
    StatusResult,
)
from YSE_App.facilities.registry import (  # noqa: F401
    autodiscover,
    facility_choices,
    get_facility,
    register,
    registered_slugs,
    unregister,
)

__all__ = [
    "FacilityAPI", "FacilityError", "FacilityValidationError", "Field", "SubmitResult", "StatusResult",
    "autodiscover", "facility_choices", "get_facility", "register", "registered_slugs", "unregister",
]
