"""Encrypted per-resource credential storage (#264).

One audited place for third-party secrets that belong to a telescope resource,
a broker connection or a sharing service, instead of ad-hoc ``settings.ini``
sections. The JSON payload is encrypted at rest with
:mod:`YSE_App.services.credentials`; nothing on this model, its ``__str__`` or
the admin ever renders the plaintext.
"""

from __future__ import annotations

from typing import Dict, Optional

from auditlog.registry import auditlog
from django.contrib.auth.models import Group, User
from django.db import models
from django.utils import timezone

from YSE_App.models.base import BaseModel
from YSE_App.services import credentials as credential_service


class EncryptedCredential(BaseModel):
    """A named secret (JSON object) for one external service, encrypted at rest."""

    KIND_FACILITY = "facility"
    KIND_BROKER = "broker"
    KIND_TNS = "tns"
    KIND_HERMES = "hermes"
    KIND_ANALYSIS = "analysis"
    KIND_GENERIC = "generic"
    KIND_CHOICES = (
        (KIND_FACILITY, "Facility / telescope API"),
        (KIND_BROKER, "Alert broker"),
        (KIND_TNS, "TNS bot"),
        (KIND_HERMES, "Hermes / SCiMMA"),
        (KIND_ANALYSIS, "Analysis service"),
        (KIND_GENERIC, "Generic"),
    )

    name = models.CharField(max_length=128, help_text="Human label, e.g. 'LCO YSE key 2026B'.")
    service = models.CharField(
        max_length=64,
        help_text="Slug of the service these credentials are for, e.g. 'lco', 'tns', 'antares'.",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_GENERIC)
    description = models.TextField(blank=True, default="")
    owner_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="encrypted_credentials",
        help_text="Group that may use these credentials (blank: staff only).",
    )
    owner_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="encrypted_credentials",
        help_text="Personal credentials belong to one user (optional).",
    )
    encrypted_payload = models.TextField(blank=True, default="", editable=False)
    key_fingerprint = models.CharField(
        max_length=16, blank=True, default="", editable=False,
        help_text="Identifies which CREDENTIALS_KEY encrypted the payload (not the key itself).",
    )
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        unique_together = (("service", "name"),)
        ordering = ("service", "name")
        verbose_name = "encrypted credential"

    def __str__(self):
        return "%s (%s)" % (self.name, self.service)

    # -- secret access -----------------------------------------------------
    @property
    def has_secret(self) -> bool:
        return bool(self.encrypted_payload)

    def set_secret(self, payload: Dict) -> None:
        """Encrypt ``payload`` into the row (does not save)."""
        self.encrypted_payload = credential_service.encrypt_payload(payload)
        self.key_fingerprint = credential_service.primary_key_fingerprint()

    def get_secret(self, touch: bool = False) -> Dict:
        """Decrypt and return the payload; ``touch=True`` records ``last_used_at``."""
        payload = credential_service.decrypt_payload(self.encrypted_payload)
        if touch and self.pk:
            now = timezone.now()
            type(self).objects.filter(pk=self.pk).update(last_used_at=now)
            self.last_used_at = now
        return payload

    def secret_keys(self):
        """Names of the fields in the payload (never their values)."""
        try:
            return sorted(self.get_secret().keys())
        except credential_service.CredentialError:
            return []

    def masked_secret(self) -> Dict:
        """Payload keys with every value replaced by a mask; safe for templates."""
        try:
            return credential_service.mask_payload(self.get_secret())
        except credential_service.CredentialError:
            return {}

    def masked_display(self) -> str:
        if not self.has_secret:
            return "(no secret stored)"
        keys = self.secret_keys()
        if not keys:
            return "(secret stored; not decryptable with the current key)"
        return ", ".join("%s=%s" % (k, credential_service.MASK) for k in keys)

    masked_display.short_description = "secret"

    def usable_by(self, user: Optional[User]) -> bool:
        """Staff, the owning user, or a member of the owning group."""
        if user is None or not user.is_authenticated or not self.is_active:
            return False
        if user.is_staff or user.is_superuser:
            return True
        if self.owner_user_id and self.owner_user_id == user.id:
            return True
        if self.owner_group_id:
            return user.groups.filter(pk=self.owner_group_id).exists()
        return False


auditlog.register(EncryptedCredential, exclude_fields=["encrypted_payload"])
