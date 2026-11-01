"""Researcher-profile rules: requirements per career level, ORCID check, field history (#627, #628).

Nothing here decrypts anything: encrypted fields are write-only on the server.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from YSE_App.models.researcher_models import (
    CAREER_OTHER,
    ENCRYPTED_FIELDS,
    EncryptedProfileValue,
    Mentorship,
    ProfileFieldHistory,
    ResearcherProfile,
)
from YSE_App.services import research_crypto

logger = logging.getLogger(__name__)

# Bump to re-evaluate every session's cached "onboarding complete" flag after a rule change.
RULES_VERSION = "2"  # 2: research fields required only with research consent (#744)

PHD_YEAR_MIN = 1950

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

# Plain profile fields recorded in ProfileFieldHistory (encrypted ones are added separately).
PLAIN_FIELDS = (
    "full_name", "preferred_name", "pronouns", "author_name_format", "institution", "department",
    "career_level", "career_level_other", "orcid", "research_interests", "website", "github_username",
    "time_zone", "observing_experience", "acknowledgment_text",
)

FIELD_LABELS = {
    "full_name": "Full name",
    "institution": "Institution",
    "department": "Department",
    "career_level": "Career level",
    "career_level_other": "Career level (other)",
    "orcid": "ORCID iD",
    "phd_year": "PhD year",
    "current_advisor": "Current advisor",
}


def phd_year_max() -> int:
    return timezone.now().year + 1


def orcid_checksum_ok(orcid: str) -> bool:
    """ISO 7064 MOD 11-2 check of a hyphenated ORCID iD."""
    if not ORCID_RE.match(orcid or ""):
        return False
    digits = orcid.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    result = (12 - total % 11) % 11
    expected = "X" if result == 10 else str(result)
    return digits[-1] == expected


def normalize_orcid(value: str) -> str:
    value = (value or "").strip().upper()
    for prefix in ("HTTPS://ORCID.ORG/", "HTTP://ORCID.ORG/", "ORCID.ORG/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if re.match(r"^\d{15}[\dX]$", value):
        value = "-".join(value[i:i + 4] for i in range(0, 16, 4))
    return value


def get_profile(user: User) -> Optional[ResearcherProfile]:
    try:
        return user.researcher_profile
    except ResearcherProfile.DoesNotExist:
        return None


def own_mentorships(user: User):
    """The user's own confirmed rows (each side keeps its own copy of a relation)."""
    return Mentorship.objects.filter(owner=user, status=Mentorship.STATUS_CONFIRMED)


def my_advisors(user: User):
    """Rows where ``user`` is the mentee, on their own side (the "My advisors" list)."""
    return own_mentorships(user).filter(mentee=user).select_related("mentor").order_by("-start_year", "-id")


def pending_mentorships(user: User):
    """Relations someone else entered with ``user`` that wait for ``user`` to confirm."""
    return (Mentorship.objects.filter(owner=user, status=Mentorship.STATUS_PENDING)
            .select_related("mentor", "mentee", "created_by").order_by("-created_at", "-id"))


def current_advisor(user: User) -> Optional[Mentorship]:
    year = timezone.now().year
    return (own_mentorships(user)
            .filter(mentee=user, kind=Mentorship.KIND_CURRENT_ADVISOR)
            .filter(Q(end_year__isnull=True) | Q(end_year__gte=year))
            .select_related("mentor").order_by("-id").first())


def encrypted_status(user: User) -> Dict[str, EncryptedProfileValue]:
    """Latest ciphertext row per encrypted field (metadata only; never decrypted)."""
    status: Dict[str, EncryptedProfileValue] = {}
    for row in EncryptedProfileValue.objects.filter(user=user).only("field_name", "set_at", "user_id"):
        if row.field_name not in status or row.set_at > status[row.field_name].set_at:
            status[row.field_name] = row
    return status


# Required from everyone, whatever their research consent (essential data, #744).
ESSENTIAL_REQUIRED = ("full_name", "institution")


def required_fields(career_level: str, key_available: bool, research: bool = True) -> List[str]:
    """Required field names for a career level (``current_advisor`` is the Mentorship row).

    ``research=False`` (no research consent): only the essential fields; everything else in the
    profile is research data and is neither offered nor required (#744).
    """
    if not research:
        return list(ESSENTIAL_REQUIRED)
    required = ["full_name", "institution", "department", "career_level"]
    if career_level == CAREER_OTHER:
        required.append("career_level_other")
    if career_level in ("postdoc", "scientific_staff", "tenure_track_untenured", "tenured"):
        required.append("orcid")
        if key_available:
            required.append("phd_year")
    if career_level in ("undergrad", "grad"):
        required.append("current_advisor")
    return required


def missing_fields(user: User, profile: Optional[ResearcherProfile] = None,
                   key_available: Optional[bool] = None, research: Optional[bool] = None) -> List[str]:
    """Required fields the user has not filled in (empty list = complete).

    ``research`` defaults to whether the user has consented to the research study.
    """
    if research is None:
        from YSE_App.services import research_consent
        research = research_consent.has_consented(user)
    if key_available is None:
        key_available = research_crypto.key_available()
    if profile is None:
        profile = get_profile(user)
    if profile is None:
        return ["profile"]
    missing = []
    for name in required_fields(profile.career_level, key_available, research):
        if name == "phd_year":
            if not EncryptedProfileValue.objects.filter(user=user, field_name="phd_year").exists():
                missing.append(name)
        elif name == "current_advisor":
            if current_advisor(user) is None:
                missing.append(name)
        elif name == "orcid":
            if not orcid_checksum_ok(profile.orcid):
                missing.append(name)
        elif not (getattr(profile, name, "") or "").strip():
            missing.append(name)
    return missing


def is_complete(user: User) -> bool:
    return not missing_fields(user)


def _plain_value(profile: ResearcherProfile, name: str) -> str:
    if name == "observing_experience":
        if not profile.pk:
            return ""
        return ", ".join(sorted(str(i.pk) for i in profile.observing_experience.all()))
    return str(getattr(profile, name, "") or "")


def snapshot(profile: ResearcherProfile) -> Dict[str, str]:
    return {name: _plain_value(profile, name) for name in PLAIN_FIELDS}


def record_plain_changes(user: User, before: Dict[str, str], after: Dict[str, str],
                         changed_by: Optional[User]) -> List[ProfileFieldHistory]:
    """Write one history row per changed plain field. Clearing a field also blanks its old values."""
    rows = []
    now = timezone.now()
    for name in PLAIN_FIELDS:
        old, new = before.get(name, ""), after.get(name, "")
        if old == new:
            continue
        if not new:
            # "You may delete optional fields at any time": drop earlier copies of the value too.
            ProfileFieldHistory.objects.filter(user=user, field_name=name).update(value_plain="")
        rows.append(ProfileFieldHistory(user=user, field_name=name, value_plain=new, cleared=not new,
                                        changed_at=now, changed_by=changed_by))
    ProfileFieldHistory.objects.bulk_create(rows)
    return rows


@transaction.atomic
def set_encrypted(user: User, field_name: str, plaintext: str, changed_by: Optional[User],
                  public_key=None) -> EncryptedProfileValue:
    """Encrypt and store a validated value; raises ResearchKeyError without a key."""
    if field_name not in ENCRYPTED_FIELDS:
        raise ValueError("not an encrypted profile field: %s" % field_name)
    ciphertext, fingerprint = research_crypto.encrypt_value(plaintext, field_name, public_key=public_key)
    now = timezone.now()
    row = EncryptedProfileValue.objects.create(
        user=user, field_name=field_name, ciphertext=ciphertext, key_fingerprint=fingerprint, set_at=now)
    ProfileFieldHistory.objects.create(user=user, field_name=field_name, encrypted_value=row,
                                       changed_at=now, changed_by=changed_by)
    return row


@transaction.atomic
def clear_encrypted(user: User, field_name: str, changed_by: Optional[User]) -> int:
    """Delete every stored ciphertext of an optional encrypted field (history keeps only the timestamp)."""
    deleted, _ = EncryptedProfileValue.objects.filter(user=user, field_name=field_name).delete()
    if deleted:
        ProfileFieldHistory.objects.create(user=user, field_name=field_name, cleared=True,
                                           changed_by=changed_by)
    return deleted


@transaction.atomic
def set_current_advisor(user: User, mentor: Optional[User], mentor_name: str,
                        changed_by: Optional[User]) -> Optional[Mentorship]:
    """Make ``mentor`` (or the free-form name) the user's current advisor; end the previous one."""
    mentor_name = (mentor_name or "").strip()[:200]
    existing = current_advisor(user)
    if existing is not None:
        same_user = mentor is not None and existing.mentor_id == mentor.pk
        same_name = mentor is None and existing.mentor_id is None and existing.mentor_name == mentor_name
        if same_user or same_name:
            return existing
    if mentor is None and not mentor_name:
        return existing
    year = timezone.now().year
    if existing is not None:
        existing.end_year = year
        existing.save(update_fields=["end_year"])
    row = create_mentorship(
        owner=user, mentor=mentor, mentor_name=mentor_name, mentee=user,
        kind=Mentorship.KIND_CURRENT_ADVISOR, start_year=year, created_by=changed_by)
    label = row.mentor_label
    ProfileFieldHistory.objects.create(user=user, field_name="current_advisor", value_plain=label,
                                       changed_by=changed_by)
    return row


@transaction.atomic
def create_mentorship(*, owner: User, mentor: Optional[User] = None, mentor_name: str = "",
                      mentee: Optional[User] = None, mentee_name: str = "", kind: str,
                      start_year=None, end_year=None, created_by: Optional[User] = None) -> Mentorship:
    """Add a relation on ``owner``'s side (confirmed at once).

    When the other side is a YSE-PZ user, also give them a pending copy and notify them; it
    shows on their side once they confirm. Free-form names need no confirmation.
    """
    row = Mentorship.objects.create(
        owner=owner, status=Mentorship.STATUS_CONFIRMED,
        mentor=mentor, mentor_name="" if mentor else (mentor_name or "").strip()[:200],
        mentee=mentee, mentee_name="" if mentee else (mentee_name or "").strip()[:200],
        kind=kind, start_year=start_year, end_year=end_year, created_by=created_by)
    other = row.other_side()
    if other is None or other.pk == owner.pk:
        return row
    already = Mentorship.objects.filter(owner=other, mentor=mentor, mentee=mentee, kind=kind).first()
    if already is not None:
        Mentorship.objects.filter(pk=row.pk).update(counterpart=already)
        if already.counterpart_id is None:
            Mentorship.objects.filter(pk=already.pk).update(counterpart=row)
        return row
    pending = Mentorship.objects.create(
        owner=other, status=Mentorship.STATUS_PENDING, mentor=mentor, mentee=mentee, kind=kind,
        start_year=start_year, end_year=end_year, created_by=created_by, counterpart=row)
    Mentorship.objects.filter(pk=row.pk).update(counterpart=pending)
    row.counterpart = pending
    transaction.on_commit(lambda: _notify_pending(pending, owner))
    return row


def _relation_text(row: Mentorship, viewer: User) -> str:
    """'your PhD advisor' / 'your advisee (PhD advisor)' from ``viewer``'s point of view."""
    kind = row.get_kind_display()
    if row.mentee_id == viewer.pk:
        return "your %s" % kind
    return "your advisee (%s)" % kind


def _notify_pending(pending: Mentorship, entered_by: User) -> None:
    from django.urls import reverse

    from YSE_App.models.researcher_models import _display_name
    from YSE_App.services import notify

    who = _display_name(entered_by)
    text = "%s listed themselves as %s. Confirm or decline on your profile." % (
        who, _relation_text(pending, pending.owner))
    try:
        notify.notify([pending.owner], text, url=reverse("researcher_profile") + "#mentorship-requests",
                      kind="mentorship", subject="Confirm a relationship with %s" % who,
                      created_by=entered_by, exclude=[entered_by.pk])
    except Exception:  # noqa: BLE001 - a notice never blocks saving the relation
        logger.exception("mentorship notice for row %s failed", pending.pk)


@transaction.atomic
def confirm_mentorship(user: User, row: Mentorship) -> Mentorship:
    """``user`` confirms a pending relation: it shows on their side from now on."""
    row.status = Mentorship.STATUS_CONFIRMED
    row.save(update_fields=["status"])
    return row


@transaction.atomic
def decline_mentorship(user: User, row: Mentorship) -> None:
    """``user`` declines: their pending copy goes; the other side's own row stays."""
    Mentorship.objects.filter(counterpart=row).update(counterpart=None)
    row.delete()


def my_students(user: User, include_past: bool = True):
    """Rows where ``user`` is the mentor, on their own side (the "My advisees" list)."""
    qs = own_mentorships(user).filter(mentor=user).select_related("mentee", "mentee__researcher_profile")
    if not include_past:
        year = timezone.now().year
        qs = qs.filter(Q(end_year__isnull=True) | Q(end_year__gte=year))
    return qs.order_by("end_year", "-start_year", "-id")


def can_manage_students(user: User) -> bool:
    """Mentorships are research data: only consenting scientific staff and faculty manage students."""
    from YSE_App.services import research_consent
    profile = get_profile(user)
    return bool(profile and profile.can_mentor and research_consent.has_consented(user))


def labels(names: Iterable[str]) -> List[Tuple[str, str]]:
    return [(n, FIELD_LABELS.get(n, n.replace("_", " ").capitalize())) for n in names]
