"""Per-point access rules, standing grants and access history (#648; PR 1b: schema only).

Ryan's design revision (2026-10-17): **rules are stored per point**.

* :class:`DataAccessRule` is one rule for one photometry point
  (``TransientPhotData``) or one spectrum (``TransientSpectrum``): it
  **allows** or **denies** one grantee (exactly one ``Group`` or one ``User``).
  At most one rule per (point or spectrum, grantee), enforced by unique
  constraints. Program, night and dataset are not rule levels: they are
  *actions* (PR 2 onwards) that write one rule per matching point.
  ``transient`` and, for points, ``photometry`` are copies of the point's
  transient and set, kept so set- and transient-level reads (and PhotStat
  invalidation) need no join through the point table.
* A PI may restrict any non-public data from anyone, also data already
  shared (Ryan, 2026-10-17): a deny may replace an allow, and an allow a deny;
  ``updated_at`` is when the rule last changed (for an allow, when the point
  was shared). Turning sharing off goes forward in time by default (earlier
  data stay visible); removing a group from a dataset's own groups writes
  ``kept`` allow rules for the points shared before the change.
  django-auditlog records every change and deletion.
* :class:`StandingAccessGrant` is an action on a program (an ``Allocation``,
  one telescope for one semester) or on one night of a program, saved so that
  points taken **later** get their rule written at ingest. Stopping it
  (``stopped_at``) applies going forward in time: points taken after the stop
  are not covered, points taken before it still are (also when uploaded or
  linked later); rules already written stay.
* :class:`GroupMembershipPeriod` is dated group membership: a person who
  leaves a group keeps the points shared with it while they were a member and
  does not get points shared with it after they left. Memberships that existed
  before tracking began have ``joined_at`` unknown (null): "since before
  tracking".
* :class:`DatasetGroupShare` records when a collaboration group was added to a
  dataset's own ``groups`` (the base sharing, unchanged). A point is shared
  with a group through ``groups`` at the later of that time and the point's
  creation. A dataset group with no share row was shared before tracking
  began.
* :class:`AccessRulesVersion` is a single-row counter, bumped whenever rules or
  standing grants are written, so resolved rules can be cached per request and
  across processes without going stale.

**Not enforced yet.** Nothing reads these rows until the engine lands (PR 2)
and ``DATA_ACCESS_GRANTS_ENABLED`` is on. The history tables
(:class:`GroupMembershipPeriod`, :class:`DatasetGroupShare`) are written from
now on, flag or not, so the history exists when the engine is turned on
(``services.access_history``).

Who may act: the program's PI (``Allocation.principal_investigator.user``) and
its program deputies (:class:`AllocationDelegate`) for program and night
actions; the data's PIs and their deputies (``services.data_sharing.share_role``)
for one dataset; staff as logged administrators. ``granted_as`` records the
capacity.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from YSE_App.models.allocation_models import Allocation
from YSE_App.models.phot_models import TransientPhotData, TransientPhotometry
from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.models.telescope_resource_models import ClassicalObservingDate
from YSE_App.models.transient_models import Transient

__all__ = [
    "AccessRulesVersion",
    "AllocationDelegate",
    "DataAccessRule",
    "DatasetGroupShare",
    "GroupMembershipPeriod",
    "StandingAccessGrant",
]

EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
EFFECT_CHOICES = (
    (EFFECT_ALLOW, "Allow"),
    (EFFECT_DENY, "Restrict"),
)

AS_PI = "pi"
AS_PROGRAM_DEPUTY = "program_deputy"
AS_GROUP_DEPUTY = "group_deputy"
AS_STAFF = "staff"
GRANTED_AS_CHOICES = (
    (AS_PI, "PI"),
    (AS_PROGRAM_DEPUTY, "Program deputy"),
    (AS_GROUP_DEPUTY, "PI's data-sharing deputy"),
    (AS_STAFF, "Administrator"),
)
# Rules written by the system to keep earlier data visible when sharing is turned off going forward
# (a group removed from a dataset's own groups; #648, PR 6a).
AS_KEPT = "kept"
RULE_GRANTED_AS_CHOICES = GRANTED_AS_CHOICES + ((AS_KEPT, "Kept after a sharing change"),)

# "At most one" in the database (exactly one is checked by ``clean``): on MySQL, Django's
# cascade sets a nullable CASCADE foreign key to NULL just before deleting the row, so
# a deleted group, user, point or dataset must not trip the check.
_ONE_GRANTEE = ~Q(grantee_group__isnull=False, grantee_user__isnull=False)


def _clean_grantee(row, errors):
    if bool(row.grantee_group_id) == bool(row.grantee_user_id):
        errors.append("Give exactly one grantee: a group or a user.")
    if row.grantee_group_id:
        from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
        if row.grantee_group.name == PUBLIC_COLLABORATION_GROUP_NAME:
            errors.append("The Public group cannot be a grantee; change the datasets' own groups instead.")


def _grantee_label(row) -> str:
    # Ids only: no query, and safe while a cascade is deleting the grantee (the audit log calls str()).
    if row.grantee_user_id:
        return "user %s" % row.grantee_user_id
    if row.grantee_group_id:
        return "group %s" % row.grantee_group_id
    return "(no grantee)"


class DataAccessRule(models.Model):
    """One allow / deny rule for one grantee on one photometry point or one spectrum (module docstring)."""

    EFFECT_ALLOW = EFFECT_ALLOW
    EFFECT_DENY = EFFECT_DENY
    AS_PI = AS_PI
    AS_PROGRAM_DEPUTY = AS_PROGRAM_DEPUTY
    AS_GROUP_DEPUTY = AS_GROUP_DEPUTY
    AS_STAFF = AS_STAFF
    AS_KEPT = AS_KEPT

    effect = models.CharField(max_length=8, choices=EFFECT_CHOICES)

    # -- target: exactly one of the point or the spectrum ---------------------
    transient_phot_data = models.ForeignKey(
        TransientPhotData, null=True, blank=True, on_delete=models.CASCADE, related_name="access_rules",
        help_text="The photometry point this rule is for.")
    transient_spectrum = models.ForeignKey(
        TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE, related_name="access_rules",
        help_text="The spectrum this rule is for.")
    # Copies of the target's set and transient (filled on save).
    photometry = models.ForeignKey(
        TransientPhotometry, null=True, blank=True, on_delete=models.CASCADE, related_name="access_rules",
        help_text="The point's photometry set (copied from the point).")
    transient = models.ForeignKey(
        Transient, on_delete=models.CASCADE, related_name="access_rules",
        help_text="The target's transient (copied from the point or spectrum).")

    # -- grantee --------------------------------------------------------------
    grantee_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.CASCADE, related_name="data_access_rules")
    grantee_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="data_access_rules")

    # -- provenance -----------------------------------------------------------
    granted_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="data_access_rules_made")
    granted_as = models.CharField(max_length=16, choices=RULE_GRANTED_AS_CHOICES)
    standing_grant = models.ForeignKey(
        "StandingAccessGrant", null=True, blank=True, on_delete=models.SET_NULL, related_name="rules",
        help_text="The standing grant that wrote this rule at ingest, if any.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(
        auto_now=True, help_text="Last change: for an allow, when the point was shared with the grantee.")

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["grantee_user", "effect"], name="yse_dar_user_effect"),
            models.Index(fields=["grantee_group", "effect"], name="yse_dar_group_effect"),
        ]
        constraints = [
            models.CheckConstraint(name="yse_dar_one_grantee", check=_ONE_GRANTEE),
            models.CheckConstraint(
                name="yse_dar_one_target",
                check=~Q(transient_phot_data__isnull=False, transient_spectrum__isnull=False),
            ),
            models.CheckConstraint(name="yse_dar_effect", check=Q(effect__in=(EFFECT_ALLOW, EFFECT_DENY))),
            # One rule per target and grantee. NULLs never collide in a unique index, so the
            # four pairs together cover every (target, grantee) combination.
            models.UniqueConstraint(fields=["transient_phot_data", "grantee_group"], name="yse_dar_point_group"),
            models.UniqueConstraint(fields=["transient_phot_data", "grantee_user"], name="yse_dar_point_user"),
            models.UniqueConstraint(fields=["transient_spectrum", "grantee_group"], name="yse_dar_spec_group"),
            models.UniqueConstraint(fields=["transient_spectrum", "grantee_user"], name="yse_dar_spec_user"),
        ]

    def __str__(self):
        target = ("spectrum %s" % self.transient_spectrum_id if self.transient_spectrum_id
                  else "point %s" % self.transient_phot_data_id)
        return "DataAccessRule %s: %s %s on %s" % (self.pk, self.get_effect_display(), _grantee_label(self), target)

    @property
    def grantee_label(self) -> str:
        return _grantee_label(self)

    def fill_copies(self):
        """Set ``photometry`` and ``transient`` from the target."""
        if self.transient_phot_data_id:
            point = self.transient_phot_data
            self.photometry_id = point.photometry_id
            self.transient_id = point.photometry.transient_id
        elif self.transient_spectrum_id:
            self.photometry_id = None
            self.transient_id = self.transient_spectrum.transient_id

    def clean_fields(self, exclude=None):
        # The copies are derived from the target, so fill them before the field checks.
        if bool(self.transient_phot_data_id) != bool(self.transient_spectrum_id):
            self.fill_copies()
        super().clean_fields(exclude=exclude)

    def clean(self):
        errors = []
        _clean_grantee(self, errors)
        if bool(self.transient_phot_data_id) == bool(self.transient_spectrum_id):
            errors.append("Give exactly one target: a photometry point or a spectrum.")
        if errors:
            raise ValidationError(errors)
        self.fill_copies()

    def save(self, *args, **kwargs):
        if not self.transient_id or (self.transient_phot_data_id and not self.photometry_id):
            self.fill_copies()
        super().save(*args, **kwargs)


class StandingAccessGrant(models.Model):
    """An access action on a program, or one night of it, applied to points that arrive later (module docstring)."""

    EFFECT_ALLOW = EFFECT_ALLOW
    EFFECT_DENY = EFFECT_DENY
    AS_PI = AS_PI
    AS_PROGRAM_DEPUTY = AS_PROGRAM_DEPUTY
    AS_STAFF = AS_STAFF

    effect = models.CharField(max_length=8, choices=EFFECT_CHOICES)
    allocation = models.ForeignKey(
        Allocation, on_delete=models.CASCADE, related_name="standing_access_grants",
        help_text="The program (allocation) whose data this applies to.")
    night_start = models.DateTimeField(
        null=True, blank=True, help_text="One night only: UT start of the night (inclusive).")
    night_end = models.DateTimeField(
        null=True, blank=True, help_text="One night only: UT end of the night (exclusive).")
    classical_night = models.ForeignKey(
        ClassicalObservingDate, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="standing_access_grants",
        help_text="Optional: the classical night this window is (display and links only).")

    grantee_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.CASCADE, related_name="standing_access_grants")
    grantee_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="standing_access_grants")

    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="standing_access_grants_made")
    created_as = models.CharField(max_length=16, choices=GRANTED_AS_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    stopped_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="When it stopped applying to new points; rules already written stay.")
    stopped_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="standing_access_grants_stopped")
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["allocation", "stopped_at"], name="yse_sag_alloc_stopped"),
        ]
        constraints = [
            models.CheckConstraint(name="yse_sag_one_grantee", check=_ONE_GRANTEE),
            models.CheckConstraint(
                name="yse_sag_window",
                check=(Q(night_start__isnull=True, night_end__isnull=True, classical_night__isnull=True)
                       | Q(night_start__isnull=False, night_end__isnull=False, night_end__gt=F("night_start"))),
            ),
            models.CheckConstraint(name="yse_sag_effect", check=Q(effect__in=(EFFECT_ALLOW, EFFECT_DENY))),
        ]

    def __str__(self):
        scope = "night %s" % self.night_start.date() if self.night_start else "program"
        return "StandingAccessGrant %s: %s %s on allocation %s (%s)%s" % (
            self.pk, self.get_effect_display(), _grantee_label(self), self.allocation_id, scope,
            " (stopped)" if self.stopped_at else "")

    @property
    def is_active(self) -> bool:
        return self.stopped_at is None

    @property
    def grantee_label(self) -> str:
        return _grantee_label(self)

    def covers_obs_date(self, obs_date) -> bool:
        """Whether a point of this program taken at ``obs_date`` is in scope: in the night (any date for a
        program-wide grant), and before the stop when the grant was stopped."""
        if self.stopped_at is not None and (obs_date is None or obs_date >= self.stopped_at
                                            or self.stopped_at <= self.created_at):
            return False  # stopped: data taken later are not covered; stopped backwards: none are
        if self.night_start is None:
            return True
        return obs_date is not None and self.night_start <= obs_date < self.night_end

    def clean(self):
        errors = []
        _clean_grantee(self, errors)
        has_window = self.night_start is not None or self.night_end is not None
        if has_window and (self.night_start is None or self.night_end is None):
            errors.append("Give both the start and the end of the night.")
        elif has_window and self.night_end <= self.night_start:
            errors.append("The night must end after it starts.")
        if self.classical_night_id and not has_window:
            errors.append("A classical night needs the night's UT window.")
        if errors:
            raise ValidationError(errors)


class GroupMembershipPeriod(models.Model):
    """One period of one user's membership of one group (``joined_at`` null: since before tracking)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="group_membership_periods")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="membership_periods")
    joined_at = models.DateTimeField(null=True, blank=True, help_text="Null: a member since before tracking began.")
    left_at = models.DateTimeField(null=True, blank=True, help_text="Null: still a member.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user_id", "group_id", "id")
        indexes = [
            models.Index(fields=["user", "group", "left_at"], name="yse_gmp_user_group_left"),
            models.Index(fields=["group", "left_at"], name="yse_gmp_group_left"),
        ]
        constraints = [
            models.CheckConstraint(
                name="yse_gmp_order",
                check=Q(joined_at__isnull=True) | Q(left_at__isnull=True) | Q(left_at__gte=F("joined_at")),
            ),
        ]

    def __str__(self):
        return "user %s in group %s from %s to %s" % (
            self.user_id, self.group_id, self.joined_at or "before tracking", self.left_at or "now")


class DatasetGroupShare(models.Model):
    """When a group was added to one dataset's own ``groups`` (no row: shared before tracking began)."""

    transient_photometry = models.ForeignKey(
        TransientPhotometry, null=True, blank=True, on_delete=models.CASCADE, related_name="group_shares")
    transient_spectrum = models.ForeignKey(
        TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE, related_name="group_shares")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="dataset_shares")
    shared_at = models.DateTimeField()

    class Meta:
        ordering = ("-shared_at", "-id")
        constraints = [
            models.CheckConstraint(
                name="yse_dgs_one_dataset",
                check=~Q(transient_photometry__isnull=False, transient_spectrum__isnull=False),
            ),
            models.UniqueConstraint(fields=["transient_photometry", "group"], name="yse_dgs_phot_group"),
            models.UniqueConstraint(fields=["transient_spectrum", "group"], name="yse_dgs_spec_group"),
        ]

    def __str__(self):
        target = ("spectrum %s" % self.transient_spectrum_id if self.transient_spectrum_id
                  else "photometry %s" % self.transient_photometry_id)
        return "%s shared with group %s at %s" % (target, self.group_id, self.shared_at)


class AccessRulesVersion(models.Model):
    """Single row (pk 1): bumped whenever rules or standing grants are written (cache key for resolved rules)."""

    version = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "access rules version %s" % self.version


class AllocationDelegate(models.Model):
    """A program deputy (#648, Q6): may set access rules for one allocation's data, like its PI.

    Separate from ``DataSharingDelegate`` (a PI's deputies for deciding data
    access requests on the PI's datasets). The program's PI is the account
    linked to ``Allocation.principal_investigator`` (``PrincipalInvestigator.user``).
    """

    allocation = models.ForeignKey(Allocation, on_delete=models.CASCADE, related_name="delegates")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="allocation_delegations")
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("allocation_id", "user__username", "id")
        constraints = [
            models.UniqueConstraint(fields=["allocation", "user"], name="yse_allocationdelegate_unique"),
        ]

    def __str__(self):
        return "user %s is a deputy for allocation %s" % (self.user_id, self.allocation_id)


auditlog.register(DataAccessRule)
auditlog.register(StandingAccessGrant)
auditlog.register(AllocationDelegate)
