"""Unit tests for check_shared_db_migrations.py (synthetic migration snippets only).

Run: python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""

import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_shared_db_migrations as chk

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

BASE = ("app/migrations/0001_initial.py", textwrap.dedent("""
    from django.db import migrations, models

    class Migration(migrations.Migration):
        operations = [
            migrations.CreateModel(
                name='Widget',
                fields=[
                    ('id', models.AutoField(primary_key=True, serialize=False)),
                    ('label', models.CharField(max_length=64)),
                    ('note', models.CharField(max_length=200, null=True, blank=True)),
                    ('size', models.IntegerField(null=True)),
                    ('body', models.TextField(blank=True, null=True)),
                ],
            ),
            migrations.CreateModel(name='Gadget', fields=[('id', models.AutoField(primary_key=True))]),
        ]
"""))


def migration(ops, preamble=""):
    body = textwrap.indent(textwrap.dedent(ops).strip("\n"), " " * 8)
    return (textwrap.dedent(preamble) + "\nfrom django.db import migrations, models\n\n"
            "class Migration(migrations.Migration):\n    operations = [\n" + body + "\n    ]\n")


def check(*new_sources, base=(BASE,)):
    new = [(f"app/migrations/{i + 2:04d}_new.py", src) for i, src in enumerate(new_sources)]
    findings, allowed = chk.check_sources(list(base), new)
    return findings, allowed


class UnsafeOperationTests(unittest.TestCase):
    def assertUnsafe(self, ops, op_name, preamble=""):
        findings, _ = check(migration(ops, preamble))
        self.assertTrue(findings, f"expected a finding for {op_name}")
        self.assertIn(op_name, [f.op for f in findings])
        return findings

    def assertSafe(self, *sources):
        findings, _ = check(*sources)
        self.assertEqual(findings, [])

    def test_remove_rename_delete_and_table_ops_on_existing_models(self):
        self.assertUnsafe("migrations.RemoveField(model_name='widget', name='note'),", "RemoveField")
        self.assertUnsafe("migrations.RenameField(model_name='widget', old_name='note', new_name='memo'),",
                          "RenameField")
        self.assertUnsafe("migrations.RenameModel(old_name='Widget', new_name='Thing'),", "RenameModel")
        self.assertUnsafe("migrations.DeleteModel(name='Gadget'),", "DeleteModel")
        self.assertUnsafe("migrations.AlterModelTable(name='widget', table='widgets'),", "AlterModelTable")

    def test_same_ops_on_a_model_created_by_the_new_migrations_are_safe(self):
        self.assertSafe(migration("""
            migrations.CreateModel(name='Fresh', fields=[('id', models.AutoField(primary_key=True)),
                                                         ('a', models.IntegerField())]),
            migrations.AddField(model_name='fresh', name='b', field=models.IntegerField(default=0)),
            migrations.AlterField(model_name='fresh', name='a', field=models.SmallIntegerField()),
            migrations.RenameField(model_name='fresh', old_name='b', new_name='c'),
            migrations.AddConstraint(model_name='fresh',
                                     constraint=models.UniqueConstraint(fields=('a',), name='fresh_a')),
            migrations.RenameModel(old_name='Fresh', new_name='Fresher'),
            migrations.RemoveField(model_name='fresher', name='c'),
            migrations.DeleteModel(name='Fresher'),
        """))

    def test_add_field_not_null_without_db_default(self):
        findings = self.assertUnsafe(
            "migrations.AddField(model_name='widget', name='hours', field=models.FloatField(default=0)),",
            "AddField")
        self.assertIn("1364", findings[0].message)
        self.assertUnsafe("migrations.AddField(model_name='widget', name='owner', "
                          "field=models.ForeignKey(on_delete=models.CASCADE, to='app.gadget')),", "AddField")

    def test_add_field_nullable_or_m2m_is_safe(self):
        self.assertSafe(migration("""
            migrations.AddField(model_name='widget', name='held',
                                field=models.BooleanField(blank=True, default=None, null=True)),
            migrations.AddField(model_name='widget', name='gadgets', field=models.ManyToManyField(to='app.gadget')),
            migrations.AddField(model_name='widget', name='twin',
                                field=models.OneToOneField(null=True, on_delete=models.SET_NULL, to='app.gadget')),
        """))

    def test_add_field_with_db_default_is_safe(self):
        self.assertSafe(migration(
            "migrations.AddField(model_name='widget', name='hours', field=models.FloatField(db_default=0)),"))

    def test_add_field_backed_by_a_mysql_default_in_a_new_migration_is_safe(self):
        add = migration("migrations.AddField(model_name='widget', name='hours', field=models.FloatField(default=0)),")
        defaults = migration(
            "# shared-db-safe: sets MySQL DEFAULTs only\nmigrations.RunPython(add_defaults, drop_defaults),",
            preamble="""
            COLUMN_DEFAULTS = [("app_widget", "hours", "double NOT NULL DEFAULT 0")]
            """)
        findings, allowed = check(add, defaults)
        self.assertEqual(findings, [])
        self.assertEqual(len(allowed), 1)

        sql = migration("""
            # shared-db-safe: adds a column DEFAULT only
            migrations.RunSQL("ALTER TABLE `app_widget` MODIFY `hours` double NOT NULL DEFAULT 0"),
        """)
        findings, _ = check(add, sql)
        self.assertEqual(findings, [])

    def test_default_for_another_column_does_not_count(self):
        add = migration("migrations.AddField(model_name='widget', name='hours', field=models.FloatField(default=0)),")
        other = migration("# shared-db-safe: x\nmigrations.RunSQL('ALTER TABLE app_widget MODIFY hours_total "
                          "double NOT NULL DEFAULT 0'),")
        findings, _ = check(add, other)
        self.assertEqual([f.op for f in findings], ["AddField"])

    def test_add_unique_column(self):
        findings = self.assertUnsafe("migrations.AddField(model_name='widget', name='code', "
                                     "field=models.CharField(max_length=8, unique=True, db_default='x')),", "AddField")
        self.assertIn("unique", findings[0].message)

    def test_alter_field_not_null_narrow_type_and_unique(self):
        self.assertIn("NOT NULL", self.assertUnsafe(
            "migrations.AlterField(model_name='widget', name='note', field=models.CharField(max_length=200)),",
            "AlterField")[0].message)
        self.assertIn("narrows max_length", self.assertUnsafe(
            "migrations.AlterField(model_name='widget', name='label', field=models.CharField(max_length=32)),",
            "AlterField")[0].message)
        self.assertIn("type", self.assertUnsafe(
            "migrations.AlterField(model_name='widget', name='size', field=models.CharField(max_length=9, "
            "null=True)),", "AlterField")[0].message)
        self.assertIn("unique", self.assertUnsafe(
            "migrations.AlterField(model_name='widget', name='label', field=models.CharField(max_length=64, "
            "unique=True)),", "AlterField")[0].message)
        self.assertIn("previous definition", self.assertUnsafe(
            "migrations.AlterField(model_name='widget', name='nowhere', field=models.IntegerField(null=True)),",
            "AlterField")[0].message)

    def test_alter_field_that_widens_or_relaxes_is_safe(self):
        self.assertSafe(migration("""
            migrations.AlterField(model_name='widget', name='label', field=models.CharField(max_length=128)),
            migrations.AlterField(model_name='widget', name='note',
                                  field=models.CharField(max_length=200, null=True, help_text='x')),
            migrations.AlterField(model_name='widget', name='label',
                                  field=models.CharField(max_length=128, null=True)),
        """))

    def test_unique_constraints_on_existing_models(self):
        self.assertUnsafe("migrations.AddConstraint(model_name='widget', "
                          "constraint=models.UniqueConstraint(fields=('label',), name='u')),", "AddConstraint")
        self.assertUnsafe("migrations.AlterUniqueTogether(name='widget', unique_together={('label', 'size')}),",
                          "AlterUniqueTogether")
        self.assertSafe(migration("migrations.AlterUniqueTogether(name='widget', unique_together=set()),"))
        self.assertSafe(migration("migrations.AddIndex(model_name='widget', "
                                  "index=models.Index(fields=['label'], name='i')),"))

    def test_run_python_and_run_sql(self):
        self.assertUnsafe("migrations.RunPython(fill, migrations.RunPython.noop),", "RunPython")
        self.assertUnsafe("migrations.RunSQL('UPDATE app_widget SET size = 0'),", "RunSQL")
        self.assertSafe(migration("migrations.RunPython(migrations.RunPython.noop, fill),"))

    def test_separate_database_and_state_checks_database_operations(self):
        self.assertUnsafe("""
            migrations.SeparateDatabaseAndState(
                database_operations=[migrations.RunSQL('DROP TABLE app_widget')],
                state_operations=[migrations.DeleteModel(name='Widget')],
            ),
        """, "RunSQL")

    def test_unknown_or_unparseable_operations_are_reported(self):
        self.assertUnsafe("MyCustomOperation(model_name='widget'),", "MyCustomOperation")
        findings, _ = check(migration("").replace("operations = [\n\n    ]", "operations = build()"))
        self.assertEqual([f.op for f in findings], ["operations"])


class OptOutTests(unittest.TestCase):
    def test_comment_above_or_inside_allows_the_operation(self):
        for ops in (
            "# shared-db-safe: names only filled where empty\nmigrations.RunPython(fill),",
            "# reviewed 2026-10-31\n# shared-db-safe: names only filled where empty\nmigrations.RunPython(fill),",
            "migrations.RunPython(  # shared-db-safe: names only filled where empty\n    fill),",
        ):
            findings, allowed = check(migration(ops))
            self.assertEqual(findings, [], ops)
            self.assertEqual(allowed[0].reason, "names only filled where empty")

    def test_opt_out_needs_a_reason(self):
        findings, _ = check(migration("# shared-db-safe:\nmigrations.RunPython(fill),"))
        self.assertEqual(len(findings), 1)

    def test_opt_out_is_per_operation(self):
        findings, allowed = check(migration("""
            # shared-db-safe: reviewed
            migrations.RunPython(fill),

            migrations.RunPython(other),
        """))
        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(findings), 1)

    def test_comment_separated_by_a_blank_line_does_not_count(self):
        findings, _ = check(migration("# shared-db-safe: reviewed\n\nmigrations.RunPython(fill),"))
        self.assertEqual(len(findings), 1)


class RepositoryMigrationsTests(unittest.TestCase):
    """Experimental's 0056-0061 (new relative to develop's 0055) pass the check."""

    def test_0056_to_0061_pass(self):
        mig_dir = os.path.join(REPO, "YSE_App", "migrations")
        names = sorted(n for n in os.listdir(mig_dir) if n[:4].isdigit() and n.endswith(".py"))
        new = [n for n in names if "0056" <= n[:4] <= "0061"]
        if len(new) != 6:
            self.skipTest("0056-0061 not all present")
        rel = lambda n: os.path.join("YSE_App", "migrations", n)
        read = lambda n: chk._read(os.path.join(mig_dir, n))
        findings, allowed = chk.check_sources(
            [(rel(n), read(n)) for n in names if n[:4] < "0056"], [(rel(n), read(n)) for n in new])
        self.assertEqual(findings, [])
        self.assertEqual([os.path.basename(a.path) for a in allowed], ["0060_fill_account_names.py"])


if __name__ == "__main__":
    unittest.main()
