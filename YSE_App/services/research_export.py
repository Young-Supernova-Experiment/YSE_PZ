"""Anonymized research database export (#746): a standalone SQLite file of platform activity.

Run with ``manage.py export_research_database --out <file>.sqlite`` (server or superuser
only: it is a management command; nothing is reachable over the web).

* **Inclusion** follows ``YSE_App/services/research_dataset.py`` (#744): activity before
  go-live for everyone; after go-live only inside consent periods (nothing for a user whose
  latest decision is withdrawn); research profile data only for consenting users.
* **Anonymization:**
  - every user is a random string drawn fresh for each export (``participant``); row ids of
    activity tables, groups and allocations are random strings too, so two exports cannot be
    joined on ids;
  - usernames, names, emails, ORCID iDs, websites, GitHub names and institutions are never
    copied (institutions appear only as a random string per export);
  - free text (comments, request notes, interest descriptions, ...) is scrubbed: ``@mentions``,
    email addresses, ORCID iDs and the known names of all users are replaced;
  - other users named in a row (e.g. who decided a request) appear only when the rules include
    them at that time, otherwise NULL.
* **Demographics** (encrypted profile values) stay ciphertext, decrypted only offline with
  the custodian's private key.
* **Excluded:** credentials, sessions, tokens, notification and job tables, request payloads,
  external ids and URLs, IP addresses and every other audit internal.

Transients, their statuses and classes are public astronomy objects and keep their real ids
and names, so the export can be linked to public data.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple  # noqa: F401

from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from YSE_App.services.research_dataset import Pseudonymizer, ResearchCohort

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ORCID_RE = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dXx]\b")
MENTION_RE = re.compile(r"(?<![\w@])@[\w.-]+")
MIN_NAME_LENGTH = 3

# Transient fields whose changes go into ``transient_changes`` (the change log).
TRANSIENT_CHANGE_FIELDS = ("status", "best_spec_class", "photo_class", "context_class", "TNS_spec_class",
                           "redshift", "redshift_err", "redshift_source")


class Scrubber:
    """Replace mentions, emails, ORCID iDs and the known names of users in free text."""

    def __init__(self, names: Iterable[str]):
        terms = set()
        for name in names:
            name = (name or "").strip()
            if len(name) < MIN_NAME_LENGTH:
                continue
            terms.add(name.lower())
            for part in re.split(r"[\s,.]+", name):
                if len(part) >= MIN_NAME_LENGTH:
                    terms.add(part.lower())
        ordered = sorted(terms, key=lambda t: (-len(t), t))
        self._names = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(t) for t in ordered),
                                 re.IGNORECASE) if ordered else None

    @classmethod
    def for_all_users(cls) -> "Scrubber":
        from YSE_App.models.researcher_models import ResearcherProfile
        names: List[str] = []
        for username, first, last in User.objects.values_list("username", "first_name", "last_name"):
            names.extend([username, first, last, ("%s %s" % (first, last)).strip()])
        for row in ResearcherProfile.objects.values_list("full_name", "preferred_name", "author_name_format"):
            names.extend(row)
        return cls(names)

    def __call__(self, text):
        if text is None:
            return None
        if not isinstance(text, str):
            text = json.dumps(text, sort_keys=True, default=str)
        text = EMAIL_RE.sub("[email]", text)
        text = ORCID_RE.sub("[orcid]", text)
        text = MENTION_RE.sub("@[person]", text)
        if self._names is not None:
            text = self._names.sub("[name]", text)
        return text


@dataclass
class TableSpec:
    """One activity table: ``actor`` and ``time`` decide inclusion (``research_dataset`` rules)."""

    name: str
    queryset: Callable[[], Any]
    actor: str
    time: str
    columns: Sequence[str] = ()            # copied as they are (public ids, numbers, states, times)
    refs: Dict[str, str] = field(default_factory=dict)   # column -> id namespace (random per export)
    users: Sequence[str] = ()              # other user columns, pseudonymized when included
    text: Sequence[str] = ()               # free text, scrubbed
    row_namespace: Optional[str] = None    # namespace of this table's own ``id``


def _specs() -> List[TableSpec]:
    from YSE_App.models import (
        DataAccessRequest, FacilityRequest, Log, TransientAnnotation, TransientFollowup,
        TransientFollowupRequest, TransientInterest, UserFavoriteTransient,
    )
    return [
        TableSpec("followups", lambda: TransientFollowup.objects.all(), "created_by_id", "created_date",
                  columns=("transient_id", "status_id", "valid_start", "valid_stop", "priority", "is_public",
                           "too_resource_id", "classical_resource_id", "queued_resource_id", "usage_hours"),
                  users=("requested_by_id",), row_namespace="followup"),
        TableSpec("followup_requests", lambda: TransientFollowupRequest.objects.all(), "requestor_id", "requested_at",
                  columns=("priority",), refs={"followup_id": "followup"}, text=("comment",),
                  row_namespace="followup_request"),
        TableSpec("comments", lambda: Log.objects.filter(transient__isnull=False), "created_by_id", "created_date",
                  columns=("transient_id", "is_public"), refs={"transient_followup_id": "followup"},
                  text=("comment",), row_namespace="comment"),
        TableSpec("annotations", lambda: TransientAnnotation.objects.all(), "created_by_id", "created_date",
                  columns=("transient_id", "origin"), text=("data",), row_namespace="annotation"),
        TableSpec("data_access_requests", lambda: DataAccessRequest.objects.all(), "requester_id", "created_date",
                  columns=("transient_id", "dataset_kind", "status", "decided_at", "decided_as"),
                  refs={"owner_group_id": "group", "target_group_id": "group"}, users=("decided_by_id",),
                  text=("message", "note"), row_namespace="data_access_request"),
        TableSpec("facility_requests", lambda: FacilityRequest.objects.all(), "submitted_by_id", "created_date",
                  columns=("transient_id", "kind", "state", "submitted_at", "hours_charged", "n_results"),
                  refs={"allocation_id": "allocation", "followup_id": "followup"},
                  row_namespace="facility_request"),
        TableSpec("interests", lambda: TransientInterest.objects.all(), "user_id", "created_date",
                  columns=("transient_id", "role", "status"), refs={"group_id": "group"},
                  text=("title", "description"), row_namespace="interest"),
        TableSpec("favorites", lambda: UserFavoriteTransient.objects.all(), "user_id", "created",
                  columns=("transient_id",)),
    ]


def _value(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


class Exporter:
    def __init__(self, cohort: Optional[ResearchCohort] = None, scrubber: Optional[Scrubber] = None):
        self.cohort = cohort or ResearchCohort.load()
        self.scrub = scrubber or Scrubber.for_all_users()
        self.people = Pseudonymizer("p_")
        self.ids = Pseudonymizer("r_")
        self.institutions = Pseudonymizer("inst_")
        self.participants: set = set()
        self.counts: Dict[str, int] = {}

    # -- helpers ---------------------------------------------------------------
    def _ref(self, namespace, value):
        return None if value is None else self.ids((namespace, value))

    def _person(self, user_id, when):
        if user_id is None or not self.cohort.includes(user_id, when):
            return None
        self.participants.add(user_id)
        return self.people(user_id)

    # -- tables ----------------------------------------------------------------
    def activity_rows(self, spec: TableSpec) -> Tuple[List[str], List[tuple]]:
        header = ["id", "participant", "time"] + list(spec.columns) + list(spec.refs) + list(spec.users) + list(spec.text)
        wanted = ["id", spec.actor, spec.time] + list(spec.columns) + list(spec.refs) + list(spec.users) + list(spec.text)
        rows = []
        for values in spec.queryset().values_list(*wanted).iterator():
            row = dict(zip(wanted, values))
            actor, when = row[spec.actor], row[spec.time]
            if not self.cohort.includes(actor, when):
                continue
            out = [self._ref(spec.row_namespace or spec.name, row["id"]), self._person(actor, when), _value(when)]
            out += [_value(row[c]) for c in spec.columns]
            out += [self._ref(ns, row[c]) for c, ns in spec.refs.items()]
            out += [self._person(row[c], when) for c in spec.users]
            out += [self.scrub(row[c]) for c in spec.text]
            rows.append(tuple(out))
        return [h.replace("_id", "") if h in spec.refs or h in spec.users else h for h in header], rows

    def transient_changes(self) -> Tuple[List[str], List[tuple]]:
        """Status, class and redshift changes from the change log (no IPs or other audit internals)."""
        from auditlog.models import LogEntry
        from django.contrib.contenttypes.models import ContentType
        from YSE_App.models import Transient
        ct = ContentType.objects.get_for_model(Transient)
        rows = []
        for object_pk, action, changes, actor_id, ts in (LogEntry.objects.filter(content_type=ct)
                                                         .values_list("object_pk", "action", "changes", "actor_id",
                                                                      "timestamp").iterator()):
            if not self.cohort.includes(actor_id, ts):
                continue
            try:
                data = json.loads(changes) if isinstance(changes, str) else (changes or {})
            except ValueError:
                continue
            kept = {k: v for k, v in (data or {}).items() if k in TRANSIENT_CHANGE_FIELDS}
            if not kept:
                continue
            rows.append((self.ids(("transient_change", object_pk, ts)), self._person(actor_id, ts), _value(ts),
                         _value(object_pk), action, self.scrub(json.dumps(kept, sort_keys=True))))
        return ["id", "participant", "time", "transient", "action", "changes"], rows

    def lookups(self) -> Dict[str, Tuple[List[str], List[tuple]]]:
        from YSE_App.models import FollowupStatus, Transient, TransientClass, TransientStatus
        return {
            "transients": (["id", "name", "status", "best_spec_class", "photo_class", "disc_date", "redshift"],
                           [tuple(_value(v) for v in r) for r in Transient.objects.values_list(
                               "id", "name", "status_id", "best_spec_class_id", "photo_class_id", "disc_date",
                               "redshift").iterator()]),
            "transient_statuses": (["id", "name"], list(TransientStatus.objects.values_list("id", "name"))),
            "transient_classes": (["id", "name"], list(TransientClass.objects.values_list("id", "name"))),
            "followup_statuses": (["id", "name"], list(FollowupStatus.objects.values_list("id", "name"))),
        }

    def research_profile_tables(self) -> Dict[str, Tuple[List[str], List[tuple]]]:
        """Participants, and research profile data of consenting participants only."""
        from YSE_App.models.researcher_models import (
            ENCRYPTED_FIELDS, EncryptedProfileValue, Mentorship, ResearcherMetrics, ResearcherProfile,
        )
        consenting = {uid for uid in self.participants if self.cohort.research_profile_allowed(uid)}
        profiles = {p.user_id: p for p in ResearcherProfile.objects.filter(user_id__in=consenting)
                    .prefetch_related("observing_experience")}
        participants, instruments = [], []
        for uid in sorted(self.participants, key=self.people):
            profile = profiles.get(uid)
            if profile is None:
                participants.append((self.people(uid), 0) + (None,) * 7)
                continue
            participants.append((
                self.people(uid), 1, profile.career_level or None, self.scrub(profile.career_level_other) or None,
                profile.department or None, self.institutions(profile.institution.strip().lower())
                if profile.institution else None, profile.pronouns or None,
                self.scrub(profile.research_interests) or None, profile.time_zone or None,
            ))
            instruments += [(self.people(uid), inst.pk) for inst in profile.observing_experience.all()]
        latest: Dict[Tuple[int, str], Any] = {}
        for row in (EncryptedProfileValue.objects.filter(user_id__in=consenting, field_name__in=ENCRYPTED_FIELDS)
                    .order_by("user_id", "field_name", "set_at", "id")):
            latest[(row.user_id, row.field_name)] = row
        demographics = [(self.people(uid), name, row.ciphertext, row.key_fingerprint)
                        for (uid, name), row in sorted(latest.items())]
        mentorships = []
        for m in Mentorship.objects.filter(Q(mentee_id__in=consenting) | Q(mentor_id__in=consenting)):
            mentee = self.people(m.mentee_id) if m.mentee_id in consenting else None
            mentor = self.people(m.mentor_id) if m.mentor_id in consenting else None
            if mentee is None and mentor is None:
                continue
            mentorships.append((mentor, mentee, m.kind, m.start_year, m.end_year))
        metrics = [(self.people(r.user_id), r.source, _value(r.fetched_at), r.h_index, r.citation_count,
                    r.refereed_count, r.first_author_count)
                   for r in ResearcherMetrics.objects.filter(user_id__in=consenting)]
        return {
            "participants": (["participant", "research_profile", "career_level", "career_level_other", "department",
                              "institution", "pronouns", "research_interests", "time_zone"], participants),
            "participant_instruments": (["participant", "instrument"], instruments),
            "demographics": (["participant", "field", "ciphertext", "key_fingerprint"], demographics),
            "mentorships": (["mentor", "mentee", "kind", "start_year", "end_year"], mentorships),
            "metrics": (["participant", "source", "fetched_at", "h_index", "citations", "refereed_papers",
                         "first_author_papers"], metrics),
        }

    # -- output ----------------------------------------------------------------
    def build(self) -> Dict[str, Tuple[List[str], List[tuple]]]:
        tables: Dict[str, Tuple[List[str], List[tuple]]] = {}
        for spec in _specs():
            tables[spec.name] = self.activity_rows(spec)
        tables["transient_changes"] = self.transient_changes()
        tables.update(self.lookups())
        tables.update(self.research_profile_tables())  # last: needs every participant
        tables["metadata"] = (["key", "value"], [
            ("generated_on", timezone.now().date().isoformat()),
            ("go_live", _value(self.cohort.go_live)),
            ("rules", "YSE-PZ research consent #744 / export #746"),
        ])
        return tables

    def write(self, path: str) -> Dict[str, int]:
        tables = self.build()
        conn = sqlite3.connect(path)
        try:
            for name, (header, rows) in tables.items():
                cols = ", ".join('"%s"' % h for h in header)
                conn.execute('DROP TABLE IF EXISTS "%s"' % name)
                conn.execute('CREATE TABLE "%s" (%s)' % (name, cols))
                if rows:
                    conn.executemany('INSERT INTO "%s" VALUES (%s)' % (name, ", ".join("?" * len(header))), rows)
                self.counts[name] = len(rows)
            conn.commit()
        finally:
            conn.close()
        return self.counts


def export(path: str) -> Dict[str, int]:
    return Exporter().write(path)


__all__ = ["Exporter", "Scrubber", "TableSpec", "export"]
