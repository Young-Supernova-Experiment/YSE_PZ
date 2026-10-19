"""One timestamped change log for values on transients and hosts (#737).

``ValueChange`` is one real change of one field: the old and new value, where the new
value came from (``source``), the writer that made it (``service`` and, when it records
runs, its ``ExternalServiceRun``), the time, and who (``actor``; NULL means the system,
i.e. a cron, job or sweep). Rows are written only through
``YSE_App.services.change_log.record_change`` and are never edited.

``category`` groups the fields (redshift, classification, host link, photo-z, override).
The planned Stage 5 decision trail (``ClassificationEvent``) can read the
``classification`` rows as its history (a proxy model or a view on this table) or, if it
needs more columns, a new table that points at these rows; nothing here has to change.

``restricted`` marks entries that may derive from restricted data (our own spectra,
values entered by hand): they are shown only to viewers who may see the transient's data
(``services.visibility.filter_transients_by_data_access``). Other entries describe public
catalogue values and follow the transient listing rule (``filter_listed_transients``).
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.models.host_models import Host
from YSE_App.models.transient_models import Transient


class ValueChange(models.Model):
    CATEGORY_REDSHIFT = "redshift"
    CATEGORY_CLASSIFICATION = "classification"
    CATEGORY_HOST = "host"
    CATEGORY_PHOTO_Z = "photo_z"
    CATEGORY_OVERRIDE = "override"
    CATEGORY_CHOICES = (
        (CATEGORY_REDSHIFT, "Redshift"),
        (CATEGORY_CLASSIFICATION, "Classification"),
        (CATEGORY_HOST, "Host link"),
        (CATEGORY_PHOTO_Z, "Photo-z"),
        (CATEGORY_OVERRIDE, "Redshift override"),
    )

    # exactly one of transient / host is set
    transient = models.ForeignKey(Transient, null=True, blank=True, on_delete=models.CASCADE,
                                  related_name="value_changes")
    host = models.ForeignKey(Host, null=True, blank=True, on_delete=models.CASCADE, related_name="value_changes")
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES)
    field = models.CharField(max_length=64)
    old_value = models.CharField(max_length=255, null=True, blank=True)
    new_value = models.CharField(max_length=255, null=True, blank=True)
    old_source = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(max_length=64, blank=True, default="", help_text="Where the new value came from.")
    service = models.CharField(max_length=64, help_text="The writer: an ExternalService slug, 'api' or 'manual'.")
    run = models.ForeignKey(ExternalServiceRun, null=True, blank=True, on_delete=models.SET_NULL,
                            related_name="value_changes")
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                              help_text="Who made the change; empty for the system (crons, jobs, sweeps).")
    restricted = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["transient", "created_at"], name="yse_valchg_transient_idx"),
            models.Index(fields=["host", "created_at"], name="yse_valchg_host_idx"),
        ]

    def __str__(self):
        target = ("transient %s" % self.transient_id) if self.transient_id else ("host %s" % self.host_id)
        return "%s %s: %r -> %r (%s)" % (target, self.field, self.old_value, self.new_value, self.service)

    @property
    def actor_name(self) -> str:
        """The username of the actor, or ``system`` (never more than the username)."""
        return self.actor.username if self.actor_id else "system"
