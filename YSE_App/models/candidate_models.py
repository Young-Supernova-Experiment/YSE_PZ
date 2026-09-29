"""Broker candidates and per-group broker filters (issues #277, #279; part of #276).

A ``Candidate`` is a broker object that passed at least one enabled
``BrokerFilter`` during a polling run of the ingest job
(:mod:`YSE_App.brokers.ingest`). It lives in its own table so the dashboard
is not flooded: a scanner saves it (creating or linking a ``Transient``
through the provider's ``save_as_transient``) or rejects it. Both models are
plain ``models.Model`` rows with their own timestamps, like ``Job``, because
the ingest job has no request user.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.db import models
from django.utils import timezone

from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient

__all__ = ["BrokerFilter", "Candidate"]


class BrokerFilter(models.Model):
    """One saved selection rule for one broker, owned by a group (or shared)."""

    name = models.CharField(max_length=128)
    broker = models.CharField(max_length=32, db_index=True, help_text="Provider slug, e.g. antares, fink, alerce.")
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL, related_name="broker_filters",
        help_text="Group whose scanners see this filter's candidates (blank: everyone).",
    )
    description = models.TextField(blank=True, default="")
    query = JSONTextField(
        null=True, blank=True,
        help_text="Broker-side query the poll runs (provider-specific keys, e.g. Fink classes / days).",
    )
    criteria = JSONTextField(
        null=True, blank=True,
        help_text="Client-side cuts on the normalised alert (see YSE_App.brokers.filters.CRITERIA).",
    )
    enabled = models.BooleanField(default=True, db_index=True)
    auto_save = models.BooleanField(
        default=False, help_text="Save passing alerts as transients at once instead of waiting for a scanner."
    )
    save_status = models.CharField(max_length=32, default="New", help_text="TransientStatus for saved transients.")
    save_obs_group = models.CharField(
        max_length=64, blank=True, default="",
        help_text="ObservationGroup name for saved transients (blank: the provider's default, e.g. ZTF).",
    )
    import_photometry = models.BooleanField(default=True, help_text="Pull the broker light curve when saving.")
    max_alerts = models.PositiveIntegerField(default=200, help_text="Alerts fetched per poll.")
    last_run_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_run_summary = models.CharField(max_length=255, blank=True, default="", editable=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="broker_filters_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("broker", "name")
        unique_together = (("broker", "name"),)

    def __str__(self):
        return "%s [%s]" % (self.name, self.broker)

    def clean(self):
        from django.core.exceptions import ValidationError

        from YSE_App.brokers import registry
        from YSE_App.brokers.filters import CriteriaError, validate_criteria

        try:
            self.criteria = validate_criteria(self.criteria)
        except CriteriaError as exc:
            raise ValidationError({"criteria": str(exc)})
        cls = registry.provider_class(self.broker)
        if cls is None:
            raise ValidationError({"broker": "unknown broker %r (known: %s)" % (self.broker, ", ".join(registry.registered_slugs()))})
        try:
            self.query = cls().validate_query(self.query)
        except ValueError as exc:
            raise ValidationError({"query": str(exc)})

    def record_run(self, summary: str):
        self.last_run_at = timezone.now()
        self.last_run_summary = (summary or "")[:255]
        type(self).objects.filter(pk=self.pk).update(last_run_at=self.last_run_at, last_run_summary=self.last_run_summary)

    def visible_to(self, user) -> bool:
        if self.group_id is None:
            return True
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        return user.groups.filter(pk=self.group_id).exists()


class Candidate(models.Model):
    NEW = "new"
    SAVED = "saved"
    REJECTED = "rejected"
    STATUS_CHOICES = ((NEW, "New"), (SAVED, "Saved"), (REJECTED, "Rejected"))

    broker = models.CharField(max_length=32, db_index=True)
    alert_id = models.CharField(max_length=128, help_text="Broker object id (ZTF name, ANTARES locus id).")
    latest_alert_id = models.CharField(max_length=128, blank=True, default="", help_text="Newest alert / candid seen.")
    ra = models.FloatField()
    dec = models.FloatField()
    discovery_mjd = models.FloatField(null=True, blank=True)
    last_mjd = models.FloatField(null=True, blank=True, db_index=True)
    last_mag = models.FloatField(null=True, blank=True)
    last_band = models.CharField(max_length=16, blank=True, default="")
    rb = models.FloatField(null=True, blank=True)
    classification = models.CharField(max_length=128, blank=True, default="")
    n_alerts = models.PositiveIntegerField(default=1, help_text="Polls that saw this object.")
    filters = models.ManyToManyField(BrokerFilter, blank=True, related_name="candidates")
    passed_filter_names = JSONTextField(null=True, blank=True)
    payload = JSONTextField(null=True, blank=True, help_text="Normalised alert (BrokerAlert.to_dict).")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=NEW, db_index=True)
    transient = models.ForeignKey(
        Transient, null=True, blank=True, on_delete=models.SET_NULL, related_name="broker_candidates"
    )
    status_changed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="candidates_reviewed"
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-last_seen", "-id")
        unique_together = (("broker", "alert_id"),)
        indexes = [
            models.Index(fields=["status", "last_seen"], name="yse_candidate_status_seen_idx"),
            models.Index(fields=["ra"], name="yse_candidate_ra_idx"),
            models.Index(fields=["dec"], name="yse_candidate_dec_idx"),
        ]

    def __str__(self):
        return "%s:%s [%s]" % (self.broker, self.alert_id, self.status)

    @property
    def alert(self):
        from YSE_App.brokers.base import BrokerAlert

        data = dict(self.payload or {})
        data.setdefault("broker", self.broker)
        data.setdefault("object_id", self.alert_id)
        data.setdefault("ra", self.ra)
        data.setdefault("dec", self.dec)
        return BrokerAlert.from_dict(data)

    @property
    def cutout_urls(self):
        return dict((self.payload or {}).get("cutout_urls") or {})

    @property
    def url(self):
        return (self.payload or {}).get("url") or ""

    @property
    def gal_b(self):
        return (self.payload or {}).get("gal_b")

    @property
    def is_new(self):
        return self.status == self.NEW

    def set_status(self, status, user=None, *, transient=None, note=""):
        self.status = status
        self.status_changed_by = user if getattr(user, "pk", None) else None
        self.status_changed_at = timezone.now()
        if transient is not None:
            self.transient = transient
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "status_changed_by", "status_changed_at", "transient", "note", "updated_at"])
