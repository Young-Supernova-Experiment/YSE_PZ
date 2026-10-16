"""Saved night plans: versions, approval, re-opening and the target-list export.

* ``save_plan`` (#662): one planner save is one ``NightPlan`` version with its blocks.
* ``approve_plan`` (#663): the night's PI (linked account, #446) or staff; at most one
  approved version per night.
* ``reopen_data`` (#664): what the embedded planner needs to continue a saved version.
* ``export_target_list`` (#77): a version in ``download_target_list``'s star-list format.

Visibility: a plan holds the blocks its author could see. Someone else sees a science block
only if its follow-up is one of the night's follow-ups visible to them (``visible_blocks``);
standards are public.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from YSE_App.models import NightPlan, PlannedBlock

MAX_BLOCKS = 400
MAX_TARGETS = 300
MAX_NOTES = 5000

# Target fields kept in solver_inputs: what reproduces the solve and the observer's edits.
TARGET_INPUT_KEYS = (
    "name", "external_id", "ra", "dec", "magnitude", "mag_band", "mag_source", "priority", "sn_mode",
    "allow_twilight", "high_airmass", "comment", "status", "overhead_minutes",
    "manual_start_time", "manual_end_time", "manual_duration", "lock_type", "schedule_before",
    "red_exptime", "red_num", "blue_exptime", "blue_num",
)
# The observer's edits a re-opened plan restores onto the night's current targets (#664).
EDIT_KEYS = (
    "manual_start_time", "manual_end_time", "manual_duration", "lock_type", "schedule_before",
    "red_exptime", "red_num", "blue_exptime", "blue_num", "overhead_minutes",
)


class PlanError(ValueError):
    """An invalid save request; the message is shown to the user."""


# ------------------------------------------------------------------ helpers


def _parse_sexagesimal(value: Any, hours: bool) -> Optional[float]:
    """Degrees from "hh:mm:ss" / "dd:mm:ss" (or a number in degrees); None if unparsable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", ":")
    sign = -1.0 if text.startswith("-") else 1.0
    try:
        parts = [abs(float(p)) for p in text.lstrip("+-").split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    parts += [0.0] * (3 - len(parts))
    value = sign * (parts[0] + parts[1] / 60.0 + parts[2] / 3600.0)
    return value * 15.0 if hours else value


def _standards() -> Dict[str, Dict[str, Any]]:
    """AstroScheduler's standard stars by name (coordinates for standard blocks)."""
    from astroscheduler import frontend

    import os
    path = os.path.join(frontend.STATIC_DIR, "standards.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {s["name"]: s for s in json.load(f)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _iso(value: Any) -> Optional[datetime.datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _sequence(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[{channel, n, exptime_s}] from the block's structured exposure plan."""
    out = []
    exposures = block.get("exposures") or {}
    for channel in ("blue", "red"):
        exp = exposures.get(channel) or {}
        n, t = _num(exp.get("num")), _num(exp.get("exptime"))
        if n and t is not None:
            out.append({"channel": channel, "n": int(n), "exptime_s": round(t, 1)})
    return out


def visible_followup_ids(night, user) -> set:
    from YSE_App.services.classical_nights import night_followups

    return set(night_followups(night, user).values_list("id", flat=True))


def _block_visible(user, plan_author_id, kind, followup_id, followup_ids) -> bool:
    if user.is_staff or user.id == plan_author_id:
        return True
    return kind != PlannedBlock.Kind.SCIENCE or followup_id in followup_ids


def visible_blocks(plan: NightPlan, user, followup_ids: Optional[set] = None,
                   with_requests: bool = False) -> List[PlannedBlock]:
    """The plan's blocks ``user`` may see: all for staff and the author; otherwise standards and
    science blocks whose follow-up the user can see on the night page. ``with_requests``
    prefetches the follow-ups' requests and requestors (for the export's comments)."""
    blocks = plan.blocks.select_related("followup", "transient").order_by("order")
    if with_requests:
        from django.db.models import Prefetch

        from YSE_App.models import TransientFollowupRequest

        blocks = blocks.prefetch_related(Prefetch(
            "followup__requests",
            queryset=TransientFollowupRequest.objects.select_related("requestor").order_by("requested_at", "id"),
        ))
    blocks = list(blocks)
    if user.is_staff or user.id == plan.author_id:
        return blocks
    if followup_ids is None:
        followup_ids = visible_followup_ids(plan.night, user)
    return [b for b in blocks if _block_visible(user, plan.author_id, b.kind, b.followup_id, followup_ids)]


# ------------------------------------------------------------------ save


def _target_list(targets: Any) -> List[Dict[str, Any]]:
    if targets is None:
        return []
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        raise PlanError("'targets' must be a list of at most %d targets" % MAX_TARGETS)
    return [t for t in targets if isinstance(t, dict)]


def save_plan(night, user, payload: Any) -> NightPlan:
    """A new version of ``night``'s plan from the planner's current schedule (#662).

    ``payload``: ``{"result": {"blocks": [...], "solver_version": "1.2.0"}, "targets": [...],
    "parent": <plan id or null>, "notes": "..."}``. Raises PlanError.
    """
    from YSE_App.services import night_planner

    if not isinstance(payload, dict):
        raise PlanError("Expected a JSON object")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("blocks"), list):
        raise PlanError("'result.blocks' is required: solve before saving")
    blocks = [b for b in result["blocks"] if isinstance(b, dict) and b.get("target_name")]
    if not blocks:
        raise PlanError("The schedule has no blocks to save")
    if len(blocks) > MAX_BLOCKS:
        raise PlanError("Too many blocks (at most %d)" % MAX_BLOCKS)
    # All submitted fields serve the blocks (warnings, magnitudes); solver_inputs keeps only
    # what reproduces the solve and the observer's edits.
    targets = _target_list(payload.get("targets"))
    notes = payload.get("notes") or None
    if notes is not None and (not isinstance(notes, str) or len(notes) > MAX_NOTES):
        raise PlanError("'notes' must be text of at most %d characters" % MAX_NOTES)
    parent = None
    if payload.get("parent") not in (None, ""):
        parent = NightPlan.objects.filter(pk=_num(payload.get("parent")) or 0, night=night).first()
        if parent is None:
            raise PlanError("'parent' is not a plan of this night")

    telescope = night.resource.telescope
    site = night_planner.observatory_payload(telescope)
    instrument = night_planner.site_for_telescope(telescope)[1]
    inputs = {
        "date": night_planner.night_date(night).isoformat(),
        "observatory": site,
        "instrument": instrument,
        "targets": [{k: t.get(k) for k in TARGET_INPUT_KEYS if k in t} for t in targets],
    }
    inputs["input_hash"] = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    solver_version = str(result.get("solver_version") or "")[:40]

    from YSE_App.models import TransientFollowup

    allowed = visible_followup_ids(night, user)
    followups = {
        str(f.id): f
        for f in TransientFollowup.objects.filter(id__in=allowed).select_related("transient")
    }
    by_id = {str(t.get("external_id")): t for t in targets if t.get("external_id")}
    by_name = {t.get("name"): t for t in targets if t.get("name")}
    standards = _standards()

    for attempt in range(3):
        try:
            with transaction.atomic():
                last = (NightPlan.objects.select_for_update().filter(night=night)
                        .order_by("-version").values_list("version", flat=True).first())
                audit = {"created_by": user, "modified_by": user}
                plan = NightPlan.objects.create(
                    night=night, version=(last or 0) + 1, parent=parent, author=user,
                    solver=("AstroScheduler %s" % solver_version).strip(), solver_inputs=inputs,
                    notes=notes, **audit,
                )
                ordered = sorted(blocks, key=lambda b: (str(b.get("start_time") or ""), str(b.get("target_name"))))
                PlannedBlock.objects.bulk_create([
                    _block(plan, i + 1, b, followups, by_id, by_name, standards, audit)
                    for i, b in enumerate(ordered)
                ])
            return plan
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _block(plan, order, block, followups, by_id, by_name, standards, audit) -> PlannedBlock:
    kind = block.get("kind") if block.get("kind") in PlannedBlock.Kind.values else PlannedBlock.Kind.SCIENCE
    followup = followups.get(str(block.get("external_id"))) if kind == PlannedBlock.Kind.SCIENCE else None
    target = by_id.get(str(block.get("external_id"))) or by_name.get(block.get("target_name")) or {}
    if followup is not None:
        ra, dec = followup.transient.ra, followup.transient.dec
    elif kind == PlannedBlock.Kind.STANDARD and block.get("target_name") in standards:
        star = standards[block["target_name"]]
        ra, dec = _parse_sexagesimal(star.get("ra"), True), _parse_sexagesimal(star.get("dec"), False)
    else:
        ra, dec = _parse_sexagesimal(target.get("ra"), True), _parse_sexagesimal(target.get("dec"), False)
    duration = _num(block.get("duration_minutes"))
    priority = _num(block.get("priority"))
    warnings = target.get("warnings") if isinstance(target.get("warnings"), list) else []
    return PlannedBlock(
        plan=plan, order=order, kind=kind,
        followup=followup, transient=followup.transient if followup else None,
        target_name=str(block.get("target_name"))[:64],
        ra=ra, dec=dec,
        mag=_num(target.get("magnitude")),
        mag_band=(str(target.get("mag_band"))[:8] if target.get("mag_band") else None),
        start_ut=_iso(block.get("start_time")), end_ut=_iso(block.get("end_time")),
        settings={k: block.get(k) for k in ("airmass_start", "airmass_end", "airmass_median", "overhead",
                                            "exposure_source", "instrument") if block.get(k) is not None},
        comments=(str(block.get("comment")) if block.get("comment") else None),
        requested_sequence=_sequence(block),
        time_with_overhead_s=int(duration * 60) if duration else None,
        warnings="; ".join(str(w) for w in warnings) or None,
        priority_at_plan=priority if kind == PlannedBlock.Kind.SCIENCE and priority else None,
        **audit,
    )


# ------------------------------------------------------------------ list, approve


def can_approve(night, user) -> bool:
    """The night resource's PI (through the PI's linked account, #446) or staff."""
    if user.is_staff or user.is_superuser:
        return True
    pi = night.resource.principal_investigator
    return bool(pi and pi.user_id and pi.user_id == user.id)


def plan_summaries(night, user) -> List[Dict[str, Any]]:
    followup_ids = visible_followup_ids(night, user)
    plans = list(night.plans.select_related("author", "approved_by").order_by("-version"))
    # Visible block counts for every plan from one query (#675).
    authors = {p.id: p.author_id for p in plans}
    counts: Dict[int, int] = {}
    for plan_id, kind, followup_id in (PlannedBlock.objects.filter(plan__night=night)
                                       .values_list("plan_id", "kind", "followup_id")):
        if _block_visible(user, authors.get(plan_id), kind, followup_id, followup_ids):
            counts[plan_id] = counts.get(plan_id, 0) + 1
    out = []
    for plan in plans:
        out.append({
            "id": plan.id,
            "version": plan.version,
            "state": plan.state,
            "parent": plan.parent_id,
            "author": plan.author.username,
            "created": plan.created_date.isoformat() if plan.created_date else None,
            "approved_by": plan.approved_by.username if plan.approved_by_id else None,
            "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
            "solver": plan.solver,
            "notes": plan.notes,
            "blocks": counts.get(plan.id, 0),
        })
    return out


def approve_plan(plan: NightPlan, user) -> NightPlan:
    """Approve ``plan``; the night's other approved version becomes superseded (#663)."""
    if not can_approve(plan.night, user):
        raise PermissionDenied("Only the night's PI or staff may approve a plan")
    with transaction.atomic():
        others = NightPlan.objects.select_for_update().filter(night=plan.night, state=NightPlan.State.APPROVED)
        for other in others.exclude(pk=plan.pk):
            other.state = NightPlan.State.SUPERSEDED
            other.modified_by = user
            other.save(update_fields=["state", "modified_by", "modified_date"])
        if plan.state != NightPlan.State.APPROVED:
            plan.state = NightPlan.State.APPROVED
            plan.approved_by = user
            plan.approved_at = timezone.now()
            plan.modified_by = user
            plan.save(update_fields=["state", "approved_by", "approved_at", "modified_by", "modified_date"])
    return plan


# ------------------------------------------------------------------ re-open


def reopen_data(plan: NightPlan, user) -> Dict[str, Any]:
    """``previous_schedule`` and the observer's saved edits by ``external_id`` (#664)."""
    blocks = visible_blocks(plan, user)
    previous = [
        {"target_name": b.target_name, "external_id": str(b.followup_id) if b.followup_id else None,
         "start_time": b.start_ut.isoformat()}
        for b in blocks if b.start_ut
    ]
    edits = {}
    for t in (plan.solver_inputs or {}).get("targets", []):
        if t.get("external_id"):
            kept = {k: t[k] for k in EDIT_KEYS if t.get(k) not in (None, "", [])}
            if kept:
                edits[str(t["external_id"])] = kept
    return {"previous_schedule": previous, "edits": edits}


def apply_edits(targets: Iterable[Dict[str, Any]], edits: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{**t, **edits.get(str(t.get("external_id")), {})} for t in targets]


# ------------------------------------------------------------------ export (#77)


def _sequence_text(sequence: List[Dict[str, Any]]) -> str:
    label = {"blue": "B", "red": "R"}
    return " ".join("%s %dx%.0f" % (label.get(s.get("channel"), s.get("channel")), s.get("n"), s.get("exptime_s"))
                    for s in sequence or [])


def export_target_list(plan: NightPlan, user, finders: bool = False) -> str:
    """The plan in ``download_target_list``'s star-list format, in planned order (#77).

    The comment carries the order, UT window and exposure sequence, then the requestors'
    comments. A follow-up's own offset star (``offset_star_*``) goes on the line after its
    target; with ``finders``, targets without one get the finder-chart offsets instead.
    """
    from YSE_App.services.followup_requests import format_comments
    from YSE_App.services.night_planner import sexagesimal

    lines = ["!Data {name %20} ra_h ra_m ra_s dec_d dec_m dec_s equinox {comment *}"]
    finder = None
    for block in visible_blocks(plan, user, with_requests=True):
        if block.ra is None or block.dec is None:
            continue
        ra, dec = sexagesimal(block.ra, block.dec)
        window = ""
        if block.start_ut and block.end_ut:
            window = "UT %s-%s" % (block.start_ut.strftime("%H:%M"), block.end_ut.strftime("%H:%M"))
        parts = ["%d." % block.order, window, _sequence_text(block.requested_sequence)]
        if block.kind != PlannedBlock.Kind.SCIENCE:
            parts.append(block.kind)
        comment = " ".join(p for p in parts if p)
        if block.followup_id:
            requested = format_comments(block.followup)
            if requested:
                comment += "; " + requested
        mag = "mag = %.2f " % block.mag if block.mag is not None else ""
        lines.append("%s  %s %s 2000 %scomment = %s" % (
            block.target_name.ljust(20), ra.replace(":", " "), dec.replace(":", " "), mag, comment))

        fu = block.followup
        if fu is not None and fu.offset_star_ra is not None and fu.offset_star_dec is not None:
            ora, odec = sexagesimal(fu.offset_star_ra, fu.offset_star_dec)
            lines.append("%s  %s %s 2000 raoffset=%.2f decoffset=%.2f" % (
                ("%s_os" % block.target_name)[:20].ljust(20), ora.replace(":", " "), odec.replace(":", " "),
                fu.offset_east or 0.0, fu.offset_north or 0.0))
        elif finders and block.transient_id:
            from YSE_App import view_utils

            finder = finder or view_utils.finder()
            offsets, _png = finder.finderchart_noview(block.transient_id)
            for off in offsets or []:
                lines.append("%s  %s %s 2000 mag = %.2f raoffset=%.2f decoffset=%.2f" % (
                    str(off["id"]).ljust(20), str(off["ra"]).replace(":", " "), str(off["dec"]).replace(":", " "),
                    float(off["mag"]), float(off["ra_off"]), float(off["dec_off"])))
    return "\n".join(lines) + "\n"
