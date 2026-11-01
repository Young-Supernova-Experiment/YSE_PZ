"""Selections (#841): a new table, plus MySQL ON DELETE on its foreign keys to develop's tables.

Additive only: one new table, ``YSE_App_selection``. develop's code never reads or writes it.
Its foreign keys point at ``auth_user``, ``auth_group``, ``explorer_query`` and
``YSE_App_userquery``, which develop deletes from on the shared database without knowing this
table, so the RunPython below gives each MySQL constraint the model's ``ON DELETE`` rule
(``YSE_App.common.fk_on_delete``, as in 0062). No existing row or column changes.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

import YSE_App.models.fields
from YSE_App.common import fk_on_delete

CASCADE = fk_on_delete.CASCADE
SET_NULL = fk_on_delete.SET_NULL

# (table, column, referenced table, ON DELETE rule); YSE_App/tests/test_selections.py checks it
# against the model.
FOREIGN_KEYS = (
    ("YSE_App_selection", "owner_id", "auth_user", CASCADE),
    ("YSE_App_selection", "team_id", "auth_group", SET_NULL),
    ("YSE_App_selection", "query_id", "explorer_query", SET_NULL),
    ("YSE_App_selection", "user_query_id", "YSE_App_userquery", SET_NULL),
    ("YSE_App_selection", "modified_by_id", "auth_user", SET_NULL),
)

forwards = fk_on_delete.forwards_for(FOREIGN_KEYS)
backwards = fk_on_delete.backwards_for(FOREIGN_KEYS)


class Migration(migrations.Migration):

    # ALTER TABLE in RunPython: MySQL commits DDL implicitly, so no surrounding transaction.
    atomic = False

    dependencies = [
        ("YSE_App", "0062_shared_db_fk_on_delete"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("explorer", "0010_sql_required"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Selection",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("slug", models.SlugField(max_length=80, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("visibility", models.CharField(choices=[("me", "Only me"), ("team", "My team"), ("all", "All YSE")],
                                                default="me", max_length=8)),
                ("kind", models.CharField(choices=[("search", "Search filters"), ("rules", "Rules"),
                                                   ("sql", "SQL query"), ("function", "Built-in list")],
                                          default="search", max_length=16)),
                ("search_params", models.TextField(blank=True, default="",
                                                   help_text="Transient search query string (kind 'search').")),
                ("rules", YSE_App.models.fields.JSONTextField(blank=True, null=True,
                                                              help_text="Rule tree as JSON (kind 'rules').")),
                ("python_query", models.CharField(blank=True, default="", max_length=64,
                                                  help_text="Registered Python query name (kind 'function').")),
                ("created_date", models.DateTimeField(auto_now_add=True)),
                ("modified_date", models.DateTimeField(auto_now=True)),
                ("modified_by", models.ForeignKey(blank=True, null=True,
                                                  on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name="+", to=settings.AUTH_USER_MODEL)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name="selections", to=settings.AUTH_USER_MODEL)),
                ("query", models.ForeignKey(blank=True, help_text="Saved Explorer query (kind 'sql').", null=True,
                                            on_delete=django.db.models.deletion.SET_NULL,
                                            related_name="selections", to="explorer.query")),
                ("team", models.ForeignKey(blank=True,
                                           help_text="The team that sees and edits this Selection (visibility 'My team').",
                                           null=True, on_delete=django.db.models.deletion.SET_NULL,
                                           related_name="selections", to="auth.group")),
                ("user_query", models.ForeignKey(blank=True,
                                                 help_text="The dashboard panel this Selection was made from, if any.",
                                                 null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="selections", to="YSE_App.userquery")),
            ],
            options={
                "ordering": ("name", "pk"),
            },
        ),
        # shared-db-safe: only sets the ON DELETE rule of the foreign keys of the table created just above (YSE_App_selection), which develop's code never reads or writes; no row or column of any other table changes (#841, as 0062)
        migrations.RunPython(forwards, backwards),
    ]
