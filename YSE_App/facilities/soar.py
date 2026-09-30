"""SOAR adapter (#302): Goodman and TripleSpec requests through the LCO observation portal.

SOAR time awarded through NOIRLab is scheduled by the LCO portal, so the adapter
is the LCO one restricted to the SOAR instruments (``SOAR_GHTS_REDCAM``,
``SOAR_GHTS_REDCAM_IMAGER``, ``SOAR_TRIPLESPEC``); the request-group document,
status polling and cancellation are shared with :mod:`YSE_App.facilities.lco`.
"""

from __future__ import annotations

from YSE_App.facilities.base import Field
from YSE_App.facilities.lco import LCOFacility
from YSE_App.facilities.registry import register

SOAR_INSTRUMENTS = ("SOAR_GHTS_REDCAM", "SOAR_GHTS_REDCAM_IMAGER", "SOAR_TRIPLESPEC")


@register
class SOARFacility(LCOFacility):
    slug = "soar"
    name = "SOAR (via LCO portal)"
    description = "Goodman spectroscopy / imaging and TripleSpec requests on SOAR, scheduled through the LCO portal."
    instruments = SOAR_INSTRUMENTS
    default_instrument = "SOAR_GHTS_REDCAM"
    setup_notes = ("proposal_id = the NOIRLab / LCO proposal code for SOAR; credential {\"api_token\": \"<LCO portal "
                   "token>\"}. Requests always use strategy 'instrument' with a SOAR instrument.")

    def fields(self, allocation=None):
        fields = [f for f in super().fields(allocation) if f.name not in ("strategy", "filters")]
        fields.insert(0, Field("strategy", "choice", label="Strategy", default="instrument",
                               choices=[("instrument", "Use the instrument chosen below")], required=True))
        fields.insert(2, Field("filters", "list", label="Filters (imager)", default=["g-SDSS", "r-SDSS"]))
        return fields

    def validate_extra(self, params, allocation=None):
        params["strategy"] = "instrument"
        if params.get("instrument") not in self.instruments:
            params["instrument"] = self.default_instrument
        return super().validate_extra(params, allocation)

    def instrument_for(self, params) -> str:
        return params.get("instrument") or self.default_instrument
