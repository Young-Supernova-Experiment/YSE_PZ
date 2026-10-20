"""Voluntary research consent: state, decisions and demographic deletion (#744).

The consent form is never a gate. It is offered once, right after the User Agreement, and is
always available from the profile. Each decision is a new ``ResearchConsentRecord`` row (kept
forever, with time and form version); a user's state is their latest row:

* ``None``: not answered yet. Stored demographic values are kept but never used for research
  (``purge_unconsented_demographics`` deletes them after a grace period);
* ``consented``: demographic fields are offered and used, and activity inside the consent
  period goes into research datasets;
* ``declined`` / ``withdrawn``: stored demographic values are deleted. A withdrawn user's
  activity from go-live on is left out of every research dataset; activity from before go-live
  was public and stays usable, anonymized (Ryan, 2026-10-20).

Research datasets apply these rules in ``YSE_App/services/research_dataset.py``.
"""

from __future__ import annotations

import datetime
from typing import List, Optional, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from YSE_App.models.researcher_models import (
    DEMOGRAPHIC_FIELDS,
    EncryptedProfileValue,
    ResearchConsentForm,
    ResearchConsentRecord,
)

CONSENTED = ResearchConsentRecord.CONSENTED
DECLINED = ResearchConsentRecord.DECLINED
WITHDRAWN = ResearchConsentRecord.WITHDRAWN


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
    return bool(user and user.is_authenticated and status(user) == CONSENTED)


def needs_prompt(user: User) -> bool:
    """True when a form is current and the user has never answered: show the one-time prompt."""
    if not (user and user.is_authenticated):
        return False
    if current_form() is None:
        return False
    return not ResearchConsentRecord.objects.filter(user=user).exists()


def delete_demographics(user: User, changed_by: Optional[User] = None) -> int:
    """Delete every stored demographic ciphertext of ``user``; returns the number of rows deleted."""
    from YSE_App.services import researcher_profile as rp
    deleted = 0
    for name in DEMOGRAPHIC_FIELDS:
        deleted += rp.clear_encrypted(user, name, changed_by=changed_by)
    return deleted


def has_demographics(user: User) -> bool:
    return EncryptedProfileValue.objects.filter(user=user, field_name__in=DEMOGRAPHIC_FIELDS).exists()


@transaction.atomic
def record_decision(user: User, decision: str, form: Optional[ResearchConsentForm] = None,
                    now: Optional[datetime.datetime] = None) -> Tuple[ResearchConsentRecord, int]:
    """Store a decision; returns ``(record, demographic rows deleted)``.

    ``consented`` needs a state other than consented; ``withdrawn`` needs consented;
    ``declined`` needs a state other than consented (a consenting user withdraws instead).
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
    deleted = 0
    if decision in (DECLINED, WITHDRAWN):
        deleted = delete_demographics(user, changed_by=user)
    return record, deleted


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
