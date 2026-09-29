"""In-app notifications and per-user delivery preferences (issue #266, part of #69).

``Notification`` is the record a user sees in the in-app list; delivery to
other channels (email, Slack webhook) happens on the job queue and is
recorded in ``delivered``. ``NotificationPreference`` holds the per-user
channel switches; a user without a row gets the defaults.
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient

__all__ = ["Notification", "NotificationPreference"]


class NotificationPreference(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="notification_preference")
    in_app = models.BooleanField(default=True, help_text="Show notifications in the in-app list and badge.")
    email = models.BooleanField(default=True, help_text="Also send each notification to the account email.")
    slack_webhook_url = models.URLField(
        max_length=500, blank=True, default="",
        help_text="Optional Slack incoming-webhook URL; each notification is posted there as text.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "notification preference"

    def __str__(self):
        return "NotificationPreference(%s: in_app=%s email=%s slack=%s)" % (
            self.user.username, self.in_app, self.email, bool(self.slack_webhook_url))

    @classmethod
    def for_user(cls, user):
        """Preferences for ``user``; an unsaved default row when none exists."""
        pref = cls.objects.filter(user=user).first()
        return pref or cls(user=user)


class Notification(models.Model):
    KIND_CHOICES = (
        ("system", "System"),
        ("comment_mention", "Comment mention"),
        ("followup_request", "Follow-up request"),
        ("followup_status", "Follow-up status"),
        ("favorite_activity", "Favorite activity"),
        ("data_access", "Data access"),
        ("interest", "Interest"),
        ("sharing_result", "Sharing result"),
        ("job_result", "Job result"),
    )

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=32, default="system", db_index=True)
    subject = models.CharField(max_length=200, blank=True, default="")
    text = models.TextField()
    url = models.CharField(max_length=500, blank=True, default="")
    transient = models.ForeignKey(
        Transient, null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications"
    )
    payload = JSONTextField(null=True, blank=True)
    delivered = JSONTextField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created", "-id")
        indexes = [
            models.Index(fields=["recipient", "read_at"], name="yse_notif_recip_read_idx"),
            models.Index(fields=["recipient", "created"], name="yse_notif_recip_created_idx"),
        ]

    def __str__(self):
        return "Notification %s -> %s: %s" % (self.pk, self.recipient.username, self.subject or self.text[:40])

    @property
    def is_read(self):
        return self.read_at is not None

    def mark_read(self):
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])
