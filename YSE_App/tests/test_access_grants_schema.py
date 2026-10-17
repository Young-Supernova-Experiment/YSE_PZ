"""Access-grant schema (#648, PR 1): constraints, soft revoke, migration, admin, and no read change.

The rules are stored but not read yet: with grant rows present and
``DATA_ACCESS_GRANTS_ENABLED`` off, every dataset read returns what the group
rule returns. Synthetic data only.
"""

import datetime
from unittest import skipUnless

from auditlog.models import LogEntry
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group
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
    Allocation,
    AllocationDelegate,
    DataAccessGrant,
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

GROUP = "dag648-private"
OTHER = "dag648-other"
G = DataAccessGrant


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("dag648_staff", is_superuser=True)
        cls.member = create_test_user("dag648_member", is_staff=False)
        cls.outsider = create_test_user("dag648_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.other, _ = Group.objects.get_or_create(name=OTHER)
        cls.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="dag648")
        now = timezone.now()
        cls.allocation = Allocation.objects.create(
            name="dag648 alloc", telescope=cls.instrument.telescope, instrument=cls.instrument,
            start_date=now - datetime.timedelta(days=30), end_date=now + datetime.timedelta(days=150),
            **audit_fields(cls.staff))
        cls.night_start = now - datetime.timedelta(days=2)
        cls.night_end = cls.night_start + datetime.timedelta(days=1)
        cls.transient = create_minimal_transient(cls.staff, name="YSEdag648", ra=40.0, dec=-5.0)
        cls.phot = {}
        cls.spec = {}
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
                TransientPhotData.objects.create(
                    photometry=phot, band=cls.band, mag=18.0, mag_err=0.05,
                    obs_date=now - datetime.timedelta(days=days), **audit_fields(cls.staff))
            cls.phot[key] = phot
            cls.spec[key] = spec

    def grant(self, *, save=True, **kw):
        defaults = dict(effect=G.EFFECT_ALLOW, granted_by=self.staff, granted_as=G.AS_STAFF)
        defaults.update(kw)
        if "level" not in defaults:
            defaults["level"] = G.derive_level(
                allocation_id=getattr(defaults.get("allocation"), "pk", None),
                night_start=defaults.get("night_start"),
                transient_photometry_id=getattr(defaults.get("transient_photometry"), "pk", None),
                transient_spectrum_id=getattr(defaults.get("transient_spectrum"), "pk", None))
        row = G(**defaults)
        if save:
            row.full_clean()
            row.save()
        return row


class ValidationTests(_Fixture):
    def assertInvalid(self, row, fragment):
        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()
        self.assertIn(fragment, " ".join(ctx.exception.messages))

    def test_each_level_is_valid(self):
        night = dict(night_start=self.night_start, night_end=self.night_end)
        rows = [
            self.grant(allocation=self.allocation, grantee_group=self.other),
            self.grant(allocation=self.allocation, grantee_user=self.outsider, **night),
            self.grant(transient_photometry=self.phot["private"], grantee_user=self.outsider),
            self.grant(transient_spectrum=self.spec["private"], grantee_group=self.other, effect=G.EFFECT_DENY),
            self.grant(transient_photometry=self.phot["private"], grantee_group=self.other, **night),
        ]
        self.assertEqual([r.level for r in rows],
                         [G.LEVEL_PROGRAM, G.LEVEL_NIGHT, G.LEVEL_DATASET, G.LEVEL_DATASET, G.LEVEL_POINTS])

    def test_exactly_one_grantee(self):
        self.assertInvalid(self.grant(save=False, allocation=self.allocation), "exactly one grantee")
        self.assertInvalid(
            self.grant(save=False, allocation=self.allocation, grantee_group=self.other, grantee_user=self.outsider),
            "exactly one grantee")

    def test_public_group_is_not_a_grantee(self):
        self.assertInvalid(self.grant(save=False, allocation=self.allocation, grantee_group=self.public), "Public")

    def test_exactly_one_target(self):
        self.assertInvalid(self.grant(save=False, grantee_user=self.outsider), "Give a target")
        self.assertInvalid(
            self.grant(save=False, grantee_user=self.outsider, transient_photometry=self.phot["private"],
                       transient_spectrum=self.spec["private"]), "one dataset")
        self.assertInvalid(
            self.grant(save=False, grantee_user=self.outsider, transient_photometry=self.phot["private"],
                       allocation=self.allocation, level=G.LEVEL_DATASET), "not a program")
        self.assertInvalid(
            self.grant(save=False, grantee_user=self.outsider, transient_spectrum=self.spec["private"],
                       night_start=self.night_start, night_end=self.night_end), "one obs date")

    def test_night_window(self):
        self.assertInvalid(
            self.grant(save=False, grantee_user=self.outsider, allocation=self.allocation,
                       night_start=self.night_start), "both the start and the end")
        self.assertInvalid(
            self.grant(save=False, grantee_user=self.outsider, allocation=self.allocation,
                       night_start=self.night_end, night_end=self.night_start), "end after it starts")

    def test_level_must_match_target(self):
        self.assertInvalid(
            self.grant(save=False, grantee_user=self.outsider, allocation=self.allocation, level=G.LEVEL_NIGHT),
            "level does not match")

    def test_one_active_rule_per_target_and_grantee(self):
        first = self.grant(allocation=self.allocation, grantee_group=self.other)
        self.assertInvalid(self.grant(save=False, allocation=self.allocation, grantee_group=self.other,
                                      effect=G.EFFECT_DENY), "already exists")
        first.revoke(self.staff)
        self.grant(allocation=self.allocation, grantee_group=self.other, effect=G.EFFECT_DENY)
        self.assertEqual(G.objects.filter(allocation=self.allocation, grantee_group=self.other).count(), 2)


@skipUnless(connection.features.supports_table_check_constraints, "database does not enforce CHECK constraints")
class DatabaseConstraintTests(_Fixture):
    """The CHECK constraints refuse bad rows even when ``clean`` is bypassed."""

    def assertRefused(self, **kw):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.grant(save=False, **kw).save()

    def test_grantee(self):
        self.assertRefused(allocation=self.allocation)
        self.assertRefused(allocation=self.allocation, grantee_group=self.other, grantee_user=self.outsider)

    def test_target(self):
        self.assertRefused(grantee_user=self.outsider, level=G.LEVEL_PROGRAM)
        self.assertRefused(grantee_user=self.outsider, transient_photometry=self.phot["private"],
                           transient_spectrum=self.spec["private"], level=G.LEVEL_DATASET)
        self.assertRefused(grantee_user=self.outsider, allocation=self.allocation, level=G.LEVEL_DATASET)
        self.assertRefused(grantee_user=self.outsider, allocation=self.allocation, level=G.LEVEL_NIGHT)
        self.assertRefused(grantee_user=self.outsider, transient_spectrum=self.spec["private"],
                           night_start=self.night_start, night_end=self.night_end, level=G.LEVEL_POINTS)

    def test_window_order_and_effect(self):
        self.assertRefused(grantee_user=self.outsider, allocation=self.allocation,
                           night_start=self.night_end, night_end=self.night_start)
        self.assertRefused(grantee_user=self.outsider, allocation=self.allocation, effect="maybe")


class SoftRevokeTests(_Fixture):
    def test_program_allow_is_revoked_going_forward(self):
        row = self.grant(allocation=self.allocation, grantee_group=self.other)
        at = timezone.now()
        row.revoke(self.outsider, at=at)
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertEqual(row.revoked_by, self.outsider)
        self.assertFalse(row.revoke_retroactive)
        self.assertTrue(row.applies_to_obs_date(at - datetime.timedelta(days=1)))
        self.assertFalse(row.applies_to_obs_date(at + datetime.timedelta(days=1)))
        self.assertTrue(G.objects.filter(pk=row.pk).exists())

    def test_deny_and_dataset_revocations_are_retroactive(self):
        deny = self.grant(allocation=self.allocation, grantee_group=self.other, effect=G.EFFECT_DENY)
        dataset = self.grant(transient_photometry=self.phot["private"], grantee_user=self.outsider)
        for row in (deny, dataset):
            row.revoke(self.staff)
            row.refresh_from_db()
            self.assertTrue(row.revoke_retroactive)
            self.assertFalse(row.applies_to_obs_date(timezone.now() - datetime.timedelta(days=100)))

    def test_override_and_idempotent(self):
        row = self.grant(allocation=self.allocation, grantee_user=self.outsider)
        row.revoke(self.staff, retroactive=True)
        first = row.revoked_at
        row.revoke(self.outsider, retroactive=False)
        row.refresh_from_db()
        self.assertEqual((row.revoked_at, row.revoked_by, row.revoke_retroactive), (first, self.staff, True))

    def test_night_window_bounds(self):
        row = self.grant(allocation=self.allocation, grantee_user=self.outsider,
                         night_start=self.night_start, night_end=self.night_end)
        self.assertTrue(row.applies_to_obs_date(self.night_start))
        self.assertFalse(row.applies_to_obs_date(self.night_end))
        self.assertFalse(row.applies_to_obs_date(self.night_start - datetime.timedelta(seconds=1)))

    def test_create_and_revoke_are_audited(self):
        row = self.grant(allocation=self.allocation, grantee_group=self.other)
        row.revoke(self.staff)
        actions = list(LogEntry.objects.get_for_object(row).values_list("action", flat=True))
        self.assertIn(LogEntry.Action.CREATE, actions)
        self.assertIn(LogEntry.Action.UPDATE, actions)
        delegate = AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider,
                                                     added_by=self.staff)
        self.assertTrue(LogEntry.objects.get_for_object(delegate).exists())

    def test_program_deputy_is_unique(self):
        AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider, added_by=self.staff)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider)


class MigrationTests(TestCase):
    def test_migrations_match_the_models(self):
        """makemigrations would create nothing for the new tables or the dataset program links."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(
            graph=loader.graph)
        names = ("dataaccessgrant", "allocationdelegate", "transientphotometry", "transientspectrum")
        mine = [
            op for migration in changes.get("YSE_App", []) for op in migration.operations
            if (getattr(op, "model_name", None) or getattr(op, "name", "")).lower() in names
        ]
        self.assertEqual(mine, [], [op.describe() for op in mine])

    def test_dataset_program_columns_are_nullable(self):
        for model in (TransientPhotometry, TransientSpectrum):
            with connection.cursor() as cursor:
                desc = connection.introspection.get_table_description(cursor, model._meta.db_table)
            by_name = {col.name: col for col in desc}
            self.assertTrue(by_name["allocation_id"].null_ok, model.__name__)

    def test_flag_defaults_off_and_tables_hidden_from_explorer(self):
        self.assertFalse(settings.DATA_ACCESS_GRANTS_ENABLED)
        for table in ("YSE_App_dataaccessgrant", "YSE_App_allocationdelegate"):
            self.assertIn(table, settings.EXPLORER_SQL_BLACKLIST)
            self.assertIn(table, settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)

    def test_api_does_not_expose_the_program_link_yet(self):
        self.assertNotIn("allocation", TransientPhotometrySerializer().fields)
        self.assertNotIn("allocation", TransientSpectrumSerializer().fields)
        self.assertIn("groups", TransientPhotometrySerializer().fields)


@override_settings(DATA_ACCESS_GRANTS_ENABLED=False)
class ReadPathsUnchangedTests(_Fixture):
    """Grant rows that would change access if read change nothing while the flag is off."""

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

    def test_reads_ignore_grant_rows(self):
        before = self._snapshot()
        night = dict(night_start=self.night_start, night_end=self.night_end)
        self.grant(allocation=self.allocation, grantee_user=self.outsider)
        self.grant(allocation=self.allocation, grantee_group=self.group, effect=G.EFFECT_DENY, **night)
        self.grant(transient_photometry=self.phot["private"], grantee_group=self.group, effect=G.EFFECT_DENY)
        self.grant(transient_spectrum=self.spec["other"], grantee_user=self.outsider)
        self.grant(transient_photometry=self.phot["other"], grantee_user=self.member, **night)
        self.assertEqual(self._snapshot(), before)
        # the group rule: member sees public + private, outsider public only
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
        row = self.grant(allocation=self.allocation, grantee_group=self.other)
        AllocationDelegate.objects.create(allocation=self.allocation, user=self.outsider, added_by=self.staff)
        for url in (reverse("admin:YSE_App_dataaccessgrant_changelist"),
                    reverse("admin:YSE_App_dataaccessgrant_add"),
                    reverse("admin:YSE_App_dataaccessgrant_change", args=[row.pk]),
                    reverse("admin:YSE_App_allocationdelegate_changelist"),
                    reverse("admin:YSE_App_allocation_change", args=[self.allocation.pk])):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_add_records_staff_and_revoke_action(self):
        resp = self.client.post(reverse("admin:YSE_App_dataaccessgrant_add"), {
            "effect": "allow", "allocation": self.allocation.pk, "grantee_group": self.other.pk, "note": "synthetic",
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, "context_data", {}).get("errors"))
        row = G.objects.get(note="synthetic")
        self.assertEqual((row.level, row.granted_as, row.granted_by), (G.LEVEL_PROGRAM, G.AS_STAFF, self.staff))
        self.client.post(reverse("admin:YSE_App_dataaccessgrant_changelist"),
                         {"action": "revoke_default", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertIsNotNone(row.revoked_at)
        self.assertFalse(row.revoke_retroactive)

    def test_no_delete(self):
        row = self.grant(allocation=self.allocation, grantee_group=self.other)
        self.assertEqual(
            self.client.get(reverse("admin:YSE_App_dataaccessgrant_delete", args=[row.pk])).status_code, 403)
        self.assertTrue(G.objects.filter(pk=row.pk).exists())
