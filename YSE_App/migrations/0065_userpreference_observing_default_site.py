"""Per-user default site for "Observable tonight" (#959).

Additive only: one nullable column, ``observing_default_site_id``, on ``YSE_App_userpreference``
(a table develop knows). develop's INSERTs leave it NULL. No existing row is changed. Its MySQL
foreign key gets ``ON DELETE SET NULL`` in the next migration (0066): Django adds the constraint
from deferred SQL at the end of this migration, so the rule cannot be changed in the same one.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("YSE_App", "0064_fit_tables_fk_on_delete"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpreference",
            name="observing_default_site",
            field=models.ForeignKey(
                blank=True,
                help_text="Site that Observable tonight starts from for this user.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="YSE_App.telescope",
            ),
        ),
    ]
