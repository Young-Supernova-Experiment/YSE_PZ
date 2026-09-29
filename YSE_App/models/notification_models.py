"""In-app notifications and per-user delivery preferences (issues #266, #320; part of #69).

``Notification`` is the record a user sees in the in-app list; delivery to
other channels (email, Slack webhook) happens on the job queue and is
recorded in ``delivered``. ``NotificationPreference`` holds the per-user
channel switches (global ``in_app`` / ``email`` / Slack webhook) and, in
``kinds``, a per-kind-group matrix (#321): each notification kind belongs to
one of ``KIND_GROUPS`` and a channel is used only when both the global switch
and the group's switch are on. A user without a row gets the defaults.
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient

__all__ = ["Notification", "NotificationPreference", "KIND_GROUPS", "KIND_GROUP_DEFAULTS", "CHANNELS",
           "kind_group"]

# Delivery channels a preference matrix knows about.
CHANNELS = ("in_app", "email", "slack")

# Preference groups shown on /notifications/preferences/: (key, label, description, kinds).
KIND_GROUPS = (
    ("comment_mention", "Comment mentions",
     "Someone writes @you, @channel or #instrument in a transient comment.",
     ("comment_mention",)),
    ("followup", "Follow-up requests and status",
     "New follow-up requests on telescopes you follow and status changes on your requests.",
     ("followup_request", "followup_status")),
    ("alert", "Alerts",
     "New-transient alerts and failures of data uploads you submitted.",
     ("alert", "upload_error")),
    ("collaboration", "Interests and data access",
     "Paper interests registered on transients you work on; data access requests you can decide and "
     "decisions on your own requests.",
     ("interest", "data_access")),
    ("system", "System and jobs",
     "Everything else: background-job results and site announcements.",
     ("system", "job_result", "favorite_activity", "sharing_result")),
)

# Sensible defaults (#321): in-app on for every group; email on for mentions,
# follow-ups and alerts, off for system chatter; Slack (webhook) follows in-app.
KIND_GROUP_DEFAULTS = {
    "comment_mention": {"in_app": True, "email": True, "slack": True},
    "followup": {"in_app": True, "email": True, "slack": True},
    "alert": {"in_app": True, "email": True, "slack": True},
    "collaboration": {"in_app": True, "email": True, "slack": True},
    "system": {"in_app": True, "email": False, "slack": False},
}

_KIND_TO_GROUP = {kind: key for key, _label, _desc, kinds in KIND_GROUPS for kind in kinds}


def kind_group(kind):
    """Preference group key for a notification kind (unknown kinds count as ``system``)."""
    return _KIND_TO_GROUP.get(kind or "system", "system")


class NotificationPreference(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="notification_preference")
    in_app = models.BooleanField(default=True, help_text="Show notifications in the in-app list and badge.")
    email = models.BooleanField(default=True, help_text="Also send each notification to the account email.")
    slack_webhook_url = models.URLField(
        max_length=500, blank=True, default="",
        help_text="Optional Slack incoming-webhook URL; each notification is posted there as text.",
    )
    kinds = JSONTextField(
        null=True, blank=True,
        help_text="Per-kind-group channel switches: {group: {in_app, email, slack}}; missing entries use the defaults.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "notification preference"

    # --- per-kind matrix -------------------------------------------------

    def group_channels(self, group):
        """{channel: bool} for a preference group, defaults filled in."""
        base = dict(KIND_GROUP_DEFAULTS.get(group) or KIND_GROUP_DEFAULTS["system"])
        stored = (self.kinds or {}).get(group) if isinstance(self.kinds, dict) else None
        if isinstance(stored, dict):
            for channel in CHANNELS:
                if channel in stored:
                    base[channel] = bool(stored[channel])
        return base

    def matrix(self):
        """{group: {channel: bool}} for every group in ``KIND_GROUPS`` (for the form and API)."""
        return {key: self.group_channels(key) for key, _label, _desc, _kinds in KIND_GROUPS}

    def set_group_channels(self, group, **channels):
        """Store switches for one group (only the channels given change)."""
        data = dict(self.kinds) if isinstance(self.kinds, dict) else {}
        current = self.group_channels(group)
        for channel, value in channels.items():
            if channel in CHANNELS:
                current[channel] = bool(value)
        data[group] = current
        self.kinds = data

    def allows(self, kind, channel):
        """True when ``channel`` is on for notifications of ``kind`` (group switch only).

        The global switches (``in_app``, ``email``, ``slack_webhook_url``) and
        the site settings are checked by ``YSE_App.services.notify``.
        """
        if channel not in CHANNELS:
            return False
        return bool(self.group_channels(kind_group(kind)).get(channel))

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
        ("alert", "Transient alert"),
        ("upload_error", "Upload failure"),
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

    @property
    def kind_label(self):
        return dict(self.KIND_CHOICES).get(self.kind) or (self.kind or "system").replace("_", " ")

    @property
    def html(self):
        """Optional HTML email body stored by the sender in ``payload["html"]``."""
        payload = self.payload if isinstance(self.payload, dict) else {}
        return payload.get("html") or ""

    def mark_read(self):
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])
