"""Alerts on Selections (#844): rules that tell people when a Selection's results change.

* ``SelectionAlert``: one rule on one :class:`~YSE_App.models.Selection`, set by ``owner``.
  It fires on a new match, a new spectrum, a class change and/or a magnitude crossing, and is
  delivered immediately or as an hourly or daily digest. It goes to the owner (``target='me'``),
  to every member of ``team`` (``'team'``) or to a Slack channel of the team (``'slack'``); only
  the team's owners (``GroupProfile.owners``) may set the last two
  (``YSE_App.services.selection_alerts``, checked on save and on every run).
* ``SelectionAlertSnapshot``: what one recipient last saw for one rule (``state``), plus the
  events that wait for the next digest (``pending``). One row per recipient user, or one row with
  no user for a Slack channel. The Selection is evaluated *as the recipient* every run, so a
  snapshot never holds anything the recipient could not see.

Both tables are new; develop's code never reads or writes them. Their foreign keys get the
matching MySQL ``ON DELETE`` in migration 0072 (``YSE_App.common.fk_on_delete``), so develop's
deletes of users, groups and (through the Selection's own cascade) Selections keep working on
the shared database.
"""

from django.contrib.auth.models import Group, User
from django.db import models

from YSE_App.models.fields import JSONTextField
from YSE_App.models.selection_models import Selection


class SelectionAlert(models.Model):
    TARGET_ME = "me"
    TARGET_TEAM = "team"
    TARGET_SLACK = "slack"
    TARGET_CHOICES = (
        (TARGET_ME, "Me"),
        (TARGET_TEAM, "Everyone in a team I own"),
        (TARGET_SLACK, "The Slack channel of a team I own"),
    )

    FREQUENCY_IMMEDIATE = "immediate"
    FREQUENCY_HOURLY = "hourly"
    FREQUENCY_DAILY = "daily"
    FREQUENCY_CHOICES = (
        (FREQUENCY_IMMEDIATE, "Immediately"),
        (FREQUENCY_HOURLY, "Hourly digest"),
        (FREQUENCY_DAILY, "Daily digest"),
    )

    BRIGHTER = "brighter"
    FAINTER = "fainter"
    MAG_DIRECTION_CHOICES = (
        (BRIGHTER, "becomes brighter than"),
        (FAINTER, "fades below"),
    )

    selection = models.ForeignKey(Selection, on_delete=models.CASCADE, related_name="alerts")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="selection_alerts",
                              help_text="Who set the alert; the recipient when the target is 'me'.")
    label = models.CharField(max_length=120, blank=True, default="",
                             help_text="Optional name shown in the messages; the Selection's name otherwise.")
    target = models.CharField(max_length=8, choices=TARGET_CHOICES, default=TARGET_ME)
    team = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE, related_name="selection_alerts",
                             help_text="The team notified (targets 'team' and 'slack').")
    slack_channel = models.CharField(max_length=32, blank=True, default="",
                                     help_text="Slack channel id (C... or G...) for target 'slack'.")

    on_new_match = models.BooleanField(default=True)
    on_new_spectrum = models.BooleanField(default=False)
    on_class_change = models.BooleanField(default=False)
    mag_threshold = models.FloatField(null=True, blank=True,
                                      help_text="Alert when the latest detection crosses this magnitude.")
    mag_direction = models.CharField(max_length=8, choices=MAG_DIRECTION_CHOICES, default=BRIGHTER)

    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default=FREQUENCY_DAILY)
    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ("selection__name", "pk")

    def __str__(self):
        return "Alert on %s (%s)" % (self.selection.name, self.target)

    @property
    def display_name(self):
        return self.label or self.selection.name

    def trigger_labels(self):
        out = []
        if self.on_new_match:
            out.append("new match")
        if self.on_new_spectrum:
            out.append("new spectrum")
        if self.on_class_change:
            out.append("class change")
        if self.mag_threshold is not None:
            out.append("%s %g mag" % (self.get_mag_direction_display(), self.mag_threshold))
        return out


class SelectionAlertSnapshot(models.Model):
    alert = models.ForeignKey(SelectionAlert, on_delete=models.CASCADE, related_name="snapshots")
    recipient = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE, related_name="+",
                                  help_text="Empty for a Slack channel (evaluated as every team member).")
    state = JSONTextField(null=True, blank=True,
                          help_text="{transient id: [class, newest spectrum id, latest point id, latest mag]}.")
    pending = JSONTextField(null=True, blank=True, help_text="Events waiting for the next delivery.")
    baseline_at = models.DateTimeField(null=True, blank=True)
    last_delivered_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("alert", "recipient"),)

    def __str__(self):
        return "Snapshot of alert %s for %s" % (self.alert_id, self.recipient_id or "Slack")
