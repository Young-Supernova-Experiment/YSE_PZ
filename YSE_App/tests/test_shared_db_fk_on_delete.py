"""Database-level ON DELETE on experimental's new tables (#941, migration 0062).

develop's code runs on the shared YSE_test database without knowing the tables that 0056-0061
created, so it deletes parent rows with no Python cascade into them. The deletes here are raw
SQL, as such code would issue them, and must succeed with the child rows removed or nulled by
MySQL itself. Synthetic data only.
"""

import importlib

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.db import connection, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddField, CreateModel
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from YSE_App.common import fk_on_delete, spectrum_arrays
from YSE_App.models import (
    CatalogDataMatch,
    CatalogEntry,
    CatalogSource,
    DuplicateDataDecision,
    GroupProfile,
    HostSpectrum,
    HostSpectrumArrays,
    Observatory,
    Telescope,
    TransientSpectrum,
    TransientSpectrumArrays,
    TransientSpectrumCatalogInfo,
    UserPreference,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_host,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

fk_migration = importlib.import_module("YSE_App.migrations.0062_shared_db_fk_on_delete")

EXPERIMENTAL_ONLY = ("0056", "0057", "0058", "0059", "0060", "0061")
RULES = {models.CASCADE: fk_migration.CASCADE, models.SET_NULL: fk_migration.SET_NULL}


def _new_tables():
    """Tables created by the experimental-only migrations, read from the migrations themselves."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    tables = set()
    for (app, name), migration in loader.disk_migrations.items():
        if app != "YSE_App" or not name.startswith(EXPERIMENTAL_ONLY):
            continue
        for op in migration.operations:
            if isinstance(op, CreateModel):
                model = op.name.lower()
                tables.add(f"YSE_App_{model}")
                tables.update(f"YSE_App_{model}_{field}" for field, f in op.fields
                              if isinstance(f, models.ManyToManyField) and not f.remote_field.through)
            elif isinstance(op, AddField) and isinstance(op.field, models.ManyToManyField):
                if not op.field.remote_field.through:
                    tables.add(f"YSE_App_{op.model_name.lower()}_{op.name}")
    return tables


def _rule(table, column):
    rows = fk_migration.current_constraints(connection, table, column)
    return [row[3] for row in rows]


def _sql(statement, params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _delete_known_children(parent_table, parent_id):
    """Delete the rows of older tables that reference ``parent_table`` row ``parent_id``.

    This is what develop's Django cascade does before it deletes the parent; the tables
    created by 0056-0061 are left for MySQL's ON DELETE.
    """
    new = {table for table, _, _, _ in fk_migration.FOREIGN_KEYS}
    with connection.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                       "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = %s", [parent_table])
        children = [row for row in cursor.fetchall() if row[0] not in new]
        for table, column in children:
            cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` = %s", [parent_id])


class ListMatchesModelsTests(SimpleTestCase):
    def test_every_cascade_and_set_null_key_of_the_new_tables_is_listed(self):
        tables = _new_tables()
        self.assertIn("YSE_App_transientspectrumarrays", tables)
        self.assertIn("YSE_App_groupprofile_deputies", tables)
        expected, protected = set(), set()
        for model in apps.get_models(include_auto_created=True):
            table = model._meta.db_table
            if table not in tables:
                continue
            for field in model._meta.local_concrete_fields:
                if not isinstance(field, models.ForeignKey):
                    continue
                entry = (table, field.column, field.related_model._meta.db_table)
                rule = RULES.get(field.remote_field.on_delete)
                if rule is None:
                    protected.add(entry)
                else:
                    expected.add(entry + (rule,))
        self.assertEqual(set(fk_migration.FOREIGN_KEYS), expected)
        self.assertTrue(protected)
        self.assertTrue(all(column in ("created_by_id", "modified_by_id") for _, column, _ in protected))

    def test_no_table_from_older_migrations_is_touched(self):
        tables = _new_tables()
        self.assertTrue({table for table, _, _, _ in fk_migration.FOREIGN_KEYS} <= tables)


class RawDeleteTests(TestCase):
    """The deletes develop's code would issue, as raw SQL, after migration 0062."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")
        self.user = create_test_user("fk_on_delete_user")
        self.audit = audit_fields(self.user)
        self.transient = create_minimal_transient(self.user, name="2099fkd")

    def test_constraints_carry_the_model_rules(self):
        for table, column, _, rule in fk_migration.FOREIGN_KEYS:
            with self.subTest(table=table, column=column):
                self.assertEqual(_rule(table, column), [rule])

    def test_transient_spectrum_delete_cascades_and_nulls(self):
        spectrum = attach_synthetic_spectrum(self.user, self.transient)
        other = attach_synthetic_spectrum(self.user, self.transient)
        spectrum_arrays.save(spectrum, [4000.0, 5000.0], [1.0, 1.1], [0.1, 0.1])
        kept = DuplicateDataDecision.objects.create(transient=self.transient, kind="spectrum", reason="later_upload",
                                                    spectrum=other, kept_spectrum=spectrum, previous_quality_mask=0)
        flagged = DuplicateDataDecision.objects.create(transient=self.transient, kind="spectrum",
                                                       reason="later_upload", spectrum=spectrum, kept_spectrum=other,
                                                       previous_quality_mask=0)
        entry = CatalogEntry.objects.create(transient=self.transient, catalog="SYN", entry_name="syn-1", **self.audit)
        source = CatalogSource.objects.create(name="synthetic source", **self.audit)
        info = TransientSpectrumCatalogInfo.objects.create(entry=entry, spectrum=spectrum, **self.audit)
        info.sources.add(source)
        match = CatalogDataMatch.objects.create(entry=entry, decision="review", existing_spectrum=spectrum)

        _sql("DELETE FROM YSE_App_transientspectrum WHERE id = %s", [spectrum.id])

        self.assertFalse(TransientSpectrum.objects.filter(pk=spectrum.pk).exists())
        self.assertFalse(TransientSpectrumArrays.objects.filter(pk=spectrum.pk).exists())
        self.assertFalse(DuplicateDataDecision.objects.filter(pk=flagged.pk).exists())
        kept.refresh_from_db()
        self.assertIsNone(kept.kept_spectrum_id)
        self.assertEqual(kept.spectrum_id, other.id)
        self.assertFalse(TransientSpectrumCatalogInfo.objects.filter(pk=info.pk).exists())
        self.assertFalse(TransientSpectrumCatalogInfo.sources.through.objects.filter(
            transientspectrumcataloginfo_id=info.pk).exists())
        self.assertFalse(CatalogDataMatch.objects.filter(pk=match.pk).exists())
        self.assertTrue(CatalogEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(CatalogSource.objects.filter(pk=source.pk).exists())

    def test_host_spectrum_delete_cascades(self):
        host = attach_synthetic_host(self.user, self.transient)
        obs_group, instrument, _ = create_instrument_stack(self.user, obs_group_name="fk-host")
        spectrum = HostSpectrum.objects.create(host=host, instrument=instrument, obs_group=obs_group, ra=host.ra,
                                               dec=host.dec, obs_date=timezone.now(), **self.audit)
        spectrum_arrays.save(spectrum, [4000.0, 5000.0], [1.0, 1.1], host=True)
        self.assertTrue(HostSpectrumArrays.objects.filter(pk=spectrum.pk).exists())

        _sql("DELETE FROM YSE_App_hostspectrum WHERE id = %s", [spectrum.id])

        self.assertFalse(HostSpectrum.objects.filter(pk=spectrum.pk).exists())
        self.assertFalse(HostSpectrumArrays.objects.filter(pk=spectrum.pk).exists())

    def _team_with_deputy(self, name):
        group = Group.objects.create(name=name)
        profile, _ = GroupProfile.objects.get_or_create(group=group)
        deputy = User.objects.create(username=f"{name}-deputy")
        profile.deputies.add(deputy)
        return group, profile, deputy

    def test_user_delete_removes_deputy_rows(self):
        group, profile, deputy = self._team_with_deputy("fk-team-user")
        through = GroupProfile.deputies.through
        # What develop's Django deletes first (it knows these tables), then the user itself.
        _delete_known_children("auth_user", deputy.id)
        _sql("DELETE FROM auth_user WHERE id = %s", [deputy.id])

        self.assertFalse(User.objects.filter(pk=deputy.pk).exists())
        self.assertFalse(through.objects.filter(user_id=deputy.id).exists())
        self.assertTrue(GroupProfile.objects.filter(pk=profile.pk).exists())

    def test_group_delete_removes_deputy_rows(self):
        group, profile, deputy = self._team_with_deputy("fk-team-group")
        through = GroupProfile.deputies.through
        # develop's Group cascade: its GroupProfile row, then the group.
        _delete_known_children("auth_group", group.id)
        _sql("DELETE FROM YSE_App_groupprofile WHERE id = %s", [profile.id])
        _sql("DELETE FROM auth_group WHERE id = %s", [group.id])

        self.assertFalse(Group.objects.filter(pk=group.pk).exists())
        self.assertFalse(through.objects.filter(groupprofile_id=profile.id).exists())
        self.assertTrue(User.objects.filter(pk=deputy.pk).exists())


class ReverseForwardTests(TransactionTestCase):
    """Reverse restores plain constraints; forward (twice, and after a lost constraint) restores the rules."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")

    def _run(self, func):
        with connection.schema_editor(atomic=False) as editor:
            func(None, editor)

    def _rules(self):
        return {(table, column): _rule(table, column) for table, column, _, _ in fk_migration.FOREIGN_KEYS}

    def _assert_forward(self):
        for table, column, _, rule in fk_migration.FOREIGN_KEYS:
            self.assertEqual(_rule(table, column), [rule], (table, column))

    def test_reverse_then_forward(self):
        try:
            names = {key: fk_migration.current_constraints(connection, *key)[0][0] for key in self._rules()}
            self._run(fk_migration.backwards)
            for key, rules in self._rules().items():
                self.assertEqual(len(rules), 1, key)
                self.assertIn(rules[0], fk_migration.PLAIN_RULES, key)
                self.assertEqual(fk_migration.current_constraints(connection, *key)[0][0], names[key])
            self._run(fk_migration.backwards)  # idempotent
        finally:
            self._run(fk_migration.forwards)
        self._assert_forward()
        self._run(fk_migration.forwards)  # idempotent
        self._assert_forward()

    def test_forward_re_adds_a_missing_constraint(self):
        table, column = "YSE_App_transientspectrumarrays", "spectrum_id"
        name = fk_migration.current_constraints(connection, table, column)[0][0]
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`")
            self.assertEqual(_rule(table, column), [])
        finally:
            self._run(fk_migration.forwards)
        self.assertEqual(_rule(table, column), [fk_migration.CASCADE])


default_site_migration = importlib.import_module("YSE_App.migrations.0064_userpreference_default_site_on_delete")


class DefaultSiteColumnTests(SimpleTestCase):
    def test_list_matches_the_model(self):
        field = UserPreference._meta.get_field("observing_default_site")
        self.assertTrue(field.null)
        self.assertIs(field.remote_field.on_delete, models.SET_NULL)
        self.assertEqual(
            default_site_migration.FOREIGN_KEYS,
            ((UserPreference._meta.db_table, field.column, Telescope._meta.db_table, fk_on_delete.SET_NULL),),
        )

    def test_0062_uses_the_shared_helper(self):
        self.assertIs(fk_migration.current_constraints, fk_on_delete.current_constraints)
        self.assertEqual(fk_migration.PLAIN_RULES, fk_on_delete.PLAIN_RULES)


class DefaultSiteRawDeleteTests(TestCase):
    """develop deletes a telescope with raw SQL semantics: it does not know the new column (#959)."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")
        self.user = create_test_user("fk_default_site_user")
        audit = audit_fields(self.user)
        observatory = Observatory.objects.create(name="Synthetic FK Obs", utc_offset=0, tz_name="UTC", **audit)
        self.telescope = Telescope.objects.create(name="Synthetic FK 2m", observatory=observatory, latitude=0.0,
                                                  longitude=0.0, elevation=0.0, **audit)
        self.pref = UserPreference.objects.create(user=self.user, comment_default_public=True,
                                                  observing_default_site=self.telescope)

    def test_exactly_one_constraint_and_it_is_set_null(self):
        for table, column, ref_table, rule in default_site_migration.FOREIGN_KEYS:
            found = fk_on_delete.current_constraints(connection, table, column)
            self.assertEqual(len(found), 1, found)
            self.assertEqual(found[0][1], ref_table)
            self.assertEqual(found[0][3], rule)

    def test_telescope_delete_nulls_the_default(self):
        _sql("DELETE FROM YSE_App_telescope WHERE id = %s", [self.telescope.id])

        self.assertFalse(Telescope.objects.filter(pk=self.telescope.pk).exists())
        self.pref.refresh_from_db()
        self.assertIsNone(self.pref.observing_default_site_id)
        self.assertTrue(self.pref.comment_default_public)


class DefaultSiteReverseForwardTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")

    def _run(self, func):
        with connection.schema_editor(atomic=False) as editor:
            func(None, editor)

    def test_reverse_then_forward(self):
        (table, column, _, rule), = default_site_migration.FOREIGN_KEYS
        try:
            self._run(default_site_migration.backwards)
            rules = _rule(table, column)
            self.assertEqual(len(rules), 1)
            self.assertIn(rules[0], fk_on_delete.PLAIN_RULES)
        finally:
            self._run(default_site_migration.forwards)
        self.assertEqual(_rule(table, column), [rule])
        self._run(default_site_migration.forwards)  # idempotent
        self.assertEqual(_rule(table, column), [rule])
