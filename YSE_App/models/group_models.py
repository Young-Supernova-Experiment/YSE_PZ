"""Research-team profiles, invitation links and per-user picker preferences (#445, #440).

* ``GroupProfile``: one per ``auth.Group``. Owners (assigned by staff in the Django admin, as
  data), a description, an ``is_active`` flag (inactive groups are hidden from pickers and
  default audiences but keep all their data and access) and an ``is_system`` flag (permission
  groups such as ``api_writers`` and the Public group: never owned, joined or invited to).
* ``GroupInvitation``: an invite link. Only the SHA-256 of its random token is stored, so the
  link cannot be rebuilt from the database. It expires, has a maximum number of uses (1 by
  default: single-use) and can be revoked. No email address is stored.
* ``GroupInvitationUse``: who used an invitation and when (one row per user).
* ``GroupMembershipRequest``: a signed-in user asks to join a team (#448); the team's owners
  (or staff) approve or deny it. System groups cannot be requested.
* ``UserPreference``: groups a user hides from pickers, default audiences for comments and
  follow-up requests, and the default for the comment "Public" box.

A missing ``GroupProfile`` row means "active, not system unless named in
``YSE_SYSTEM_GROUP_NAMES`` or holding model permissions" (YSE_App/services/groups.py), so
groups created by code that predates these tables behave as before.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.db import models
from django.utils import timezone


class GroupProfile(models.Model):
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="group_profile")
    owners = models.ManyToManyField(
        User, blank=True, related_name="owned_group_profiles",
        help_text="Owners can invite people to the group, see its members and remove members. "
                  "System groups cannot have owners.",
    )
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive groups are hidden from group pickers and default audiences. Their data, "
                  "visibility and members are unchanged.",
    )
    is_system = models.BooleanField(
        default=False,
        help_text="Permission or site-wide group (for example Public, api_writers, explorer_users): no owners, "
                  "no invitations, membership is managed by staff in the admin only.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("group__name",)
        verbose_name = "group profile"

    def __str__(self):
        return "Group profile: %s" % self.group.name


class GroupInvitation(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="invitations")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    max_uses = models.PositiveIntegerField(default=1)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "group invitation"

    def __str__(self):
        return "Invitation #%s to %s" % (self.pk, self.group.name)

    @property
    def use_count(self) -> int:
        cached = getattr(self, "_use_count", None)
        if cached is not None:
            return cached
        return self.uses.count()

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_used_up(self) -> bool:
        return self.use_count >= self.max_uses

    @property
    def is_usable(self) -> bool:
        return not (self.is_revoked or self.is_expired or self.is_used_up)

    @property
    def status(self) -> str:
        if self.is_revoked:
            return "revoked"
        if self.is_expired:
            return "expired"
        if self.is_used_up:
            return "used"
        return "open"


class GroupInvitationUse(models.Model):
    invitation = models.ForeignKey(GroupInvitation, on_delete=models.CASCADE, related_name="uses")
    used_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    used_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-used_at", "-id")
        unique_together = (("invitation", "used_by"),)
        verbose_name = "group invitation use"

    def __str__(self):
        return "Use of invitation #%s" % self.invitation_id


class UserPreference(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="yse_preference")
    hidden_groups = models.ManyToManyField(
        Group, blank=True, related_name="hidden_by_preferences",
        help_text="Groups the user belongs to but does not want offered in pickers.",
    )
    comment_default_groups = models.ManyToManyField(Group, blank=True, related_name="comment_default_for_preferences")
    followup_default_groups = models.ManyToManyField(Group, blank=True, related_name="followup_default_for_preferences")
    # None = the site default (not public).
    comment_default_public = models.BooleanField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "user preference"

    def __str__(self):
        return "Preferences #%s" % self.pk


class GroupMembershipRequest(models.Model):
    """A request to join a research team (#448), decided by the team's owners or staff."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "pending"),
        (STATUS_APPROVED, "approved"),
        (STATUS_DENIED, "denied"),
        (STATUS_CANCELLED, "cancelled"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="group_membership_requests")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="membership_requests")
    message = models.TextField(blank=True, default="", help_text="Optional note to the team's owners.")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "group membership request"
        indexes = [models.Index(fields=["group", "status"], name="yse_gmr_group_status")]

    def __str__(self):
        return "Membership request #%s for %s (%s)" % (self.pk, self.group.name, self.status)

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING
