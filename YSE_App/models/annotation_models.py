"""Structured annotations on transients (#316: #317, #318, #319).

``TransientAnnotation`` keeps one flat key/value document per ``(transient,
origin)``: the result of a catalogue cross-match (``gaia_dr3``, ``wise``,
``quasar``), a broker score (``antares``), a pipeline output, or a user's own
notes (``user:<username>``). ``TransientAnnotationValue`` is the flattened,
indexed side table of that document (one row per key, with the value as text
and, when it reads as a number, as a float) that the search filters and the
optional results column use, so a filter on ``gaia_dr3.parallax_over_error``
is one indexed ``EXISTS`` rather than JSON parsing per row. The value rows are
rebuilt whenever the annotation is saved through
:mod:`YSE_App.services.annotations`.

The design follows SkyPortal's annotations (BSD-3-Clause: an origin, a JSON
``data`` document, a group audience, unique per object and origin) in spirit;
no code is copied.
"""

from __future__ import annotations

import json
import math

from auditlog.registry import auditlog
from django.contrib.auth.models import Group
from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient

# Keys every annotation service writes next to its catalogue values.
VERDICT_KEY = "verdict"
SUMMARY_KEY = "summary"
VERDICT_STELLAR = "stellar"
VERDICT_AGN = "AGN-like"
VERDICT_CLEAN = "clean"
VERDICT_UNKNOWN = "unknown"
# Verdicts that earn a badge on the Summary tab (#318).
BADGE_VERDICTS = (VERDICT_STELLAR, VERDICT_AGN)

USER_ORIGIN_PREFIX = "user:"
LEGACY_ORIGIN = "legacy"

MAX_VALUE_TEXT = 255


class TransientAnnotation(BaseModel):
    """One origin's key/value document about a transient."""

    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="annotations")
    origin = models.CharField(
        max_length=64, db_index=True,
        help_text="Who produced it: a service slug (gaia_dr3, wise, quasar), a broker (antares) or user:<username>.",
    )
    data = JSONTextField(default=dict, help_text="Flat JSON object: key -> number, string, boolean or null.")
    groups = models.ManyToManyField(
        Group, blank=True, related_name="transient_annotations",
        help_text="Collaboration groups that may see it; empty = every logged-in user.",
    )
    service = models.ForeignKey(
        ExternalService, null=True, blank=True, on_delete=models.SET_NULL, related_name="annotations",
    )
    run = models.ForeignKey(
        ExternalServiceRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="annotations",
        help_text="The service run that last wrote this annotation.",
    )

    class Meta:
        unique_together = (("transient", "origin"),)
        ordering = ("origin",)
        indexes = [
            models.Index(fields=["origin", "modified_date"], name="yse_annot_origin_mod_idx"),
        ]

    def __str__(self):
        return "%s / %s" % (self.transient_id, self.origin)

    # -- convenience ---------------------------------------------------------
    @property
    def items(self):
        """``[(key, value)]`` of the document, verdict and summary first, the rest sorted."""
        data = self.data if isinstance(self.data, dict) else {}
        first = [(k, data[k]) for k in (VERDICT_KEY, SUMMARY_KEY) if k in data]
        rest = sorted((k, v) for k, v in data.items() if k not in (VERDICT_KEY, SUMMARY_KEY))
        return first + rest

    @property
    def verdict(self) -> str:
        data = self.data if isinstance(self.data, dict) else {}
        value = data.get(VERDICT_KEY)
        return str(value) if value not in (None, "") else ""

    @property
    def summary(self) -> str:
        data = self.data if isinstance(self.data, dict) else {}
        value = data.get(SUMMARY_KEY)
        return str(value) if value not in (None, "") else ""

    @property
    def has_badge(self) -> bool:
        return self.verdict in BADGE_VERDICTS

    @property
    def is_user_origin(self) -> bool:
        return self.origin.startswith(USER_ORIGIN_PREFIX)

    @property
    def data_json(self) -> str:
        return json.dumps(self.data if isinstance(self.data, dict) else {}, indent=2, sort_keys=True, default=str)

    def visible_to(self, user) -> bool:
        """Staff and superusers always; otherwise no groups, or a shared group."""
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        group_ids = {g.pk for g in self.groups.all()}
        if not group_ids:
            return True
        return bool(group_ids & set(user.groups.values_list("pk", flat=True)))

    def sync_values(self):
        """Rebuild the ``TransientAnnotationValue`` rows from ``data``."""
        rows = []
        data = self.data if isinstance(self.data, dict) else {}
        for key, value in data.items():
            text, num = coerce_value(value)
            if text is None and num is None:
                continue
            rows.append(TransientAnnotationValue(
                annotation=self, transient_id=self.transient_id, origin=self.origin,
                key=str(key)[:64], value_text=text, value_num=num,
            ))
        TransientAnnotationValue.objects.filter(annotation=self).delete()
        if rows:
            TransientAnnotationValue.objects.bulk_create(rows)
        return len(rows)


def coerce_value(value):
    """``(text, number)`` for one annotation value; ``(None, None)`` for null / unusable."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return ("true" if value else "false"), (1.0 if value else 0.0)
    if isinstance(value, (int, float)):
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return str(value)[:MAX_VALUE_TEXT], None
        text = repr(value) if isinstance(value, float) else str(value)
        return text[:MAX_VALUE_TEXT], num
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None, None
        try:
            num = float(text)
            if math.isnan(num) or math.isinf(num):
                num = None
        except ValueError:
            num = None
        return text[:MAX_VALUE_TEXT], num
    try:
        return json.dumps(value, default=str, sort_keys=True)[:MAX_VALUE_TEXT], None
    except (TypeError, ValueError):
        return str(value)[:MAX_VALUE_TEXT], None


class TransientAnnotationValue(models.Model):
    """Flattened, indexed view of ``TransientAnnotation.data`` for filters and table columns (#319)."""

    annotation = models.ForeignKey(TransientAnnotation, on_delete=models.CASCADE, related_name="values")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="annotation_values")
    origin = models.CharField(max_length=64)
    key = models.CharField(max_length=64)
    value_text = models.CharField(max_length=MAX_VALUE_TEXT, null=True, blank=True)
    value_num = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = (("annotation", "key"),)
        indexes = [
            models.Index(fields=["origin", "key", "value_num"], name="yse_annval_okn_idx"),
            models.Index(fields=["key", "value_num"], name="yse_annval_kn_idx"),
            models.Index(fields=["transient", "origin", "key"], name="yse_annval_tok_idx"),
        ]

    def __str__(self):
        return "%s.%s=%s" % (self.origin, self.key, self.value_text)


auditlog.register(TransientAnnotation)
