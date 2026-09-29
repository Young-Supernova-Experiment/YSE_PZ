"""Instrument logs (#309: #310).

An :class:`InstrumentLog` is one dated block of operational messages for an
instrument: entered by hand on the ``/instruments/<id>/logs/`` page or the
``/api/instrumentlogs/`` endpoint (``source`` ``manual``), or pulled from a
facility API by the ``instrument_logs.pull`` job (``source`` ``api``; see
:meth:`YSE_App.facilities.base.FacilityAPI.fetch_instrument_log`). ``log`` is
the structured form, ``{"logs": [{"timestamp", "message", "level"}, ...]}``;
``message`` is a free-text summary. ``fingerprint`` (sha256 of instrument,
start, message and entries) makes pulls idempotent.

The shape follows SkyPortal's ``InstrumentLog`` (BSD-3-Clause) in spirit; no
code is copied.
"""

from __future__ import annotations

from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.models.fields import JSONTextField
from YSE_App.models.instrument_models import Instrument


class InstrumentLog(BaseModel):
    SOURCE_MANUAL = "manual"
    SOURCE_API = "api"
    SOURCE_CHOICES = ((SOURCE_MANUAL, "Entered by hand"), (SOURCE_API, "Pulled from the facility API"))

    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="logs")
    start = models.DateTimeField(db_index=True, help_text="Start of the period the log covers (UTC).")
    end = models.DateTimeField(null=True, blank=True, help_text="End of the period (UTC); blank = a point in time.")
    message = models.TextField(blank=True, default="", help_text="Free-text summary; the entries live in 'log'.")
    log = JSONTextField(default=dict, help_text='Structured entries: {"logs": [{"timestamp", "message", "level"}]}.')
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=SOURCE_MANUAL, db_index=True)
    source_name = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Where an API pull came from (facility slug or endpoint host).",
    )
    fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True, editable=False)
    run = models.ForeignKey(
        ExternalServiceRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="instrument_logs",
        help_text="The facility-API pull that produced this log.",
    )

    class Meta:
        ordering = ("-start", "-id")
        indexes = [models.Index(fields=["instrument", "start"], name="yse_instlog_instr_start_idx")]

    def __str__(self):
        return "%s log %s" % (self.instrument.name, self.start.strftime("%Y-%m-%d %H:%M"))

    @property
    def entries(self):
        """Normalised list of ``{"timestamp", "message", "level"}`` dicts."""
        from YSE_App.services.instrument_logs import normalize_entries

        return normalize_entries(self.log)

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def summary(self) -> str:
        if self.message:
            return self.message
        entries = self.entries
        if not entries:
            return ""
        first = entries[0].get("message", "")
        return first if len(entries) == 1 else "%s (+%d more)" % (first, len(entries) - 1)
