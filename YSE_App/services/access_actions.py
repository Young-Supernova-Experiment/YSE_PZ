"""Access actions: who may set per-point rules, and the writes (#648, PR 2).

Program, night and dataset are *actions* that write one ``DataAccessRule``
per matching photometry point or spectrum (Ryan, 2026-10-17). The later UI
and API (PR 5) call these functions; the admin (staff) writes rows directly.

**Who may act**

* Program and night actions (:func:`program_action`): the allocation PI's
  linked account, the program's deputies (``AllocationDelegate``), or staff
  as administrators (:func:`program_role`).
* One dataset (:func:`dataset_action`) or one object's night
  (:func:`object_night_action`): the data's PIs and their deputies
  (``data_sharing.share_role``), the program's PI or deputies when the
  dataset is linked to a program, or staff (:func:`dataset_role`).
* Deleting a rule (:func:`delete_rule`): staff only, to fix a mistake; logged.

**What is written**

* An allow creates the rule, or turns the grantee's deny into an allow.
* Once a point is shared it stays shared: a deny is not written for a point
  the grantee can already see (the group is on the dataset, or it already has
  an allow; for a user, the point is visible to them now). At ingest
  (a standing grant applied to a new point) a deny is written even when the
  dataset's own groups include the grantee: the point was never shared
  before the restriction.
* Public data (no collaboration group other than Public) gets no rules from
  an action: it can never be restricted, and everyone may see it already. At
  ingest the rule is written anyway, because a new dataset's groups are set
  just after it is saved (a rule never hides data that stay public).
* A program or night action is also saved as a ``StandingAccessGrant``, so
  points that arrive later get the rule at ingest (:func:`apply_standing_grants_to_set`,
  :func:`apply_standing_grants_to_spectrum`, called by the upload paths and
  signals). Stopping it (:func:`stop_standing_grant`) affects only later
  points.

Every write bumps the rules version (``access_rules.bump_version``) and, for
a group grantee with the flag on, removes the affected PhotStat scope rows and
queues their rebuild (``photstat.invalidate_scopes``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models.data_access_grant_models import (
    AS_GROUP_DEPUTY,
    AS_PI,
    AS_PROGRAM_DEPUTY,
    AS_STAFF,
    EFFECT_ALLOW,
    EFFECT_DENY,
)

logger = logging.getLogger(__name__)

CHUNK = 1000

_local = threading.local()


def is_staff(user) -> bool:
    return bool(getattr(user, "is_authenticated", False) and user.is_active
                and (user.is_staff or user.is_superuser))


@contextlib.contextmanager
def _quiet_signals():
    """Rule saves inside an action skip the per-row version bump; the action bumps once."""
    depth = getattr(_local, "depth", 0)
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth = depth


def signals_quiet() -> bool:
    return getattr(_local, "depth", 0) > 0


# ------------------------------------------------------------------- roles


def program_role(user, allocation, *, staff_override: bool = True) -> Optional[str]:
    """``"pi"``, ``"program_deputy"``, ``"staff"`` (with ``staff_override``) or ``None`` for one allocation."""
    if allocation is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    pi = getattr(allocation, "principal_investigator", None)
    if pi is not None and pi.user_id is not None and pi.user_id == user.pk:
        return AS_PI
    from YSE_App.models import AllocationDelegate

    if AllocationDelegate.objects.filter(allocation_id=allocation.pk, user_id=user.pk).exists():
        return AS_PROGRAM_DEPUTY
    if staff_override and is_staff(user):
        return AS_STAFF
    return None


def dataset_role(user, dataset) -> Optional[str]:
    """The capacity in which ``user`` may set rules on one photometry set or spectrum, or ``None``."""
    from YSE_App.services import data_sharing

    role = data_sharing.share_role(user, dataset, staff_override=False)
    if role == data_sharing.ROLE_PI:
        return AS_PI
    if getattr(dataset, "allocation_id", None):
        prole = program_role(user, dataset.allocation, staff_override=False)
        if prole is not None:
            return prole
    if role == data_sharing.ROLE_DEPUTY:
        return AS_GROUP_DEPUTY
    if is_staff(user):
        return AS_STAFF
    return None


# ------------------------------------------------------------------ grantee


def _check_grantee(grantee_group, grantee_user):
    if (grantee_group is None) == (grantee_user is None):
        raise ValidationError({"grantee": "Give exactly one grantee: a group or a user."})
    if grantee_group is not None and grantee_group.name == PUBLIC_COLLABORATION_GROUP_NAME:
        raise ValidationError({"grantee": "The Public group cannot be a grantee; change the datasets' own groups "
                                          "instead."})
    if grantee_user is not None and not grantee_user.is_active:
        raise ValidationError({"grantee": "Choose an active account."})


def _check_effect(effect):
    if effect not in (EFFECT_ALLOW, EFFECT_DENY):
        raise ValidationError({"effect": "Choose allow or restrict."})


@dataclass
class WriteResult:
    created: int = 0
    upgraded: int = 0  # a deny that became an allow
    unchanged: int = 0
    kept_shared: int = 0  # a deny not written: the grantee can already see the point
    public: int = 0  # public data: no rule needed
    transients: frozenset = frozenset()

    def add(self, other: "WriteResult") -> "WriteResult":
        return WriteResult(
            self.created + other.created, self.upgraded + other.upgraded, self.unchanged + other.unchanged,
            self.kept_shared + other.kept_shared, self.public + other.public,
            frozenset(self.transients | other.transients))

    def as_dict(self):
        return {"created": self.created, "upgraded": self.upgraded, "unchanged": self.unchanged,
                "kept_shared": self.kept_shared, "public": self.public}


# ------------------------------------------------------------------- writes


def _restricted_dataset_ids(through, fk: str, ids) -> set:
    """Datasets among ``ids`` with a collaboration group other than Public (the others are public)."""
    return set(through.objects.filter(**{fk + "__in": list(ids)}).exclude(
        group__name=PUBLIC_COLLABORATION_GROUP_NAME).values_list(fk, flat=True))


def _visible_to_grantee(model, ids, grantee_group, grantee_user) -> set:
    """The rows among ``ids`` the grantee can already see (a deny would not hide them: they stay shared)."""
    from YSE_App.models import DataAccessRule, TransientPhotData, TransientPhotometry, TransientSpectrum
    from YSE_App.services import visibility

    ids = list(ids)
    if not ids:
        return set()
    rows = model.objects.filter(pk__in=ids)
    if grantee_user is not None:
        if visibility.user_sees_everything(grantee_user):
            return set(ids)
        if model is TransientPhotData:
            rows = rows.filter(visibility.visible_photometry_ids_q(grantee_user))
        else:
            rows = rows.filter(pk__in=TransientSpectrum.objects.filter(
                visibility.spectrum_visible_q(grantee_user)).values("pk"))
        return set(rows.values_list("pk", flat=True))
    target = "transient_phot_data_id" if model is TransientPhotData else "transient_spectrum_id"
    allowed = set(DataAccessRule.objects.filter(**{target + "__in": ids}, grantee_group_id=grantee_group.pk,
                                                effect=EFFECT_ALLOW).values_list(target, flat=True))
    if model is TransientPhotData:
        sets = TransientPhotometry.groups.through.objects.filter(group_id=grantee_group.pk)
        rows = rows.filter(photometry_id__in=sets.values("transientphotometry_id"))
    else:
        specs = TransientSpectrum.groups.through.objects.filter(group_id=grantee_group.pk)
        rows = rows.filter(pk__in=specs.values("transientspectrum_id"))
    return allowed | set(rows.values_list("pk", flat=True))


def _write(model, ids, *, effect, grantee_group, grantee_user, granted_by, granted_as, standing_grant=None,
           at_ingest=False) -> WriteResult:
    from YSE_App.models import DataAccessRule, TransientPhotData, TransientPhotometry, TransientSpectrum

    result = WriteResult()
    ids = sorted({int(i) for i in ids if i is not None})
    if not ids:
        return result
    is_point = model is TransientPhotData
    target = "transient_phot_data_id" if is_point else "transient_spectrum_id"
    grantee = {"grantee_group_id": grantee_group.pk} if grantee_group is not None else {
        "grantee_user_id": grantee_user.pk}
    transients = set()
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        if is_point:
            rows = {pk: (phot_id, tid) for pk, phot_id, tid in TransientPhotData.objects.filter(pk__in=chunk)
                    .values_list("pk", "photometry_id", "photometry__transient_id")}
            restricted = _restricted_dataset_ids(TransientPhotometry.groups.through, "transientphotometry_id",
                                                 {p for p, _t in rows.values()})
            public = {pk for pk, (phot_id, _t) in rows.items() if phot_id not in restricted}
        else:
            rows = {pk: (None, tid) for pk, tid in TransientSpectrum.objects.filter(pk__in=chunk)
                    .values_list("pk", "transient_id")}
            restricted = _restricted_dataset_ids(TransientSpectrum.groups.through, "transientspectrum_id", rows)
            public = {pk for pk in rows if pk not in restricted}
        if at_ingest:
            # A new dataset gets its groups just after it is saved (the m2m comes second), so at
            # ingest every point gets the rule; one on data that stay public never hides them.
            public = set()
        result.public += len(public)
        todo = [pk for pk in sorted(rows) if pk not in public]
        if not todo:
            continue
        existing = {getattr(r, target): r for r in DataAccessRule.objects.filter(**{target + "__in": todo}, **grantee)}
        if effect == EFFECT_DENY and not at_ingest:
            shared = _visible_to_grantee(model, [pk for pk in todo if pk not in existing], grantee_group,
                                         grantee_user)
        else:
            shared = set()
        new = []
        for pk in todo:
            row = existing.get(pk)
            if row is not None:
                if effect == EFFECT_ALLOW and row.effect == EFFECT_DENY and not at_ingest:
                    row.effect = EFFECT_ALLOW
                    row.granted_by = granted_by
                    row.granted_as = granted_as
                    row.save(update_fields=["effect", "granted_by", "granted_as", "updated_at"])
                    result.upgraded += 1
                    transients.add(rows[pk][1])
                else:
                    result.unchanged += 1
                continue
            if pk in shared:
                result.kept_shared += 1
                continue
            phot_id, tid = rows[pk]
            new.append(DataAccessRule(
                **{target: pk}, photometry_id=phot_id, transient_id=tid, effect=effect, granted_by=granted_by,
                granted_as=granted_as, standing_grant=standing_grant, **grantee))
            transients.add(tid)
        if new:
            # ignore_conflicts: a concurrent writer may have added the same (target, grantee) rule.
            DataAccessRule.objects.bulk_create(new, batch_size=CHUNK, ignore_conflicts=True)
            result.created += len(new)
    result.transients = frozenset(t for t in transients if t is not None)
    return result


def _after_write(result: WriteResult, grantee_group) -> None:
    from YSE_App.services import access_rules, photstat

    if not (result.created or result.upgraded):
        return
    access_rules.bump_version()
    if grantee_group is not None and access_rules.enabled():
        photstat.invalidate_scopes([(tid, grantee_group.pk) for tid in result.transients])


def _audit(instance, actor, description: dict) -> None:
    """One audit-log entry for an action on ``instance`` (the per-point rows are written in bulk)."""
    try:
        from auditlog.models import LogEntry

        LogEntry.objects.log_create(
            instance, action=LogEntry.Action.UPDATE, actor=actor if getattr(actor, "pk", None) else None,
            changes=json.dumps({"access_action": [None, json.dumps(description, sort_keys=True, default=str)]}))
    except Exception:  # noqa: BLE001
        logger.exception("access action not written to the audit log")


def write_rules(*, points: Iterable[int] = (), spectra: Iterable[int] = (), effect: str, grantee_group=None,
                grantee_user=None, granted_by=None, granted_as: str, standing_grant=None,
                at_ingest: bool = False) -> WriteResult:
    """Write rules for point ids ``points`` and spectrum ids ``spectra`` (no permission check: callers check)."""
    from YSE_App.models import TransientPhotData, TransientSpectrum

    _check_grantee(grantee_group, grantee_user)
    _check_effect(effect)
    kw = dict(effect=effect, grantee_group=grantee_group, grantee_user=grantee_user, granted_by=granted_by,
              granted_as=granted_as, standing_grant=standing_grant, at_ingest=at_ingest)
    with transaction.atomic(), _quiet_signals():
        result = _write(TransientPhotData, points, **kw).add(_write(TransientSpectrum, spectra, **kw))
        _after_write(result, grantee_group)
    return result


# ------------------------------------------------------------------ actions


def _night_q(field: str, night_start, night_end) -> Q:
    if night_start is None and night_end is None:
        return Q()
    if night_start is None or night_end is None or night_end <= night_start:
        raise ValidationError({"night": "Give the night's UT start and end, the end after the start."})
    return Q(**{field + "__gte": night_start, field + "__lt": night_end})


def _grant_targets(grant, *, created_since=None, photometry_id=None, spectrum_id=None, point_id=None):
    """``(point ids, spectrum ids)`` a standing grant covers (optionally only new ones, or one dataset)."""
    from YSE_App.models import TransientPhotData, TransientSpectrum

    points = TransientPhotData.objects.filter(photometry__allocation_id=grant.allocation_id).filter(
        _night_q("obs_date", grant.night_start, grant.night_end))
    spectra = TransientSpectrum.objects.filter(allocation_id=grant.allocation_id).filter(
        _night_q("obs_date", grant.night_start, grant.night_end))
    if created_since is not None:
        points = points.filter(created_date__gte=created_since)
        spectra = spectra.filter(created_date__gte=created_since)
    if photometry_id is not None:
        points = points.filter(photometry_id=photometry_id)
        spectra = spectra.none()
    if point_id is not None:
        points = points.filter(pk=point_id)
        spectra = spectra.none()
    if spectrum_id is not None:
        spectra = spectra.filter(pk=spectrum_id)
        points = points.none()
    return list(points.values_list("pk", flat=True)), list(spectra.values_list("pk", flat=True))


def program_action(actor, allocation, *, effect: str, grantee_group=None, grantee_user=None, night_start=None,
                   night_end=None, classical_night=None, note: str = ""):
    """Allow or restrict a grantee on a program, or one night of it: now and for points that arrive later.

    Returns ``(StandingAccessGrant, WriteResult)``.
    """
    from YSE_App.models import StandingAccessGrant

    role = program_role(actor, allocation)
    if role is None:
        raise PermissionDenied({"message": "Only the program's PI, its program deputies or an administrator may "
                                           "set access for this program."})
    _check_grantee(grantee_group, grantee_user)
    _check_effect(effect)
    _night_q("obs_date", night_start, night_end)
    grant = StandingAccessGrant(
        allocation=allocation, effect=effect, grantee_group=grantee_group, grantee_user=grantee_user,
        night_start=night_start, night_end=night_end, classical_night=classical_night, created_by=actor,
        created_as=role, note=(note or "").strip())
    grant.full_clean()
    with transaction.atomic():
        grant.save()
        points, spectra = _grant_targets(grant)
        result = write_rules(points=points, spectra=spectra, effect=effect, grantee_group=grantee_group,
                             grantee_user=grantee_user, granted_by=actor, granted_as=role, standing_grant=grant)
    return grant, result


def stop_standing_grant(actor, grant) -> None:
    """Stop a standing grant: points that arrive later are no longer covered; rules already written stay."""
    if program_role(actor, grant.allocation) is None:
        raise PermissionDenied({"message": "Only the program's PI, its program deputies or an administrator may "
                                           "stop this."})
    if grant.stopped_at is not None:
        return
    grant.stopped_at = timezone.now()
    grant.stopped_by = actor
    grant.save(update_fields=["stopped_at", "stopped_by"])


def _require_dataset_role(actor, dataset) -> str:
    from YSE_App.services import data_sharing

    role = dataset_role(actor, dataset)
    if role is None:
        raise PermissionDenied({"message": "Only the data's PIs, their deputies, the program's PI or deputies, or "
                                           "an administrator may set access for these data."})
    if data_sharing.is_public_dataset(dataset):
        raise ValidationError({"dataset": "Public data can never be restricted, and everyone may already see it."})
    return role


def dataset_action(actor, dataset, *, effect: str, grantee_group=None, grantee_user=None) -> WriteResult:
    """Allow or restrict a grantee on every point of one photometry set, or on one spectrum."""
    from YSE_App.models import TransientPhotometry

    role = _require_dataset_role(actor, dataset)
    if isinstance(dataset, TransientPhotometry):
        points = list(dataset.transientphotdata_set.values_list("pk", flat=True))
        result = write_rules(points=points, effect=effect, grantee_group=grantee_group, grantee_user=grantee_user,
                             granted_by=actor, granted_as=role)
    else:
        result = write_rules(spectra=[dataset.pk], effect=effect, grantee_group=grantee_group,
                             grantee_user=grantee_user, granted_by=actor, granted_as=role)
    _audit(dataset, actor, {"effect": effect, "grantee_group": getattr(grantee_group, "pk", None),
                            "grantee_user": getattr(grantee_user, "pk", None), "as": role, **result.as_dict()})
    return result


def object_night_action(actor, transient, night_start, night_end, *, effect: str, grantee_group=None,
                        grantee_user=None) -> WriteResult:
    """Allow or restrict a grantee on all of one object's points (any filter) and spectra from one night.

    Datasets ``actor`` may not set access for are left out; :class:`PermissionDenied` when that is all of them.
    """
    from YSE_App.models import TransientPhotometry, TransientSpectrum
    from YSE_App.services import data_sharing

    window = _night_q("obs_date", night_start, night_end)
    from YSE_App.models import TransientPhotData

    in_night = TransientPhotData.objects.filter(window, photometry__transient_id=transient.pk)
    sets = TransientPhotometry.objects.filter(
        transient_id=transient.pk, pk__in=in_night.values("photometry_id"))
    spectra = TransientSpectrum.objects.filter(window, transient_id=transient.pk)
    total = WriteResult()
    permitted = 0
    for dataset in list(sets) + list(spectra):
        role = dataset_role(actor, dataset)
        if role is None or data_sharing.is_public_dataset(dataset):
            continue
        permitted += 1
        if isinstance(dataset, TransientPhotometry):
            points = list(dataset.transientphotdata_set.filter(window).values_list("pk", flat=True))
            part = write_rules(points=points, effect=effect, grantee_group=grantee_group, grantee_user=grantee_user,
                               granted_by=actor, granted_as=role)
        else:
            part = write_rules(spectra=[dataset.pk], effect=effect, grantee_group=grantee_group,
                               grantee_user=grantee_user, granted_by=actor, granted_as=role)
        total = total.add(part)
    if not permitted:
        raise PermissionDenied({"message": "You may not set access for any of this object's data from that night."})
    _audit(transient, actor, {"effect": effect, "night_start": night_start, "night_end": night_end,
                              "grantee_group": getattr(grantee_group, "pk", None),
                              "grantee_user": getattr(grantee_user, "pk", None), **total.as_dict()})
    return total


def delete_rule(actor, rule) -> None:
    """Staff only: delete one rule made by mistake (django-auditlog records the deletion)."""
    if not is_staff(actor):
        raise PermissionDenied({"message": "Only an administrator may remove an access rule."})
    from YSE_App.services import access_rules, photstat

    rule.delete()
    if rule.grantee_group_id and rule.transient_phot_data_id and access_rules.enabled():
        photstat.invalidate_scopes([(rule.transient_id, rule.grantee_group_id)])


# ---------------------------------------------------------------- at ingest


def _active_grants(allocation_id):
    from YSE_App.models import StandingAccessGrant

    # Night grants first: at ingest the first rule for a grantee wins, so the
    # more specific night action beats the program-wide one.
    grants = list(StandingAccessGrant.objects.filter(allocation_id=allocation_id, stopped_at__isnull=True)
                  .select_related("grantee_group", "grantee_user", "created_by"))
    grants.sort(key=lambda g: (g.night_start is None, g.created_at, g.pk))
    return grants


def _apply(grants, *, linked: bool = False, **target) -> WriteResult:
    """Write the grants' rules for one target.

    At ingest (``linked=False``) only points created after each grant count, and a deny is
    written even where the dataset's own groups include the grantee (the point was never
    shared before). For a dataset linked to the program later (``linked=True``, PR 3) every
    matching point counts, as for the action itself, and a deny is not written for points the
    grantee can already see (they stay shared).
    """
    total = WriteResult()
    for grant in grants:
        points, spectra = _grant_targets(grant, created_since=None if linked else grant.created_at, **target)
        if not points and not spectra:
            continue
        if grant.grantee_user is not None and not grant.grantee_user.is_active:
            continue
        total = total.add(write_rules(
            points=points, spectra=spectra, effect=grant.effect, grantee_group=grant.grantee_group,
            grantee_user=grant.grantee_user, granted_by=grant.created_by, granted_as=grant.created_as,
            standing_grant=grant, at_ingest=not linked))
    return total


def apply_standing_grants_to_set(photometry, *, linked: bool = False) -> WriteResult:
    """Write the program's active standing grants' rules for the set's points (idempotent).

    ``linked``: the set was just linked to the program after its data were taken (all its
    points count; see :func:`_apply`). Otherwise only points created after each grant.
    """
    allocation_id = getattr(photometry, "allocation_id", None)
    if not allocation_id:
        return WriteResult()
    grants = _active_grants(allocation_id)
    if not grants:
        return WriteResult()
    return _apply(grants, linked=linked, photometry_id=photometry.pk)


def apply_standing_grants_to_spectrum(spectrum, *, linked: bool = False) -> WriteResult:
    allocation_id = getattr(spectrum, "allocation_id", None)
    if not allocation_id:
        return WriteResult()
    grants = _active_grants(allocation_id)
    if not grants:
        return WriteResult()
    return _apply(grants, linked=linked, spectrum_id=spectrum.pk)


def on_point_created(point) -> None:
    """``post_save`` of a new ``TransientPhotData``: no query unless its set is linked to a program."""
    photometry = point._state.fields_cache.get("photometry")
    if photometry is None:
        from YSE_App.models import TransientPhotometry

        photometry = TransientPhotometry.objects.filter(pk=point.photometry_id).only("pk", "allocation_id").first()
    if photometry is None or not photometry.allocation_id:
        return
    grants = _active_grants(photometry.allocation_id)
    if grants:
        _apply(grants, point_id=point.pk)
