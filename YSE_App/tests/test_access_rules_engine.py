"""Per-point access rules engine (#648, PR 2): precedence, history, cache keys, PhotStat and actions.

Synthetic data only. Most tests run with ``DATA_ACCESS_GRANTS_ENABLED`` on;
``FlagOffTests`` checks that with it off nothing changes (same rows, same SQL).
"""

import datetime

from django.contrib.auth.models import AnonymousUser, Group
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.data import PhotometryService, SpectraService
from YSE_App.models import (
    AccessRulesVersion,
    Allocation,
    AllocationDelegate,
    DataAccessRule,
    GroupMembershipPeriod,
    PrincipalInvestigator,
    StandingAccessGrant,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.models.phot_stat_models import TransientPhotStatScope
from YSE_App.services import access_actions, access_rules, photstat, photstat_visibility, visibility
from YSE_App.services.visibility import (
    group_access_plot_cache_token,
    hidden_photometry_exists,
    photometry_visible_q,
    spectrum_visible_q,
    transient_access_q,
    visible_photometry_ids_q,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

R = DataAccessRule
ALLOW, DENY = R.EFFECT_ALLOW, R.EFFECT_DENY
ON = override_settings(DATA_ACCESS_GRANTS_ENABLED=True)
OFF = override_settings(DATA_ACCESS_GRANTS_ENABLED=False)


def _pks(qs):
    return set(qs.values_list("pk", flat=True))


class _Fixture(TestCase):
    """One transient with a public, a G-only, an O-only and a G+O photometry set (two points each) and spectra."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("are648_staff", is_staff=True)
        cls.member = create_test_user("are648_member", is_staff=False)  # in G
        cls.member2 = create_test_user("are648_member2", is_staff=False)  # in G
        cls.omember = create_test_user("are648_omember", is_staff=False)  # in O
        cls.both = create_test_user("are648_both", is_staff=False)  # in G and O
        cls.outsider = create_test_user("are648_outsider", is_staff=False)
        cls.pi_user = create_test_user("are648_pi", is_staff=False)
        cls.deputy = create_test_user("are648_deputy", is_staff=False)
        cls.g, _ = Group.objects.get_or_create(name="are648-g")
        cls.o, _ = Group.objects.get_or_create(name="are648-o")
        cls.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        cls.member.groups.add(cls.g)
        cls.member2.groups.add(cls.g)
        cls.omember.groups.add(cls.o)
        cls.both.groups.add(cls.g, cls.o)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="are648")
        now = timezone.now()
        cls.now = now
        cls.pi = PrincipalInvestigator.objects.create(
            name="are648 PI", phone="0", email="are648-pi@example.com", institution="Synthetic", user=cls.pi_user,
            **audit_fields(cls.staff))
        cls.allocation = Allocation.objects.create(
            name="are648 alloc", telescope=cls.instrument.telescope, instrument=cls.instrument,
            principal_investigator=cls.pi, start_date=now - datetime.timedelta(days=30),
            end_date=now + datetime.timedelta(days=150), **audit_fields(cls.staff))
        AllocationDelegate.objects.create(allocation=cls.allocation, user=cls.deputy, added_by=cls.staff)
        cls.transient = create_minimal_transient(cls.staff, name="YSEare648", ra=41.0, dec=-6.0)
        cls.sets = {}
        cls.points = {}
        cls.specs = {}
        for key, groups in (("pub", ()), ("g", (cls.g,)), ("o", (cls.o,)), ("go", (cls.g, cls.o))):
            phot = TransientPhotometry.objects.create(
                transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group,
                allocation=cls.allocation, **audit_fields(cls.staff))
            spec = TransientSpectrum.objects.create(
                transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group,
                ra=cls.transient.ra, dec=cls.transient.dec, obs_date=now - datetime.timedelta(days=1),
                allocation=cls.allocation, **audit_fields(cls.staff))
            if groups:
                phot.groups.add(*groups)
                spec.groups.add(*groups)
            phot.pis.add(cls.pi)
            spec.pis.add(cls.pi)
            for i, days in enumerate((3, 1)):
                cls.points[(key, i)] = TransientPhotData.objects.create(
                    photometry=phot, band=cls.band, mag=18.0 + i, mag_err=0.05,
                    obs_date=now - datetime.timedelta(days=days), **audit_fields(cls.staff))
            cls.sets[key] = phot
            cls.specs[key] = spec

    def setUp(self):
        cache.clear()

    def rule(self, target, effect=ALLOW, *, group=None, user=None):
        kw = {"transient_spectrum": target} if isinstance(target, TransientSpectrum) else {"transient_phot_data": target}
        return R.objects.create(effect=effect, grantee_group=group, grantee_user=user, granted_by=self.staff,
                                granted_as=R.AS_STAFF, **kw)

    def fresh(self, user):
        return type(user).objects.get(pk=user.pk)

    def points_of(self, user):
        user = self.fresh(user)
        q = visible_photometry_ids_q(user)
        return _pks(TransientPhotData.objects.filter(q) if q is not None else TransientPhotData.objects.all())

    def sets_of(self, user):
        return _pks(TransientPhotometry.objects.filter(photometry_visible_q(self.fresh(user))).distinct())

    def specs_of(self, user):
        return _pks(TransientSpectrum.objects.filter(spectrum_visible_q(self.fresh(user))).distinct())

    def p(self, *keys):
        return {self.points[k].pk for k in keys}

    def all_points(self, *sets):
        return {self.points[(s, i)].pk for s in sets for i in (0, 1)}


@OFF
class FlagOffTests(_Fixture):
    def test_rules_change_nothing_and_sql_is_unchanged(self):
        users = (self.member, self.outsider, self.both, AnonymousUser())
        before = {}
        for u in users:
            before[str(u)] = (str(TransientPhotometry.objects.filter(photometry_visible_q(u)).query),
                              str(TransientSpectrum.objects.filter(spectrum_visible_q(u)).query),
                              group_access_plot_cache_token(u))
        self.rule(self.points[("g", 0)], DENY, user=self.member)
        self.rule(self.points[("o", 0)], ALLOW, user=self.outsider)
        self.rule(self.specs["g"], DENY, group=self.g)
        for u in users:
            self.assertEqual(visibility._photometry_access_rules(u), visibility.NO_RULES)
            self.assertEqual(visibility._spectrum_access_rules(u), visibility.NO_RULES)
            self.assertIsNone(visibility.photometry_points_visible_q(u))
            after = (str(TransientPhotometry.objects.filter(photometry_visible_q(u)).query),
                     str(TransientSpectrum.objects.filter(spectrum_visible_q(u)).query),
                     group_access_plot_cache_token(u))
            self.assertEqual(after, before[str(u)])
        self.assertEqual(self.points_of(self.member), self.all_points("pub", "g", "go"))
        self.assertEqual(self.points_of(self.outsider), self.all_points("pub"))

    def test_no_queries_for_the_rules(self):
        user = self.fresh(self.member)
        with CaptureQueriesContext(connection) as ctx:
            access_rules.summary_for(user)
            access_rules.cache_token_suffix(user)
            visibility._photometry_access_rules(user)
        self.assertEqual(len(ctx.captured_queries), 0)


@ON
class NoRulesTests(_Fixture):
    """A user to whom no rule applies: today's rows and SQL, one version read per request."""

    def test_same_sql_and_rows_as_flag_off(self):
        self.rule(self.points[("o", 0)], ALLOW, user=self.outsider)  # someone else's rule
        for u in (self.member, self.both):
            u = self.fresh(u)
            with OFF:
                off = (str(TransientPhotometry.objects.filter(photometry_visible_q(u)).query),
                       str(TransientSpectrum.objects.filter(spectrum_visible_q(u)).query),
                       str(TransientPhotData.objects.filter(visible_photometry_ids_q(u)).query),
                       str(PhotometryService.GetAuthorizedTransientPhotData_ByUser(u).query))
            on = (str(TransientPhotometry.objects.filter(photometry_visible_q(u)).query),
                  str(TransientSpectrum.objects.filter(spectrum_visible_q(u)).query),
                  str(TransientPhotData.objects.filter(visible_photometry_ids_q(u)).query),
                  str(PhotometryService.GetAuthorizedTransientPhotData_ByUser(u).query))
            self.assertEqual(on, off)

    def test_query_counts_unchanged_after_the_first_read(self):
        def reads(u):
            list(PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(u, self.transient.pk))
            list(SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(u, self.transient.pk))
            list(Transient.objects.filter(transient_access_q(u)))
            group_access_plot_cache_token(u)

        with OFF:
            u = self.fresh(self.member)
            with CaptureQueriesContext(connection) as ctx:
                reads(u)
            off = len(ctx.captured_queries)
        u = self.fresh(self.member)
        access_rules.summary_for(u)  # the request's version read (and the summary, cached)
        with CaptureQueriesContext(connection) as ctx:
            reads(u)
        self.assertEqual(len(ctx.captured_queries), off)
        # a later request: one version read, the summary comes from the cache
        u = self.fresh(self.member)
        with CaptureQueriesContext(connection) as ctx:
            access_rules.summary_for(u)
        self.assertEqual(len(ctx.captured_queries), 1)


@ON
class PrecedenceTests(_Fixture):
    def test_personal_allow_widens_to_one_point(self):
        point = self.points[("g", 0)]
        self.rule(point, ALLOW, user=self.outsider)
        self.assertEqual(self.points_of(self.outsider), self.all_points("pub") | {point.pk})
        # set level: the G set is only partly visible, so it is not listed as a set
        self.assertNotIn(self.sets["g"].pk, self.sets_of(self.outsider))
        outsider = self.fresh(self.outsider)
        self.assertEqual(
            _pks(PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(outsider, self.transient.pk)),
            self.all_points("pub") | {point.pk})
        self.assertTrue(Transient.objects.filter(transient_access_q(outsider), pk=self.transient.pk).exists())
        # both points allowed: the whole set is visible
        self.rule(self.points[("g", 1)], ALLOW, user=self.outsider)
        self.assertIn(self.sets["g"].pk, self.sets_of(self.outsider))

    def test_partly_allowed_transient_is_listed(self):
        lonely = create_minimal_transient(self.staff, name="YSEare648b", ra=42.0, dec=-6.0)
        phot = TransientPhotometry.objects.create(transient=lonely, instrument=self.instrument,
                                                  obs_group=self.obs_group, **audit_fields(self.staff))
        phot.groups.add(self.g)
        pts = [TransientPhotData.objects.create(photometry=phot, band=self.band, mag=19.0, mag_err=0.1,
                                                obs_date=self.now - datetime.timedelta(days=d),
                                                **audit_fields(self.staff)) for d in (1, 2)]
        outsider = self.fresh(self.outsider)
        self.assertFalse(Transient.objects.filter(transient_access_q(outsider), pk=lonely.pk).exists())
        self.rule(pts[0], ALLOW, user=self.outsider)
        outsider = self.fresh(self.outsider)
        self.assertTrue(Transient.objects.filter(transient_access_q(outsider), pk=lonely.pk).exists())
        self.assertTrue(Transient.objects.filter(hidden_photometry_exists(outsider), pk=lonely.pk).exists())

    def test_personal_deny_narrows(self):
        point = self.points[("g", 0)]
        self.rule(point, DENY, user=self.member)
        self.assertEqual(self.points_of(self.member), self.all_points("pub", "g", "go") - {point.pk})
        self.assertNotIn(self.sets["g"].pk, self.sets_of(self.member))
        self.assertIn(self.sets["go"].pk, self.sets_of(self.member))
        member = self.fresh(self.member)
        self.assertTrue(Transient.objects.filter(hidden_photometry_exists(member), pk=self.transient.pk).exists())
        self.assertEqual(self.points_of(self.member2), self.all_points("pub", "g", "go"))  # only that user

    def test_public_data_is_never_hidden(self):
        for key in (("pub", 0), ("pub", 1)):
            self.rule(self.points[key], DENY, user=self.member)
            self.rule(self.points[key], DENY, group=self.g)
        self.rule(self.specs["pub"], DENY, user=self.member)
        self.assertTrue(self.all_points("pub") <= self.points_of(self.member))
        self.assertIn(self.sets["pub"].pk, self.sets_of(self.member))
        self.assertIn(self.specs["pub"].pk, self.specs_of(self.member))

    def test_group_rule_beats_the_datasets_groups(self):
        point = self.points[("go", 0)]
        self.rule(point, DENY, group=self.g)
        self.assertNotIn(point.pk, self.points_of(self.member))
        self.assertNotIn(point.pk, self.points_of(self.both))  # in O too, but a G deny is a rule
        self.assertIn(point.pk, self.points_of(self.omember))  # not in G
        self.rule(self.points[("o", 0)], ALLOW, group=self.g)
        self.assertIn(self.points[("o", 0)].pk, self.points_of(self.member))

    def test_user_rule_beats_group_rule(self):
        point = self.points[("g", 0)]
        self.rule(point, DENY, group=self.g)
        self.rule(point, ALLOW, user=self.member)
        self.assertIn(point.pk, self.points_of(self.member))
        self.assertNotIn(point.pk, self.points_of(self.member2))
        other = self.points[("o", 0)]
        self.rule(other, ALLOW, group=self.g)
        self.rule(other, DENY, user=self.both)
        self.assertNotIn(other.pk, self.points_of(self.both))
        self.assertIn(other.pk, self.points_of(self.member))

    def test_deny_beats_allow_among_groups(self):
        point = self.points[("g", 1)]
        self.rule(point, ALLOW, group=self.o)
        self.rule(point, DENY, group=self.g)
        self.assertNotIn(point.pk, self.points_of(self.both))
        self.assertIn(point.pk, self.points_of(self.omember))

    def test_spectra(self):
        self.rule(self.specs["g"], ALLOW, user=self.outsider)
        self.rule(self.specs["go"], DENY, group=self.g)
        self.assertEqual(self.specs_of(self.outsider), {self.specs["pub"].pk, self.specs["g"].pk})
        self.assertEqual(self.specs_of(self.member), {self.specs["pub"].pk, self.specs["g"].pk})
        self.assertEqual(self.specs_of(self.omember), {self.specs["pub"].pk, self.specs["o"].pk,
                                                       self.specs["go"].pk})
        outsider = self.fresh(self.outsider)
        self.assertTrue(visibility.object_visible_to_user(outsider, self.specs["g"]))
        self.assertIn(self.specs["g"].pk,
                      _pks(SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(outsider,
                                                                                         self.transient.pk)))

    def test_service_readers_and_anonymous_are_unaffected(self):
        self.rule(self.points[("g", 0)], DENY, group=self.g)
        self.rule(self.points[("g", 1)], ALLOW, user=self.staff)
        with override_settings(DATA_SERVICE_READER_USERNAMES=[self.staff.username]):
            self.assertIsNone(visible_photometry_ids_q(self.fresh(self.staff)))
        # staff follow the rules like everyone else (#706)
        self.assertEqual(self.points_of(self.staff), self.all_points("pub") | {self.points[("g", 1)].pk})
        self.assertEqual(visibility._photometry_access_rules(AnonymousUser()), visibility.NO_RULES)


@ON
class MembershipHistoryTests(_Fixture):
    def leave(self, user, group, at=None):
        user.groups.remove(group)
        if at is not None:
            GroupMembershipPeriod.objects.filter(user=user, group=group, left_at__isnull=False).update(
                joined_at=None, left_at=at)
        access_rules.bump_version()

    def test_leaver_keeps_points_shared_before_leaving(self):
        self.leave(self.member, self.g)
        kept = self.points_of(self.member)
        self.assertEqual(kept, self.all_points("pub", "g", "go"))
        # a point added to the G set after they left is not theirs
        later = TransientPhotData.objects.create(photometry=self.sets["g"], band=self.band, mag=20.0, mag_err=0.1,
                                                 obs_date=self.now, **audit_fields(self.staff))
        self.assertNotIn(later.pk, self.points_of(self.member))
        self.assertIn(later.pk, self.points_of(self.member2))
        # a set shared with G after they left is not theirs either
        self.sets["o"].groups.add(self.g)
        self.assertFalse(self.all_points("o") & self.points_of(self.member))
        self.assertTrue(self.all_points("o") <= self.points_of(self.member2))
        # spectra: kept from before, not after
        self.assertIn(self.specs["g"].pk, self.specs_of(self.member))

    def test_leaver_keeps_allows_made_before_leaving(self):
        early = self.rule(self.points[("o", 0)], ALLOW, group=self.g)
        late = self.rule(self.points[("o", 1)], ALLOW, group=self.g)
        left_at = self.now + datetime.timedelta(minutes=5)
        R.objects.filter(pk=early.pk).update(updated_at=left_at - datetime.timedelta(minutes=1))
        R.objects.filter(pk=late.pk).update(updated_at=left_at + datetime.timedelta(minutes=1))
        self.leave(self.member, self.g, left_at)
        visible = self.points_of(self.member)
        self.assertIn(early.transient_phot_data_id, visible)
        self.assertNotIn(late.transient_phot_data_id, visible)

    def test_rejoining_restores_everything(self):
        self.leave(self.member, self.g, self.now - datetime.timedelta(days=10))
        self.assertFalse(self.all_points("g") & self.points_of(self.member))
        self.member.groups.add(self.g)
        self.assertTrue(self.all_points("g") <= self.points_of(self.member))


@ON
class CacheKeyTests(_Fixture):
    def test_tokens(self):
        a, b = self.fresh(self.member), self.fresh(self.member2)
        self.assertEqual(group_access_plot_cache_token(a), group_access_plot_cache_token(b))
        version_token = group_access_plot_cache_token(a)
        self.rule(self.points[("g", 0)], DENY, group=self.g)  # a group rule: still shared, new version
        a, b = self.fresh(self.member), self.fresh(self.member2)
        self.assertEqual(group_access_plot_cache_token(a), group_access_plot_cache_token(b))
        self.assertNotEqual(group_access_plot_cache_token(a), version_token)
        self.rule(self.points[("g", 1)], ALLOW, user=self.member)  # personal: never shared with b
        a, b = self.fresh(self.member), self.fresh(self.member2)
        self.assertNotEqual(group_access_plot_cache_token(a), group_access_plot_cache_token(b))
        with OFF:
            self.assertEqual(group_access_plot_cache_token(a), group_access_plot_cache_token(b))

    def test_version_bumps_and_is_never_reused(self):
        before = access_rules.current_version()
        access_rules.bump_version()
        after = access_rules.current_version()
        self.assertGreater(after, before)
        AccessRulesVersion.objects.filter(pk=1).update(version=before)  # as if rolled back
        access_rules.bump_version()
        self.assertGreaterEqual(access_rules.current_version(), after)

    def test_summary_is_cached_per_version(self):
        u = self.fresh(self.member)
        first = access_rules.summary_for(u)
        self.assertTrue(first.empty)
        self.rule(self.points[("o", 0)], ALLOW, user=self.member)
        self.assertEqual(access_rules.summary_for(u).personal, frozenset({ALLOW}))  # same object, new version


@ON
class PhotStatTests(_Fixture):
    def scopes(self):
        return {row.scope_key: row for row in TransientPhotStatScope.objects.filter(transient=self.transient)}

    def test_group_rules_fold_into_the_stored_scopes(self):
        photstat.sync_scopes([self.transient.pk])
        g_key = "g%d" % self.g.pk
        base = self.scopes()[g_key].num_obs_global
        self.rule(self.points[("o", 0)], ALLOW, group=self.g)
        self.rule(self.points[("g", 0)], DENY, group=self.g)
        photstat.sync_scopes([self.transient.pk])
        self.assertEqual(self.scopes()[g_key].num_obs_global, base)  # +1 allowed, -1 denied
        self.rule(self.points[("o", 1)], ALLOW, group=self.g)
        photstat.sync_scopes([self.transient.pk])
        self.assertEqual(self.scopes()[g_key].num_obs_global, base + 1)

    def test_scope_key_format(self):
        photstat.sync_scopes([self.transient.pk])
        self.assertIn("g%d" % self.g.pk, self.scopes())

    def test_invalidation_deletes_the_groups_scope_and_queues_a_rebuild(self):
        from YSE_App.models.job_models import Job

        photstat.sync_scopes([self.transient.pk])
        self.assertIn("g%d" % self.g.pk, self.scopes())
        Job.objects.all().delete()
        with override_settings(JOB_RUNNER_INLINE=False):
            self.rule(self.points[("o", 0)], ALLOW, group=self.g)
        self.assertNotIn("g%d" % self.g.pk, self.scopes())
        self.assertIn("g%d" % self.o.pk, self.scopes())  # other groups' scopes stay
        job = Job.objects.get(kind="photstat.sync_scopes")
        self.assertEqual(job.payload["transient_ids"], [self.transient.pk])

    def test_personal_deny_limits_to_the_public_scope(self):
        photstat.sync_scopes([self.transient.pk])
        self.rule(self.points[("g", 0)], DENY, user=self.member)
        member = self.fresh(self.member)
        rows = TransientPhotStatScope.objects.filter(transient=self.transient).filter(
            photstat_visibility._candidate_scope_q(member))
        self.assertEqual({r.group_id for r in rows}, {None})
        other = self.fresh(self.member2)
        rows = TransientPhotStatScope.objects.filter(transient=self.transient).filter(
            photstat_visibility._candidate_scope_q(other))
        self.assertIn(self.g.pk, {r.group_id for r in rows})

    def test_group_deny_drops_the_other_groups_scopes(self):
        self.rule(self.points[("go", 0)], DENY, group=self.g)
        photstat.sync_scopes([self.transient.pk])  # the rule removed G's scope; rebuild it
        both = self.fresh(self.both)
        rows = TransientPhotStatScope.objects.filter(transient=self.transient).filter(
            photstat_visibility._candidate_scope_q(both))
        self.assertEqual({r.group_id for r in rows}, {None, self.g.pk})

    def test_table_cells_are_exact_for_a_user_with_rules(self):
        photstat.recompute(self.transient.pk)
        self.rule(self.points[("g", 0)], ALLOW, user=self.outsider)
        outsider = self.fresh(self.outsider)
        row = photstat_visibility.visible_stats(outsider, [self.transient.pk])[self.transient.pk]
        self.assertEqual(row.num_obs_global, 3)  # two public points and the allowed one
        member = self.fresh(self.member)
        self.rule(self.points[("g", 1)], DENY, user=self.member)
        member = self.fresh(self.member)
        row = photstat_visibility.visible_stat(member, self.transient.pk)
        self.assertEqual(row.num_obs_global, 5)  # pub 2 + g 1 + go 2


@ON
class ActionTests(_Fixture):
    def test_who_may_act_on_a_dataset(self):
        dataset = self.sets["g"]
        for user, role in ((self.pi_user, R.AS_PI), (self.deputy, R.AS_PROGRAM_DEPUTY), (self.staff, R.AS_STAFF)):
            self.assertEqual(access_actions.dataset_role(user, dataset), role)
        for user in (self.member, self.outsider):
            self.assertIsNone(access_actions.dataset_role(user, dataset))
            with self.assertRaises(PermissionDenied):
                access_actions.dataset_action(user, dataset, effect=ALLOW, grantee_user=self.outsider)
        with self.assertRaises(ValidationError):
            access_actions.dataset_action(self.pi_user, self.sets["pub"], effect=DENY, grantee_group=self.g)
        with self.assertRaises(ValidationError):
            access_actions.dataset_action(self.pi_user, dataset, effect=ALLOW, grantee_group=self.public)

    def test_a_pi_may_restrict_shared_data(self):
        before = access_rules.current_version()
        result = access_actions.dataset_action(self.pi_user, self.sets["g"], effect=ALLOW, grantee_user=self.outsider)
        self.assertEqual(result.created, 2)
        self.assertGreater(access_rules.current_version(), before)
        self.assertTrue(self.all_points("g") <= self.points_of(self.outsider))
        # restricting data already shared with them (Ryan, 2026-10-17): the allow becomes a deny
        result = access_actions.dataset_action(self.pi_user, self.sets["g"], effect=DENY, grantee_user=self.outsider)
        self.assertEqual((result.created, result.overridden), (0, 2))
        self.assertFalse(self.all_points("g") & self.points_of(self.outsider))
        # and from the group on the dataset itself
        result = access_actions.dataset_action(self.pi_user, self.sets["g"], effect=DENY, grantee_group=self.g)
        self.assertEqual(result.created, 2)
        self.assertFalse(self.all_points("g") & self.points_of(self.member))

    def test_deny_then_allow_upgrades(self):
        access_actions.dataset_action(self.pi_user, self.specs["o"], effect=DENY, grantee_user=self.outsider)
        rule = R.objects.get(transient_spectrum=self.specs["o"], grantee_user=self.outsider)
        self.assertEqual(rule.effect, DENY)
        result = access_actions.dataset_action(self.pi_user, self.specs["o"], effect=ALLOW, grantee_user=self.outsider)
        self.assertEqual(result.upgraded, 1)
        rule.refresh_from_db()
        self.assertEqual(rule.effect, ALLOW)
        self.assertIn(self.specs["o"].pk, self.specs_of(self.outsider))

    def test_program_action_now_and_at_ingest(self):
        with self.assertRaises(PermissionDenied):
            access_actions.program_action(self.member, self.allocation, effect=ALLOW, grantee_user=self.outsider)
        grant, result = access_actions.program_action(self.deputy, self.allocation, effect=ALLOW,
                                                      grantee_user=self.outsider)
        self.assertEqual(grant.created_as, StandingAccessGrant.AS_PROGRAM_DEPUTY)
        self.assertEqual(result.created, 6 + 3)  # g, o, go points; their spectra
        self.assertEqual(result.public, 2 + 1)
        new = TransientPhotData.objects.create(photometry=self.sets["o"], band=self.band, mag=20.0, mag_err=0.1,
                                               obs_date=self.now, **audit_fields(self.staff))
        self.assertTrue(R.objects.filter(transient_phot_data=new, grantee_user=self.outsider,
                                         standing_grant=grant).exists())
        spec = TransientSpectrum.objects.create(
            transient=self.transient, instrument=self.instrument, obs_group=self.obs_group, ra=1.0, dec=1.0,
            obs_date=self.now, allocation=self.allocation, **audit_fields(self.staff))
        spec.groups.add(self.o)
        self.assertTrue(R.objects.filter(transient_spectrum=spec, grantee_user=self.outsider).exists())
        access_actions.stop_standing_grant(self.pi_user, grant)
        grant.refresh_from_db()
        later = TransientPhotData.objects.create(
            photometry=self.sets["o"], band=self.band, mag=20.0, mag_err=0.1,
            obs_date=grant.stopped_at + datetime.timedelta(hours=1), **audit_fields(self.staff))
        self.assertFalse(R.objects.filter(transient_phot_data=later).exists())  # taken after the stop
        late_upload = TransientPhotData.objects.create(
            photometry=self.sets["o"], band=self.band, mag=20.0, mag_err=0.1,
            obs_date=grant.stopped_at - datetime.timedelta(hours=1), **audit_fields(self.staff))
        self.assertTrue(R.objects.filter(transient_phot_data=late_upload, standing_grant=grant).exists())
        self.assertTrue(R.objects.filter(transient_phot_data=new).exists())  # already shared: stays

    def test_night_deny_at_ingest_beats_the_datasets_group(self):
        start = self.now - datetime.timedelta(hours=6)
        end = self.now + datetime.timedelta(hours=6)
        grant, result = access_actions.program_action(self.pi_user, self.allocation, effect=DENY,
                                                      grantee_group=self.g, night_start=start, night_end=end)
        self.assertEqual(result.created, 0)  # no existing point or spectrum was taken that night
        tonight = TransientPhotData.objects.create(photometry=self.sets["g"], band=self.band, mag=20.0, mag_err=0.1,
                                                   obs_date=self.now, **audit_fields(self.staff))
        other_night = TransientPhotData.objects.create(
            photometry=self.sets["g"], band=self.band, mag=20.0, mag_err=0.1,
            obs_date=self.now - datetime.timedelta(days=2), **audit_fields(self.staff))
        self.assertTrue(R.objects.filter(transient_phot_data=tonight, grantee_group=self.g, effect=DENY).exists())
        self.assertFalse(R.objects.filter(transient_phot_data=other_night).exists())
        visible = self.points_of(self.member)
        self.assertNotIn(tonight.pk, visible)
        self.assertIn(other_night.pk, visible)

    def test_bulk_writes_get_standing_grants_from_the_set_pass(self):
        access_actions.program_action(self.pi_user, self.allocation, effect=ALLOW, grantee_user=self.outsider)
        # bulk_create fires no post_save; the upload path then runs the set pass
        TransientPhotData.objects.bulk_create([TransientPhotData(
            photometry=self.sets["go"], band=self.band, mag=21.0, mag_err=0.1, obs_date=self.now,
            **audit_fields(self.staff))])
        access_actions.apply_standing_grants_to_set(self.sets["go"])
        self.assertEqual(R.objects.filter(photometry=self.sets["go"], grantee_user=self.outsider).count(), 3)

    def test_object_night_action(self):
        start = self.now - datetime.timedelta(days=1, hours=6)
        end = self.now - datetime.timedelta(hours=18)
        result = access_actions.object_night_action(self.pi_user, self.transient, start, end, effect=ALLOW,
                                                    grantee_user=self.outsider)
        self.assertEqual(result.created, 3 + 3)  # the day-1 point of g, o, go and their spectra
        with self.assertRaises(PermissionDenied):
            access_actions.object_night_action(self.outsider, self.transient, start, end, effect=ALLOW,
                                               grantee_user=self.member)

    def test_only_staff_delete_rules_and_scopes_follow(self):
        rule = self.rule(self.points[("o", 0)], ALLOW, group=self.g)
        with self.assertRaises(PermissionDenied):
            access_actions.delete_rule(self.pi_user, rule)
        before = access_rules.current_version()
        access_actions.delete_rule(self.staff, rule)
        self.assertFalse(R.objects.filter(pk=rule.pk).exists())
        self.assertGreater(access_rules.current_version(), before)

    def test_bounded_queries_for_a_user_with_rules(self):
        self.rule(self.points[("g", 0)], ALLOW, user=self.outsider)
        u = self.fresh(self.outsider)
        access_rules.summary_for(u)
        with CaptureQueriesContext(connection) as ctx:
            list(PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(u, self.transient.pk))
            list(Transient.objects.filter(transient_access_q(u)))
            photstat_visibility.visible_stats(u, [self.transient.pk])
        self.assertLessEqual(len(ctx.captured_queries), 10, [q["sql"][:100] for q in ctx.captured_queries])

    def test_rule_writes_skip_public_data(self):
        result = access_actions.write_rules(points=list(self.all_points("pub")), effect=DENY, grantee_group=self.g,
                                            granted_by=self.staff, granted_as=R.AS_STAFF)
        self.assertEqual((result.created, result.public), (0, 2))
        self.assertFalse(R.objects.filter(photometry=self.sets["pub"]).exists())
