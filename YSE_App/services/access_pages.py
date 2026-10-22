"""Helpers for the access-rule pages and API (#648, PR 5).

The actions themselves are ``services.access_actions``; this module adds what
the screens need: the programs a user manages, program deputies, grantee
lookup, an object's observing nights, rule summaries, the datasets a user
manages (the dashboard) and the plain-language result of an action.

Everything here is only reachable while ``DATA_ACCESS_GRANTS_ENABLED`` is on
(:func:`require_enabled`).
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Tuple

from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME


def require_enabled() -> None:
    from YSE_App.services import access_rules

    if not access_rules.enabled():
        raise Http404("access rules are not enabled")


# ------------------------------------------------------------------ programs


def managed_allocations(user, *, own_only: bool = False):
    """Allocations whose access ``user`` may set: as the PI's account, a program deputy, or staff (all).

    ``own_only``: only as the PI or a program deputy (the dashboard; staff use the program list).
    """
    from YSE_App.models import Allocation
    from YSE_App.services.access_actions import is_staff

    qs = Allocation.objects.select_related("principal_investigator", "telescope").order_by("-start_date", "name")
    if is_staff(user) and not own_only:
        return qs
    if not getattr(user, "is_authenticated", False):
        return qs.none()
    return qs.filter(Q(principal_investigator__user=user) | Q(delegates__user=user)).distinct()


def can_manage_deputies(user, allocation) -> bool:
    """The allocation PI's linked account, or staff."""
    from YSE_App.services.access_actions import is_staff

    if is_staff(user):
        return True
    pi = allocation.principal_investigator
    return bool(getattr(user, "is_authenticated", False) and pi is not None and pi.user_id == user.pk)


@transaction.atomic
def add_program_deputy(actor, allocation, deputy: User, note: str = ""):
    from YSE_App.models import AllocationDelegate

    if not can_manage_deputies(actor, allocation):
        raise PermissionDenied({"message": "Only the program's PI (or an administrator) may designate program "
                                           "deputies."})
    if deputy is None or not deputy.is_active:
        raise ValidationError({"user": "Choose an active account."})
    pi = allocation.principal_investigator
    if pi is not None and pi.user_id == deputy.pk:
        raise ValidationError({"user": "The program's PI already sets its access."})
    row, _created = AllocationDelegate.objects.get_or_create(
        allocation=allocation, user=deputy, defaults={"added_by": actor, "note": (note or "").strip()[:255]})
    return row


@transaction.atomic
def remove_program_deputy(actor, row) -> None:
    if not can_manage_deputies(actor, row.allocation):
        raise PermissionDenied({"message": "Only the program's PI (or an administrator) may remove program "
                                           "deputies."})
    row.delete()


# ------------------------------------------------------------------ grantees


def resolve_grantee(group=None, user=None) -> Tuple[Optional[Group], Optional[User]]:
    """``(group, user)`` from an id or a name / username; exactly one must be given."""
    group = None if group in (None, "") else group
    user = None if user in (None, "") else user
    if (group is None) == (user is None):
        raise ValidationError({"grantee": "Give exactly one grantee: a group or a user."})
    if group is not None:
        text = str(group).strip()
        row = (Group.objects.filter(pk=int(text)).first() if text.isdigit() else None) or \
            Group.objects.filter(name=text).first()
        if row is None:
            raise ValidationError({"grantee": "No group %r." % text[:64]})
        if row.name == PUBLIC_COLLABORATION_GROUP_NAME:
            raise ValidationError({"grantee": "The Public group cannot be a grantee."})
        return row, None
    text = str(user).strip()
    row = User.objects.filter(username=text, is_active=True).first()
    if row is None and text.isdigit():
        row = User.objects.filter(pk=int(text), is_active=True).first()
    if row is None:
        raise ValidationError({"grantee": "No active account %r." % text[:64]})
    return None, row


def grantee_choices():
    return list(Group.objects.exclude(name=PUBLIC_COLLABORATION_GROUP_NAME).order_by("name"))


# ------------------------------------------------------------------ nights


def night_window(night_date: datetime.date, utc_offset_hours: float) -> Tuple[datetime.datetime, datetime.datetime]:
    """The UT window of the night starting on local date ``night_date``: local noon to local noon."""
    local_noon = datetime.datetime(night_date.year, night_date.month, night_date.day, 12, tzinfo=datetime.timezone.utc)
    start = local_noon - datetime.timedelta(hours=float(utc_offset_hours or 0))
    return start, start + datetime.timedelta(days=1)


def night_of(obs_date: datetime.datetime, utc_offset_hours: float) -> datetime.date:
    """The local night date of an observation (noon to noon)."""
    return (obs_date + datetime.timedelta(hours=float(utc_offset_hours or 0)) - datetime.timedelta(hours=12)).date()


def object_nights(transient, phot_sets, spectra) -> List[Dict]:
    """The nights of the object's data in ``phot_sets`` / ``spectra`` (those the viewer manages):
    one row per (local night, observatory offset).

    Each row: ``night`` (date), ``utc_offset``, ``start`` / ``end`` (UT window), ``points``,
    ``spectra``, ``telescopes``. All of the object's points from that night, in any filter, are
    changed together by the night action.
    """
    from YSE_App.models import TransientPhotData, TransientSpectrum

    rows: Dict[Tuple, Dict] = {}

    def add(obs_date, offset, telescope, kind):
        if obs_date is None:
            return
        offset = offset or 0
        key = (night_of(obs_date, offset), offset)
        row = rows.get(key)
        if row is None:
            start, end = night_window(key[0], offset)
            row = rows[key] = {"night": key[0], "utc_offset": offset, "start": start, "end": end, "points": 0,
                               "spectra": 0, "telescopes": set()}
        row[kind] += 1
        if telescope:
            row["telescopes"].add(telescope)

    if not phot_sets and not spectra:
        return []
    for obs_date, offset, telescope in TransientPhotData.objects.filter(
            photometry__transient_id=transient.pk, photometry_id__in=[d.pk for d in phot_sets]).values_list(
            "obs_date", "photometry__instrument__telescope__observatory__utc_offset",
            "photometry__instrument__telescope__name"):
        add(obs_date, offset, telescope, "points")
    for obs_date, offset, telescope in TransientSpectrum.objects.filter(
            transient_id=transient.pk, pk__in=[d.pk for d in spectra]).values_list(
            "obs_date", "instrument__telescope__observatory__utc_offset", "instrument__telescope__name"):
        add(obs_date, offset, telescope, "spectra")
    out = sorted(rows.values(), key=lambda r: (r["night"], r["utc_offset"]), reverse=True)
    for row in out:
        row["telescopes"] = ", ".join(sorted(row["telescopes"]))
    return out


def parse_night(data) -> Tuple[datetime.datetime, datetime.datetime]:
    """A night from request data: ``night_start`` + ``night_end`` (ISO), or ``night`` (date) + ``utc_offset``."""
    from django.utils.dateparse import parse_date, parse_datetime

    start, end = data.get("night_start"), data.get("night_end")
    if start and end:
        start, end = parse_datetime(str(start)), parse_datetime(str(end))
        if start is None or end is None:
            raise ValidationError({"night": "night_start and night_end must be ISO date-times."})
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.timezone.utc)
        return start, end
    day = parse_date(str(data.get("night") or ""))
    if day is None:
        raise ValidationError({"night": "Give night (a date) or night_start and night_end."})
    try:
        offset = float(data.get("utc_offset") or 0)
    except (TypeError, ValueError):
        raise ValidationError({"utc_offset": "utc_offset must be a number of hours."})
    return night_window(day, offset)


# ------------------------------------------------------------------ datasets


def manageable_datasets(user, transient) -> Tuple[list, list]:
    """``(photometry sets, spectra)`` of ``transient`` that ``user`` may set access for (no public data)."""
    from YSE_App.models import TransientPhotometry, TransientSpectrum
    from YSE_App.services import access_actions, data_sharing

    out = []
    for qs in (TransientPhotometry.objects.filter(transient_id=transient.pk),
               TransientSpectrum.objects.filter(transient_id=transient.pk)):
        rows = []
        for dataset in qs.select_related("instrument", "allocation").prefetch_related("groups", "pis"):
            if data_sharing.is_public_dataset(dataset):
                continue
            role = access_actions.dataset_role(user, dataset)
            if role is not None:
                dataset.access_role = role
                rows.append(dataset)
        out.append(rows)
    return out[0], out[1]


def rule_summary(phot_sets, spectra) -> List[Dict]:
    """Per dataset and grantee: the number of allow and restrict rules (for datasets the viewer manages)."""
    from YSE_App.models import DataAccessRule

    phot_ids = [d.pk for d in phot_sets]
    spec_ids = [d.pk for d in spectra]
    if not phot_ids and not spec_ids:
        return []
    rows = (DataAccessRule.objects.filter(Q(photometry_id__in=phot_ids) | Q(transient_spectrum_id__in=spec_ids))
            .order_by().values("photometry_id", "transient_spectrum_id", "grantee_group_id", "grantee_group__name",
                               "grantee_user_id", "grantee_user__username", "effect")
            .annotate(n=Count("id")))
    out: Dict[Tuple, Dict] = {}
    for row in rows:
        key = (row["photometry_id"], row["transient_spectrum_id"], row["grantee_group_id"], row["grantee_user_id"])
        item = out.setdefault(key, {
            "kind": "spectrum" if row["transient_spectrum_id"] else "photometry",
            "dataset_id": row["transient_spectrum_id"] or row["photometry_id"],
            "grantee_group_id": row["grantee_group_id"], "grantee_user_id": row["grantee_user_id"],
            "grantee_username": row["grantee_user__username"] or "",
            "grantee": ("group %s" % row["grantee_group__name"]) if row["grantee_group_id"]
            else ("user %s" % row["grantee_user__username"]),
            "allow": 0, "deny": 0})
        item[row["effect"]] += row["n"]
    return sorted(out.values(), key=lambda r: (r["kind"], r["dataset_id"], r["grantee"]))


def dataset_for(kind: str, dataset_id):
    from YSE_App.models import TransientPhotometry, TransientSpectrum

    model = TransientSpectrum if kind == "spectrum" else TransientPhotometry
    return model.objects.filter(pk=dataset_id).first()


# ------------------------------------------------------------------ wording


def describe(result, effect: str, grantee_label: str) -> str:
    """One sentence for a page message: what was written, restrictions of shared data, public items."""
    verb = "may now see" if effect == "allow" else "may not see"
    parts = ["%s %s %d new item%s" % (grantee_label, verb, result.created, "" if result.created == 1 else "s")]
    if result.upgraded:
        parts.append("%d restriction%s turned into access" % (result.upgraded, "" if result.upgraded == 1 else "s"))
    if result.overridden:
        parts.append("%d shared item%s now restricted" % (result.overridden, "" if result.overridden == 1 else "s"))
    if result.unchanged:
        parts.append("%d already had this rule" % result.unchanged)
    text = "; ".join(parts) + "."
    if result.public:
        text += " %d public item%s need%s no rule (public data are visible to everyone and are never restricted)." % (
            result.public, "" if result.public == 1 else "s", "s" if result.public == 1 else "")
    return text


FORWARD_HELP = "Going forward (the default): data taken before now stay visible to them."
BACKWARD_HELP = ("Backwards: they also lose access to data taken earlier, including data they may already have "
                 "looked at or downloaded.")


def grantee_label(group, user) -> str:
    return ("group %s" % group.name) if group is not None else ("user %s" % user.username)


# ------------------------------------------------------------------ dashboard

DASHBOARD_LIMIT = 200


def dashboard_datasets(user, limit: int = DASHBOARD_LIMIT) -> List[Dict]:
    """The restricted datasets ``user`` leads: as their PI or deputy (``share_role``) or through a program they
    lead or are a deputy of. Staff only see their own here (they act as administrators on the object pages).

    Each row: ``kind``, ``dataset``, ``transient``, ``groups``, ``grantees`` (rules per grantee),
    ``opted_in`` and ``can_opt_in`` (automatic publishing; never as staff on a PI's behalf).
    """
    from YSE_App.models import AllocationDelegate, AutopublishOptIn, TransientPhotometry, TransientSpectrum
    from YSE_App.services import data_sharing

    if not getattr(user, "is_authenticated", False):
        return []
    programs = set(AllocationDelegate.objects.filter(user=user).values_list("allocation_id", flat=True))
    from YSE_App.models import Allocation

    programs |= set(Allocation.objects.filter(principal_investigator__user=user).values_list("pk", flat=True))
    out = []
    for kind, model in (("photometry", TransientPhotometry), ("spectrum", TransientSpectrum)):
        q = data_sharing.shareable_q(user, model)
        if programs:
            program_q = Q(allocation_id__in=sorted(programs))
            q = program_q if q is None else q | program_q
        if q is None:
            continue
        restricted = model.groups.through.objects.exclude(group__name=PUBLIC_COLLABORATION_GROUP_NAME).values(
            "transientphotometry_id" if kind == "photometry" else "transientspectrum_id")
        rows = list(model.objects.filter(q).filter(pk__in=restricted).select_related("transient", "instrument")
                    .prefetch_related("groups").order_by("-pk")[:limit])
        if not rows:
            continue
        sets, spectra = (rows, []) if kind == "photometry" else ([], rows)
        rules: Dict[int, List[Dict]] = {}
        for item in rule_summary(sets, spectra):
            rules.setdefault(item["dataset_id"], []).append(item)
        field = "photometry_id" if kind == "photometry" else "spectrum_id"
        opted = set(AutopublishOptIn.objects.filter(**{field + "__in": [d.pk for d in rows]}).values_list(
            field, flat=True))
        for dataset in rows:
            out.append({
                "kind": kind, "dataset": dataset, "transient": dataset.transient,
                "groups": [g for g in dataset.groups.all() if g.name != PUBLIC_COLLABORATION_GROUP_NAME],
                "grantees": rules.get(dataset.pk, []), "opted_in": dataset.pk in opted,
                "can_opt_in": data_sharing.share_role(user, dataset, staff_override=False) is not None,
            })
    return out
