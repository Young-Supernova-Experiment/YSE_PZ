"""Voluntary research consent: state, decisions, reminders and opt-out notices (#744).

The consent form is never a gate. Each decision is a new ``ResearchConsentRecord`` row (kept
forever, with time and form version); a user's state is their latest row:

* ``None``: not answered yet. Research profile fields are not offered (stored values are kept
  but never used for research). Reminded: see ``prompt_due``.
* ``consented``: research profile fields are offered (and the usual ones required), and
  activity inside the consent period goes into research datasets.
* ``declined`` / ``withdrawn``: excluded from research at once. Nothing is deleted
  automatically: the administrators in ``RESEARCH_OPTOUT_NOTIFY_USERNAMES`` (default: every
  active superuser) are notified and delete the research data with
  ``purge_unconsented_demographics``. Activity from before go-live was public and stays
  usable, anonymized (Ryan, 2026-10-20).

Which profile fields are research data: ``RESEARCH_PROFILE_FIELDS`` and
``RESEARCH_ENCRYPTED_FIELDS`` in ``models/researcher_models.py``, plus mentorships,
affiliation history and SciX metrics. Research datasets apply the rules in
``YSE_App/services/research_dataset.py``.
"""

from __future__ import annotations

import datetime
import logging
from typing import List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from YSE_App.models.researcher_models import (
    ResearchConsentForm,
    ResearchConsentPrompt,
    ResearchConsentRecord,
)

logger = logging.getLogger(__name__)

CONSENTED = ResearchConsentRecord.CONSENTED
DECLINED = ResearchConsentRecord.DECLINED
WITHDRAWN = ResearchConsentRecord.WITHDRAWN

# Reminders for users who have not answered (Ryan, 2026-10-20).
SECOND_PROMPT_AFTER_LOGINS = 10
SECOND_PROMPT_AFTER = datetime.timedelta(days=30)


class ConsentError(ValueError):
    """A decision that does not fit the user's current state (e.g. withdrawing without consent)."""


def current_form() -> Optional[ResearchConsentForm]:
    return ResearchConsentForm.objects.filter(is_current=True).order_by("-effective_date", "-id").first()


def go_live() -> Optional[datetime.datetime]:
    """Start (00:00 UTC) of the first form's effective date, or None while no form exists.

    Activity before this moment is exempt; from it on, only consenting users are used.
    """
    first = ResearchConsentForm.objects.order_by("effective_date", "id").values_list("effective_date", flat=True).first()
    if first is None:
        return None
    return datetime.datetime.combine(first, datetime.time.min, tzinfo=datetime.timezone.utc)


def latest_record(user: User) -> Optional[ResearchConsentRecord]:
    return (ResearchConsentRecord.objects.filter(user=user).select_related("form")
            .order_by("-decided_at", "-id").first())


def status(user: User) -> Optional[str]:
    row = latest_record(user)
    return row.decision if row else None


def has_consented(user: User) -> bool:
    return bool(user is not None and user.is_authenticated and status(user) == CONSENTED)


def has_answered(user: User) -> bool:
    return ResearchConsentRecord.objects.filter(user=user).exists()


# -- prompt and reminders ------------------------------------------------------

def prompt_due(user: User, now: Optional[datetime.datetime] = None) -> bool:
    """Should the form be offered now? Only to users who have never answered.

    First time: never prompted. Second time: 10 logins or 30 days after the first prompt,
    whichever comes first. After that: at the first login of each calendar year.
    """
    if not (user is not None and user.is_authenticated):
        return False
    if current_form() is None or has_answered(user):
        return False
    prompt = ResearchConsentPrompt.objects.filter(user=user).first()
    if prompt is None:
        return True
    now = now or timezone.now()
    if prompt.prompt_count <= 1:
        return (prompt.logins_since_first >= SECOND_PROMPT_AFTER_LOGINS
                or now - prompt.first_prompted_at >= SECOND_PROMPT_AFTER)
    return timezone.localtime(prompt.last_prompted_at).year < timezone.localtime(now).year


def reminder_due(user: User, now: Optional[datetime.datetime] = None) -> bool:
    """A reminder (not the first offer): the user was prompted before and ``prompt_due`` holds.

    The first offer comes right after the User Agreement; logins only ever trigger reminders.
    """
    if not (user is not None and user.is_authenticated):
        return False
    return ResearchConsentPrompt.objects.filter(user=user).exists() and prompt_due(user, now=now)


def record_prompt(user: User, now: Optional[datetime.datetime] = None) -> ResearchConsentPrompt:
    now = now or timezone.now()
    prompt, created = ResearchConsentPrompt.objects.get_or_create(
        user=user, defaults={"first_prompted_at": now, "last_prompted_at": now})
    if not created:
        ResearchConsentPrompt.objects.filter(pk=prompt.pk).update(
            last_prompted_at=now, prompt_count=F("prompt_count") + 1)
        prompt.refresh_from_db()
    return prompt


def note_login(user: User) -> None:
    """Count a login toward the second request (only for prompted users who have not answered)."""
    if has_answered(user):
        return
    ResearchConsentPrompt.objects.filter(user=user, prompt_count=1).update(
        logins_since_first=F("logins_since_first") + 1)


# -- decisions -------------------------------------------------------------------

@transaction.atomic
def record_decision(user: User, decision: str, form: Optional[ResearchConsentForm] = None,
                    now: Optional[datetime.datetime] = None) -> ResearchConsentRecord:
    """Store a decision. Declining or withdrawing notifies the opt-out administrators.

    ``consented`` needs a state other than consented; ``withdrawn`` needs consented;
    ``declined`` needs a state other than consented (a consenting user withdraws instead).
    Nothing is deleted here: an administrator purges the research data by hand.
    """
    if decision not in (CONSENTED, DECLINED, WITHDRAWN):
        raise ConsentError("unknown decision: %s" % decision)
    form = form or current_form()
    if form is None:
        raise ConsentError("no research consent form is in effect")
    # Lock the user's rows so two tabs cannot record conflicting decisions at once.
    list(ResearchConsentRecord.objects.select_for_update().filter(user=user).values_list("pk", flat=True))
    state = status(user)
    if decision == CONSENTED and state == CONSENTED:
        raise ConsentError("already consented")
    if decision == WITHDRAWN and state != CONSENTED:
        raise ConsentError("only a consenting user can withdraw")
    if decision == DECLINED and state == CONSENTED:
        raise ConsentError("a consenting user withdraws instead of declining")
    record = ResearchConsentRecord.objects.create(
        user=user, form=form, decision=decision, decided_at=now or timezone.now())
    if decision in (DECLINED, WITHDRAWN):
        transaction.on_commit(lambda: notify_optout(record))
    return record


def optout_recipients() -> List[User]:
    names = tuple(getattr(settings, "RESEARCH_OPTOUT_NOTIFY_USERNAMES", ()) or ())
    if names:
        return list(User.objects.filter(username__in=names, is_active=True).order_by("pk"))
    return list(User.objects.filter(is_superuser=True, is_active=True).order_by("pk"))


def notify_optout(record: ResearchConsentRecord) -> None:
    """Tell the opt-out administrators (in-app and email) that a user's research data needs purging."""
    from YSE_App.services.notify import notify
    recipients = optout_recipients()
    if not recipients:
        logger.warning("research opt-out #%s: nobody to notify (no RESEARCH_OPTOUT_NOTIFY_USERNAMES "
                       "and no active superuser)", record.pk)
        return
    username = record.user.get_username()
    verb = "withdrew from" if record.decision == WITHDRAWN else "declined"
    text = ("Research consent: user %s %s the study on %s. Their research data are already excluded from "
            "research. Delete their research profile data with: manage.py purge_unconsented_demographics "
            "--user %s (dry run), then add --apply."
            % (username, verb, timezone.localtime(record.decided_at).date().isoformat(), username))
    try:
        notify(recipients, text, url=reverse("admin:YSE_App_researchconsentrecord_changelist"),
               kind="research_optout", subject="Research opt-out: purge research data")
    except Exception:  # never let a notice failure undo or block the user's choice
        logger.exception("research opt-out #%s: notification failed", record.pk)


def consent_periods(records) -> List[Tuple[datetime.datetime, Optional[datetime.datetime]]]:
    """``[(start, end or None)]`` from one user's records, in any order.

    A period starts at a consent and ends at the next withdrawal (or decline); ``None`` = open.
    """
    periods: List[Tuple[datetime.datetime, Optional[datetime.datetime]]] = []
    start = None
    for row in sorted(records, key=lambda r: (r.decided_at, r.pk or 0)):
        if row.decision == CONSENTED:
            if start is None:
                start = row.decided_at
        elif start is not None:
            periods.append((start, row.decided_at))
            start = None
    if start is not None:
        periods.append((start, None))
    return periods


def history(user: User):
    return ResearchConsentRecord.objects.filter(user=user).select_related("form").order_by("-decided_at", "-id")
