"""Delete stored demographic values of users who have not consented to the research study (#744).

Users who never answered the research consent form keep their encrypted demographic values
(country, gender) until a grace period that Ryan chooses ends; they are never used for research
in the meantime. Run this only when Ryan says the grace period is over.

Also removes any demographic values left over for users whose latest decision is decline or
withdraw (those are normally deleted when the decision is recorded).

Dry run by default: prints counts only (never names or values). ``--apply`` deletes.
``--older-than-days N`` limits the purge to values stored more than N days ago.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from YSE_App.models.researcher_models import (
    DEMOGRAPHIC_FIELDS, EncryptedProfileValue, ProfileFieldHistory, ResearchConsentRecord,
)
from YSE_App.services import research_consent as rc


def unconsented_values(older_than_days: int = 0):
    """Demographic ciphertext rows whose owner's latest decision is not ``consented``."""
    latest = (ResearchConsentRecord.objects.filter(user_id=OuterRef("user_id"))
              .order_by("-decided_at", "-id").values("decision")[:1])
    qs = (EncryptedProfileValue.objects.filter(field_name__in=DEMOGRAPHIC_FIELDS)
          .annotate(latest_decision=Subquery(latest)))
    # Spelled out: a NULL (never answered) must match, which ``exclude(=consented)`` may not do.
    qs = qs.filter(Q(latest_decision__isnull=True) | Q(latest_decision__in=(rc.DECLINED, rc.WITHDRAWN)))
    if older_than_days:
        qs = qs.filter(set_at__lt=timezone.now() - datetime.timedelta(days=older_than_days))
    return qs


class Command(BaseCommand):
    help = "Delete demographic values of users without research consent (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete (default: dry run, counts only).")
        parser.add_argument("--older-than-days", type=int, default=0,
                            help="Only values stored more than this many days ago (default 0: all).")

    def handle(self, *args, **options):
        days = options["older_than_days"]
        if days < 0:
            raise CommandError("--older-than-days must be 0 or more")
        qs = unconsented_values(days)
        rows = list(qs.values_list("pk", "user_id", "field_name", "latest_decision"))
        users = {uid for _pk, uid, _f, _d in rows}
        unanswered = len({uid for _pk, uid, _f, decision in rows if decision is None})
        self.stdout.write("%d demographic value(s) of %d user(s) without consent (%d never answered)."
                          % (len(rows), len(users), unanswered))
        if not options["apply"]:
            self.stdout.write("Dry run: nothing deleted. Run again with --apply to delete.")
            return
        # Delete by primary key (MySQL refuses a DELETE whose subquery reads the same table).
        by_field = {}
        for pk, uid, name, _d in rows:
            by_field.setdefault((uid, name), []).append(pk)
        deleted = 0
        for (uid, name), pks in sorted(by_field.items()):
            with transaction.atomic():
                n, _ = EncryptedProfileValue.objects.filter(pk__in=pks).delete()
                deleted += n
                if n:
                    ProfileFieldHistory.objects.create(user_id=uid, field_name=name, cleared=True, changed_by=None)
        self.stdout.write("Deleted %d demographic value(s)." % deleted)
