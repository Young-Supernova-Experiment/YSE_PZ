"""Team requests made on the signup form wait for onboarding.

One nullable column, ``YSE_App_groupmembershiprequest.held``. NULL means "not held", so
INSERTs from code that does not know the column (develop, on the shared YSE_test
database) keep working, and existing rows are unchanged.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0057_catalog_import'),
    ]

    operations = [
        migrations.AddField(
            model_name='groupmembershiprequest',
            name='held',
            field=models.BooleanField(blank=True, default=None, help_text='Made on the signup form; owners see it once the user has finished onboarding.', null=True),
        ),
    ]
