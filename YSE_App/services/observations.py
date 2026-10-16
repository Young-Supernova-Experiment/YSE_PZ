"""Execution feedback from the planner (#667, #668).

The embedded planner's Start / Completed / Failed / Punted actions reach YSE_PZ through
AstroScheduler's ``onObservationEvent`` hook (set by the planner page). Each one:

==========  ====================  ==========================================
action      follow-up status      observed block
==========  ====================  ==========================================
start       InProcess             none (its time becomes the block's start)
complete    Successful            outcome ``success``
fail        Failed                outcome ``failed`` (reason required)
punt        unchanged             outcome ``skipped`` (reason required)
==========  ====================  ==========================================

Status changes use the normal save, so the follow-up notifications apply. The first outcome
of a night moves its approved plan to ``executing``.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone

from YSE_App.models import FollowupStatus, NightPlan, ObservedBlock, OutcomeReason

# The draft's seed reasons (same list as migration 0040): code, label, category, applies_to.
REASONS = [
    ("weather", "Weather", "weather", ["block", "event"]),
    ("instrument", "Instrument problem", "instrument", ["block", "event"]),
    ("telescope", "Telescope problem", "telescope", ["block", "event"]),
    ("ran_out_of_time", "Ran out of time", "time", ["block", "consideration", "event"]),
    ("deprioritized", "Deprioritized", "priority", ["block", "consideration", "event"]),
    ("low_sn", "Low S/N", "data", ["block", "event"]),
    ("wrong_object", "Wrong object", "target", ["block", "event"]),
    ("not_visible", "Not visible / not found", "target", ["block", "consideration", "event"]),
    ("too_faint", "Too faint", "target", ["block", "consideration", "event"]),
    ("classified_elsewhere", "Classified elsewhere", "priority", ["block", "consideration", "event"]),
    ("superseded", "Superseded", "priority", ["block", "consideration", "event"]),
    ("other", "Other", "other", ["block", "consideration", "event"]),
]

# action -> (observed-block outcome, follow-up status name)
ACTIONS = {
    "start": (None, "InProcess"),
    "complete": (ObservedBlock.Outcome.SUCCESS, "Successful"),
    "fail": (ObservedBlock.Outcome.FAILED, "Failed"),
    "punt": (ObservedBlock.Outcome.SKIPPED, None),
}
# Outcome -> the status AstroScheduler's solver skips (#668).
PLANNER_STATUS = {
    ObservedBlock.Outcome.SUCCESS: "Observed",
    ObservedBlock.Outcome.PARTIAL: "Observed",
    ObservedBlock.Outcome.FAILED: "Failed",
    ObservedBlock.Outcome.SKIPPED: "Punted",
}
MAX_NOTE = 2000


class ObservationError(ValueError):
    """A refused event; ``status`` is the HTTP status, the message is shown to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ensure_reasons(user) -> None:
    """Seed the draft's reasons if missing (migration 0040 skips databases without users)."""
    existing = set(OutcomeReason.objects.values_list("code", flat=True))
    for code, label, category, applies_to in REASONS:
        if code not in existing:
            OutcomeReason.objects.get_or_create(code=code, defaults={
                "label": label, "category": category, "applies_to": applies_to,
                "created_by": user, "modified_by": user,
            })


def can_record(night, user) -> bool:
    """Staff, the night's PI (linked account, #446), or users authorised for its resource."""
    from YSE_App.data import ObservingResourceService

    if user.is_staff or user.is_superuser:
        return True
    pi = night.resource.principal_investigator
    if pi and pi.user_id and pi.user_id == user.id:
        return True
    return ObservingResourceService.GetAuthorizedClassicalResource_ByUser(user).filter(pk=night.resource_id).exists()


def _when(value: Any) -> Optional[datetime.datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _plan_for(night, plan_id: Any) -> Optional[NightPlan]:
    """The plan the observer has open, else the night's approved (or executing) plan."""
    if plan_id not in (None, ""):
        plan = NightPlan.objects.filter(pk=plan_id if str(plan_id).isdigit() else 0, night=night).first()
        if plan is not None:
            return plan
    return (NightPlan.objects.filter(night=night, state__in=[NightPlan.State.EXECUTING, NightPlan.State.APPROVED])
            .order_by("-state", "-version").first())


def record_event(night, user, payload: Any) -> Dict[str, Any]:
    """Record one planner action for ``night`` (#667). Raises ObservationError."""
    from YSE_App.services.night_plans import visible_followup_ids

    if not isinstance(payload, dict):
        raise ObservationError("Expected a JSON object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ObservationError("Unknown action %r" % action)
    if not can_record(night, user):
        raise ObservationError("You may not record observations for this night", 403)
    external_id = str(payload.get("external_id") or "")
    if not external_id.isdigit():
        raise ObservationError("Only YSE_PZ follow-ups can be recorded (this target has no follow-up id)")
    if int(external_id) not in visible_followup_ids(night, user):
        raise ObservationError("That follow-up is not one of this night's follow-ups you can see", 403)

    outcome, status_name = ACTIONS[action]
    reason = None
    if outcome is not None and outcome != ObservedBlock.Outcome.SUCCESS:
        ensure_reasons(user)
        reason = OutcomeReason.objects.filter(code=payload.get("reason") or "", active=True).first()
        if reason is None:
            raise ObservationError("Choose a reason for %s" % ("a failure" if action == "fail" else "a punt"))
    note = payload.get("note") or ""
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ObservationError("The note must be text of at most %d characters" % MAX_NOTE)
    at = _when(payload.get("time_utc")) or timezone.now()

    from YSE_App.models import TransientFollowup

    with transaction.atomic():
        followup = TransientFollowup.objects.select_for_update().select_related("transient", "status").get(
            pk=int(external_id))
        observed = None
        if outcome is not None:
            plan = _plan_for(night, payload.get("plan"))
            planned = plan.blocks.filter(followup=followup).order_by("order").first() if plan else None
            observed = ObservedBlock.objects.create(
                followup=followup, transient=followup.transient, kind=ObservedBlock.Kind.SCIENCE,
                target_name=followup.transient.name[:64], ra=followup.transient.ra, dec=followup.transient.dec,
                mag=planned.mag if planned else None, mag_band=planned.mag_band if planned else None,
                start_ut=_when(payload.get("started_utc")), end_ut=at,
                settings={k: payload.get(k) for k in ("planned_start", "planned_end") if payload.get(k)} or None,
                comments=note or None, night=night, planned_block=planned, outcome=outcome, reason=reason,
                recorded_by=user, source=ObservedBlock.Source.ASTROSCHEDULER,
                created_by=user, modified_by=user,
            )
            if plan is not None and plan.state == NightPlan.State.APPROVED:
                plan.state = NightPlan.State.EXECUTING
                plan.modified_by = user
                plan.save(update_fields=["state", "modified_by", "modified_date"])
        if status_name and (followup.status is None or followup.status.name != status_name):
            status = FollowupStatus.objects.filter(name=status_name).first()
            if status is None:
                status = FollowupStatus.objects.create(name=status_name, created_by=user, modified_by=user)
            followup.status = status
            followup.modified_by = user
            followup.save(update_fields=["status", "modified_by", "modified_date"])
    return {
        "action": action,
        "followup_status": followup.status.name if followup.status_id else None,
        "observed_block": observed.id if observed else None,
        "outcome": outcome,
    }


def planner_statuses(night) -> Dict[str, str]:
    """``{follow-up id: AstroScheduler status}`` from each follow-up's latest outcome tonight (#668)."""
    latest: Dict[str, str] = {}
    for followup_id, outcome in (ObservedBlock.objects.filter(night=night, followup__isnull=False)
                                 .order_by("end_ut", "id").values_list("followup_id", "outcome")):
        latest[str(followup_id)] = PLANNER_STATUS.get(outcome, "Observed")
    return latest


def tonight_log(night, user) -> List[Dict[str, Any]]:
    """The night's observed blocks ``user`` may see, newest first (#668)."""
    from YSE_App.services.night_plans import visible_followup_ids

    blocks = (ObservedBlock.objects.filter(night=night).select_related("reason", "recorded_by")
              .order_by("-end_ut", "-id"))
    if not (user.is_staff or user.is_superuser):
        visible = visible_followup_ids(night, user)
        blocks = [b for b in blocks if b.followup_id is None or b.followup_id in visible or b.recorded_by_id == user.id]
    return [{
        "id": b.id,
        "target_name": b.target_name,
        "outcome": b.outcome,
        "reason": b.reason.label if b.reason_id else None,
        "note": b.comments,
        "start_ut": b.start_ut.isoformat() if b.start_ut else None,
        "end_ut": b.end_ut.isoformat() if b.end_ut else None,
        "recorded_by": b.recorded_by.username,
        "planned": b.planned_block_id is not None,
    } for b in blocks]
