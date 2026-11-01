"""Per-point program link (#714): ``YSE_App_transientphotdata.allocation_id``, nullable and indexed.

Access is per point (Ryan, 2026-10-18): the program a point was taken under is
stored on the point; the header's ``allocation`` stays as the default for its
points. No foreign-key constraint, so MySQL adds the column and its index
online (no table copy); existing rows stay NULL (their header's link applies)
until ``manage.py migrate_point_programs --apply`` copies the header links.
Safe on the shared database: develop code ignores the column.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0046_access_rule_kept'),
    ]

    operations = [
        migrations.AddField(
            model_name='transientphotdata',
            name='allocation',
            field=models.ForeignKey(
                blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='photometry_points', to='YSE_App.allocation',
                help_text="Program (allocation) this point was taken under, if known; empty: its header's."),
        ),
    ]
