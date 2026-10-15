"""Configured external feeds: Hermes / SCiMMA topics, Einstein Probe alerts and JPL Scout (#280).

A ``FeedSource`` is one configured stream the ``feeds.*`` jobs read
(:mod:`YSE_App.feeds`): a Hermes topic to consume (#281), an Einstein Probe
alert endpoint (#282) or a JPL Scout sweep (#283). It carries the credential
(a Hermes API token), the topic / URL, per-source options in ``config`` and
the bookkeeping of the last poll. Like ``BrokerFilter`` and ``Candidate`` it
is a plain ``models.Model`` because the polling jobs have no request user.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from YSE_App.models.credential_models import EncryptedCredential
from YSE_App.models.fields import JSONTextField

__all__ = ["FeedSource"]


class FeedSource(models.Model):
    KIND_HERMES = "hermes"
    KIND_EP = "ep"
    KIND_SCOUT = "scout"
    KIND_CHOICES = (
        (KIND_HERMES, "Hermes / SCiMMA topic"),
        (KIND_EP, "Einstein Probe alerts"),
        (KIND_SCOUT, "JPL Scout NEO sweep"),
    )

    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    topic = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Hermes: the topic to read (e.g. hermes.test, tns.new-objects). Other kinds ignore it.",
    )
    description = models.TextField(blank=True, default="")
    credential = models.ForeignKey(
        EncryptedCredential, null=True, blank=True, on_delete=models.SET_NULL, related_name="feed_sources",
        help_text="Hermes: JSON secret with hermes_token (or token). EP / Scout: optional.",
    )
    enabled = models.BooleanField(default=True, db_index=True)
    config = JSONTextField(
        default=dict, blank=True,
        help_text="Per-kind JSON options (docs/feeds-*.md): criteria, auto_save, save_status, save_obs_group, "
                  "match_radius_arcsec, import_photometry, max_per_run, comment; hermes: since_hours, kinds, classify; "
                  "ep: url, topic, max_error_arcsec, annotate; scout: min_neo_score, max_vmag, max_unc_arcmin, neofixer.",
    )
    last_polled = models.DateTimeField(null=True, blank=True, editable=False)
    last_summary = models.CharField(max_length=255, blank=True, default="", editable=False)
    last_error = models.TextField(blank=True, default="", editable=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="feed_sources_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("kind", "name")
        verbose_name = "feed source"

    def __str__(self):
        return "%s [%s%s]" % (self.name, self.kind, " " + self.topic if self.topic else "")

    # -- configuration -------------------------------------------------------
    def config_dict(self) -> dict:
        return self.config if isinstance(self.config, dict) else {}

    def config_value(self, key, default=None):
        value = self.config_dict().get(key)
        return default if value is None else value

    def clean(self):
        from django.core.exceptions import ValidationError

        from YSE_App.brokers.filters import CriteriaError
        from YSE_App.feeds import registry

        cfg = self.config_dict()
        if self.config not in (None, "") and not isinstance(self.config, dict):
            raise ValidationError({"config": "config must be a JSON object"})
        cls = registry.provider_class_for_kind(self.kind)
        if cls is None:
            raise ValidationError({"kind": "no feed provider for kind %r" % self.kind})
        try:
            cfg = cls().validate_config(cfg)
        except (ValueError, CriteriaError) as exc:
            raise ValidationError({"config": str(exc)})
        if self.kind == self.KIND_HERMES and not self.topic:
            raise ValidationError({"topic": "a Hermes source needs a topic"})
        self.config = cfg

    @property
    def has_credential(self) -> bool:
        return bool(self.credential_id and self.credential.is_active and self.credential.has_secret)

    def secret(self) -> dict:
        """Decrypted credential payload (``{}`` when there is none or it cannot be read)."""
        if not self.has_credential:
            return {}
        try:
            return self.credential.get_secret(touch=True) or {}
        except Exception:  # noqa: BLE001 - key missing / rotated: the poll reports it
            return {}

    # -- bookkeeping ---------------------------------------------------------
    def record_poll(self, summary: str, error: str = ""):
        self.last_polled = timezone.now()
        self.last_summary = (summary or "")[:255]
        self.last_error = (error or "")[:10000]
        type(self).objects.filter(pk=self.pk).update(
            last_polled=self.last_polled, last_summary=self.last_summary, last_error=self.last_error)

    def visible_to(self, user) -> bool:
        if user is None or not user.is_authenticated:
            return False
        return bool(self.enabled or user.is_staff or user.is_superuser)
