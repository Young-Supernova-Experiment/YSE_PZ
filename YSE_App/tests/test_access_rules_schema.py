"""Per-point access rules schema (#648, PR 1b): constraints, history recording, migration, admin, no read change.

Rules and standing grants are stored but not read yet: with rows present and
``DATA_ACCESS_GRANTS_ENABLED`` off, every dataset read returns what the group
rule returns. Group membership and dataset group shares are recorded from now
on. Synthetic data only.
"""

import datetime
from importlib import import_module
from types import SimpleNamespace

from auditlog.models import LogEntry
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.data import PhotometryService, SpectraService
from YSE_App.models import (
    AccessRulesVersion,
    Allocation,
    AllocationDelegate,
    DataAccessRule,
    DatasetGroupShare,
    GroupMembershipPeriod,
    StandingAccessGrant,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.serializers.phot_serializers import TransientPhotometrySerializer
from YSE_App.serializers.spectra_serializers import TransientSpectrumSerializer
from YSE_App.services import visibility
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

GROUP = "dar648-private"
OTHER = "dar648-other"
R = DataAccessRule
S = StandingAccessGrant


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("dar648_staff", is_superuser=True)
        cls.member = create_test_user("dar648_member", is_staff=False)
        cls.outsider = create_test_user("dar648_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.other, _ = Group.objects.get_or_create(name=OTHER)
        cls.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="dar648")
        now = timezone.now()
        cls.allocation = Allocation.objects.create(
            name="dar648 alloc", telescope=cls.instrument.telescope, instrument=cls.instrument,
            start_date=now - datetime.timedelta(days=30), end_date=now + datetime.timedelta(days=150),
            **audit_fields(cls.staff))
        cls.night_start = now - datetime.timedelta(days=2)
        cls.night_end = cls.night_start + datetime.timedelta(days=1)
        cls.transient = create_minimal_transient(cls.staff, name="YSEdar648", ra=40.0, dec=-5.0)
        cls.phot = {}
        cls.spec = {}
        cls.point = {}
        for key, groups in (("public", ()), ("private", (cls.group,)), ("other", (cls.other,))):
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
            for days in (1.5, 5):
                cls.point[(key, days)] = TransientPhotData.objects.create(
                    photometry=phot, band=cls.band, mag=18.0, mag_err=0.05,
                    obs_date=now - datetime.timedelta(days=days), **audit_fields(cls.staff))
            cls.phot[key] = phot
            cls.spec[key] = spec

    def rule(self, *, save=True, **kw):
        defaults = dict(effect=R.EFFECT_ALLOW, granted_by=self.staff, granted_as=R.AS_STAFF)
        defaults.update(kw)
        row = R(**defaults)
        if save:
            row.full_clean()
            row.save()
        return row

    def standing(self, *, save=True, **kw):
        defaults = dict(effect=S.EFFECT_ALLOW, allocation=self.allocation, created_by=self.staff,
                        created_as=S.AS_STAFF)
        defaults.update(kw)
        row = S(**defaults)
        if save:
            row.full_clean()
            row.save()
        return row


class RuleTests(_Fixture):
    def assertInvalid(self, row, fragment):
        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()
        self.assertIn(fragment, " ".join(ctx.exception.messages))

    def test_point_and_spectrum_rules_copy_their_set_and_transient(self):
        point = self.point[("private", 5)]
        row = self.rule(transient_phot_data=point, grantee_user=self.outsider)
        self.assertEqual((row.photometry_id, row.transient_id), (self.phot["private"].pk, self.transient.pk))
        row = self.rule(transient_spectrum=self.spec["other"], grantee_group=self.group, effect=R.EFFECT_DENY)
        self.assertEqual((row.photometry_id, row.transient_id), (None, self.transient.pk))
        # save() alone (bulk writers, PR 2) fills the copies too
        row = R(transient_phot_data=self.point[("other", 5)], grantee_group=self.group, effect=R.EFFECT_ALLOW,
                granted_as=R.AS_STAFF)
        row.save()
        self.assertEqual(row.photometry_id, self.phot["other"].pk)

    def test_exactly_one_grantee_and_target(self):
        point = self.point[("private", 5)]
        self.assertInvalid(self.rule(save=False, transient_phot_data=point), "exactly one grantee")
        self.assertInvalid(self.rule(save=False, transient_phot_data=point, grantee_user=self.outsider,
                                     grantee_group=self.other), "exactly one grantee")
        self.assertInvalid(self.rule(save=False, grantee_user=self.outsider), "exactly one target")
        self.assertInvalid(self.rule(save=False, transient_phot_data=point, transient_spectrum=self.spec["other"],
                                     grantee_user=self.outsider), "exactly one target")
        self.assertInvalid(self.rule(save=False, transient_phot_data=point, grantee_group=self.public),
                           "Public group cannot be a grantee")

    def test_database_checks(self):
        point = self.point[("private", 5)]
        # The database refuses two grantees or two targets ("exactly one" is checked by clean()).
        bad = (
            dict(transient_phot_data=point, grantee_user=self.outsider, grantee_group=self.other),
            dict(transient_phot_data=point, transient_spectrum=self.spec["other"], grantee_user=self.outsider),
            dict(transient_phot_data=point, grantee_user=self.outsider, effect="maybe"),
        )
        for kw in bad:
            with self.subTest(kw=sorted(kw)):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        self.rule(save=False, **kw).save()

    def test_one_rule_per_target_and_grantee(self):
        point = self.point[("private", 5)]
        spec = self.spec["other"]
        for kw in (dict(transient_phot_data=point, grantee_user=self.outsider),
                   dict(transient_phot_data=point, grantee_group=self.other),
                   dict(transient_spectrum=spec, grantee_user=self.outsider),
                   dict(transient_spectrum=spec, grantee_group=self.other)):
            with self.subTest(kw=sorted(kw)):
                self.rule(**kw)
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        self.rule(save=False, effect=R.EFFECT_DENY, **kw).save()
        # different grantees and targets do not collide
        self.rule(transient_phot_data=point, grantee_user=self.member)
        self.rule(transient_phot_data=self.point[("private", 1.5)], grantee_user=self.outsider)
        self.assertEqual(R.objects.count(), 6)

    def test_create_and_delete_are_audited(self):
        row = self.rule(transient_phot_data=self.point[("private", 5)], grantee_user=self.outsider)
        pk = row.pk
        self.assertTrue(LogEntry.objects.get_for_object(row).filter(action=LogEntry.Action.CREATE).exists())
        row.delete()
        self.assertTrue(LogEntry.objects.filter(object_pk=str(pk), action=LogEntry.Action.DELETE,
                                                content_type__model="dataaccessrule").exists())
        delegate = AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider,
                                                     added_by=self.staff)
        self.assertTrue(LogEntry.objects.get_for_object(delegate).exists())

    def test_rules_follow_their_point_grantee_and_dataset(self):
        point = self.point[("other", 5)]
        self.rule(transient_phot_data=point, grantee_user=self.outsider)
        point.delete()
        self.assertFalse(R.objects.exists())
        self.rule(transient_phot_data=self.point[("other", 1.5)], grantee_group=self.other)
        self.rule(transient_spectrum=self.spec["private"], grantee_user=self.outsider)
        self.rule(transient_phot_data=self.point[("private", 1.5)], grantee_user=self.member)
        self.other.delete()
        self.outsider.delete()
        self.assertEqual(list(R.objects.values_list("grantee_user_id", flat=True)), [self.member.pk])
        self.phot["private"].delete()
        self.assertFalse(R.objects.exists())
        grant = self.standing(grantee_user=self.member)
        self.member.delete()
        self.assertFalse(S.objects.filter(pk=grant.pk).exists())


class StandingGrantTests(_Fixture):
    def test_program_and_night(self):
        program = self.standing(grantee_group=self.other)
        self.assertTrue(program.covers_obs_date(self.night_start - datetime.timedelta(days=10)))
        night = self.standing(grantee_user=self.outsider, effect=S.EFFECT_DENY,
                              night_start=self.night_start, night_end=self.night_end)
        self.assertTrue(night.covers_obs_date(self.night_start))
        self.assertFalse(night.covers_obs_date(self.night_end))
        self.assertFalse(night.covers_obs_date(None))
        self.assertTrue(night.is_active)

    def test_validation(self):
        for kw, fragment in (
            (dict(), "exactly one grantee"),
            (dict(grantee_group=self.public), "Public group"),
            (dict(grantee_group=self.other, night_start=self.night_start), "both the start and the end"),
            (dict(grantee_group=self.other, night_start=self.night_end, night_end=self.night_start), "must end"),
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(ValidationError) as ctx:
                    self.standing(save=False, **kw).full_clean()
                self.assertIn(fragment, " ".join(ctx.exception.messages))

    def test_database_checks(self):
        for kw in (dict(grantee_group=self.other, grantee_user=self.outsider),
                   dict(grantee_group=self.other, night_start=self.night_start),
                   dict(grantee_group=self.other, night_start=self.night_end, night_end=self.night_start)):
            with self.subTest(kw=sorted(kw)):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        self.standing(save=False, **kw).save()

    def test_audited(self):
        row = self.standing(grantee_group=self.other)
        self.assertTrue(LogEntry.objects.get_for_object(row).exists())

    def test_program_deputy_is_unique(self):
        AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider, added_by=self.staff)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider)


class MembershipHistoryTests(_Fixture):
    def periods(self, user, group):
        return list(GroupMembershipPeriod.objects.filter(user=user, group=group).order_by("id")
                    .values_list("joined_at", "left_at"))

    def test_join_and_leave_from_the_user(self):
        user = create_test_user("dar648_hist", is_staff=False)
        before = timezone.now()
        user.groups.add(self.other)
        [(joined, left)] = self.periods(user, self.other)
        self.assertIsNotNone(joined)
        self.assertGreaterEqual(joined, before)
        self.assertIsNone(left)
        user.groups.add(self.other)  # already a member: no new period
        self.assertEqual(len(self.periods(user, self.other)), 1)
        user.groups.remove(self.other)
        [(_joined, left)] = self.periods(user, self.other)
        self.assertIsNotNone(left)
        user.groups.add(self.other)
        self.assertEqual([p[1] is None for p in self.periods(user, self.other)], [False, True])

    def test_join_and_leave_from_the_group_and_clear(self):
        user = create_test_user("dar648_hist2", is_staff=False)
        self.other.user_set.add(user)
        self.group.user_set.add(user)
        self.assertIsNone(self.periods(user, self.other)[0][1])
        self.other.user_set.remove(user)
        self.assertIsNotNone(self.periods(user, self.other)[0][1])
        user.groups.clear()
        self.assertIsNotNone(self.periods(user, self.group)[-1][1])
        self.assertFalse(GroupMembershipPeriod.objects.filter(user=user, left_at__isnull=True).exists())

    def test_leaving_without_a_recorded_join(self):
        GroupMembershipPeriod.objects.filter(user=self.member, group=self.group).delete()
        self.member.groups.remove(self.group)
        [(joined, left)] = self.periods(self.member, self.group)
        self.assertIsNone(joined)
        self.assertIsNotNone(left)

    def test_seed_from_current_memberships(self):
        GroupMembershipPeriod.objects.all().delete()
        AccessRulesVersion.objects.all().delete()
        migration = import_module("YSE_App.migrations.0045_per_point_access_rules")
        migration.seed(apps, SimpleNamespace(connection=connection))
        expected = set(User.groups.through.objects.values_list("user_id", "group_id"))
        rows = GroupMembershipPeriod.objects.values_list("user_id", "group_id", "joined_at", "left_at")
        self.assertEqual({(u, g) for u, g, _j, _l in rows}, expected)
        self.assertTrue(all(j is None and left is None for _u, _g, j, left in rows))
        self.assertEqual(AccessRulesVersion.objects.get(pk=1).version, 0)
        migration.seed(apps, SimpleNamespace(connection=connection))  # idempotent
        self.assertEqual(GroupMembershipPeriod.objects.count(), len(expected))


class DatasetShareTests(_Fixture):
    def test_fixture_adds_are_recorded(self):
        self.assertTrue(DatasetGroupShare.objects.filter(transient_photometry=self.phot["private"],
                                                         group=self.group).exists())
        self.assertTrue(DatasetGroupShare.objects.filter(transient_spectrum=self.spec["other"],
                                                         group=self.other).exists())
        self.assertFalse(DatasetGroupShare.objects.filter(transient_photometry=self.phot["public"]).exists())

    def test_add_remove_and_reverse(self):
        phot = self.phot["public"]
        before = timezone.now()
        phot.groups.add(self.other)
        row = DatasetGroupShare.objects.get(transient_photometry=phot, group=self.other)
        self.assertGreaterEqual(row.shared_at, before)
        phot.groups.remove(self.other)
        self.assertFalse(DatasetGroupShare.objects.filter(transient_photometry=phot).exists())
        self.other.transientspectrum_set.add(self.spec["public"])
        self.assertTrue(DatasetGroupShare.objects.filter(transient_spectrum=self.spec["public"],
                                                         group=self.other).exists())
        self.other.transientspectrum_set.clear()
        self.assertFalse(DatasetGroupShare.objects.filter(transient_spectrum__isnull=False,
                                                          group=self.other).exists())
        self.phot["private"].groups.set([self.other])
        self.assertEqual(
            set(DatasetGroupShare.objects.filter(transient_photometry=self.phot["private"])
                .values_list("group_id", flat=True)), {self.other.pk})

    def test_share_row_follows_its_dataset(self):
        self.phot["private"].delete()
        self.assertFalse(DatasetGroupShare.objects.filter(group=self.group, transient_spectrum__isnull=True).exists())


class MigrationTests(TestCase):
    def test_migrations_match_the_models(self):
        """makemigrations would create nothing for the access tables or the dataset program links."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(
            graph=loader.graph)
        names = ("dataaccessgrant", "dataaccessrule", "standingaccessgrant", "groupmembershipperiod",
                 "datasetgroupshare", "accessrulesversion", "allocationdelegate", "transientphotometry",
                 "transientspectrum")
        mine = [
            op for migration in changes.get("YSE_App", []) for op in migration.operations
            if (getattr(op, "model_name", None) or getattr(op, "name", "")).lower() in names
        ]
        self.assertEqual(mine, [], [op.describe() for op in mine])

    def test_version_row_exists(self):
        self.assertEqual(AccessRulesVersion.objects.get(pk=1).version, 0)

    def test_dataset_program_columns_are_nullable(self):
        for model in (TransientPhotometry, TransientSpectrum):
            with connection.cursor() as cursor:
                desc = connection.introspection.get_table_description(cursor, model._meta.db_table)
            by_name = {col.name: col for col in desc}
            self.assertTrue(by_name["allocation_id"].null_ok, model.__name__)

    def test_flag_defaults_off_and_tables_hidden_from_explorer(self):
        self.assertFalse(settings.DATA_ACCESS_GRANTS_ENABLED)
        for table in ("YSE_App_dataaccessrule", "YSE_App_standingaccessgrant", "YSE_App_allocationdelegate",
                      "YSE_App_groupmembershipperiod", "YSE_App_datasetgroupshare"):
            self.assertIn(table, settings.EXPLORER_SQL_BLACKLIST)
            self.assertIn(table, settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)

    def test_api_does_not_expose_the_program_link_yet(self):
        self.assertNotIn("allocation", TransientPhotometrySerializer().fields)
        self.assertNotIn("allocation", TransientSpectrumSerializer().fields)
        self.assertIn("groups", TransientPhotometrySerializer().fields)


@override_settings(DATA_ACCESS_GRANTS_ENABLED=False)
class ReadPathsUnchangedTests(_Fixture):
    """Rules that would change access if read change nothing while the flag is off."""

    def _snapshot(self):
        out = {}
        for label, user in (("member", self.member), ("outsider", self.outsider), ("anon", AnonymousUser())):
            out[label] = (
                set(TransientPhotometry.objects.filter(visibility.photometry_visible_q(user))
                    .values_list("pk", flat=True)),
                set(TransientSpectrum.objects.filter(visibility.spectrum_visible_q(user))
                    .values_list("pk", flat=True)),
                set(PhotometryService.GetAuthorizedTransientPhotometry_ByUser(user).values_list("pk", flat=True)),
                set(SpectraService.GetAuthorizedTransientSpectrum_ByUser(user).values_list("pk", flat=True)),
                str(TransientPhotometry.objects.filter(visibility.photometry_visible_q(user)).query),
            )
        return out

    def test_reads_ignore_rules(self):
        before = self._snapshot()
        self.rule(transient_phot_data=self.point[("other", 5)], grantee_user=self.outsider)
        self.rule(transient_phot_data=self.point[("private", 5)], grantee_group=self.group, effect=R.EFFECT_DENY)
        self.rule(transient_spectrum=self.spec["other"], grantee_user=self.outsider)
        self.standing(grantee_user=self.outsider)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(before["member"][0], {self.phot["public"].pk, self.phot["private"].pk})
        self.assertEqual(before["outsider"][0], {self.phot["public"].pk})
        for user in (self.member, self.outsider):
            self.assertEqual(visibility._photometry_access_rules(user), visibility.NO_RULES)
            self.assertEqual(visibility._spectrum_access_rules(user), visibility.NO_RULES)


class AdminTests(_Fixture):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

    def test_pages_load(self):
        rule = self.rule(transient_phot_data=self.point[("other", 5)], grantee_group=self.group)
        grant = self.standing(grantee_group=self.other)
        AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider, added_by=self.staff)
        period = GroupMembershipPeriod.objects.filter(user=self.member).first()
        share = DatasetGroupShare.objects.first()
        for url in (reverse("admin:YSE_App_dataaccessrule_changelist"),
                    reverse("admin:YSE_App_dataaccessrule_add"),
                    reverse("admin:YSE_App_dataaccessrule_change", args=[rule.pk]),
                    reverse("admin:YSE_App_standingaccessgrant_changelist"),
                    reverse("admin:YSE_App_standingaccessgrant_add"),
                    reverse("admin:YSE_App_standingaccessgrant_change", args=[grant.pk]),
                    reverse("admin:YSE_App_groupmembershipperiod_changelist"),
                    reverse("admin:YSE_App_groupmembershipperiod_change", args=[period.pk]),
                    reverse("admin:YSE_App_datasetgroupshare_changelist"),
                    reverse("admin:YSE_App_datasetgroupshare_change", args=[share.pk]),
                    reverse("admin:YSE_App_allocationdelegate_changelist"),
                    reverse("admin:YSE_App_allocation_change", args=[self.allocation.pk])):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_add_rule_records_staff_and_staff_may_delete(self):
        point = self.point[("other", 5)]
        resp = self.client.post(reverse("admin:YSE_App_dataaccessrule_add"), {
            "effect": "allow", "transient_phot_data": point.pk, "grantee_group": self.group.pk,
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, "context_data", {}).get("errors"))
        row = R.objects.get(transient_phot_data=point)
        self.assertEqual((row.granted_as, row.granted_by, row.transient_id), (R.AS_STAFF, self.staff,
                                                                               self.transient.pk))
        resp = self.client.post(reverse("admin:YSE_App_dataaccessrule_delete", args=[row.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(R.objects.filter(pk=row.pk).exists())

    def test_standing_grant_stop_and_no_delete(self):
        grant = self.standing(grantee_group=self.other)
        self.assertEqual(
            self.client.get(reverse("admin:YSE_App_standingaccessgrant_delete", args=[grant.pk])).status_code, 403)
        self.client.post(reverse("admin:YSE_App_standingaccessgrant_changelist"),
                         {"action": "stop", "_selected_action": [grant.pk]})
        grant.refresh_from_db()
        self.assertIsNotNone(grant.stopped_at)
        self.assertEqual(grant.stopped_by, self.staff)

    def test_history_is_read_only(self):
        period = GroupMembershipPeriod.objects.filter(user=self.member).first()
        self.assertEqual(self.client.get(reverse("admin:YSE_App_groupmembershipperiod_add")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("admin:YSE_App_groupmembershipperiod_delete", args=[period.pk])).status_code,
            403)
