"""Favorite transients and their activity notifications (#323, part of #320).

Starring: :func:`toggle`, :func:`add`, :func:`remove`, :func:`favorite_state`
(is it mine + how many stars, one query), :func:`favorite_ids` (for the JSON
the table stars are painted from) and :func:`favorites_for_user`.

Activity: the model signal handlers in ``YSE_App.signals`` call the
``on_*`` helpers here for a new comment, a status / class / redshift change
(from the auditlog entry), a new spectrum, new photometry (from the stat
recompute) and a follow-up request or status change. Each becomes one
*event* and goes to the transient's favoriters through
:func:`record_activity`, which batches: an unread ``favorite_activity``
notification for the same recipient and transient younger than
``FAVORITE_ACTIVITY_BATCH_MINUTES`` (default 60) collects the new event
instead of a second row, and the email / Slack delivery job of a fresh row
is delayed by the same window so it carries every event of the hour. The
actor of an event never receives it.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from YSE_App.jobs import enqueue, job
from YSE_App.models.favorite_models import UserFavoriteTransient
from YSE_App.models.notification_models import Notification
from YSE_App.models.transient_models import Transient

logger = logging.getLogger(__name__)

KIND = "favorite_activity"
BATCH_SETTING = "FAVORITE_ACTIVITY_BATCH_MINUTES"
MAX_EVENTS_IN_TEXT = 12
# Transient fields whose change is an event, with the label used in the text.
WATCHED_TRANSIENT_FIELDS = {
    "status": "status",
    "best_spec_class": "spectroscopic class",
    "photo_class": "photometric class",
    "redshift": "redshift",
    "TNS_spec_class": "TNS class",
}


def batch_minutes() -> int:
    try:
        return max(0, int(getattr(settings, BATCH_SETTING, 60) or 0))
    except (TypeError, ValueError):
        return 60


def display_name(user: Optional[User]) -> str:
    if user is None:
        return "someone"
    full = ("%s %s" % (user.first_name, user.last_name)).strip()
    return full or user.username


def transient_url(transient: Transient) -> str:
    return reverse("transient_detail", kwargs={"slug": transient.slug})


# --- stars -----------------------------------------------------------------

def is_favorite(user, transient_id: int) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return UserFavoriteTransient.objects.filter(user=user, transient_id=transient_id).exists()


def favorite_state(user, transient_id: int) -> Tuple[bool, int]:
    """``(starred by this user, number of users who starred it)`` in one query."""
    row = UserFavoriteTransient.objects.filter(transient_id=transient_id).aggregate(
        n=Count("id"), mine=Count("id", filter=Q(user_id=getattr(user, "pk", None) or -1)))
    return bool(row["mine"]), int(row["n"] or 0)


def favorite_ids(user) -> List[int]:
    """Ids of the transients ``user`` starred (the JSON the table stars are painted from)."""
    if not getattr(user, "is_authenticated", False):
        return []
    return list(UserFavoriteTransient.objects.filter(user=user).order_by().values_list("transient_id", flat=True))


def favorite_counts(transient_ids: Iterable[int]) -> Dict[int, int]:
    ids = list(transient_ids)
    if not ids:
        return {}
    rows = UserFavoriteTransient.objects.filter(transient_id__in=ids).values("transient_id").annotate(n=Count("id"))
    return {row["transient_id"]: row["n"] for row in rows}


def favorites_for_user(user):
    """Transients ``user`` starred, most recently starred first (``favorited_at`` annotated)."""
    from django.db.models import Max

    return (Transient.objects.filter(favorited_by__user=user)
            .annotate(favorited_at=Max("favorited_by__created", filter=Q(favorited_by__user=user)))
            .select_related("status", "obs_group", "host", "best_spec_class")
            .order_by("-favorited_at", "-id"))


def add(user: User, transient: Transient) -> Tuple[UserFavoriteTransient, bool]:
    """Star ``transient`` for ``user``; ``(row, created)``."""
    try:
        with transaction.atomic():
            row, created = UserFavoriteTransient.objects.get_or_create(user=user, transient=transient)
    except IntegrityError:  # concurrent double click
        row, created = UserFavoriteTransient.objects.get(user=user, transient=transient), False
    return row, created


def remove(user: User, transient) -> bool:
    transient_id = getattr(transient, "pk", transient)
    deleted, _ = UserFavoriteTransient.objects.filter(user=user, transient_id=transient_id).delete()
    return bool(deleted)


def toggle(user: User, transient: Transient) -> bool:
    """Star or unstar; returns the new state (``True`` = starred)."""
    if remove(user, transient):
        return False
    add(user, transient)
    return True


def favoriters(transient_id: int, *, exclude=None) -> List[User]:
    """Active users who starred the transient, minus ``exclude`` (users or ids)."""
    skip = {getattr(u, "pk", u) for u in (exclude or ()) if u is not None}
    rows = (UserFavoriteTransient.objects.filter(transient_id=transient_id, user__is_active=True)
            .exclude(user_id__in=skip).select_related("user"))
    return [row.user for row in rows]


# --- activity --------------------------------------------------------------

def _event(kind: str, text: str, actor: Optional[User], at=None, count: int = 1) -> dict:
    return {"kind": kind, "text": text, "by": display_name(actor) if actor else "",
            "at": (at or timezone.now()).isoformat(), "count": int(count)}


def _line(event: dict) -> str:
    return "%s%s" % (event["text"], (" (%s)" % event["by"]) if event.get("by") else "")


def build_text(transient_name: str, events: List[dict]) -> str:
    if len(events) == 1:
        return "%s: %s" % (transient_name, _line(events[0]))
    shown = events[-MAX_EVENTS_IN_TEXT:]
    lines = ["%d updates on %s:" % (len(events), transient_name)]
    if len(events) > len(shown):
        lines.append("- ... %d earlier update(s)" % (len(events) - len(shown)))
    lines += ["- " + _line(e) for e in shown]
    return "\n".join(lines)


def record_activity(transient: Transient, kind: str, text: str, *, actor: Optional[User] = None,
                    recipients: Optional[List[User]] = None, count: int = 1,
                    merge_text=None) -> List[Notification]:
    """Deliver one activity event on ``transient`` to its favoriters (batched, see module doc).

    Returns the notification rows created or extended. ``recipients``
    overrides the favoriter lookup (tests, re-sends). With ``merge_text``
    (a callable ``total -> text``) an event of the same ``kind`` that is the
    latest one in an open notification is merged into it (its ``count`` grows
    by ``count``) instead of being appended: photometry arriving point by point
    reads "12 new photometry points" rather than twelve lines.
    """
    from YSE_App.services import notify as notify_service

    if recipients is None:
        recipients = favoriters(transient.pk, exclude=[actor] if actor is not None else None)
    else:
        actor_id = getattr(actor, "pk", None)
        recipients = [u for u in recipients if u.pk != actor_id]
    if not recipients:
        return []
    event = _event(kind, text, actor, count=count)
    minutes = batch_minutes()
    now = timezone.now()
    open_rows = {}
    if minutes:
        since = now - datetime.timedelta(minutes=minutes)
        for row in Notification.objects.filter(recipient__in=[u.pk for u in recipients], transient=transient,
                                               kind=KIND, read_at__isnull=True, created__gte=since):
            open_rows.setdefault(row.recipient_id, row)
    subject = "Favorite activity: %s" % transient.name
    url = transient_url(transient)
    touched = []
    fresh = []
    for user in recipients:
        row = open_rows.get(user.pk)
        if row is not None:
            payload = dict(row.payload) if isinstance(row.payload, dict) else {}
            events = list(payload.get("events") or [])
            if merge_text is not None and events and events[-1].get("kind") == kind:
                total = int(events[-1].get("count") or 1) + int(count)
                events[-1] = dict(events[-1], text=merge_text(total), count=total, at=event["at"])
            else:
                events.append(event)
            payload["events"] = events
            row.payload = payload
            row.text = build_text(transient.name, events)
            row.save(update_fields=["payload", "text"])
            touched.append(row)
        else:
            fresh.append(user)
    if fresh:
        touched += notify_service.notify(
            fresh, build_text(transient.name, [event]), url, KIND, subject=subject, transient=transient,
            payload={"events": [event]}, created_by=actor, delay=minutes * 60 or None,
        )
    return touched


def _safely(func, *args, **kwargs):
    """Run an activity hook; a failure is logged, never raised into the save that triggered it."""
    try:
        return func(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("favorite activity hook %s failed", getattr(func, "__name__", func))
        return []


def _has_favoriters(transient_id) -> bool:
    return UserFavoriteTransient.objects.filter(transient_id=transient_id).exists()


def _shorten(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def on_comment(log, *, audience_known: bool = False) -> List[Notification]:
    """A new ``Log`` comment on a transient.

    The comment text is shown only to favoriters who may read the comment.
    ``create_transient_comment`` calls this once the audience groups are set
    (``audience_known``); for a ``Log`` saved any other way the groups are not
    known at ``post_save`` time, so a non-public comment is announced without
    its text.
    """
    if not log.transient_id or not (log.comment or "").strip():
        return []
    if not _has_favoriters(log.transient_id):
        return []
    recipients = favoriters(log.transient_id, exclude=[log.created_by])
    if not recipients:
        return []
    if getattr(log, "is_public", True):
        viewers, others = recipients, []
    elif audience_known:
        from YSE_App.models.log_models import Log
        from YSE_App.services.visibility import filter_transient_comments_for_user

        row = Log.objects.filter(pk=log.pk)
        viewers = [u for u in recipients if filter_transient_comments_for_user(row, u).exists()]
        others = [u for u in recipients if u not in viewers]
    else:
        viewers, others = [], recipients
    rows = []
    if viewers:
        rows += record_activity(log.transient, "comment", 'new comment: "%s"' % _shorten(log.comment),
                                actor=log.created_by, recipients=viewers)
    if others:
        rows += record_activity(log.transient, "comment", "new comment (restricted to collaboration groups)",
                                actor=log.created_by, recipients=others)
    return rows


def on_transient_changed(transient_id: int, changes: Dict[str, Iterable], *, actor: Optional[User] = None) -> List[Notification]:
    """Status / class / redshift changes from an auditlog ``changes_dict`` (``{field: [old, new]}``)."""
    parts = []
    for field, label in WATCHED_TRANSIENT_FIELDS.items():
        change = changes.get(field)
        if not change or len(change) != 2:
            continue
        old, new = (str(v) if v not in (None, "None") else "" for v in change)
        if old == new:
            continue
        if not old:
            parts.append("%s set to %s" % (label, new))
        elif not new:
            parts.append("%s cleared (was %s)" % (label, old))
        else:
            parts.append("%s changed %s -> %s" % (label, old, new))
    if not parts or not _has_favoriters(transient_id):
        return []
    transient = Transient.objects.filter(pk=transient_id).first()
    if transient is None:
        return []
    return record_activity(transient, "change", "; ".join(parts), actor=actor)


SPECTRUM_JOB_KIND = "favorites.spectrum_activity"
# Seconds between a spectrum's creation and its announcement: the upload paths
# attach its collaboration groups (and access rules) after the ``post_save``.
SPECTRUM_DELAY_SETTING = "FAVORITE_SPECTRUM_DELAY_SECONDS"


def spectrum_delay_seconds() -> int:
    try:
        return max(0, int(getattr(settings, SPECTRUM_DELAY_SETTING, 120) or 0))
    except (TypeError, ValueError):
        return 120


def on_spectrum(spectrum) -> List[Notification]:
    """A new spectrum: queue its announcement (see :func:`announce_spectrum`).

    At ``post_save`` the spectrum's groups are not set yet, so its visibility
    is decided later, by a job that runs after :func:`spectrum_delay_seconds`.
    Never inline: an inline run would decide before the groups exist.
    """
    if not _has_favoriters(spectrum.transient_id):
        return []
    enqueue(SPECTRUM_JOB_KIND, {"spectrum_id": spectrum.pk}, delay=spectrum_delay_seconds() or None,
            transient=spectrum.transient, inline=False)
    return []


def _user_sees_spectrum(user: User, spectrum) -> bool:
    from YSE_App.services.visibility import dataset_visible_to_user, user_sees_everything

    return user_sees_everything(user) or dataset_visible_to_user(user, spectrum)


def announce_spectrum(spectrum_id: int) -> List[Notification]:
    """Announce a new spectrum to the favoriters who may see it (#761); the others get nothing."""
    from YSE_App.models.spectra_models import TransientSpectrum

    spectrum = (TransientSpectrum.objects.filter(pk=spectrum_id)
                .select_related("instrument", "transient", "created_by").first())
    if spectrum is None or not _has_favoriters(spectrum.transient_id):
        return []
    actor = getattr(spectrum, "created_by", None)
    recipients = favoriters(spectrum.transient_id, exclude=[actor] if actor is not None else None)
    viewers = [u for u in recipients if _user_sees_spectrum(u, spectrum)]
    if not viewers:
        return []
    instrument = getattr(spectrum, "instrument", None)
    text = "new spectrum" + (" from %s" % instrument.name if instrument is not None else "")
    obs_date = getattr(spectrum, "obs_date", None)
    if obs_date:
        from YSE_App.common.utc_dates import utc_day

        text += " (observed %s)" % utc_day(obs_date)  # YYYYMMDD.ddd UTC (#807)
    return record_activity(spectrum.transient, "spectrum", text, actor=actor, recipients=viewers)


@job(SPECTRUM_JOB_KIND)
def spectrum_activity_job(payload):
    spectrum_id = (payload or {}).get("spectrum_id")
    if spectrum_id:
        announce_spectrum(int(spectrum_id))


def _visible_point_ids(user: User, transient_id: int) -> Optional[set]:
    """Ids of the transient's photometry points ``user`` may see (per point, #648); ``None``: all of them."""
    from YSE_App.models.phot_models import TransientPhotData
    from YSE_App.services.visibility import visible_photometry_ids_q

    q = visible_photometry_ids_q(user)
    if q is None:
        return None
    return set(TransientPhotData.objects.filter(photometry__transient_id=transient_id).filter(q)
               .values_list("pk", flat=True))


def on_photometry(transient_id: int, new_points: int) -> List[Notification]:
    """New detections counted by the photometry-stat recompute (``num_det_global`` grew).

    Per recipient and per point (#761): the new detections are the
    ``new_points`` most recently stored detection points; each favoriter is
    told how many of them they may see, with the latest magnitude of the
    detections they may see (the photometry statistics of their visible
    points). A favoriter who may see none of the new points gets no event.
    """
    if new_points <= 0 or not _has_favoriters(transient_id):
        return []
    transient = Transient.objects.filter(pk=transient_id).first()
    if transient is None:
        return []
    recipients = favoriters(transient_id)
    if not recipients:
        return []
    from YSE_App.services import photstat

    points = photstat.points_by_transient([transient_id]).get(transient_id, [])
    detections = [p for p in points if photstat.is_detection(p) and photstat._finite(p.mjd) is not None]
    newest = sorted(detections, key=lambda p: p.point_id or 0)[-int(new_points):]
    new_ids = {p.point_id for p in newest}

    by_text: Dict[Tuple[int, Optional[float], str], List[User]] = {}
    for user in recipients:
        visible = _visible_point_ids(user, transient_id)
        mine = points if visible is None else [p for p in points if p.point_id in visible]
        n_new = len(new_ids) if visible is None else len(new_ids & visible)
        if n_new <= 0:
            continue
        values = photstat.compute_stats(mine)
        band = ""
        if values.last_detected_band_id is not None:
            band = next((p.band_name for p in mine if p.band_id == values.last_detected_band_id), "") or ""
        by_text.setdefault((n_new, values.last_detected_mag, band), []).append(user)

    rows: List[Notification] = []
    for (n_new, latest_mag, latest_band), users in by_text.items():
        def text_for(total, latest_mag=latest_mag, latest_band=latest_band):
            text = "%d new photometry point%s" % (total, "" if total == 1 else "s")
            if latest_mag is not None:
                text += ", latest %.2f%s" % (float(latest_mag), (" " + latest_band) if latest_band else "")
            return text

        rows += record_activity(transient, "photometry", text_for(n_new), count=n_new, merge_text=text_for,
                                recipients=users)
    return rows


def on_followup(followup, *, created: bool, old_status: str = "") -> List[Notification]:
    from YSE_App.services.followup_notices import followup_telescope

    if not _has_favoriters(followup.transient_id):
        return []
    telescope = followup_telescope(followup)
    where = (" on %s" % telescope.name) if telescope is not None else ""
    status = getattr(getattr(followup, "status", None), "name", "") or ""
    if created:
        text = "follow-up requested%s" % where
        if status:
            text += " (%s)" % status
        actor = getattr(followup, "created_by", None)
    else:
        if old_status == status:
            return []
        text = "follow-up%s: status %s -> %s" % (where, old_status or "?", status or "?")
        actor = getattr(followup, "modified_by", None) or getattr(followup, "created_by", None)
    return record_activity(followup.transient, "followup", text, actor=actor)
