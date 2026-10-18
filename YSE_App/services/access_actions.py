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
* Turning sharing off (:func:`stop_standing_grant`, :func:`remove_rules`,
  :func:`remove_dataset_group`): the same people. Staff may also delete a
  rule in the admin.

**What is written** (Ryan, 2026-10-17, replacing the "once shared, stays shared" reading)

* An allow creates the rule, or turns the grantee's deny into an allow.
* A restriction (deny) may be put on any non-public data, also data the
  grantee can already see: it creates the rule, or turns the grantee's allow
  into a deny.
* Turning sharing off goes **forward in time** by default: data taken before
  the change stay visible to that grantee. A **backwards** option also removes
  access to the earlier data:
  - stopping a standing grant: forward keeps the rules it wrote (and keeps
    covering data taken before the stop); backwards deletes them;
  - removing an allow from data: forward changes nothing for data already
    taken; backwards deletes the allow rules;
  - removing a group from a dataset's own groups: forward writes ``kept``
    allow rules for the group on the points shared with it before the change
    (dated when they were shared, so former members keep them too); backwards
    writes none.
* Standing grants applied at ingest or to newly linked data never replace an
  explicit rule for the same grantee (the first rule wins; night grants are
  applied before program-wide ones).
* Public data (no collaboration group other than Public) gets no rules from
  an action: it can never be restricted, and everyone may see it already. At
  ingest the rule is written anyway, because a new dataset's groups are set
  just after it is saved (a rule never hides data that stay public).
* A program or night action is also saved as a ``StandingAccessGrant``, so
  points that arrive later get the rule at ingest (:func:`apply_standing_grants_to_set`,
  :func:`apply_standing_grants_to_spectrum`, called by the upload paths and
  signals). Stopping it (:func:`stop_standing_grant`) applies going forward
  in time: points taken (``obs_date``) after the stop are not covered, points
  taken before it still are, also when they are uploaded or linked to the
  program later. A night grant covers only its night, whenever the points
  arrive.

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
    AS_KEPT,
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
    overridden: int = 0  # an allow that became a deny (a restriction of shared data)
    public: int = 0  # public data: no rule needed
    transients: frozenset = frozenset()

    def add(self, other: "WriteResult") -> "WriteResult":
        return WriteResult(
            self.created + other.created, self.upgraded + other.upgraded, self.unchanged + other.unchanged,
            self.overridden + other.overridden, self.public + other.public,
            frozenset(self.transients | other.transients))

    def as_dict(self):
        return {"created": self.created, "upgraded": self.upgraded, "unchanged": self.unchanged,
                "overridden": self.overridden, "public": self.public}


# ------------------------------------------------------------------- writes


def _restricted_dataset_ids(through, fk: str, ids) -> set:
    """Datasets among ``ids`` with a collaboration group other than Public (the others are public)."""
    return set(through.objects.filter(**{fk + "__in": list(ids)}).exclude(
        group__name=PUBLIC_COLLABORATION_GROUP_NAME).values_list(fk, flat=True))


def _write(model, ids, *, effect, grantee_group, grantee_user, granted_by, granted_as, standing_grant=None,
           at_ingest=False, skip_public=False) -> WriteResult:
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
        if at_ingest and not skip_public:
            # A new dataset gets its groups just after it is saved (the m2m comes second), so at
            # ingest every point gets the rule; one on data that stay public never hides them.
            public = set()
        result.public += len(public)
        todo = [pk for pk in sorted(rows) if pk not in public]
        if not todo:
            continue
        existing = {getattr(r, target): r for r in DataAccessRule.objects.filter(**{target + "__in": todo}, **grantee)}
        new = []
        for pk in todo:
            row = existing.get(pk)
            if row is not None:
                if row.effect != effect and not at_ingest:
                    # An explicit action replaces the grantee's rule: deny -> allow shares, allow -> deny
                    # restricts data already shared (a PI may restrict any non-public data).
                    if effect == EFFECT_ALLOW:
                        result.upgraded += 1
                    else:
                        result.overridden += 1
                    row.effect = effect
                    row.granted_by = granted_by
                    row.granted_as = granted_as
                    row.standing_grant = standing_grant
                    row.save(update_fields=["effect", "granted_by", "granted_as", "standing_grant", "updated_at"])
                    transients.add(rows[pk][1])
                else:
                    result.unchanged += 1
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

    if not (result.created or result.upgraded or result.overridden):
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
                at_ingest: bool = False, skip_public: bool = False) -> WriteResult:
    """Write rules for point ids ``points`` and spectrum ids ``spectra`` (no permission check: callers check).

    ``at_ingest``: a standing grant applied to new (or newly linked) data: an existing rule for
    the grantee is left alone. Otherwise an explicit action: it replaces the grantee's rule.
    """
    from YSE_App.models import TransientPhotData, TransientSpectrum

    _check_grantee(grantee_group, grantee_user)
    _check_effect(effect)
    kw = dict(effect=effect, grantee_group=grantee_group, grantee_user=grantee_user, granted_by=granted_by,
              granted_as=granted_as, standing_grant=standing_grant, at_ingest=at_ingest, skip_public=skip_public)
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
    if grant.stopped_at is not None:
        # Stopping applies going forward in time (Ryan): data taken before the stop stay covered.
        points = points.filter(obs_date__lt=grant.stopped_at)
        spectra = spectra.filter(obs_date__lt=grant.stopped_at)
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


def stop_standing_grant(actor, grant, *, backwards: bool = False) -> int:
    """Stop a standing grant. Returns the number of rules deleted (backwards only).

    Forward (the default): data taken after now are no longer covered; the rules it wrote stay,
    and data taken before the stop are still covered when they arrive or are linked later.
    ``backwards``: also delete every rule it wrote, so earlier data lose (an allow) or regain
    (a restriction) access too; the grant then covers nothing.
    """
    from YSE_App.models import DataAccessRule

    if program_role(actor, grant.allocation) is None:
        raise PermissionDenied({"message": "Only the program's PI, its program deputies or an administrator may "
                                           "stop this."})
    deleted = 0
    with transaction.atomic():
        if grant.stopped_at is None:
            grant.stopped_at = timezone.now()
            grant.stopped_by = actor
            grant.save(update_fields=["stopped_at", "stopped_by"])
        if backwards:
            # A backwards stop covers no data at all: mark it stopped at its own creation
            # (``_program_grants`` and ``covers_obs_date`` then skip it).
            if grant.stopped_at > grant.created_at:
                grant.stopped_at = grant.created_at
                grant.save(update_fields=["stopped_at"])
            deleted = _delete_rules(DataAccessRule.objects.filter(standing_grant=grant),
                                    grant.grantee_group_id)
    return deleted


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
    from YSE_App.models import DataAccessRule

    _delete_rules(DataAccessRule.objects.filter(pk=rule.pk), rule.grantee_group_id)


def _delete_rules(rules, grantee_group_id=None) -> int:
    """Delete ``rules`` (each deletion audited), bump the version, refresh the group's PhotStat scopes."""
    from YSE_App.services import access_rules, photstat

    transients = set(rules.filter(transient_phot_data__isnull=False).values_list("transient_id", flat=True))
    with transaction.atomic(), _quiet_signals():
        n, _ = rules.delete()
        if n:
            access_rules.bump_version()
            if grantee_group_id and access_rules.enabled():
                photstat.invalidate_scopes([(t, grantee_group_id) for t in transients])
    return n


def remove_rules(actor, dataset, *, effect: str, grantee_group=None, grantee_user=None, backwards: bool = False,
                 night_start=None, night_end=None) -> int:
    """Turn off one grantee's rules of ``effect`` on one dataset (optionally one night of it).

    * An allow (turning sharing off): forward (the default) changes nothing for data already
      taken, which stay visible; ``backwards`` deletes the allow rules.
    * A restriction (lifting it): the deny rules are deleted (it shares, so it is never "forward").

    Returns the number of rules deleted. Whoever may set access for the dataset (:func:`dataset_role`).
    """
    from YSE_App.models import DataAccessRule, TransientPhotometry

    if dataset_role(actor, dataset) is None:
        raise PermissionDenied({"message": "Only the data's PIs, their deputies, the program's PI or deputies, or "
                                           "an administrator may change access to these data."})
    _check_grantee(grantee_group, grantee_user)
    _check_effect(effect)
    if effect == EFFECT_ALLOW and not backwards:
        return 0
    if isinstance(dataset, TransientPhotometry):
        rules = DataAccessRule.objects.filter(photometry_id=dataset.pk).filter(
            _night_q("transient_phot_data__obs_date", night_start, night_end))
    else:
        rules = DataAccessRule.objects.filter(transient_spectrum_id=dataset.pk)
    rules = rules.filter(effect=effect).filter(
        **({"grantee_group": grantee_group} if grantee_group is not None else {"grantee_user": grantee_user}))
    n = _delete_rules(rules, getattr(grantee_group, "pk", None))
    _audit(dataset, actor, {"removed": effect, "backwards": backwards, "rules": n,
                            "grantee_group": getattr(grantee_group, "pk", None),
                            "grantee_user": getattr(grantee_user, "pk", None)})
    return n


# ------------------------------------------------------- dataset groups (#648, PR 6a)

_group_removal = threading.local()


def _keep_suppressed() -> bool:
    return getattr(_group_removal, "backwards", 0) > 0


def remove_dataset_group(actor, dataset, group, *, backwards: bool = False) -> int:
    """Remove ``group`` from a dataset's own groups. Returns the number of ``kept`` rules written.

    Forward (the default): the points shared with the group before now stay visible to it (and
    to people who were members when they were shared), through ``kept`` allow rules.
    ``backwards``: the group loses the earlier data too (no rules written).
    """
    from YSE_App.services import data_sharing

    if dataset_role(actor, dataset) is None:
        raise PermissionDenied({"message": "Only the data's PIs, their deputies, the program's PI or deputies, or "
                                           "an administrator may change the groups of these data."})
    if data_sharing.is_public_dataset(dataset) or not dataset.groups.filter(pk=group.pk).exists():
        return 0
    from YSE_App.models import DataAccessRule

    before = DataAccessRule.objects.filter(grantee_group=group, granted_as=AS_KEPT).count()
    depth = getattr(_group_removal, "backwards", 0)
    _group_removal.backwards = depth + (1 if backwards else 0)
    try:
        dataset.groups.remove(group)
    finally:
        _group_removal.backwards = depth
    written = DataAccessRule.objects.filter(grantee_group=group, granted_as=AS_KEPT).count() - before
    _audit(dataset, actor, {"group_removed": group.pk, "backwards": backwards, "kept_rules": written})
    return written


def keep_after_group_removal(model, pairs, shared_at, at=None) -> int:
    """Forward default when groups are removed from datasets: ``kept`` allows for the earlier points.

    ``pairs``: ``(dataset_id, group_id)`` removed; ``shared_at``: ``{pair: share time or None}`` from
    ``DatasetGroupShare`` (``None``: shared before tracking). Each kept rule is dated when its point
    was shared with the group (the later of the share time and the point's creation), so the
    leaving-member rule still applies. Datasets left with no restricting group are public: nothing
    to keep. Called from ``access_history`` (the ``m2m_changed`` receiver); skipped for a backwards
    removal. Returns the number of rules written.
    """
    from django.db.models import OuterRef, Subquery

    from YSE_App.models import DataAccessRule, TransientPhotData, TransientPhotometry, TransientSpectrum
    from YSE_App.services import access_rules

    if _keep_suppressed() or not pairs:
        return 0
    at = at or timezone.now()
    is_phot = model is TransientPhotometry
    through = model.groups.through
    fk = "transientphotometry_id" if is_phot else "transientspectrum_id"
    restricted = set(through.objects.filter(**{fk + "__in": {d for d, _g in pairs}}).exclude(
        group__name=PUBLIC_COLLABORATION_GROUP_NAME).values_list(fk, flat=True))
    written = 0
    with transaction.atomic(), _quiet_signals():
        for dataset_id, group_id in sorted(pairs):
            if dataset_id not in restricted:
                continue
            if is_phot:
                targets = list(TransientPhotData.objects.filter(photometry_id=dataset_id, obs_date__lt=at)
                               .values_list("pk", "photometry__transient_id"))
                rows = [DataAccessRule(transient_phot_data_id=pk, photometry_id=dataset_id, transient_id=tid,
                                       grantee_group_id=group_id, effect=EFFECT_ALLOW, granted_as=AS_KEPT)
                        for pk, tid in targets]
            else:
                targets = list(TransientSpectrum.objects.filter(pk=dataset_id, obs_date__lt=at)
                               .values_list("pk", "transient_id"))
                rows = [DataAccessRule(transient_spectrum_id=pk, transient_id=tid, grantee_group_id=group_id,
                                       effect=EFFECT_ALLOW, granted_as=AS_KEPT) for pk, tid in targets]
            if not rows:
                continue
            # A rule the group already has (an allow, or a restriction) stays as it is.
            DataAccessRule.objects.bulk_create(rows, batch_size=CHUNK, ignore_conflicts=True)
            target = "transient_phot_data" if is_phot else "transient_spectrum"
            new = DataAccessRule.objects.filter(granted_as=AS_KEPT, grantee_group_id=group_id,
                                                **{target + "_id__in": [pk for pk, _t in targets]},
                                                updated_at__gte=at)
            source = TransientPhotData if is_phot else TransientSpectrum
            new.update(updated_at=Subquery(source.objects.filter(pk=OuterRef(target + "_id"))
                                           .values("created_date")[:1]))
            share = shared_at.get((dataset_id, group_id))
            if share is not None:
                DataAccessRule.objects.filter(granted_as=AS_KEPT, grantee_group_id=group_id,
                                              **{target + "_id__in": [pk for pk, _t in targets]},
                                              updated_at__lt=share).update(updated_at=share)
            written += len(rows)
        if written:
            access_rules.bump_version()
    return written


# ---------------------------------------------------------------- at ingest


def _program_grants(allocation_id):
    """The program's standing grants, active and stopped (a stopped one still covers data taken before its stop)."""
    from YSE_App.models import StandingAccessGrant

    # Night grants first: at ingest the first rule for a grantee wins, so the
    # more specific night action beats the program-wide one.
    from django.db.models import F

    # A grant stopped backwards (stopped_at at or before its creation) covers no data at all.
    grants = list(StandingAccessGrant.objects.filter(allocation_id=allocation_id)
                  .exclude(stopped_at__lte=F("created_at"))
                  .select_related("grantee_group", "grantee_user", "created_by"))
    grants.sort(key=lambda g: (g.night_start is None, g.created_at, g.pk))
    return grants


def _apply(grants, *, linked: bool = False, **target) -> WriteResult:
    """Write the grants' rules for one target.

    At ingest (``linked=False``) only points created after each grant count. For a dataset
    linked to the program later (``linked=True``, PR 3) every matching point counts. Either
    way an explicit rule the grantee already has on a point is left alone.
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
            standing_grant=grant, at_ingest=True, skip_public=linked))
    return total


def apply_standing_grants_to_set(photometry, *, linked: bool = False) -> WriteResult:
    """Write the program's active standing grants' rules for the set's points (idempotent).

    ``linked``: the set was just linked to the program after its data were taken (all its
    points count; see :func:`_apply`). Otherwise only points created after each grant.
    """
    allocation_id = getattr(photometry, "allocation_id", None)
    if not allocation_id:
        return WriteResult()
    grants = _program_grants(allocation_id)
    if not grants:
        return WriteResult()
    return _apply(grants, linked=linked, photometry_id=photometry.pk)


def apply_standing_grants_to_spectrum(spectrum, *, linked: bool = False) -> WriteResult:
    allocation_id = getattr(spectrum, "allocation_id", None)
    if not allocation_id:
        return WriteResult()
    grants = _program_grants(allocation_id)
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
    grants = _program_grants(photometry.allocation_id)
    if grants:
        _apply(grants, point_id=point.pk)
