"""Redshift provenance and override (#436, #437).

Adds nullable columns only (online in MySQL 8, no table copy):

* ``Host.redshift_source`` (varchar 64): where ``Host.redshift`` came from (``NED``,
  ``DESI DR1``, ``Prost``, ``manual``, ...);
* ``Host.redshift_override`` and ``Transient.redshift_override`` (boolean): a redshift
  set by hand that wins over every catalogue and is never overwritten.

Shared ``YSE_test`` (#395): the columns are nullable, so INSERTs from code that does not
know them still work and leave NULL, which the code reads as "no source" / "no override".
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0048_data_access_request_revoke'),
    ]

    operations = [
        migrations.AddField(
            model_name='host',
            name='redshift_source',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='host',
            name='redshift_override',
            field=models.BooleanField(blank=True, default=False, null=True),
        ),
        migrations.AddField(
            model_name='transient',
            name='redshift_override',
            field=models.BooleanField(blank=True, default=False, null=True),
        ),
    ]
