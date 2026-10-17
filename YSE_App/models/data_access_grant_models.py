"""Read-time access grants and restrictions, and program deputies (#648, PR 1: schema only).

A :class:`DataAccessGrant` row is one rule: it **allows** or **denies** one
grantee (exactly one ``Group`` or one ``User``) access to one target:

* ``program``: every dataset taken under one ``Allocation`` (one telescope,
  one semester; Ryan, 2026-10-17, Q1). Datasets name their program with the
  nullable ``allocation`` FK on ``TransientPhotometry`` / ``TransientSpectrum``;
  data with no program link are not covered by program or night rules.
* ``night``: the program's data taken in one night, a UT window
  ``[night_start, night_end)`` (local noon to noon at the telescope), with an
  optional ``ClassicalObservingDate`` link for display. A spectrum matches by
  its ``obs_date``; photometry matches **per point** by each
  ``TransientPhotData.obs_date``, so a set can be partly covered.
* ``dataset``: one ``TransientPhotometry`` set or one ``TransientSpectrum``.
* ``points``: the points of one ``TransientPhotometry`` set taken in one night
  window. This is how per-point rules are stored (Q4: "stored per point, changed
  per object per night in the UI"): each point is decided by its own obs date,
  and the UI writes one row per photometry set of the object that has points
  in the chosen night, so all of one object's images from that night, in any
  filter, change together. A set-plus-window row (rather than one row per
  ``TransientPhotData``) also covers points of that night uploaded again or
  later, so a re-reduction cannot drop a restriction.

**Not enforced yet.** Nothing reads these rows until the precedence engine
lands (PR 2) and ``DATA_ACCESS_GRANTS_ENABLED`` is turned on; the read hooks
in ``services.visibility`` still return no rules.

**Precedence (PR 2 onwards, per the design):** staff see everything; public
data can never be restricted; the most specific level wins (points > dataset >
night > program > the dataset's own ``groups``); within a level a rule for the
user beats a rule for one of the user's groups; then deny beats allow.

**Revocation is soft.** Rows are never deleted; :meth:`DataAccessGrant.revoke`
sets ``revoked_at`` / ``revoked_by``. By default revoking an *allow* at the
program or night level applies **going forward** (Ryan, 2026-10-17, Q8):
``revoke_retroactive`` stays False and data whose obs date is before
``revoked_at`` keep the access; data taken later do not get it. Revoking a
*deny*, or a dataset / points rule (whose data already exist, so a forward
revocation would change nothing), is retroactive: the row stops applying
altogether. Either default can be overridden per revocation.
:meth:`DataAccessGrant.applies_to_obs_date` encodes this.

**No expiry.** Granted access has no end date (Q7); archive data become public
by changing the datasets' own access on the next archive sync.

Who may set rules: the program's PI (``Allocation.principal_investigator.user``),
its program deputies (:class:`AllocationDelegate`, separate from the PI's
group-sharing deputies ``DataSharingDelegate``), the data's PIs and their
deputies at the dataset level (``services.data_sharing.share_role``), and staff
as logged administrators. ``granted_as`` records the capacity. Both models are
registered with django-auditlog, so creation and revocation are logged.
"""

from __future__ import annotations

from auditlog.registry import auditlog
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from YSE_App.models.allocation_models import Allocation
from YSE_App.models.phot_models import TransientPhotometry
from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.models.telescope_resource_models import ClassicalObservingDate

__all__ = ["AllocationDelegate", "DataAccessGrant"]


_NO_WINDOW = Q(night_start__isnull=True, night_end__isnull=True, classical_night__isnull=True)
_WINDOW = Q(night_start__isnull=False, night_end__isnull=False)
_NO_DATASET = Q(transient_photometry__isnull=True, transient_spectrum__isnull=True)


class DataAccessGrant(models.Model):
    """One read-time allow / deny rule for one grantee on one target (see the module docstring)."""

    EFFECT_ALLOW = "allow"
    EFFECT_DENY = "deny"
    EFFECT_CHOICES = (
        (EFFECT_ALLOW, "Allow"),
        (EFFECT_DENY, "Restrict"),
    )

    LEVEL_PROGRAM = "program"
    LEVEL_NIGHT = "night"
    LEVEL_DATASET = "dataset"
    LEVEL_POINTS = "points"
    LEVEL_CHOICES = (
        (LEVEL_PROGRAM, "Program"),
        (LEVEL_NIGHT, "Night of a program"),
        (LEVEL_DATASET, "Dataset"),
        (LEVEL_POINTS, "Photometry points of one night"),
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

    effect = models.CharField(max_length=8, choices=EFFECT_CHOICES)
    level = models.CharField(
        max_length=16, choices=LEVEL_CHOICES, db_index=True,
        help_text="Which target is set (checked against the target fields).")

    # -- target ---------------------------------------------------------------
    allocation = models.ForeignKey(
        Allocation, null=True, blank=True, on_delete=models.CASCADE, related_name="access_grants",
        help_text="Program and night levels: the program (allocation).")
    night_start = models.DateTimeField(
        null=True, blank=True, help_text="Night and points levels: UT start of the night (inclusive).")
    night_end = models.DateTimeField(
        null=True, blank=True, help_text="Night and points levels: UT end of the night (exclusive).")
    classical_night = models.ForeignKey(
        ClassicalObservingDate, null=True, blank=True, on_delete=models.SET_NULL, related_name="access_grants",
        help_text="Optional: the classical night this window is (display and links only).")
    transient_photometry = models.ForeignKey(
        TransientPhotometry, null=True, blank=True, on_delete=models.CASCADE, related_name="access_grants",
        help_text="Dataset level: one photometry set. Points level: the set whose points in the window are covered.")
    transient_spectrum = models.ForeignKey(
        TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE, related_name="access_grants",
        help_text="Dataset level: one spectrum.")

    # -- grantee --------------------------------------------------------------
    grantee_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.CASCADE, related_name="data_access_grants")
    grantee_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="data_access_grants")

    # -- provenance -----------------------------------------------------------
    granted_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="data_access_grants_made")
    granted_as = models.CharField(max_length=16, choices=GRANTED_AS_CHOICES)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="data_access_grants_revoked")
    revoke_retroactive = models.BooleanField(
        default=False,
        help_text="Revoked rows only: True = the rule no longer applies to any data; False = it still applies "
                  "to data whose obs date is before the revocation (forward revocation).")

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["grantee_user", "revoked_at"], name="yse_dag_user_revoked"),
            models.Index(fields=["grantee_group", "revoked_at"], name="yse_dag_group_revoked"),
        ]
        constraints = [
            models.CheckConstraint(
                name="yse_dag_one_grantee",
                check=(Q(grantee_group__isnull=False, grantee_user__isnull=True)
                       | Q(grantee_group__isnull=True, grantee_user__isnull=False)),
            ),
            models.CheckConstraint(
                name="yse_dag_one_target",
                check=(
                    (Q(level="program", allocation__isnull=False) & _NO_WINDOW & _NO_DATASET)
                    | (Q(level="night", allocation__isnull=False) & _WINDOW & _NO_DATASET)
                    | (Q(level="dataset", allocation__isnull=True) & _NO_WINDOW
                       & (Q(transient_photometry__isnull=False, transient_spectrum__isnull=True)
                          | Q(transient_photometry__isnull=True, transient_spectrum__isnull=False)))
                    | (Q(level="points", allocation__isnull=True) & _WINDOW
                       & Q(transient_photometry__isnull=False, transient_spectrum__isnull=True))
                ),
            ),
            models.CheckConstraint(
                name="yse_dag_window_order",
                check=Q(night_start__isnull=True) | Q(night_end__isnull=True) | Q(night_end__gt=F("night_start")),
            ),
            models.CheckConstraint(name="yse_dag_effect", check=Q(effect__in=("allow", "deny"))),
        ]

    def __str__(self):
        return "DataAccessGrant %s: %s %s on %s %s%s" % (
            self.pk, self.get_effect_display(), self.grantee_label, self.get_level_display(), self.target_label,
            " (revoked)" if self.revoked_at else "")

    # -- derived --------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def grantee_label(self) -> str:
        if self.grantee_user_id:
            return "user %s" % self.grantee_user.username
        if self.grantee_group_id:
            return "group %s" % self.grantee_group.name
        return "(no grantee)"

    @property
    def target_label(self) -> str:
        if self.transient_spectrum_id:
            return "spectrum %s" % self.transient_spectrum_id
        if self.transient_photometry_id:
            return "photometry %s" % self.transient_photometry_id
        if self.allocation_id:
            return "allocation %s" % self.allocation_id
        return "(no target)"

    @classmethod
    def derive_level(cls, *, allocation_id=None, night_start=None, transient_photometry_id=None,
                     transient_spectrum_id=None) -> str:
        """The level implied by which target fields are set (``clean`` checks the rest)."""
        if transient_photometry_id or transient_spectrum_id:
            return cls.LEVEL_POINTS if night_start is not None else cls.LEVEL_DATASET
        if night_start is not None:
            return cls.LEVEL_NIGHT
        return cls.LEVEL_PROGRAM

    def default_revoke_retroactive(self) -> bool:
        """Forward revocation for program / night allows (Q8); retroactive for everything else."""
        return not (self.effect == self.EFFECT_ALLOW and self.level in (self.LEVEL_PROGRAM, self.LEVEL_NIGHT))

    def applies_to_obs_date(self, obs_date) -> bool:
        """Whether this row still governs data taken at ``obs_date`` (target matching aside).

        Active rows always do. A revoked row does only when it was revoked
        going forward and the data were taken before the revocation. The
        night window, when set, must also contain ``obs_date``.
        """
        if self.night_start is not None and obs_date is not None:
            if not (self.night_start <= obs_date < self.night_end):
                return False
        if self.revoked_at is None:
            return True
        if self.revoke_retroactive or obs_date is None:
            return False
        return obs_date < self.revoked_at

    # -- validation and actions -----------------------------------------------
    def _same_target_q(self) -> Q:
        return Q(
            level=self.level, allocation_id=self.allocation_id, night_start=self.night_start,
            night_end=self.night_end, transient_photometry_id=self.transient_photometry_id,
            transient_spectrum_id=self.transient_spectrum_id, grantee_group_id=self.grantee_group_id,
            grantee_user_id=self.grantee_user_id,
        )

    def clean(self):
        errors = []
        if bool(self.grantee_group_id) == bool(self.grantee_user_id):
            errors.append("Give exactly one grantee: a group or a user.")
        if self.grantee_group_id:
            from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
            if self.grantee_group.name == PUBLIC_COLLABORATION_GROUP_NAME:
                errors.append("The Public group cannot be a grantee; change the datasets' own groups instead.")

        has_window = self.night_start is not None or self.night_end is not None
        if has_window and (self.night_start is None or self.night_end is None):
            errors.append("Give both the start and the end of the night.")
        elif has_window and self.night_end <= self.night_start:
            errors.append("The night must end after it starts.")
        if self.classical_night_id and not has_window:
            errors.append("A classical night needs the night's UT window.")

        expected = self.derive_level(
            allocation_id=self.allocation_id, night_start=self.night_start,
            transient_photometry_id=self.transient_photometry_id, transient_spectrum_id=self.transient_spectrum_id)
        if not self.level:
            self.level = expected
        n_datasets = int(bool(self.transient_photometry_id)) + int(bool(self.transient_spectrum_id))
        if n_datasets > 1:
            errors.append("Give one dataset: a photometry set or a spectrum.")
        if n_datasets and self.allocation_id:
            errors.append("A dataset rule names the dataset, not a program.")
        if not n_datasets and not self.allocation_id:
            errors.append("Give a target: a program, a night of a program, a dataset, or one night of a "
                          "photometry set.")
        if self.transient_spectrum_id and has_window:
            errors.append("A spectrum has one obs date; restrict the spectrum itself.")
        if self.level != expected and not errors:
            errors.append("The level does not match the target (expected %s)." % expected)

        if not errors and self.revoked_at is None:
            clash = DataAccessGrant.objects.filter(self._same_target_q(), revoked_at__isnull=True)
            if self.pk:
                clash = clash.exclude(pk=self.pk)
            if clash.exists():
                errors.append("An active rule already exists for this grantee and target; revoke it first.")
        if errors:
            raise ValidationError(errors)

    def revoke(self, by: User | None, *, retroactive: bool | None = None, at=None, save: bool = True):
        """Soft-revoke this row (never deleted). ``retroactive=None`` uses :meth:`default_revoke_retroactive`."""
        if self.revoked_at is not None:
            return self
        self.revoked_at = at or timezone.now()
        self.revoked_by = by
        self.revoke_retroactive = self.default_revoke_retroactive() if retroactive is None else bool(retroactive)
        if save:
            self.save(update_fields=["revoked_at", "revoked_by", "revoke_retroactive"])
        return self


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
        return "%s is a deputy for allocation %s" % (self.user.username, self.allocation_id)


auditlog.register(DataAccessGrant)
auditlog.register(AllocationDelegate)
