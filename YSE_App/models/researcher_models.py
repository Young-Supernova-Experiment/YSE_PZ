"""Researcher profile, mentorship, affiliation history, metrics and the user agreement.

Issues #627 (profile and encrypted fields), #628 (mentorship and affiliation
history), #629 (researcher metrics) and #630 (user agreement and onboarding
gate); docs/researcher-profile.md.

Privacy (org policy, FERPA): the encrypted fields (country, gender, PhD year)
are never columns here. They are stored only as public-key ciphertext in
``EncryptedProfileValue``, which nothing on the server can decrypt. None of
these tables has a REST serializer, and all of them are excluded from SQL
Explorer (``EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES`` and
``EXPLORER_SQL_BLACKLIST`` in settings). They are not registered with
django-auditlog; ``ProfileFieldHistory`` is the change record.

These models do not extend ``BaseModel``: the agreement is seeded by a data
migration before any user exists, and profile rows belong to their user.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from YSE_App.models.fields import JSONTextField
from YSE_App.models.instrument_models import Instrument

CAREER_UNDERGRAD = "undergrad"
CAREER_GRAD = "grad"
CAREER_POSTDOC = "postdoc"
CAREER_SCIENTIFIC_STAFF = "scientific_staff"
CAREER_TENURE_TRACK = "tenure_track_untenured"
CAREER_TENURED = "tenured"
CAREER_OTHER = "other"
CAREER_LEVEL_CHOICES = (
    (CAREER_UNDERGRAD, "Undergraduate student"),
    (CAREER_GRAD, "Graduate student"),
    (CAREER_POSTDOC, "Postdoc"),
    (CAREER_SCIENTIFIC_STAFF, "Scientific staff"),
    (CAREER_TENURE_TRACK, "Tenure-track faculty (not yet tenured)"),
    (CAREER_TENURED, "Tenured faculty"),
    (CAREER_OTHER, "Other"),
)
STUDENT_LEVELS = frozenset({CAREER_UNDERGRAD, CAREER_GRAD})
POSTDOC_AND_ABOVE = frozenset({CAREER_POSTDOC, CAREER_SCIENTIFIC_STAFF, CAREER_TENURE_TRACK, CAREER_TENURED})
# Career levels that see and manage "My students" on their profile.
MENTOR_LEVELS = frozenset({CAREER_SCIENTIFIC_STAFF, CAREER_TENURE_TRACK, CAREER_TENURED})

# Profile fields stored only as ciphertext (EncryptedProfileValue.field_name).
ENCRYPTED_FIELDS = ("country", "phd_year", "gender")


def _display_name(user):
    if user is None:
        return ""
    profile = getattr(user, "researcher_profile", None)
    if profile is not None and profile.full_name:
        return profile.full_name
    return user.get_full_name() or user.get_username()


class ResearcherProfile(models.Model):
    """One per user. The legacy phone ``Profile`` model is separate and unchanged."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="researcher_profile")
    full_name = models.CharField(max_length=200, blank=True, default="")
    preferred_name = models.CharField(max_length=100, blank=True, default="")
    pronouns = models.CharField(max_length=32, blank=True, default="")
    author_name_format = models.CharField(
        max_length=100, blank=True, default="",
        help_text="How your name appears in author lists, e.g. 'Lastname, F. M.'.",
    )
    institution = models.CharField(max_length=200, blank=True, default="")
    department = models.CharField(max_length=200, blank=True, default="")
    career_level = models.CharField(max_length=32, blank=True, default="", choices=CAREER_LEVEL_CHOICES)
    career_level_other = models.CharField(max_length=100, blank=True, default="")
    orcid = models.CharField(max_length=19, blank=True, default="", verbose_name="ORCID iD")
    research_interests = models.TextField(blank=True, default="")
    website = models.URLField(blank=True, default="")
    github_username = models.CharField(max_length=39, blank=True, default="")
    time_zone = models.CharField(max_length=64, blank=True, default="")
    observing_experience = models.ManyToManyField(Instrument, blank=True, related_name="+")
    acknowledgment_text = models.TextField(
        blank=True, default="",
        help_text="Personal grants and fellowships to acknowledge in papers.",
    )
    profile_completed_at = models.DateTimeField(null=True, blank=True)
    profile_confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "researcher profile"

    def __str__(self):
        return "Researcher profile #%s" % self.pk

    @property
    def is_student(self) -> bool:
        return self.career_level in STUDENT_LEVELS

    @property
    def is_postdoc_or_above(self) -> bool:
        return self.career_level in POSTDOC_AND_ABOVE

    @property
    def can_mentor(self) -> bool:
        return self.career_level in MENTOR_LEVELS


class EncryptedProfileValue(models.Model):
    """Public-key ciphertext of one encrypted profile field; never decryptable on the server."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="encrypted_profile_values")
    field_name = models.CharField(max_length=32)
    ciphertext = models.TextField(editable=False)
    key_fingerprint = models.CharField(max_length=64, editable=False)
    set_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("user", "field_name", "-set_at", "-id")
        indexes = [models.Index(fields=["user", "field_name", "set_at"], name="yse_encprof_user_field")]
        verbose_name = "encrypted profile value"

    def __str__(self):
        return self.display()

    def display(self) -> str:
        return "(encrypted, set on %s)" % timezone.localtime(self.set_at).date().isoformat()


class ProfileFieldHistory(models.Model):
    """One row per change to any profile field: gives every field a timestamp."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="profile_field_history")
    field_name = models.CharField(max_length=64)
    value_plain = models.TextField(blank=True, default="")
    encrypted_value = models.ForeignKey(
        EncryptedProfileValue, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    cleared = models.BooleanField(default=False)
    changed_at = models.DateTimeField(default=timezone.now)
    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ("-changed_at", "-id")
        indexes = [models.Index(fields=["user", "field_name", "changed_at"], name="yse_profhist_user_field")]
        verbose_name = "profile field history"
        verbose_name_plural = "profile field history"

    def __str__(self):
        return "%s changed" % self.field_name


class Mentorship(models.Model):
    """Advisor/supervisor relation; either side may be a free-form name when not a user."""

    KIND_PHD_ADVISOR = "phd_advisor"
    KIND_CURRENT_ADVISOR = "current_advisor"
    KIND_RESEARCH_SUPERVISOR = "research_supervisor"
    KIND_CHOICES = (
        (KIND_PHD_ADVISOR, "PhD advisor"),
        (KIND_CURRENT_ADVISOR, "Current advisor"),
        (KIND_RESEARCH_SUPERVISOR, "Research supervisor"),
    )

    mentor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="mentorships_as_mentor")
    mentor_name = models.CharField(max_length=200, blank=True, default="")
    mentee = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="mentorships_as_mentee")
    mentee_name = models.CharField(max_length=200, blank=True, default="")
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    start_year = models.SmallIntegerField(null=True, blank=True)
    end_year = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ("-start_year", "-id")
        constraints = [
            models.CheckConstraint(
                check=Q(mentor__isnull=False) | ~Q(mentor_name=""), name="yse_mentorship_has_mentor"),
            models.CheckConstraint(
                check=Q(mentee__isnull=False) | ~Q(mentee_name=""), name="yse_mentorship_has_mentee"),
        ]
        verbose_name = "mentorship"

    def __str__(self):
        return "%s (%s)" % (self.get_kind_display(), self.pk)

    def clean(self):
        if self.mentor_id is None and not (self.mentor_name or "").strip():
            raise ValidationError("Give the mentor as a user or a name.")
        if self.mentee_id is None and not (self.mentee_name or "").strip():
            raise ValidationError("Give the mentee as a user or a name.")
        if self.start_year and self.end_year and self.end_year < self.start_year:
            raise ValidationError("The end year is before the start year.")

    def save(self, *args, **kwargs):
        # Keep a name on each side so the row stays valid if a user account is deleted.
        if self.mentor_id and not self.mentor_name:
            self.mentor_name = _display_name(self.mentor)[:200]
        if self.mentee_id and not self.mentee_name:
            self.mentee_name = _display_name(self.mentee)[:200]
        super().save(*args, **kwargs)

    @property
    def mentor_label(self) -> str:
        return _display_name(self.mentor) if self.mentor_id else self.mentor_name

    @property
    def mentee_label(self) -> str:
        return _display_name(self.mentee) if self.mentee_id else self.mentee_name

    @property
    def is_current(self) -> bool:
        return self.end_year is None or self.end_year >= timezone.now().year


class AffiliationHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="affiliation_history")
    institution = models.CharField(max_length=200)
    department = models.CharField(max_length=200, blank=True, default="")
    position = models.CharField(max_length=200, blank=True, default="")
    start_year = models.SmallIntegerField(null=True, blank=True)
    end_year = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-start_year", "-id")
        verbose_name = "affiliation history"
        verbose_name_plural = "affiliation history"

    def __str__(self):
        return "Affiliation #%s" % self.pk


class ResearcherMetrics(models.Model):
    SOURCE_SCIX = "scix"
    SOURCE_ORCID = "orcid"
    SOURCE_OPENALEX = "openalex"
    SOURCE_CHOICES = (
        (SOURCE_SCIX, "SciX (ADS)"),
        (SOURCE_ORCID, "ORCID"),
        (SOURCE_OPENALEX, "OpenAlex"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="researcher_metrics")
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    fetched_at = models.DateTimeField(default=timezone.now)
    h_index = models.IntegerField(null=True, blank=True)
    citation_count = models.IntegerField(null=True, blank=True)
    refereed_count = models.IntegerField(null=True, blank=True)
    first_author_count = models.IntegerField(null=True, blank=True)
    raw = JSONTextField(null=True)

    class Meta:
        ordering = ("-fetched_at", "-id")
        indexes = [models.Index(fields=["user", "source", "fetched_at"], name="yse_resmetrics_user_src")]
        verbose_name = "researcher metrics"
        verbose_name_plural = "researcher metrics"

    def __str__(self):
        return "%s metrics #%s" % (self.source, self.pk)


class UserAgreement(models.Model):
    """A version of the YSE-PZ User Agreement. Saving one as current makes every other version not current."""

    version = models.CharField(max_length=32, unique=True)
    title = models.CharField(max_length=200)
    body = models.TextField(help_text="HTML shown on /agreement/.")
    effective_date = models.DateField(default=timezone.now)
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_date", "-id")
        verbose_name = "user agreement"

    def __str__(self):
        return "%s (%s)%s" % (self.title, self.version, " [current]" if self.is_current else "")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_current:
                type(self).objects.filter(is_current=True).exclude(pk=self.pk).update(is_current=False)
        _invalidate_gate_cache()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        _invalidate_gate_cache()
        return result


def _invalidate_gate_cache():
    from YSE_App.onboarding import invalidate_current_agreement
    invalidate_current_agreement()


class AgreementAcceptance(models.Model):
    """A user's acceptance of one agreement version. No IP address or user agent is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agreement_acceptances")
    agreement = models.ForeignKey(UserAgreement, on_delete=models.PROTECT, related_name="acceptances")
    accepted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-accepted_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["user", "agreement"], name="yse_agreement_accept_once"),
        ]
        verbose_name = "agreement acceptance"

    def __str__(self):
        return "Acceptance #%s" % self.pk


# ---------------------------------------------------------------------------
# Voluntary research consent (#744). Separate from the User Agreement and never a gate.
# Like the tables above: no REST serializer, excluded from SQL Explorer, admin read-only.

# Which researcher-profile data are kept for everyone and which are research data (Ryan,
# 2026-10-20, #744). Essential: what running the site needs (username, display name,
# publication and acknowledgment names, email, group memberships and roles, institution,
# ORCID). Everything else in the researcher profile is research data: offered only to users who
# consent, never required from others, excluded from research once a user declines or
# withdraws, and deleted by an administrator with ``purge_unconsented_demographics``.
ESSENTIAL_PROFILE_FIELDS = (
    "full_name", "preferred_name", "author_name_format", "acknowledgment_text", "institution", "orcid",
)
RESEARCH_PROFILE_FIELDS = (
    "pronouns", "department", "career_level", "career_level_other", "research_interests", "website",
    "github_username", "time_zone", "observing_experience",
)
# All encrypted fields (country, PhD year, gender) are research data.
RESEARCH_ENCRYPTED_FIELDS = ENCRYPTED_FIELDS
# Kept for older imports: the encrypted research fields.
DEMOGRAPHIC_FIELDS = RESEARCH_ENCRYPTED_FIELDS


class ResearchConsentForm(models.Model):
    """A version of the research consent form. ``effective_date`` is the form's go-live date.

    Activity before the go-live date of the first form is exempt (it was visible to other
    users, as on a public forum) and may be used anonymized; activity from that date on is
    used only for users who consented, inside their consent periods.
    """

    version = models.CharField(max_length=32, unique=True)
    title = models.CharField(max_length=200)
    body = models.TextField(help_text="HTML shown on /research-consent/.")
    effective_date = models.DateField(default=timezone.now, help_text="Go-live date of this form.")
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_date", "-id")
        verbose_name = "research consent form"

    def __str__(self):
        return "%s (%s)%s" % (self.title, self.version, " [current]" if self.is_current else "")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_current:
                type(self).objects.filter(is_current=True).exclude(pk=self.pk).update(is_current=False)


class ResearchConsentRecord(models.Model):
    """One consent decision, kept forever: the user's state is their latest record.

    No IP address or user agent is stored.
    """

    CONSENTED = "consented"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    DECISION_CHOICES = (
        (CONSENTED, "Consented"),
        (DECLINED, "Declined"),
        (WITHDRAWN, "Withdrawn"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="research_consent_records")
    form = models.ForeignKey(ResearchConsentForm, on_delete=models.PROTECT, related_name="records")
    decision = models.CharField(max_length=16, choices=DECISION_CHOICES)
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-decided_at", "-id")
        indexes = [models.Index(fields=["user", "decided_at"], name="yse_rconsent_user_time")]
        verbose_name = "research consent record"

    def __str__(self):
        return "Research consent record #%s" % self.pk


class ResearchConsentPrompt(models.Model):
    """When the consent form was offered to a user who has not answered yet (reminders, #744).

    Second request: after 10 logins or 1 month from the first prompt, whichever comes first;
    then once a year at the first login of the year. Users who answered are never reminded.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="research_consent_prompt")
    first_prompted_at = models.DateTimeField(default=timezone.now)
    last_prompted_at = models.DateTimeField(default=timezone.now)
    prompt_count = models.PositiveIntegerField(default=1)
    logins_since_first = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "research consent prompt"

    def __str__(self):
        return "Research consent prompt #%s" % self.pk
