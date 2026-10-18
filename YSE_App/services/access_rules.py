"""Read-time access rules engine (#648, PR 2): per-point rules, precedence and the rules cache.

Only active with ``DATA_ACCESS_GRANTS_ENABLED``. With the flag off (the
default), with staff and anonymous users, and for any user to whom no rule
applies, the hooks in ``services.visibility`` get ``NO_RULES`` and every read
is exactly today's collaboration-group rule, with the same SQL.

**Precedence** for one user U and one photometry point or spectrum (Ryan,
#648, 2026-10-17):

1. Service readers (``visibility.user_sees_everything``: the configured cron and script
   accounts) see everything; staff follow the rules like everyone else (#706).
2. Public data (no collaboration group, or for a Public member only Public)
   can never be restricted.
3. A rule for U personally decides (one per point and user).
4. Otherwise the rules for U's current groups: a deny beats an allow.
5. Otherwise the dataset's own ``groups`` (today's rule), plus what U kept
   from groups U has left: a point shared with such a group (through the
   dataset's ``groups`` or an allow rule) before U last left it stays visible
   to U; a point shared with it later does not.

As SQL (``None`` = the term cannot match, so it is dropped)::

    visible = PUBLIC | PA | (~PD & ~GD & (BASE | GA | XA | XB))

with PA / PD a personal allow / deny on the point, GA / GD an allow / deny for
one of U's groups, XA an allow for a group U left (granted before U left),
XB the dataset's ``groups`` containing a group U left with the point shared
before U left. A rule term is a correlated ``EXISTS`` on the
``(target, grantee)`` unique index.

**Photometry sets** are visible at set level only when *every* point of the
set is visible, so code that reads a set and then all its points never shows
a hidden point; point reads (``visibility.visible_photometry_ids_q``,
``PhotometryService.*PhotData*``) use the exact point condition, and
``visibility.transient_access_q`` counts a transient with any visible point.
A partly visible set counts as hidden photometry for PhotStat.

**Cache.** The rules that can apply to U (which effects U has personally,
which of U's groups have rules, and the groups U has left) are summarised in
:class:`RulesSummary`, cached in the Django cache under the database rules
version (``AccessRulesVersion``, bumped on every rule, standing grant or
group membership write) and memoised on the user object for the request. The
version is read from the database once per request when the flag is on, so a
change reaches every worker at once.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Dict, FrozenSet, NamedTuple, Optional, Tuple

from django.conf import settings
from django.db.models import Exists, F, OuterRef, Q

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME

ALLOW = "allow"
DENY = "deny"

CACHE_SECONDS = 3600
# A memo on a long-lived user object (a job, a shell) is re-checked after this long.
MEMO_SECONDS = 10.0

_local = threading.local()


def enabled() -> bool:
    return bool(getattr(settings, "DATA_ACCESS_GRANTS_ENABLED", False))


# ------------------------------------------------------------------ version


def _local_generation() -> int:
    return getattr(_local, "generation", 0)


def current_version() -> int:
    """The database rules version (one query)."""
    from YSE_App.models import AccessRulesVersion

    value = AccessRulesVersion.objects.filter(pk=1).values_list("version", flat=True).first()
    return int(value or 0)


def bump_version() -> None:
    """Invalidate every cached rules summary (all processes: the version is in the database).

    The new version is at least the current time in microseconds, so a bump
    rolled back with its transaction is never reused by a later one (a
    summary cached under it from rolled-back rows can never be served).
    """
    from django.db.models import BigIntegerField, ExpressionWrapper, Value
    from django.db.models.functions import Greatest

    from YSE_App.models import AccessRulesVersion

    stamp = time.time_ns() // 1000
    big = BigIntegerField()
    new = Greatest(ExpressionWrapper(F("version") + Value(1, output_field=big), output_field=big),
                   Value(stamp, output_field=big), output_field=big)
    if not AccessRulesVersion.objects.filter(pk=1).update(version=new):
        AccessRulesVersion.objects.get_or_create(pk=1, defaults={"version": stamp})
    _local.generation = _local_generation() + 1


# ------------------------------------------------------------------ summary


class RulesSummary(NamedTuple):
    """What can apply to one user (see the module docstring); picklable for the cache."""

    version: int
    user_id: int
    group_ids: Tuple[int, ...]
    public_group_id: Optional[int]
    personal: FrozenSet[str]
    groups: Dict[int, FrozenSet[str]]
    left: Dict[int, object]  # group id -> latest left_at, for groups the user is no longer in
    left_allow: FrozenSet[int]  # groups in ``left`` with allow rules

    @property
    def empty(self) -> bool:
        return not self.personal and not self.groups and not self.left

    def group_ids_with(self, effect: str):
        return sorted(g for g, effects in self.groups.items() if effect in effects)


def _load_summary(user, version: int) -> RulesSummary:
    from YSE_App.models import DataAccessRule, GroupMembershipPeriod

    memberships = list(user.groups.values_list("id", "name"))
    gids = tuple(sorted(g for g, _n in memberships))
    public_gid = next((g for g, n in memberships if n == PUBLIC_COLLABORATION_GROUP_NAME), None)
    left: Dict[int, object] = {}
    for gid, left_at in GroupMembershipPeriod.objects.filter(
            user_id=user.pk, left_at__isnull=False).values_list("group_id", "left_at"):
        if gid in gids:
            continue
        if gid not in left or left_at > left[gid]:
            left[gid] = left_at
    personal = frozenset(
        DataAccessRule.objects.filter(grantee_user_id=user.pk).order_by().values_list("effect", flat=True)
        .distinct())
    groups: Dict[int, set] = {}
    left_allow = set()
    wanted = set(gids) | set(left)
    if wanted:
        for gid, effect in (DataAccessRule.objects.filter(grantee_group_id__in=sorted(wanted)).order_by()
                            .values_list("grantee_group_id", "effect").distinct()):
            if gid in gids:
                groups.setdefault(gid, set()).add(effect)
            elif effect == ALLOW:
                left_allow.add(gid)
    return RulesSummary(
        version=version, user_id=user.pk, group_ids=gids, public_group_id=public_gid, personal=personal,
        groups={g: frozenset(e) for g, e in groups.items()}, left=left, left_allow=frozenset(left_allow))


def summary_for(user) -> Optional[RulesSummary]:
    """The user's :class:`RulesSummary`, or ``None`` when no rule can apply (flag off, staff, anonymous)."""
    if not enabled() or user is None or not getattr(user, "is_authenticated", False):
        return None
    from YSE_App.services.visibility import user_sees_everything

    if user_sees_everything(user):  # service readers only; staff follow the rules like everyone (#706)
        return None
    memo = getattr(user, "_yse_access_rules_memo", None)
    now = time.monotonic()
    if memo is not None and memo[0] == _local_generation() and now - memo[1] < MEMO_SECONDS:
        return memo[2]
    from django.core.cache import cache

    version = current_version()
    key = "yse_access_rules_v1_%s_%s" % (version, user.pk)
    summary = cache.get(key)
    if summary is None or getattr(summary, "version", None) != version:
        summary = _load_summary(user, version)
        cache.set(key, summary, CACHE_SECONDS)
    try:
        user._yse_access_rules_memo = (_local_generation(), now, summary)
    except AttributeError:
        pass
    return summary


def active_summary(user) -> Optional[RulesSummary]:
    """:func:`summary_for` when some rule or left group applies, else ``None``."""
    summary = summary_for(user)
    if summary is None or summary.empty:
        return None
    return summary


def cache_token_suffix(user) -> str:
    """Suffix for plot / fragment cache keys: the rules version, plus a per-user digest when needed.

    Users with the same groups share cached plots, as before, unless one has
    personal rules or has left groups (then their access is their own).
    Empty with the flag off.
    """
    summary = summary_for(user)
    if summary is None:
        return ""
    suffix = "_a%d" % summary.version
    if summary.personal or summary.left:
        raw = "u%s|%s" % (summary.user_id, sorted((g, str(t)) for g, t in summary.left.items()))
        suffix += "_p" + hashlib.sha256(raw.encode()).hexdigest()[:12]
    return suffix


# --------------------------------------------------------------- conditions


def _or(*qs):
    out = None
    for q in qs:
        if q is not None:
            out = q if out is None else out | q
    return out


def _and_not(q, *negated):
    if q is None:
        return None
    for n in negated:
        if n is not None:
            q = q & ~n
    return q


class _Terms(NamedTuple):
    pa: Optional[Q]
    pd: Optional[Q]
    ga: Optional[Q]
    gd: Optional[Q]
    xa: Optional[Q]
    xb: Optional[Q]

    @property
    def rule_visible(self):
        """Visible through a rule or a left group (not the dataset's own groups or public)."""
        return _or(self.pa, _and_not(_or(self.ga, self.xa, self.xb), self.pd, self.gd))

    @property
    def rule_hidden(self):
        """Hidden by a rule (unless public)."""
        return _or(self.pd, _and_not(self.gd, self.pa))


def _rule_terms(summary: RulesSummary, target: str, dataset_ref: str, through, through_fk: str,
                share_fk: str) -> _Terms:
    """The rule terms for rows whose rule target column is ``target`` (``transient_phot_data_id`` /
    ``transient_spectrum_id``); ``dataset_ref`` is the dataset id on the outer row (``photometry_id`` /
    ``pk``)."""
    from YSE_App.models import DataAccessRule, DatasetGroupShare

    def rules(**flt):
        return Q(Exists(DataAccessRule.objects.filter(**{target: OuterRef("pk")}, **flt)))

    pa = rules(grantee_user_id=summary.user_id, effect=ALLOW) if ALLOW in summary.personal else None
    pd = rules(grantee_user_id=summary.user_id, effect=DENY) if DENY in summary.personal else None
    allow_groups = summary.group_ids_with(ALLOW)
    deny_groups = summary.group_ids_with(DENY)
    ga = rules(grantee_group_id__in=allow_groups, effect=ALLOW) if allow_groups else None
    gd = rules(grantee_group_id__in=deny_groups, effect=DENY) if deny_groups else None
    xa = None
    xb = None
    for gid, left_at in sorted(summary.left.items()):
        if gid in summary.left_allow:
            xa = _or(xa, rules(grantee_group_id=gid, effect=ALLOW, updated_at__lt=left_at))
        on_dataset = Q(Exists(through.objects.filter(**{through_fk: OuterRef(dataset_ref)}, group_id=gid)))
        shared_later = Q(Exists(DatasetGroupShare.objects.filter(
            **{share_fk: OuterRef(dataset_ref)}, group_id=gid, shared_at__gte=left_at)))
        xb = _or(xb, on_dataset & Q(created_date__lt=left_at) & ~shared_later)
    return _Terms(pa, pd, ga, gd, xa, xb)


def _point_terms(summary: RulesSummary) -> _Terms:
    from YSE_App.models import TransientPhotometry

    return _rule_terms(summary, "transient_phot_data_id", "photometry_id", TransientPhotometry.groups.through,
                       "transientphotometry_id", "transient_photometry_id")


def _spectrum_terms(summary: RulesSummary) -> _Terms:
    from YSE_App.models import TransientSpectrum

    return _rule_terms(summary, "transient_spectrum_id", "pk", TransientSpectrum.groups.through,
                       "transientspectrum_id", "transient_spectrum_id")


def _public_set_q(summary: RulesSummary, set_ref: str) -> Q:
    """The point's / set's photometry set is public for this user (no deny may hide it)."""
    from YSE_App.models import TransientPhotometry

    links = TransientPhotometry.groups.through.objects.filter(transientphotometry_id=OuterRef(set_ref))
    if summary.public_group_id is not None:
        links = links.exclude(group_id=summary.public_group_id)
    return Q(~Exists(links))


def point_visible_q(summary: RulesSummary) -> Q:
    """Condition on ``TransientPhotData``: the points the summary's user may see (the full rule)."""
    from YSE_App.models import TransientPhotometry

    terms = _point_terms(summary)
    base = None
    if summary.group_ids:
        base = Q(Exists(TransientPhotometry.groups.through.objects.filter(
            transientphotometry_id=OuterRef("photometry_id"), group_id__in=list(summary.group_ids))))
    return _or(_public_set_q(summary, "photometry_id"), terms.pa,
               _and_not(_or(base, terms.ga, terms.xa, terms.xb), terms.pd, terms.gd))


def _rule_sets_q(summary: RulesSummary, effect: str) -> Optional[Q]:
    """``TransientPhotometry`` rows with a point rule of ``effect`` that may apply to the user."""
    from YSE_App.models import DataAccessRule

    grantee = None
    if effect in summary.personal:
        grantee = Q(grantee_user_id=summary.user_id)
    gids = summary.group_ids_with(effect)
    if effect == ALLOW:
        gids = sorted(set(gids) | set(summary.left_allow))
    if gids:
        grantee = _or(grantee, Q(grantee_group_id__in=gids))
    if grantee is None:
        return None
    rows = DataAccessRule.objects.filter(grantee, effect=effect, transient_phot_data__isnull=False)
    return Q(pk__in=rows.values("photometry_id"))


def photometry_rules(user):
    """``AccessRules`` on ``TransientPhotometry`` for ``user`` (set level: every point visible)."""
    from YSE_App.models import TransientPhotData, TransientPhotometry
    from YSE_App.services.visibility import NO_RULES, AccessRules

    summary = active_summary(user)
    if summary is None:
        return NO_RULES
    terms = _point_terms(summary)
    points = TransientPhotData.objects.filter(photometry_id=OuterRef("pk"))
    allow = None
    visible = terms.rule_visible
    if visible is not None:
        candidates = _rule_sets_q(summary, ALLOW)
        if summary.left:
            candidates = _or(candidates, Q(pk__in=TransientPhotometry.groups.through.objects.filter(
                group_id__in=sorted(summary.left)).values("transientphotometry_id")))
        if candidates is not None:
            allow = candidates & Q(Exists(points)) & ~Q(Exists(points.filter(~visible)))
    deny = None
    hidden = terms.rule_hidden
    if hidden is not None:
        candidates = _rule_sets_q(summary, DENY)
        if candidates is not None:
            deny = candidates & Q(Exists(points.filter(hidden)))
    if allow is None and deny is None:
        return NO_RULES
    return AccessRules(allow=allow, deny=deny)


def spectrum_rules(user):
    """``AccessRules`` on ``TransientSpectrum`` for ``user``."""
    from YSE_App.services.visibility import NO_RULES, AccessRules

    summary = active_summary(user)
    if summary is None:
        return NO_RULES
    terms = _spectrum_terms(summary)
    allow, deny = terms.rule_visible, terms.rule_hidden
    if allow is None and deny is None:
        return NO_RULES
    return AccessRules(allow=allow, deny=deny)


# ------------------------------------------------------------- for PhotStat


def scope_restrictions(user):
    """``(personal_deny_transients_q, {group_id: transients_q})`` for the PhotStat scope choice, or ``None``.

    On a transient where the user has a personal deny, only the public scope
    may be used (Ryan, Q5); where one of the user's groups ``h`` has a deny,
    the scopes of the user's other groups may contain the denied points, so
    only ``h``'s scope (built without them) and the public scope remain.
    """
    from YSE_App.models import DataAccessRule

    summary = active_summary(user)
    if summary is None:
        return None
    personal = None
    if DENY in summary.personal:
        personal = DataAccessRule.objects.filter(grantee_user_id=summary.user_id, effect=DENY,
                                                 transient_phot_data__isnull=False).values("transient_id")
    by_group = {
        gid: DataAccessRule.objects.filter(grantee_group_id=gid, effect=DENY,
                                           transient_phot_data__isnull=False).values("transient_id")
        for gid in summary.group_ids_with(DENY)
    }
    if personal is None and not by_group:
        return None
    return personal, by_group


def point_group_rules(transient_ids) -> Dict[int, Dict[int, Dict[int, str]]]:
    """``{transient_id: {point_id: {group_id: effect}}}``: the group rules on those transients' points (one query)."""
    from YSE_App.models import DataAccessRule

    ids = sorted({int(t) for t in transient_ids if t is not None})
    out: Dict[int, Dict[int, Dict[int, str]]] = {}
    if not ids:
        return out
    rows = DataAccessRule.objects.filter(
        transient_id__in=ids, grantee_group__isnull=False, transient_phot_data__isnull=False,
    ).values_list("transient_id", "transient_phot_data_id", "grantee_group_id", "effect")
    for tid, point_id, gid, effect in rows:
        out.setdefault(tid, {}).setdefault(point_id, {})[gid] = effect
    return out
