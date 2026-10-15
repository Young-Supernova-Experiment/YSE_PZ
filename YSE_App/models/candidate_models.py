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

from YSE_App.models.credential_models import EncryptedCredential
from YSE_App.models.fields import JSONTextField
from YSE_App.models.tag_models import TransientTag
from YSE_App.models.transient_models import Transient

__all__ = ["BrokerFilter", "BrokerFilterVersion", "Candidate", "BrokerConnection", "IngestHeartbeat"]


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
    default_tags = models.ManyToManyField(
        TransientTag, blank=True, related_name="broker_filters",
        help_text="TransientTags added to every transient saved from this filter's candidates (#279).",
    )
    notify_group = models.BooleanField(
        default=False, help_text="Notify the group's members (kind 'candidate') when an alert passes this filter."
    )
    topics = JSONTextField(
        null=True, blank=True,
        help_text="Stream topics this filter applies to (JSON list; blank = every topic of the broker's connection).",
    )
    version = models.PositiveIntegerField(default=1, editable=False, help_text="Bumped when query or criteria change.")
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

    def topic_list(self):
        topics = self.topics
        if isinstance(topics, str):
            topics = [t.strip() for t in topics.split(",") if t.strip()]
        return [str(t) for t in (topics or [])]

    def applies_to_topic(self, topic) -> bool:
        """True when the filter has no topic restriction or lists ``topic``."""
        wanted = self.topic_list()
        return not wanted or not topic or str(topic) in wanted

    def rule_snapshot(self) -> dict:
        return {"query": self.query, "criteria": self.criteria, "topics": self.topics}

    def record_version(self, user=None, note: str = "", *, force: bool = False):
        """Append a ``BrokerFilterVersion`` for the current rule (#277).

        The first call writes version 1; later calls bump ``version`` only when
        the query / criteria / topics differ from the latest recorded version.
        Returns the row written, or None when nothing changed.
        """
        latest = self.versions.order_by("-version").first()
        snapshot = self.rule_snapshot()
        if latest is not None and not force and latest.snapshot() == snapshot:
            return None
        version = 1 if latest is None else latest.version + 1
        if version != self.version:
            self.version = version
            type(self).objects.filter(pk=self.pk).update(version=version)
        return BrokerFilterVersion.objects.create(
            filter=self, version=version, query=self.query, criteria=self.criteria, topics=self.topics,
            saved_by=user if getattr(user, "pk", None) else None, note=(note or "")[:255],
        )


class BrokerFilterVersion(models.Model):
    """One recorded state of a filter's rule; older versions are inactive history (#277)."""

    filter = models.ForeignKey(BrokerFilter, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    query = JSONTextField(null=True, blank=True)
    criteria = JSONTextField(null=True, blank=True)
    topics = JSONTextField(null=True, blank=True)
    saved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    saved_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ("filter", "-version")
        unique_together = (("filter", "version"),)

    def __str__(self):
        return "%s v%d" % (self.filter, self.version)

    def snapshot(self) -> dict:
        return {"query": self.query, "criteria": self.criteria, "topics": self.topics}

    @property
    def is_current(self) -> bool:
        return self.version == self.filter.version


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
    rejected_by_groups = models.ManyToManyField(
        Group, blank=True, related_name="rejected_candidates",
        help_text="Groups whose scanners rejected it; hidden from their queue, still shown to other groups (#279).",
    )
    topic = models.CharField(max_length=128, blank=True, default="", help_text="Stream topic the last alert came from.")
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

    def filter_groups(self):
        """Groups owning the filters this candidate passed (None entries = shared filters)."""
        return [f.group for f in self.filters.all()]

    def rejected_for(self, user) -> bool:
        """True when one of ``user``'s groups rejected it (staff never hide anything this way)."""
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        return self.rejected_by_groups.filter(pk__in=user.groups.values("pk")).exists()

    def set_status(self, status, user=None, *, transient=None, note=""):
        self.status = status
        self.status_changed_by = user if getattr(user, "pk", None) else None
        self.status_changed_at = timezone.now()
        if transient is not None:
            self.transient = transient
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "status_changed_by", "status_changed_at", "transient", "note", "updated_at"])


class BrokerConnection(models.Model):
    """A broker stream a ``broker_ingest`` worker consumes (#273 ``BrokerConnection``, #278).

    ``kind`` says which client reads it: ``kafka`` (``confluent_kafka.Consumer``:
    Fink, Lasair, ...) or ``antares`` (``antares_client.StreamingClient``).
    Nothing consumes it unless ``enabled`` is set *and* an operator runs the
    command (or the docker / systemd service); the row only holds the
    configuration and the credential. SASL credentials live in the linked
    ``EncryptedCredential`` (``username`` / ``password``, or ``api_key`` /
    ``api_secret`` for ANTARES).
    """

    KIND_KAFKA = "kafka"
    KIND_ANTARES = "antares"
    KIND_CHOICES = ((KIND_KAFKA, "Kafka (confluent-kafka)"), (KIND_ANTARES, "ANTARES streaming client"))
    FORMAT_JSON = "json"
    FORMAT_AVRO = "avro"
    FORMAT_CHOICES = ((FORMAT_JSON, "JSON"), (FORMAT_AVRO, "Avro (fastavro)"))

    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=64, unique=True)
    broker = models.CharField(max_length=32, db_index=True, help_text="Provider slug whose parser decodes the messages.")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_KAFKA)
    bootstrap_servers = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Kafka bootstrap servers (host:port, comma-separated). Blank for ANTARES.",
    )
    topics = JSONTextField(null=True, blank=True, help_text="Topics to subscribe to (JSON list).")
    group_id = models.CharField(max_length=128, blank=True, default="", help_text="Kafka consumer group id.")
    message_format = models.CharField(max_length=8, choices=FORMAT_CHOICES, default=FORMAT_JSON)
    credential = models.ForeignKey(
        EncryptedCredential, null=True, blank=True, on_delete=models.SET_NULL, related_name="broker_connections",
        help_text="SASL username / password (Kafka) or api_key / api_secret (ANTARES).",
    )
    config = JSONTextField(
        null=True, blank=True,
        help_text="Extra consumer settings: security_protocol, sasl_mechanism, batch_size, auto_offset_reset, "
                  "and any confluent-kafka option under \"kafka\".",
    )
    enabled = models.BooleanField(default=False, db_index=True, help_text="Off by default; the worker refuses a disabled connection.")
    description = models.TextField(blank=True, default="")
    last_message_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, default="", editable=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="broker_connections_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("broker", "name")

    def __str__(self):
        return "%s [%s %s]" % (self.name, self.broker, self.kind)

    def topic_list(self):
        topics = self.topics
        if isinstance(topics, str):
            topics = [t.strip() for t in topics.split(",") if t.strip()]
        return [str(t) for t in (topics or [])]

    def config_dict(self) -> dict:
        return self.config if isinstance(self.config, dict) else {}

    def secret(self) -> dict:
        if not self.credential_id or not self.credential.is_active or not self.credential.has_secret:
            return {}
        try:
            return self.credential.get_secret(touch=True) or {}
        except Exception:  # noqa: BLE001 - key missing / rotated: the worker reports it
            return {}

    def clean(self):
        from django.core.exceptions import ValidationError

        from YSE_App.brokers import registry

        if registry.provider_class(self.broker) is None:
            raise ValidationError({"broker": "unknown broker %r (known: %s)" % (self.broker, ", ".join(registry.registered_slugs()))})
        if self.topics not in (None, "") and not isinstance(self.topics, (list, str)):
            raise ValidationError({"topics": "topics must be a JSON list of strings"})
        if self.kind == self.KIND_KAFKA and not self.bootstrap_servers:
            raise ValidationError({"bootstrap_servers": "a Kafka connection needs bootstrap servers"})
        if not self.topic_list():
            raise ValidationError({"topics": "at least one topic is required"})
        if self.config not in (None, "") and not isinstance(self.config, dict):
            raise ValidationError({"config": "config must be a JSON object"})

    def record_error(self, error: str):
        self.last_error = (error or "")[:10000]
        type(self).objects.filter(pk=self.pk).update(last_error=self.last_error)


class IngestHeartbeat(models.Model):
    """Liveness of one ``broker_ingest`` worker on one topic, updated every batch (#278)."""

    STATUS_RUNNING = "running"
    STATUS_STOPPED = "stopped"
    STATUS_ERROR = "error"
    STATUS_CHOICES = ((STATUS_RUNNING, "Running"), (STATUS_STOPPED, "Stopped"), (STATUS_ERROR, "Error"))

    connection = models.ForeignKey(BrokerConnection, on_delete=models.CASCADE, related_name="heartbeats")
    topic = models.CharField(max_length=128, blank=True, default="")
    worker = models.PositiveIntegerField(default=0)
    hostname = models.CharField(max_length=128, blank=True, default="")
    pid = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    messages = models.PositiveIntegerField(default=0)
    candidates = models.PositiveIntegerField(default=0)
    saved = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    last_offset = models.BigIntegerField(null=True, blank=True)
    lag = models.BigIntegerField(null=True, blank=True, help_text="Messages behind the topic end (when the client reports it).")
    last_alert_id = models.CharField(max_length=128, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    last_error_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("connection", "topic", "worker")
        unique_together = (("connection", "topic", "worker"),)

    def __str__(self):
        return "%s/%s#%d" % (self.connection.slug, self.topic or "*", self.worker)

    def age_seconds(self, now=None) -> float:
        return ((now or timezone.now()) - self.last_seen).total_seconds()

    def is_stale(self, threshold_minutes: float, now=None) -> bool:
        return self.status == self.STATUS_RUNNING and self.age_seconds(now) > float(threshold_minutes) * 60.0
