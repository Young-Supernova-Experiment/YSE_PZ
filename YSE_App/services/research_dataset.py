"""Anonymized research datasets: who and what may be included (#744; research encryption #627/#633).

Every research dataset must be built through this module. The rules (Ryan, 2026-10-20):

* **Before go-live** (the first consent form's ``effective_date``): activity is exempt, because
  it was public, as on an internet forum, and is included anonymized for everyone, including users
  who declined or withdrew (Ryan, 2026-10-20).
* **From go-live on:** activity is included only for users who consented, and only inside their
  consent periods (consent to the next withdrawal). A user whose latest decision is ``withdrawn``
  has none of their post-go-live activity included, not even from an earlier consent period.
* **Research profile data** (``RESEARCH_PROFILE_FIELDS``, mentorships, and the encrypted values)
  only for users whose latest decision is ``consented``. Encrypted values are exported as
  ciphertext for offline decryption by the custodian. Nothing here decrypts anything.
* **Anonymized:** users appear only as a random string, drawn fresh for every export, so two
  exports cannot be joined; only the fields a source names are copied, and a source may not
  name its user field. The full database export is ``manage.py export_research_database``
  (``YSE_App/services/research_export.py``, #746).

Use::

    cohort = ResearchCohort.load()
    dataset = build_dataset([
        ActivitySource("comments", rows, user_key="created_by_id", time_key="created_date",
                       fields=("transient_id",)),
    ], cohort=cohort)
"""

from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from django.utils import timezone

from YSE_App.models.researcher_models import ENCRYPTED_FIELDS, EncryptedProfileValue, ResearchConsentRecord
from YSE_App.services import research_consent as rc

# Fields that would identify a person; a source may never copy them into a dataset.
IDENTIFYING_FIELDS = frozenset({
    "user", "user_id", "username", "email", "first_name", "last_name", "full_name", "name",
    "created_by", "created_by_id", "modified_by", "modified_by_id", "ip", "ip_address",
})


@dataclass
class ResearchCohort:
    """Consent state of every user, loaded once per export."""

    go_live: Optional[datetime.datetime]
    latest: Dict[int, str] = field(default_factory=dict)
    periods: Dict[int, List[Tuple[datetime.datetime, Optional[datetime.datetime]]]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ResearchCohort":
        by_user: Dict[int, list] = {}
        for row in ResearchConsentRecord.objects.order_by("user_id", "decided_at", "id").only(
                "id", "user_id", "decision", "decided_at"):
            by_user.setdefault(row.user_id, []).append(row)
        latest = {uid: rows[-1].decision for uid, rows in by_user.items()}
        periods = {uid: rc.consent_periods(rows) for uid, rows in by_user.items()}
        return cls(go_live=rc.go_live(), latest=latest, periods=periods)

    def is_withdrawn(self, user_id) -> bool:
        return self.latest.get(user_id) == rc.WITHDRAWN

    def demographics_allowed(self, user_id) -> bool:
        """Research profile data (plain and encrypted) only for consenting users."""
        return self.latest.get(user_id) == rc.CONSENTED

    research_profile_allowed = demographics_allowed

    def includes(self, user_id, when: Optional[datetime.datetime]) -> bool:
        """May activity by ``user_id`` at ``when`` go into a research dataset?"""
        if user_id is None or when is None:
            return False
        if isinstance(when, datetime.date) and not isinstance(when, datetime.datetime):
            when = datetime.datetime.combine(when, datetime.time.min, tzinfo=datetime.timezone.utc)
        if timezone.is_naive(when):
            when = timezone.make_aware(when, datetime.timezone.utc)
        if self.go_live is None or when < self.go_live:
            return True  # exempt: public before the consent form went live
        if self.is_withdrawn(user_id):
            return False
        for start, end in self.periods.get(user_id, ()):
            if start <= when and (end is None or when < end):
                return True
        return False


def consenting_user_ids() -> List[int]:
    """Users whose latest consent decision is ``consented``."""
    return sorted(uid for uid, decision in ResearchCohort.load().latest.items() if decision == rc.CONSENTED)


class Pseudonymizer:
    """Random identifiers, fresh for every instance (one per export): stable inside an export,
    unrelated to the real id and to every other export."""

    def __init__(self, prefix: str = "p_"):
        self.prefix = prefix
        self._map: Dict[Any, str] = {}

    def __call__(self, key) -> Optional[str]:
        if key is None:
            return None
        value = self._map.get(key)
        if value is None:
            value = self.prefix + secrets.token_hex(10)
            while value in self._map.values():  # pragma: no cover - 80-bit collision
                value = self.prefix + secrets.token_hex(10)
            self._map[key] = value
        return value


@dataclass
class ActivitySource:
    """One kind of activity: rows (dicts or objects) with a user field and a time field."""

    name: str
    rows: Iterable[Any]
    user_key: str
    time_key: str
    fields: Sequence[str] = ()

    def __post_init__(self):
        bad = (set(self.fields) & IDENTIFYING_FIELDS) | ({self.user_key} & set(self.fields))
        if bad:
            raise ValueError("identifying fields cannot go into a research dataset: %s" % ", ".join(sorted(bad)))


def _get(row, key):
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def filter_activity(source: ActivitySource, cohort: ResearchCohort) -> List[Any]:
    """The rows of ``source`` that the rules allow (not yet anonymized)."""
    return [row for row in source.rows if cohort.includes(_get(row, source.user_key), _get(row, source.time_key))]


def anonymize(source: ActivitySource, rows: Iterable[Any], pseudonym: Pseudonymizer) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        item = {"participant": pseudonym(_get(row, source.user_key)), "time": _iso(_get(row, source.time_key))}
        for name in source.fields:
            item[name] = _iso(_get(row, name))
        out.append(item)
    return out


def demographic_ciphertexts(cohort: ResearchCohort, pseudonym: Pseudonymizer,
                            user_ids: Optional[Iterable[int]] = None) -> List[Dict[str, str]]:
    """Latest ciphertext per encrypted field, for consenting users only (decrypted offline)."""
    allowed = {uid for uid, decision in cohort.latest.items() if decision == rc.CONSENTED}
    if user_ids is not None:
        allowed &= set(user_ids)
    latest: Dict[Tuple[int, str], EncryptedProfileValue] = {}
    rows = (EncryptedProfileValue.objects.filter(user_id__in=allowed, field_name__in=ENCRYPTED_FIELDS)
            .order_by("user_id", "field_name", "set_at", "id"))
    for row in rows:
        latest[(row.user_id, row.field_name)] = row
    return [{"participant": pseudonym(uid), "field": name, "ciphertext": row.ciphertext,
             "key_fingerprint": row.key_fingerprint}
            for (uid, name), row in sorted(latest.items())]


def build_dataset(sources: Iterable[ActivitySource], cohort: Optional[ResearchCohort] = None,
                  pseudonym: Optional[Pseudonymizer] = None, include_demographics: bool = True) -> Dict[str, Any]:
    """An anonymized dataset: ``{"go_live", "tables": {name: rows}, "demographics": [...]}``."""
    cohort = cohort or ResearchCohort.load()
    pseudonym = pseudonym or Pseudonymizer()
    tables, participants = {}, set()
    for source in sources:
        rows = filter_activity(source, cohort)
        participants.update(_get(r, source.user_key) for r in rows)
        tables[source.name] = anonymize(source, rows, pseudonym)
    demographics = demographic_ciphertexts(cohort, pseudonym, participants) if include_demographics else []
    return {
        "go_live": _iso(cohort.go_live),
        "tables": tables,
        "demographics": demographics,
    }
