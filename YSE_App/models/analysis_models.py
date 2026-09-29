"""Analysis services (#312, #313): what an ``ExternalService`` of kind ``analysis`` needs on top.

``AnalysisService`` is the per-service profile of an :class:`ExternalService`
row with ``kind='analysis'``: how it is executed (an in-process Python
runner or a webhook to ``ExternalService.base_url``), which data it wants
(``input_spec``), what it returns (``output_spec``), the parameter form it
shows on the transient page (``param_schema``) and a timeout. Groups, the
per-user daily cap and the credential live on the ``ExternalService`` row.

``AnalysisResultFile`` stores what a run produced beyond its ``result`` JSON:
plot images, inference data (arviz/joblib blobs, opaque to us), tables. Files
live under ``MEDIA_ROOT/service_runs/<run uuid>/`` and are served through an
access-checked view, never by the media URL.

The shape (webhook with a per-run token, results + plots + opaque inference
files, per-user caps) follows SkyPortal's analysis services (BSD-3-Clause) in
spirit; no code is copied from that project.
"""

from __future__ import annotations

import os

from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.fields import JSONTextField

INPUT_PHOTOMETRY = "photometry"
INPUT_SPECTRA = "spectra"
INPUT_REDSHIFT = "redshift"
INPUT_HOST = "host"
INPUT_CHOICES = (INPUT_PHOTOMETRY, INPUT_SPECTRA, INPUT_REDSHIFT, INPUT_HOST)

OUTPUT_RESULTS = "results"
OUTPUT_PLOTS = "plots"
OUTPUT_FILES = "files"
OUTPUT_CHOICES = (OUTPUT_RESULTS, OUTPUT_PLOTS, OUTPUT_FILES)


class AnalysisService(BaseModel):
    RUNNER_INPROCESS = "inprocess"
    RUNNER_WEBHOOK = "webhook"
    RUNNER_CHOICES = (
        (RUNNER_INPROCESS, "In-process Python runner (job queue)"),
        (RUNNER_WEBHOOK, "Webhook (POST to base_url, results by callback)"),
    )

    service = models.OneToOneField(
        ExternalService, on_delete=models.CASCADE, related_name="analysis", primary_key=True,
    )
    runner_kind = models.CharField(max_length=12, choices=RUNNER_CHOICES, default=RUNNER_INPROCESS)
    runner_path = models.CharField(
        max_length=200, blank=True, default="",
        help_text="In-process: a built-in name (sncosmo_fit, bazin_fit) or the dotted path of a module "
                  "exposing run(payload, params). Blank for webhooks.",
    )
    input_spec = JSONTextField(
        default=list, help_text='Data sent to the service: subset of ["photometry", "spectra", "redshift", "host"].',
    )
    output_spec = JSONTextField(
        default=list, help_text='What the service returns: subset of ["results", "plots", "files"].',
    )
    param_schema = JSONTextField(
        default=dict,
        help_text='Parameter form: {"name": {"type": "number|text|choice|boolean", "label", "default", "choices", "help"}}.',
    )
    timeout_seconds = models.PositiveIntegerField(
        default=600, help_text="In-process runners are abandoned, webhook runs marked failed, after this long.",
    )
    display_order = models.IntegerField(default=100)
    summary_keys = JSONTextField(
        default=list,
        help_text="Result keys shown in the runs table (default: the first few numeric results).",
    )

    class Meta:
        ordering = ("display_order", "service__name")

    def __str__(self):
        return "analysis %s (%s)" % (self.service.slug, self.runner_kind)

    # -- convenience --------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.service.slug

    @property
    def name(self) -> str:
        return self.service.name

    @property
    def is_webhook(self) -> bool:
        return self.runner_kind == self.RUNNER_WEBHOOK

    def wants(self, item: str) -> bool:
        spec = self.input_spec or []
        return item in spec or "all" in spec

    def produces(self, item: str) -> bool:
        return item in (self.output_spec or [])

    def default_params(self) -> dict:
        """Defaults declared in ``param_schema`` under the service's ``default_params``."""
        out = {}
        for name, field in (self.param_schema or {}).items():
            if isinstance(field, dict) and "default" in field:
                out[name] = field["default"]
        out.update(self.service.default_params or {})
        return out


def _result_file_upload_to(instance, filename):
    return "service_runs/%s/%s" % (instance.run.uuid, os.path.basename(filename))


class AnalysisResultFile(BaseModel):
    """One file a run produced: a plot image, an inference-data blob, a table."""

    KIND_PLOT = "plot"
    KIND_INFERENCE = "inference"
    KIND_DATA = "data"
    KIND_OTHER = "other"
    KIND_CHOICES = (
        (KIND_PLOT, "Plot"),
        (KIND_INFERENCE, "Inference data (arviz / joblib)"),
        (KIND_DATA, "Data table"),
        (KIND_OTHER, "Other"),
    )

    run = models.ForeignKey(ExternalServiceRun, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_OTHER)
    name = models.CharField(max_length=200)
    file = models.FileField(upload_to=_result_file_upload_to, max_length=500)
    content_type = models.CharField(max_length=100, blank=True, default="application/octet-stream")
    size = models.PositiveIntegerField(default=0)
    meta = JSONTextField(default=dict, help_text="Free-form: plot kind, axis labels, format, ...")

    class Meta:
        ordering = ("run", "kind", "name")

    def __str__(self):
        return "%s (%s, run %s)" % (self.name, self.kind, str(self.run.uuid)[:8])

    @property
    def is_image(self) -> bool:
        return (self.content_type or "").startswith("image/")
