"""Delete the research data of users who declined or withdrew from the research study (#744).

Nothing is deleted automatically when someone declines or withdraws: they are excluded from
research at once, and the administrators in ``RESEARCH_OPTOUT_NOTIFY_USERNAMES`` (default:
every active superuser) are notified to run this command (Ryan, 2026-10-20).

Research data = every researcher-profile item that is not essential to run the site:

* the encrypted values (country, PhD year, gender);
* the plain research fields (``RESEARCH_PROFILE_FIELDS``: pronouns, department, career level,
  research interests, website, GitHub, time zone), blanked;
* their field history;
* the advisor / student rows the user entered themselves, whatever their role (rows the other
  person entered are that person's information and stay: privacy covers what an individual
  revealed, Ryan 2026-10-20);
* affiliation history and SciX metrics.

Kept: username, display and publication names, acknowledgment text, email, institution, ORCID,
observing experience, group memberships and roles, and all site activity.

Dry run by default (prints usernames and counts, never values). ``--apply`` deletes.
``--user NAME`` (repeatable) limits the run to those users; consenting users and users who have
not answered are never touched.
"""

from __future__ import annotations

from typing import Dict

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from YSE_App.models.researcher_models import (
    RESEARCH_ENCRYPTED_FIELDS,
    RESEARCH_PROFILE_FIELDS,
    AffiliationHistory,
    EncryptedProfileValue,
    Mentorship,
    ProfileFieldHistory,
    ResearcherMetrics,
    ResearcherProfile,
    ResearchConsentRecord,
)
from YSE_App.services import research_consent as rc

PLAIN_TEXT_FIELDS = tuple(RESEARCH_PROFILE_FIELDS)
HISTORY_FIELDS = RESEARCH_PROFILE_FIELDS + tuple(RESEARCH_ENCRYPTED_FIELDS) + ("current_advisor",)


def opted_out_user_ids():
    """Users whose latest consent decision is declined or withdrawn."""
    latest: Dict[int, str] = {}
    for user_id, decision in ResearchConsentRecord.objects.order_by("user_id", "decided_at", "id").values_list(
            "user_id", "decision"):
        latest[user_id] = decision
    return sorted(uid for uid, decision in latest.items() if decision in (rc.DECLINED, rc.WITHDRAWN))


def _mentorships(user):
    """Rows the user entered themselves (as mentee or mentor)."""
    return Mentorship.objects.filter(Q(mentee=user) | Q(mentor=user), created_by=user)


def research_data_counts(user) -> Dict[str, int]:
    profile = ResearcherProfile.objects.filter(user=user).first()
    plain = 0
    if profile is not None:
        plain = sum(1 for f in PLAIN_TEXT_FIELDS if getattr(profile, f, ""))
    return {
        "encrypted values": EncryptedProfileValue.objects.filter(user=user).count(),
        "profile fields": plain,
        "field history rows": ProfileFieldHistory.objects.filter(user=user, field_name__in=HISTORY_FIELDS)
        .filter(cleared=False).count(),
        "mentorships": _mentorships(user).count(),
        "affiliations": AffiliationHistory.objects.filter(user=user).count(),
        "metrics": ResearcherMetrics.objects.filter(user=user).count(),
    }


@transaction.atomic
def purge_research_data(user) -> Dict[str, int]:
    counts = research_data_counts(user)
    purged_fields = set(EncryptedProfileValue.objects.filter(user=user).values_list("field_name", flat=True))
    EncryptedProfileValue.objects.filter(user=user).delete()
    profile = ResearcherProfile.objects.filter(user=user).first()
    if profile is not None:
        for name in PLAIN_TEXT_FIELDS:
            if getattr(profile, name, ""):
                purged_fields.add(name)
                setattr(profile, name, "")
        profile.save(update_fields=list(PLAIN_TEXT_FIELDS) + ["updated_at"])
    if _mentorships(user).filter(mentee=user, kind=Mentorship.KIND_CURRENT_ADVISOR).exists():
        purged_fields.add("current_advisor")
    _mentorships(user).delete()
    AffiliationHistory.objects.filter(user=user).delete()
    ResearcherMetrics.objects.filter(user=user).delete()
    # Drop the old values from the history and leave one "cleared" marker per purged field.
    ProfileFieldHistory.objects.filter(user=user, field_name__in=HISTORY_FIELDS).delete()
    ProfileFieldHistory.objects.bulk_create([
        ProfileFieldHistory(user=user, field_name=name, cleared=True, changed_by=None)
        for name in sorted(purged_fields)
    ])
    return counts


class Command(BaseCommand):
    help = "Delete the research data of users who declined or withdrew (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete (default: dry run, counts only).")
        parser.add_argument("--user", action="append", default=[], metavar="USERNAME",
                            help="Only this user (repeatable).")

    def handle(self, *args, **options):
        targets = set(opted_out_user_ids())
        if options["user"]:
            wanted = list(User.objects.filter(username__in=options["user"]).values_list("pk", "username"))
            unknown = set(options["user"]) - {name for _pk, name in wanted}
            if unknown:
                raise CommandError("unknown user(s): %s" % ", ".join(sorted(unknown)))
            refused = sorted(name for pk, name in wanted if pk not in targets)
            if refused:
                raise CommandError("not declined or withdrawn, so not purged: %s" % ", ".join(refused))
            targets = {pk for pk, _name in wanted}
        users = list(User.objects.filter(pk__in=targets).order_by("username"))
        total = 0
        for user in users:
            counts = research_data_counts(user)
            n = sum(counts.values())
            if not n:
                continue
            total += n
            detail = ", ".join("%s %d" % (k, v) for k, v in counts.items() if v)
            if options["apply"]:
                purge_research_data(user)
                self.stdout.write("%s: deleted %s" % (user.get_username(), detail))
            else:
                self.stdout.write("%s: would delete %s" % (user.get_username(), detail))
        if not total:
            self.stdout.write("Nothing to delete.")
        elif not options["apply"]:
            self.stdout.write("Dry run: nothing deleted. Run again with --apply to delete.")
